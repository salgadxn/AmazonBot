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
from urllib.parse import quote_plus

from . import rules
from .config import (DATA_DIR, OUTPUT_DIR, PRICE_LIST_DIR, env, load_config,
                     load_retailers, load_seed_brands)
from .outputs import (BRAND_COLUMNS, FIND_SUPPLIER_COLUMNS, LEAD_COLUMNS,
                      discord_alert, discord_summary, write_csv, write_sheets)
from .profit import ProductCosts, calc_profit, max_buy_price
from .spapi import SPAPI, CatalogItem, SPAPIAuthError, SPAPIError
from .store import Store
from .suppliers import (best_serp_offer, find_retailer, load_price_lists,
                        match_price_list, serpapi_lookup)

log = logging.getLogger("amazonbot")


def make_client() -> SPAPI:
    return SPAPI(env("SPAPI_LWA_CLIENT_ID", True), env("SPAPI_LWA_CLIENT_SECRET", True),
                 env("SPAPI_REFRESH_TOKEN", True), env("SPAPI_SELLER_ID", True))


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
    print("\nEverything works. Your keys are set up correctly.")
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
    for kw in todays:
        try:
            items, _ = api.search_catalog(keywords=kw)
        except SPAPIAuthError:
            raise
        except SPAPIError as exc:
            run_log.append(f"discovery '{kw}' failed: {exc}")
            continue
        best_per_brand: dict[str, CatalogItem] = {}
        for it in items:
            if not it.brand or store.known_brand(it.brand) or it.is_variation_parent:
                continue
            cur = best_per_brand.get(it.brand)
            if cur is None or (it.rank or 10**9) < (cur.rank or 10**9):
                best_per_brand[it.brand] = it
        for brand, it in list(best_per_brand.items())[:10]:
            e = api.get_eligibility(it.asin)
            store.save_eligibility(it.asin, e.status, e.message, e.link)
            if e.status == "Eligible":
                store.add_brand(brand, f"discovered ({kw})")
                added += 1
    run_log.append(f"Brand discovery: searched {todays}, added {added} eligible brand(s)")


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
            pre_filter = [
                it for it in items.values()
                if not (cfg["search"]["skip_variation_parents"] and it.is_variation_parent)
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
        priced = []
        for it in eligible:
            o = offers.get(it.asin)
            if not o or o.error or not o.sell_price:
                continue
            if o.sell_price < min_price:
                continue
            priced.append((it, o))

        # 4. Fees
        fees = api.get_fees({it.asin: o.sell_price for it, o in priced}) if priced else {}

        # 5. Analyse
        leads, find_supplier, viable = [], [], []
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        for it, o in priced:
            f = fees.get(it.asin)
            hist = store.analyze(it.asin, o.sell_price, o.total_offers)
            store.save_snapshot(it.asin, o.buybox_price, o.fba_offers, o.fbm_offers,
                                o.total_offers, o.amazon_on_listing, it.rank)
            if not f or f.error:
                continue
            flags = list(hist.flags)
            if o.amazon_on_listing:
                flags.insert(0, "Amazon is selling this now")
            if o.buybox_price is None:
                flags.append("No buy box right now (suppressed?) — priced off lowest FBA offer")
            if o.buybox_is_fba is False:
                flags.append("Buy box held by a merchant-fulfilled seller")

            pc = ProductCosts(sell_price=o.sell_price, referral_fee=f.referral,
                              fba_fee=f.fba, closing_fee=f.closing, category=it.category,
                              weight_lb=it.weight_lb, length_in=it.length_in,
                              width_in=it.width_in, height_in=it.height_in)
            est = rules.est_monthly_sales(it.rank)
            std_max = max_buy_price(pc, cfg, cfg["thresholds"]["min_profit"],
                                    cfg["thresholds"]["min_roi"])
            sm = cfg["starter_mode"]
            starter_max = (max_buy_price(pc, cfg, sm["min_profit"], sm["min_roi"])
                           if sm.get("enabled") and (est or 0) >= sm["min_est_monthly_sales"]
                           else 0.0)
            if max(std_max, starter_max) <= 0 or o.amazon_on_listing:
                continue
            viable.append(dict(it=it, o=o, f=f, pc=pc, est=est, flags=flags, hist=hist,
                               std_max=std_max, starter_max=starter_max))

        # 6. Suppliers
        serp_key = env("SERPAPI_KEY")
        serp_budget = cfg["suppliers"]["serpapi_max_lookups_per_run"] if serp_key else 0
        viable.sort(key=lambda v: rules.score(0.5, 5, v["est"], v["o"].fba_offers, True,
                                              len(v["flags"])), reverse=True)
        for v in viable:
            it, o, pc = v["it"], v["o"], v["pc"]
            offer = match_price_list(it.upcs, price_list)
            if offer is None and it.upcs:
                upc = it.upcs[0]
                results = store.cached_serp(upc, cfg["suppliers"]["serpapi_cache_days"])
                if results is None and serp_budget > 0:
                    serp_budget -= 1
                    results = serpapi_lookup(upc, serp_key)
                    store.save_serp(upc, results)
                if results:
                    offer = best_serp_offer(results, it.title, retailers, cfg)

            base_row = {
                "Product": it.title, "ASIN": it.asin, "Amazon link": it.amazon_url,
                "Brand": it.brand, "Category": it.category, "Amazon price": o.sell_price,
                "Est. sales/mo (estimate)": v["est"] or "", "Sales rank": it.rank or "",
                "FBA sellers": o.fba_offers, "FBM sellers": o.fbm_offers,
                "Amazon on listing": "Yes" if o.amazon_on_listing else "No",
                "Eligibility": "Eligible", "Referral fee": v["f"].referral,
                "FBA fee": v["f"].fba, "Price stability": v["hist"].stability,
                "Red flags": "; ".join(v["flags"]), "Last checked": now,
            }

            if offer is None:
                q = it.upcs[0] if it.upcs else it.title
                find_supplier.append({**base_row,
                    "Score": rules.score(0.35, 5, v["est"], o.fba_offers, False, len(v["flags"])),
                    "UPC": ", ".join(it.upcs[:3]),
                    "Max buy price (Standard)": round(v["std_max"], 2),
                    "Max buy price (Starter)": round(v["starter_max"], 2) or "",
                    "Google search": f"https://www.google.com/search?tbm=shop&q={quote_plus(q)}"})
                continue

            rkey, retailer = (offer.retailer_key, retailers["retailers"][offer.retailer_key]) \
                if offer.retailer_key else find_retailer(offer.supplier, retailers)
            res = calc_profit(pc, offer.price, cfg, retailer=retailer)
            min_p, min_r, rule = rules.targets(v["est"], cfg)
            certain_profit = res.profit + res.savings_certain
            certain_cost = res.landed_cost - res.savings_certain
            certain_roi = certain_profit / certain_cost if certain_cost else 0
            ok = rules.passes(res.profit, res.roi, min_p, min_r) or \
                rules.passes(certain_profit, certain_roi, min_p, min_r)
            notes = list(res.notes)
            if not rkey:
                notes.append("unknown retailer — verify it's legit & gives an invoice")
            if retailer.get("free_shipping_min") and offer.price < retailer["free_shipping_min"]:
                notes.append(f"order ${retailer['free_shipping_min']:.0f}+ for free shipping")
            if not offer.verified:
                notes.append("product match is weak — confirm it's the same item/size/pack")
            if not ok:
                continue
            qty = rules.suggested_qty(v["est"], o.fba_offers, res.landed_cost, cfg)
            leads.append({**base_row,
                "Score": rules.score(res.roi, res.profit, v["est"], o.fba_offers,
                                     offer.verified, len(v["flags"])),
                "Supplier": offer.supplier, "Supplier link": offer.link,
                "Match": f"{offer.match_type} ({offer.match_score:.2f})",
                "Buy price": offer.price, "Closing fee": res.closing,
                "Storage (est)": res.storage, "Returns reserve (est)": res.returns,
                "Inbound ship": res.inbound, "Prep+misc": res.prep_misc, "Tax": res.tax,
                "Total cost (landed)": res.landed_cost, "Net profit": res.profit,
                "ROI": pct(res.roi), "Margin": pct(res.margin),
                "Profit if price -10%": res.profit_at_drop,
                "Profit w/ savings": res.profit_with_savings,
                "ROI w/ savings": pct(res.roi_with_savings),
                "Certain savings": res.savings_certain, "Rule": rule,
                "Suggested qty": qty, "Notes": "; ".join(notes)})

        leads.sort(key=lambda r: r["Score"], reverse=True)
        find_supplier.sort(key=lambda r: r["Score"], reverse=True)
        run_log.append(f"Leads with a supplier: {len(leads)}; eligible products needing a "
                       f"supplier: {len(find_supplier)}")

        # 7. Outputs
        brand_rows = [dict(zip(BRAND_COLUMNS, b)) for b in store.all_brands()]
        write_csv(OUTPUT_DIR / "leads.csv", LEAD_COLUMNS, leads)
        write_csv(OUTPUT_DIR / "find_supplier.csv", FIND_SUPPLIER_COLUMNS, find_supplier)
        write_csv(OUTPUT_DIR / "brands.csv", BRAND_COLUMNS, brand_rows)

        sheet_id, sa_json = env("GOOGLE_SHEET_ID"), env("GOOGLE_SERVICE_ACCOUNT_JSON")
        run_log.append(f"Finished in {(time.time() - started) / 60:.1f} min")
        if sheet_id and sa_json:
            try:
                write_sheets(sheet_id, sa_json, {
                    "Leads": (LEAD_COLUMNS, leads),
                    "Find Supplier": (FIND_SUPPLIER_COLUMNS, find_supplier),
                    "Brands": (BRAND_COLUMNS, brand_rows),
                    "Run Log": (["Message"], [{"Message": m} for m in run_log]),
                })
            except Exception as exc:  # keep the run's CSVs even if Sheets fails
                run_log.append(f"Google Sheet update failed: {exc}")

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
                                     f"{len(find_supplier)} eligible products to price-check, "
                                     f"{sent} new alert(s).")
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
