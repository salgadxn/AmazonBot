"""Demand estimate, pass/fail rules, suggested quantity and ranking score."""
from __future__ import annotations

import math


def est_monthly_sales(rank: int | None) -> int | None:
    """ROUGH sales estimate from sales rank (one curve for all categories).
    Always labelled 'Estimate' in the sheet. Replace with Keepa's
    'bought in past month' data when Keepa is added."""
    if not rank or rank <= 0:
        return None
    return max(1, int(270000 / (rank ** 0.75)))


def max_rank_for(category: str, cfg: dict) -> int:
    table = cfg["max_rank"]
    return int(table.get(category, table["default"]))


def rank_ok(rank: int | None, category: str, cfg: dict) -> bool:
    return bool(rank) and rank <= max_rank_for(category, cfg)


def targets(est_sales: int | None, cfg: dict) -> tuple[float, float, str]:
    """(min_profit, min_roi, rule name) — starter mode only for fast sellers."""
    sm = cfg["starter_mode"]
    if sm.get("enabled") and (est_sales or 0) >= sm["min_est_monthly_sales"]:
        return sm["min_profit"], sm["min_roi"], "Starter"
    t = cfg["thresholds"]
    return t["min_profit"], t["min_roi"], "Standard"


def passes(profit: float, roi: float, min_profit: float, min_roi: float) -> bool:
    return profit >= min_profit and roi >= min_roi


def suggested_qty(est_sales: int | None, fba_offers: int, landed_cost: float,
                  cfg: dict) -> int:
    """Monthly sales / (competing FBA sellers + you), capped for beginners."""
    if not est_sales:
        return 0
    qty = est_sales // (fba_offers + 1)
    b = cfg["buying"]
    qty = min(qty, b["max_units_first_buy"])
    if landed_cost > 0:
        qty = min(qty, int(b["max_spend_per_product"] // landed_cost))
    return max(qty, 0)


def score(roi: float, profit: float, est_sales: int | None, fba_offers: int,
          verified_match: bool, n_flags: int) -> float:
    """0-100 ranking: profit/ROI, demand, competition, data confidence."""
    s = min(roi, 1.5) / 1.5 * 35
    s += min(profit, 20) / 20 * 15
    s += min(math.log10((est_sales or 1) + 1) / math.log10(1001), 1) * 25
    s += 10 / (fba_offers + 1)
    s += 15 if verified_match else 5
    s -= 10 * n_flags
    return round(max(s, 0), 1)


import re as _re

BUNDLE_WORDS = _re.compile(
    r"(\bbundle\b|\bincludes\b|\+ ?your|recipe card|vitamin guide|\bwith (a |an )?(free )?"
    r"(guide|card|ebook|e-book|pouch|pouches|scoop|spoon|sticker)s?\b|\bbonus\b)", _re.I)
EXPIRY_WORDS = _re.compile(
    r"(test kit|vitamin|supplement|softgel|gummies|capsule|tablet|lozenge|probiotic|"
    r"protein|formula|sunscreen|spf|medicine|drops\b)", _re.I)
EXPIRY_CATEGORIES = {"grocery", "grocery & gourmet food", "baby"}


def listing_flags(title: str, category: str, est_sales: int | None,
                  fba_offers: int, cfg: dict) -> tuple[list[str], list[str], bool]:
    """Returns (serious flags, info warnings, is_seller_bundle)."""
    serious, info = [], []
    bundle = bool(BUNDLE_WORDS.search(title or ""))
    if bundle:
        serious.append("Seller-made bundle — you'd have to copy their exact add-ons")
    lim = cfg.get("competition", {})
    if est_sales and fba_offers:
        per_seller = est_sales / (fba_offers + 1)
        if per_seller < lim.get("min_sales_per_seller", 2):
            serious.append(f"Crowded: ~{per_seller:.1f} sales/mo per FBA seller "
                           f"({fba_offers} FBA sellers)")
    if EXPIRY_WORDS.search(title or "") or category.lower() in EXPIRY_CATEGORIES:
        info.append("Has expiration date — Amazon needs 90+ days left; check dates")
    return serious, info, bundle
