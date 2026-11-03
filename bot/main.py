"""AmazonBot daily run.

    python -m bot.main            # full daily run
    python -m bot.main --check    # test your Amazon keys (safe, read-only)
    python -m bot.main --asins B0XXXXXXX,B0YYYYYYY   # analyse specific products
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time

from . import rules
from .config import (DATA_DIR, OUTPUT_DIR, PRICE_LIST_DIR, env, load_config,
                     load_retailers, load_seed_brands)
from .outputs import (BRAND_COLUMNS, LEAD_COLUMNS, NO_SUPPLIER_COLUMNS, OPTION_COLUMNS,
                      REVIEW_COLUMNS, USAGE_COLUMNS, discord_alert, discord_summary, write_csv, write_sheets)
from .profit import ProductCosts, max_buy_price
from .providers import BestBuyProvider, SerpApiProvider, SerperProvider, TavilyProvider
from .sourcing import Candidate, decide, is_lead, needs_verification, no_supplier_row, evaluate_offer
from .spapi import SPAPI, CatalogItem, SPAPIAuthError, SPAPIError
from .store import Store
from .suppliers import dedupe, discover, free_offers, load_price_lists, upc12, verify

log = logging.getLogger("amazonbot")


def make_client() -> SPAPI:
    return SPAPI(env("SPAPI_LWA_CLIENT_ID", True), env("SPAPI_LWA_CLIENT_SECRET", True),
                 env("SPAPI_REFRESH_TOKEN", True), env("SPAPI_SELLER_ID", True))


def make_providers(cfg: dict, store) -> dict:
    return {
        "bestbuy": BestBuyProvider(env("BESTBUY_API_KEY"), cfg, store),
        "serper": SerperProvider(env("SERPER_API_KEY"), cfg, store),
        "serpapi": SerpApiProvider(env("SERPAPI_KEY"), cfg, store),
        "tavily": TavilyProvider(env("TAVILY_API_KEY"), cfg, store),
    }


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


# ----------------------------------------------------------------- check mode

def check_setup() -> int:
    api = make_client()
    print("1) Logging in to Amazon ...", end=" ")
    api._access_token()
    print("OK")
    print("2) Searching the catalog for 'Great Value' ...", end=" ")
    items, _ = api.search_catalog(keywords="Great Value", brand="Great Value")
    print(f"OK ({len(items)} products)")
    print("3) Checking your eligibility on 3 of them:")
    for item in items[:3]:
        e = api.get_eligibility(item.asin)
        print(f"   {item.asin}  {e.status:15}  {item.title[:60]}")
    if items:
        print("4) Live offers + fees for the first one ...", end=" ")
        offers = api.get_offers([items[0].asin]).get(items[0].asin)
        price = offers.sell_price if offers else None
        print(f"price ${price}" if price else "no buy box right now")
        if price:
            fees = api.get_fees({items[0].asin: price}).get(items[0].asin)
            print(f"   referral ${fees.referral:.2f}, FBA ${fees.fba:.2f}")
    import tempfile
    from pathlib import Path
    tmp = Store(Path(tempfile.mkdtemp()) / "check.db")
    cfg = load_config()
    print("5) Pricing providers:")
    provs = make_providers(cfg, tmp)
    if provs["bestbuy"].active:
        provs["bestbuy"].lookup_upcs(["027242923232"], 0)   # one call to validate the key
    for prov in provs.values():
        r = prov.report()
        left = f", {r['Left on free plan']} left" if r["Left on free plan"] != "" else ""
        print(f"   {prov.name:9} {r['Status']}{left}  (free limit: {prov.limit_note or '-'})")
    print("\nAmazon keys work. Providers marked OFF/STOPPED are skipped; Leads need at least "
          "Serper or SerpApi (or your own price lists).")
    return 0


# ----------------------------------------------------------------- discovery

def discover_brands(api: SPAPI, store: Store, cfg: dict, run_log: list[str]) -> None:
    """Search category keywords, find brands not yet known, test one product
    per brand. Brands you're eligible for get added to the scan list."""
    kws = cfg.get("discovery_keywords", [])
    n = cfg["search"]["discovery_keywords_per_run"]
    if not kws or not n:
        return
    start = (dt.date.today().toordinal() * n) % len(kws)
    todays = [kws[(start + i) % len(kws)] for i in range(min(n, len(kws)))]
    added = 0
    per_kw = int(cfg["search"].get("discovery_brands_per_keyword", 10))
    pages = int(cfg["search"].get("discovery_pages_per_keyword", 1))
    tested = 0
    for kw in todays:
        items, token = [], None
        try:
            for _ in range(pages):
                found, token = api.search_catalog(keywords=kw, page_token=token)
                items += found
                if not token:
                    break
        except SPAPIAuthError:
            raise
        except SPAPIError as exc:
            run_log.append(f"discovery '{kw}' failed: {exc}")
            if not items:
                continue
        best_per_brand: dict[str, CatalogItem] = {}
        excluded = {b.lower() for b in cfg["search"].get("exclude_brands", [])}
        for it in items:
            if not it.brand or it.brand.lower() in excluded or store.known_brand(it.brand) \
                    or it.is_variation_parent:
                continue
            cur = best_per_brand.get(it.brand)
            if cur is None or (it.rank or 10**9) < (cur.rank or 10**9):
                best_per_brand[it.brand] = it
        for brand, it in list(best_per_brand.items())[:per_kw]:
            tested += 1
            e = api.get_eligibility(it.asin)
            store.save_eligibility(it.asin, e.status, e.message, e.link)
            if e.status == "Eligible":
                store.add_brand(brand, f"discovered ({kw})")
                added += 1
    run_log.append(f"Brand discovery: {len(todays)} keywords, tested {tested} new brands, "
                   f"added {added} eligible brand(s)")


def collect_candidates(api: SPAPI, store: Store, cfg: dict, price_list: dict,
                       run_log: list[str]) -> tuple[dict[str, CatalogItem], dict[str, str]]:
    items: dict[str, CatalogItem] = {}
    brand_of: dict[str, str] = {}
    s = cfg["search"]
    for brand in store.brands_to_scan(s["max_brands_per_run"]):
        token, pages = None, 0
        while pages < s["max_pages_per_brand"]:
            try:
                found, token = api.search_catalog(keywords=brand, brand=brand, page_token=token)
            except SPAPIAuthError:
                raise
            except SPAPIError as exc:
                run_log.append(f"search '{brand}' failed: {exc}")
                break
            for it in found:
                items.setdefault(it.asin, it)
                brand_of.setdefault(it.asin, brand)
            pages += 1
            if not token:
                break
        if pages:
            brand_of.setdefault(f"__scanned__{brand}", brand)

    upcs = list(price_list)
    for start in range(0, len(upcs), 20):
        try:
            found, _ = api.search_catalog(identifiers=upcs[start:start + 20])
        except SPAPIError as exc:
            run_log.append(f"price-list UPC lookup failed: {exc}")
            continue
        for it in found:
            items.setdefault(it.asin, it)
            brand_of.setdefault(it.asin, "(price list)")
    return items, brand_of


# ----------------------------------------------------------------- main run

def run(asins: list[str] | None = None) -> int:
    started = time.time()
    cfg, retailers = load_config(), load_retailers()
    api = make_client()
    store = Store(DATA_DIR / "amazonbot.db")
    run_log: list[str] = [f"Run started {dt.datetime.now():%Y-%m-%d %H:%M}"]
    price_list = load_price_lists(PRICE_LIST_DIR)
    if price_list:
        run_log.append(f"Loaded {len(price_list)} UPCs from price lists")

    for b in load_seed_brands():
        store.add_brand(b, "seed")
    for b, in store.db.execute("SELECT brand FROM brands WHERE status != 'skip'").fetchall():
        if b.lower() in {x.lower() for x in cfg["search"].get("exclude_brands", [])}:
            store.mark_brand_skip(b, "excluded brand (generic / Amazon's own)")

    try:
        # 1. Candidates
        if asins:
            items, brand_of = {}, {}
            for start in range(0, len(asins), 20):
                found, _ = api.search_catalog(identifiers=asins[start:start + 20],
                                              identifiers_type="ASIN")
                for it in found:
                    items[it.asin] = it
            pre_filter = list(items.values())
        else:
            discover_brands(api, store, cfg, run_log)
            items, brand_of = collect_candidates(api, store, cfg, price_list, run_log)
            excluded = {b.lower() for b in cfg["search"].get("exclude_brands", [])}
            pre_filter = [
                it for it in items.values()
                if not (cfg["search"]["skip_variation_parents"] and it.is_variation_parent)
                and (it.brand or "").lower() not in excluded
                and rules.rank_ok(it.rank, it.category, cfg)
            ]
        run_log.append(f"Found {len(items)} products, {len(pre_filter)} pass the sales-rank filter")

        # 2. Eligibility
        eligible: list[CatalogItem] = []
        elig_status: dict[str, tuple] = {}
        brand_stats: dict[str, list[int]] = {}
        for it in pre_filter:
            cached = store.cached_eligibility(it.asin, cfg["search"]["eligibility_cache_days"])
            if cached is None:
                e = api.get_eligibility(it.asin)
                cached = (e.status, e.message, e.link)
                store.save_eligibility(it.asin, *cached)
            elig_status[it.asin] = cached
            b = brand_of.get(it.asin)
            if b:
                st = brand_stats.setdefault(b, [0, 0])
                st[1] += 1
                st[0] += cached[0] == "Eligible"
            if cached[0] == "Eligible":
                eligible.append(it)
        for key, b in brand_of.items():
            if key.startswith("__scanned__"):
                e_cnt, c_cnt = brand_stats.get(b, [0, 0])
                store.update_brand(b, e_cnt, c_cnt)
        run_log.append(f"Eligible: {len(eligible)} of {len(pre_filter)} checked")

        # 3. Live offers
        offers = api.get_offers([it.asin for it in eligible]) if eligible else {}
        min_price = cfg["thresholds"]["min_sell_price"]
        min_offers = int(cfg["search"].get("min_total_offers", 3))
        priced, few_sellers = [], 0
        brand_single: dict[str, list[int]] = {}
        for it in eligible:
            o = offers.get(it.asin)
            if not o or o.error or not o.sell_price:
                continue
            b = brand_of.get(it.asin)
            if b:
                st = brand_single.setdefault(b, [0, 0])
                st[1] += 1
                st[0] += o.total_offers < min_offers
            if o.total_offers < min_offers:
                few_sellers += 1      # 1-2 sellers = usually the brand owner / private label
                continue
            if o.sell_price < min_price:
                continue
            priced.append((it, o))
        ratio = float(cfg["search"].get("private_label_ratio", 0.8))
        pl_brands = [b for b, (single, total) in brand_single.items()
                     if total >= 5 and single / total >= ratio and not b.startswith("(")]
        for b in pl_brands:
            store.mark_brand_skip(b, "private label? (most listings have only 1-2 sellers)")
        run_log.append(f"Skipped {few_sellers} listings with fewer than {min_offers} sellers"
                       + (f"; brands marked private label and skipped from now on: {', '.join(pl_brands)}"
                          if pl_brands else ""))

        # 4. Fees
        fees = api.get_fees({it.asin: o.sell_price for it, o in priced}) if priced else {}

        # 5. Analyse each product (history, flags, max buy price)
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        candidates: list[Candidate] = []
        dropped = {"no fees": 0, "Amazon selling": 0, "compliance": 0, "bundle": 0, "no margin": 0}
        for it, o in priced:
            f = fees.get(it.asin)
            hist = store.analyze(it.asin, o.sell_price, o.total_offers)
            store.save_snapshot(it.asin, o.buybox_price, o.fba_offers, o.fbm_offers,
                                o.total_offers, o.amazon_on_listing, it.rank)
            if not f or f.error:
                dropped["no fees"] += 1
                continue
            est = rules.est_monthly_sales(it.rank)
            flags = list(hist.flags)
            if o.amazon_on_listing:
                flags.insert(0, ("amazon_now", "Amazon is selling this now"))
            if o.buybox_price is None:
                flags.append(("no_buybox", "No buy box right now (suppressed?) — priced off lowest FBA offer"))
            if o.buybox_is_fba is False:
                flags.append(("mfn_buybox", "Buy box held by a merchant-fulfilled seller"))
            lflags, is_bundle = rules.listing_flags(it.title, it.category, est, o.fba_offers, cfg)
            flags += lflags
            if o.amazon_on_listing:
                dropped["Amazon selling"] += 1
                continue
            if rules.blocking(flags, cfg):
                dropped["compliance"] += 1
                continue
            if is_bundle and cfg.get("competition", {}).get("skip_seller_bundles", True):
                dropped["bundle"] += 1
                continue
            pc = ProductCosts(sell_price=o.sell_price, referral_fee=f.referral,
                              fba_fee=f.fba, closing_fee=f.closing, category=it.category,
                              weight_lb=it.weight_lb, length_in=it.length_in,
                              width_in=it.width_in, height_in=it.height_in)
            min_p, min_r, rule = rules.targets(est, cfg)
            mb = max_buy_price(pc, cfg, min_p, min_r)
            if mb <= 0:
                dropped["no margin"] += 1
                continue
            c = Candidate(it=it, o=o, f=f, pc=pc, est=est, flags=flags, hist=hist, max_buy=mb,
                          min_profit=min_p, min_roi=min_r, rule=rule)
            c.priority = rules.score(0.5, min_p, est, o.fba_offers, True, rules.n_serious(flags)) \
                + min(mb, 30) / 3
            candidates.append(c)
        run_log.append("Dropped before sourcing: " +
                       ", ".join(f"{k} {v}" for k, v in dropped.items() if v) if any(dropped.values())
                       else "Dropped before sourcing: none")

        # 6. Suppliers: free sources first, then discovery, then verification
        providers = make_providers(cfg, store)
        for p in providers.values():
            run_log.append(f"Provider {p.name}: " + (f"OFF ({p.disabled_reason})" if p.disabled_reason
                           else f"ON, run budget {p.budget}" + (f", {p.plan_left} left on free plan"
                                                                 if p.plan_left is not None else "")))
        if not any(not providers[k].disabled_reason for k in ("serper", "serpapi")):
            run_log.append("⚠️ SUPPLIER SEARCH OFF: no Google Shopping provider (add SERPER_API_KEY and/or SERPAPI_KEY)")

        all_upcs = [upc12(u) for c in candidates for u in c.it.upcs[:1]]
        bby_hits = providers["bestbuy"].lookup_upcs(all_upcs, int(cfg["suppliers"]["cache_days"])) \
            if all_upcs and providers["bestbuy"].active else {}
        if bby_hits:
            run_log.append(f"Best Buy carries {len(bby_hits)} of {len(set(all_upcs))} UPCs")

        candidates.sort(key=lambda c: c.priority, reverse=True)
        leads, review, options, no_supplier = [], [], [], []
        min_mb = cfg["suppliers"].get("min_candidate_max_buy", 3.0)
        skip_days = cfg["suppliers"].get("recheck_after_days", {})
        deadline = started + 60 * float(cfg["search"].get("max_run_minutes", 50))
        seen_upcs: dict[str, str] = {}
        for c in candidates:
            blocks = rules.lead_blockers(c.flags, cfg)
            if blocks:
                no_supplier.append(no_supplier_row(c, now, "Not searched — listing problem: " + "; ".join(blocks)))
                continue
            if c.max_buy < min_mb:
                no_supplier.append(no_supplier_row(
                    c, now, f"Not searched — max buy price ${c.max_buy:.2f} is under ${min_mb:.2f}, "
                            "almost never available that cheap"))
                continue

            offers = free_offers(c.it, price_list, bby_hits, retailers)
            status, searched = "", False
            evals = [evaluate_offer(c, o, retailers, cfg) for o in offers]
            if not is_lead(evals):
                last = store.last_sourcing(c.it.asin)
                wait = int(skip_days.get(last[1], 0)) if last else 0
                age = (dt.date.today() - dt.date.fromisoformat(last[0])).days if last else 999
                upc_key = upc12(c.it.upcs[0]) if c.it.upcs else ""
                if last and age < wait:
                    if not offers:
                        recheck = dt.date.fromisoformat(last[0]) + dt.timedelta(days=wait)
                        no_supplier.append(no_supplier_row(
                            c, now, f"Searched {last[0]}: {last[2]} — rechecks {recheck} (saves searches)"))
                        continue
                elif upc_key and upc_key in seen_upcs:
                    status = f"Same UPC as {seen_upcs[upc_key]} (searched once this run)"
                elif time.time() > deadline:
                    no_supplier.append(no_supplier_row(c, now, "Not searched this run: time limit reached "
                                                                "(GitHub free minutes) — next run"))
                    continue
                else:
                    found = discover(c.it, providers, retailers, cfg)
                    searched = bool(found.tried)
                    status = found.status
                    offers = dedupe(offers + found.offers)
                    if upc_key:
                        seen_upcs[upc_key] = c.it.asin
                    evals = [evaluate_offer(c, o, retailers, cfg) for o in offers]
                    if not is_lead(evals) and needs_verification(evals):
                        extra = verify(c.it, offers, providers, retailers, cfg)
                        if extra:
                            offers = dedupe(offers + extra)
            lead, rev, opts, none, outcome = decide(c, dedupe(offers), status, retailers, cfg, now)
            if searched:
                store.log_sourcing(c.it.asin, outcome, (none or {}).get("Why no lead", outcome))
            options += opts
            if lead:
                leads.append(lead)
            elif rev:
                review.append(rev)
            else:
                no_supplier.append(none)

        for rows in (leads, review, no_supplier):
            rows.sort(key=lambda r: r["Score"], reverse=True)
        usage_rows = [p.report() for p in providers.values()]
        for u in usage_rows:
            run_log.append(f"{u['Provider']}: used {u['Used this run']} (+{u['Cache hits (free)']} cached), "
                           f"left {u['Left on free plan'] if u['Left on free plan'] != '' else '?'}"
                           + (f" — errors: {u['Errors']}" if u["Errors"] else ""))
        result_line = (f"RESULT: {len(leads)} Leads, {len(review)} Needs Review, "
                       f"{len(no_supplier)} without a usable supplier, {len(options)} supplier offers compared")
        run_log.append(result_line)

        # 7. Outputs
        brand_rows = [dict(zip(BRAND_COLUMNS, b)) for b in store.all_brands()]
        tabs = {
            "Leads": (LEAD_COLUMNS, leads),
            "Needs Review": (REVIEW_COLUMNS, review),
            "Supplier Options": (OPTION_COLUMNS, options),
            "No Supplier Yet": (NO_SUPPLIER_COLUMNS, no_supplier),
            "API Usage": (USAGE_COLUMNS, usage_rows),
            "Brands": (BRAND_COLUMNS, brand_rows),
        }
        for name, (cols, rows) in tabs.items():
            write_csv(OUTPUT_DIR / f"{name.lower().replace(' ', '_')}.csv", cols, rows)

        sheet_id, sa_json = env("GOOGLE_SHEET_ID"), env("GOOGLE_SERVICE_ACCOUNT_JSON")
        run_log.append(f"Finished in {(time.time() - started) / 60:.1f} min")
        if sheet_id and sa_json:
            try:
                write_sheets(sheet_id, sa_json, {
                    **tabs, "Run Log": (["Message"], [{"Message": m} for m in run_log])})
            except Exception as exc:  # keep the run's CSVs even if Sheets fails
                run_log.append(f"Google Sheet update failed: {exc}")
        else:
            run_log.append("Google Sheet not set up (GOOGLE_SHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON) — "
                           "results are in the run's 'results' download")

        webhook = env("DISCORD_WEBHOOK_URL")
        if webhook:
            a = cfg["alerts"]
            sent = 0
            for lead in leads:
                roi = lead["Net profit"] / lead["Total cost (landed)"]
                if lead["Net profit"] < a["min_profit"] or roi < a["min_roi"]:
                    continue
                if store.should_alert(lead["ASIN"], lead["Net profit"], lead["Eligibility"], a):
                    discord_alert(webhook, lead)
                    store.mark_alerted(lead["ASIN"], lead["Net profit"], lead["Eligibility"])
                    sent += 1
            discord_summary(webhook, f"**AmazonBot daily run** — {len(leads)} leads, "
                                     f"{len(review)} to review, {sent} new alert(s).\n"
                                     + "\n".join(m for m in run_log if m.startswith(("Serper", "SerpApi", "Tavily", "Best Buy", "⚠️"))))
    finally:
        (OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / "run_log.txt").write_text("\n".join(run_log), encoding="utf-8")
        store.close()
    print("\n".join(run_log))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="AmazonBot")
    parser.add_argument("--check", action="store_true", help="test your Amazon keys")
    parser.add_argument("--asins", help="comma-separated ASINs to analyse")
    args = parser.parse_args()
    try:
        if args.check:
            return check_setup()
        asins = [a.strip() for a in args.asins.split(",")] if args.asins else None
        return run(asins)
    except SPAPIAuthError as exc:
        print(f"\nAMAZON LOGIN PROBLEM: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
