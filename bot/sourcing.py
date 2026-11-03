"""Turn supplier offers into Leads / Needs Review / Supplier Options / No Supplier rows."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from . import rules
from .matching import EXACT, MISMATCH, STRONG, UNCERTAIN, USABLE
from .profit import calc_profit
from .suppliers import SupplierOffer, find_retailer


@dataclass
class Candidate:
    it: object          # CatalogItem
    o: object           # Offers
    f: object           # Fees
    pc: object          # ProductCosts
    est: int | None
    flags: list = field(default_factory=list)   # (key, text)
    hist: object = None
    max_buy: float = 0.0
    min_profit: float = 0.0
    min_roi: float = 0.0
    rule: str = ""
    priority: float = 0.0


def flags_text(flags) -> str:
    return "; ".join(t for _, t in flags)


def base_row(c: Candidate, now: str) -> dict:
    it, o, f = c.it, c.o, c.f
    return {
        "Product": it.title, "ASIN": it.asin, "Amazon link": it.amazon_url, "Brand": it.brand,
        "Category": it.category, "Amazon price": o.sell_price,
        "Est. sales/mo (estimate)": c.est or "", "Sales rank": it.rank or "",
        "FBA sellers": o.fba_offers, "FBM sellers": o.fbm_offers, "Eligibility": "Eligible",
        "Referral fee": f.referral, "FBA fee": f.fba,
        "Price stability": c.hist.stability if c.hist else "", "Flags": flags_text(c.flags),
        "Last checked": now,
    }


def no_supplier_row(c: Candidate, now: str, why: str) -> dict:
    q = c.it.upcs[0] if c.it.upcs else f"{c.it.brand} {c.it.title}"
    return {**base_row(c, now),
            "Score": rules.score(0.35, c.min_profit, c.est, c.o.fba_offers, False, rules.n_serious(c.flags)),
            "UPC": ", ".join(c.it.upcs[:3]) or "(none on listing)",
            "Max buy price": round(c.max_buy, 2), "Why no lead": why,
            "Google search": f"https://www.google.com/search?tbm=shop&q={quote_plus(q)}"}


@dataclass
class Evaluated:
    offer: SupplierOffer
    res: object               # ProfitResult
    passes: bool
    blockers: list[str]       # reasons it can't be a Lead (besides profit)
    qty: int
    ship_unit: float
    notes: list[str]


def evaluate_offer(c: Candidate, offer: SupplierOffer, retailers: dict, cfg: dict) -> Evaluated:
    s_cfg = cfg["suppliers"]
    _, retailer = find_retailer(offer.retailer if offer.retailer_key != "price list" else "", retailers)
    unit_cost = offer.unit_cost
    qty = max(rules.suggested_qty(c.est, c.o.fba_offers, max(unit_cost, 0.01), cfg), 1)
    notes = []
    if offer.shipping is None:
        ship_unit = 0.0
        fs_min = retailer.get("free_shipping_min") or 0
        if fs_min and unit_cost * qty < fs_min:
            notes.append(f"shipping not shown; order ${fs_min:.0f}+ for free shipping")
        elif offer.source != "Your price list":
            notes.append("shipping not shown (assumed free)")
    else:
        ship_unit = offer.shipping / qty
    res = calc_profit(c.pc, unit_cost, cfg, retailer=retailer, shipping_in=ship_unit)
    notes += res.notes

    certain_profit = res.profit + res.savings_certain
    certain_cost = res.landed_cost - res.savings_certain
    certain_roi = certain_profit / certain_cost if certain_cost else 0
    passes = rules.passes(res.profit, res.roi, c.min_profit, c.min_roi) or \
        rules.passes(certain_profit, certain_roi, c.min_profit, c.min_roi)

    blockers = []
    if offer.match not in USABLE:
        blockers.append(f"{offer.match} product match: {offer.match_reason}")
    if offer.availability == "Out of stock":
        blockers.append("out of stock")
    elif offer.availability != "In stock" and cfg["leads"].get("require_in_stock"):
        blockers.append("stock not confirmed")
    if not offer.trusted and not s_cfg.get("allow_unknown_retailers"):
        blockers.append("store not in your trusted retailers list — verify it's legit and gives invoices")
    if offer.third_party and not s_cfg.get("allow_marketplace_sellers"):
        blockers.append(f"marketplace seller ({offer.seller_note}) — invoices won't help ungating")
    try:
        age = (dt.date.today() - dt.date.fromisoformat(offer.checked)).days
    except ValueError:
        age = 99
    if age > s_cfg.get("max_price_age_days", 2):
        blockers.append(f"price is {age} days old")
    if offer.availability == "Unknown":
        notes.append("stock not shown — confirm on the store page")
    if offer.pack_multiplier > 1:
        notes.append(f"buy {offer.pack_multiplier} per Amazon unit, bundle + 'do not separate' sticker")
    return Evaluated(offer, res, passes, blockers, qty, ship_unit, notes)


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def offer_cols(ev: Evaluated, others: str = "") -> dict:
    o = ev.offer
    return {
        "Retailer": o.retailer, "Buy link": o.link, "Advertised price": o.price,
        "Units per Amazon unit": o.pack_multiplier, "Cost per Amazon unit": o.unit_cost,
        "Retailer shipping": "not shown" if o.shipping is None else
        ("free" if o.shipping == 0 else f"${o.shipping:.2f}/order"),
        "Availability": o.availability, "Price checked": o.checked, "Match": o.match,
        "Match details": o.match_reason, "Other suppliers": others,
    }


def profit_cols(ev: Evaluated, c: Candidate) -> dict:
    r = ev.res
    return {
        "Closing fee": r.closing, "Storage (est)": r.storage, "Returns reserve (est)": r.returns,
        "Inbound ship": r.inbound, "Prep+misc": r.prep_misc, "Tax": r.tax,
        "Retailer ship/unit": ev.ship_unit, "Total cost (landed)": r.landed_cost,
        "Net profit": r.profit, "ROI": pct(r.roi), "Margin": pct(r.margin),
        "Profit if price -10%": r.profit_at_drop, "Profit w/ savings": r.profit_with_savings,
        "ROI w/ savings": pct(r.roi_with_savings), "Certain savings": r.savings_certain,
        "Rule": c.rule, "Suggested qty": ev.qty, "Notes": "; ".join(dict.fromkeys(ev.notes)),
    }


def summarize_others(evals: list[Evaluated], chosen: Evaluated | None, limit: int = 4) -> str:
    parts = []
    for ev in evals:
        if ev is chosen or ev.offer.match == MISMATCH:
            continue
        o = ev.offer
        tag = "" if o.match in USABLE else f" ({o.match.lower()} match)"
        stock = " OOS" if o.availability == "Out of stock" else ""
        parts.append(f"{o.retailer} ${o.unit_cost:.2f}{tag}{stock}")
        if len(parts) >= limit:
            break
    return "; ".join(parts)


def decide(c: Candidate, sourcing, retailers: dict, cfg: dict, now: str):
    """Returns (lead_row | None, review_row | None, option_rows, no_supplier_row | None)."""
    evals = [evaluate_offer(c, o, retailers, cfg) for o in sourcing.offers]
    options = []
    for ev in evals[: cfg["suppliers"].get("max_options_per_product", 8)]:
        o = ev.offer
        why = list(ev.blockers)
        if not ev.passes:
            why.append(f"profit ${ev.res.profit:.2f} / ROI {pct(ev.res.roi)} below "
                       f"${c.min_profit:.2f} / {pct(c.min_roi)}")
        options.append({
            "ASIN": c.it.asin, "Product": c.it.title, "Retailer": o.retailer,
            "Trusted store": "Yes" if o.trusted else "No",
            "Advertised price": o.price, "Units per Amazon unit": o.pack_multiplier,
            "Cost per Amazon unit": o.unit_cost,
            "Retailer shipping": offer_cols(ev)["Retailer shipping"],
            "Availability": o.availability, "Match": o.match, "Match details": o.match_reason,
            "Net profit": ev.res.profit, "ROI": pct(ev.res.roi),
            "Can be a Lead": "Yes" if ev.passes and not ev.blockers else "No",
            "Why not": "; ".join(why), "Buy link": o.link, "Source": o.source,
            "Price checked": o.checked})

    lead_blocks = rules.lead_blockers(c.flags, cfg)
    winners = [ev for ev in evals if ev.passes and not ev.blockers]
    if winners and not lead_blocks:
        best = max(winners, key=lambda e: e.res.profit)
        row = {**base_row(c, now), **offer_cols(best, summarize_others(evals, best)),
               **profit_cols(best, c),
               "Score": rules.score(best.res.roi, best.res.profit, c.est, c.o.fba_offers,
                                    best.offer.match == EXACT, rules.n_serious(c.flags))}
        return row, None, options, None

    profitable = [ev for ev in evals if ev.passes and ev.offer.match != MISMATCH
                  and ev.offer.availability != "Out of stock"]
    if profitable or (winners and lead_blocks):
        best = max(profitable or winners, key=lambda e: e.res.profit)
        why = (["listing flag: " + b for b in lead_blocks]) + best.blockers
        row = {**base_row(c, now), **offer_cols(best, summarize_others(evals, best)),
               **profit_cols(best, c), "Why not a Lead": "; ".join(why),
               "Score": rules.score(best.res.roi, best.res.profit, c.est, c.o.fba_offers,
                                    False, rules.n_serious(c.flags))}
        return None, row, options, None

    return None, None, options, no_supplier_row(c, now, explain_no_lead(c, sourcing, evals))


def explain_no_lead(c: Candidate, sourcing, evals: list[Evaluated]) -> str:
    if not evals:
        return sourcing.status or "No store offers found"
    usable = [e for e in evals if e.offer.match in USABLE]
    in_stock = [e for e in usable if e.offer.availability != "Out of stock"]
    if in_stock:
        cheapest = min(in_stock, key=lambda e: e.offer.unit_cost)
        o = cheapest.offer
        return (f"Too expensive: cheapest matching offer ${o.unit_cost:.2f} at {o.retailer} "
                f"→ profit ${cheapest.res.profit:.2f} / ROI {pct(cheapest.res.roi)} "
                f"(needs buy price ≤ ${c.max_buy:.2f})")
    if usable:
        return f"Matching offers found ({len(usable)}) but all are out of stock"
    unc = [e for e in evals if e.offer.match == UNCERTAIN]
    if unc:
        e = min(unc, key=lambda e: e.offer.unit_cost)
        return (f"Only uncertain matches (e.g. {e.offer.retailer} ${e.offer.unit_cost:.2f}: "
                f"{e.offer.match_reason}) and none profitable")
    e = evals[0]
    return f"{len(evals)} offers found but none are the same size/pack (e.g. {e.offer.retailer}: {e.offer.match_reason})"
