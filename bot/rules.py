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

# (key, regex, message). Which keys BLOCK a product is set in config.yaml -> compliance.block
COMPLIANCE = [
    ("hazmat", r"aerosol|flammable|lithium|butane|propane|lighter fluid|\blighters?\b|nail polish|"
               r"spray paint|kerosene|lamp oil|hand sanitizer|rubbing alcohol|isopropyl|perfume|"
               r"eau de (toilette|parfum)|cologne|hair ?spray|dry shampoo|fireworks|bleach",
     "Possible hazmat (dangerous goods review / often restricted)"),
    ("pesticide", r"insecticide|pesticide|bug spray|insect repellent|mosquito repellent|kills? "
                  r"(bugs|ants|roaches|fleas|ticks|mosquitoes)|flea (and|&) tick|rodenticide|"
                  r"weed killer|herbicide|lice treatment|ant bait|roach bait|(mouse|rat) poison|fungicide",
     "Pesticide claim (EPA rules; usually restricted)"),
    ("cbd", r"\bcbd\b|hemp extract|cannabidiol|delta[- ]?[89]|kratom|\bthc\b|kava",
     "CBD/hemp/kratom (prohibited or heavily restricted)"),
    ("age_restricted", r"nicotine|tobacco|\bvape\b|e-cig|\bwine\b|\bbeer\b|liquor",
     "Age-restricted / regulated product"),
    ("supplement", r"supplement|vitamin|softgel|capsule|probiotic|protein powder|collagen|"
                   r"melatonin|multivitamin|fish oil|omega",
     "Dietary supplement — Amazon may require testing/compliance documents"),
    ("medical", r"test kit|thermometer|blood pressure|glucose|\ba1c\b|covid|pregnancy test|"
                r"ovulation|hearing aid|condom|contact lens|pulse oximeter|nebulizer",
     "Medical device — may need approval or documents"),
    ("infant", r"infant formula|baby formula|toddler formula",
     "Infant formula (strict expiry & approval rules)"),
    ("meltable", r"chocolate|candy bar|lip balm|\bwax\b|crayon|gummy bears|gummies",
     "Meltable — FBA won't accept it in warm months (about Apr 15 – Oct 15)"),
]
_COMPLIANCE_RE = [(k, _re.compile(rx, _re.I), msg) for k, rx, msg in COMPLIANCE]
EXPIRY_WORDS = _re.compile(
    r"(test kit|vitamin|supplement|softgel|gummies|capsule|tablet|lozenge|probiotic|"
    r"protein|formula|sunscreen|spf|medicine|drops\b)", _re.I)
EXPIRY_CATEGORIES = {"grocery", "grocery & gourmet food", "baby"}


def listing_flags(title: str, category: str, est_sales: int | None,
                  fba_offers: int, cfg: dict) -> tuple[list[tuple[str, str]], bool]:
    """Returns (flags as (key, text), is_seller_bundle)."""
    flags: list[tuple[str, str]] = []
    bundle = bool(BUNDLE_WORDS.search(title or ""))
    if bundle:
        flags.append(("bundle", "Seller-made bundle — you'd have to copy their exact add-ons"))
    lim = cfg.get("competition", {})
    if est_sales and fba_offers:
        per_seller = est_sales / (fba_offers + 1)
        if per_seller < lim.get("min_sales_per_seller", 2):
            flags.append(("crowded", f"Crowded: ~{per_seller:.1f} sales/mo per FBA seller "
                                     f"({fba_offers} FBA sellers)"))
    for key, rx, msg in _COMPLIANCE_RE:
        if rx.search(title or ""):
            flags.append((key, msg))
    if EXPIRY_WORDS.search(title or "") or (category or "").lower() in EXPIRY_CATEGORIES:
        flags.append(("expiration", "Has expiration date — Amazon needs 90+ days left; check dates"))
    return flags, bundle


def blocking(flags: list[tuple[str, str]], cfg: dict) -> list[str]:
    """Flags that remove a product completely (compliance.block)."""
    keys = set(cfg.get("compliance", {}).get("block", []))
    return [t for k, t in flags if k in keys]


def lead_blockers(flags: list[tuple[str, str]], cfg: dict) -> list[str]:
    """Flags that keep a product out of the Leads tab (leads.exclude_flags)."""
    keys = set(cfg.get("leads", {}).get("exclude_flags", []))
    return [t for k, t in flags if k in keys]


SERIOUS = {"amazon_now", "amazon_recent", "ip_drop", "price_spike", "bundle", "crowded",
           "no_buybox", "hazmat", "pesticide", "cbd"}


def n_serious(flags: list[tuple[str, str]]) -> int:
    return sum(1 for k, _ in flags if k in SERIOUS)
