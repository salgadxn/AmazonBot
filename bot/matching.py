"""Is this supplier offer the same product as the Amazon listing?

Match levels (strongest first):
  Exact     - UPC confirmed (your price list, or retailer lookup by UPC returned it)
  Strong    - brand + size/count agree and titles are similar
  Uncertain - can't confirm size/count or brand -> needs your eyes
  Mismatch  - size, count or pack clearly different -> never used for a lead

If the Amazon listing is a multipack (e.g. "Pack of 3") and the store sells
single units of the same size, the match is Strong with "buy 3" (pack_multiplier).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

EXACT, STRONG, UNCERTAIN, MISMATCH = "Exact", "Strong", "Uncertain", "Mismatch"
USABLE = (EXACT, STRONG)

# unit -> (group, factor to a base unit)
UNITS = {
    "fl oz": ("volume", 29.5735), "floz": ("volume", 29.5735), "fluid ounce": ("volume", 29.5735),
    "fluid ounces": ("volume", 29.5735), "ml": ("volume", 1.0), "milliliter": ("volume", 1.0),
    "l": ("volume", 1000.0), "liter": ("volume", 1000.0), "litre": ("volume", 1000.0),
    "gal": ("volume", 3785.41), "gallon": ("volume", 3785.41), "qt": ("volume", 946.353),
    "oz": ("weight", 28.3495), "ounce": ("weight", 28.3495), "ounces": ("weight", 28.3495),
    "g": ("weight", 1.0), "gram": ("weight", 1.0), "grams": ("weight", 1.0),
    "kg": ("weight", 1000.0), "lb": ("weight", 453.592), "lbs": ("weight", 453.592),
    "pound": ("weight", 453.592), "pounds": ("weight", 453.592),
    "mg": ("strength", 1.0), "mcg": ("strength", 0.001), "iu": ("iu", 1.0),
}
COUNT_WORDS = ("ct", "count", "counts", "softgels", "softgel", "capsules", "capsule", "caps",
               "tablets", "tablet", "tabs", "gummies", "lozenges", "packets", "pods", "rolls",
               "sheets", "wipes", "bags", "bars", "pieces", "pcs", "pc", "servings", "k-cups",
               "kcups", "cups", "sticks", "pads", "liners", "diapers", "pouches", "cans", "bottles",
               "ea", "each")

_unit_alt = "|".join(sorted((re.escape(u) for u in list(UNITS) + list(COUNT_WORDS)),
                            key=len, reverse=True))
SIZE_RE = re.compile(rf"(\d+(?:[.,]\d+)?)\s*-?\s*({_unit_alt})\b\.?", re.I)
PACK_RE = re.compile(
    r"(?:pack\s+of|case\s+of|set\s+of|box\s+of|lot\s+of)\s*(\d+)"
    r"|(\d+)\s*-?\s*(?:pack|pk|packs|ct\.? pack|count pack)\b"
    r"|\b(\d+)\s*x\s*\d", re.I)
WORD_RE = re.compile(r"[a-z0-9]+")
STOP = {"the", "and", "for", "with", "of", "by", "a", "an", "in", "to", "ea", "each", "new",
        "pack", "count", "ct", "oz", "fl", "size", "value", "amazon", "walmart", "target"}


@dataclass
class ParsedTitle:
    sizes: dict          # group -> list of base values
    pack: int | None
    words: set


def parse_title(title: str) -> ParsedTitle:
    t = (title or "").lower().replace("fl. oz", "fl oz").replace("fl.oz", "fl oz")
    sizes: dict[str, list[float]] = {}
    for num, unit in SIZE_RE.findall(t):
        val = float(num.replace(",", "."))
        unit = unit.lower().rstrip(".")
        if unit in UNITS:
            group, factor = UNITS[unit]
            sizes.setdefault(group, []).append(val * factor)
        else:
            sizes.setdefault("count", []).append(val)
    pack = None
    for m in PACK_RE.finditer(t):
        n = next(int(g) for g in m.groups() if g)
        if 1 < n <= 48:
            pack = n
            break
    words = {w for w in WORD_RE.findall(t) if len(w) > 1 and w not in STOP and not w.isdigit()}
    return ParsedTitle(sizes, pack, words)


def _close(a: float, b: float, tol: float = 0.04) -> bool:
    return abs(a - b) <= tol * max(a, b)


def similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class MatchResult:
    level: str
    reason: str
    pack_multiplier: int = 1


def compare(amazon_title: str, brand: str, offer_title: str,
            upc_confirmed: bool = False) -> MatchResult:
    if upc_confirmed:
        return MatchResult(EXACT, "UPC confirmed")
    a, o = parse_title(amazon_title), parse_title(offer_title)
    reasons = []

    brand_words = {w for w in WORD_RE.findall((brand or "").lower()) if len(w) > 1}
    brand_ok = bool(brand_words) and brand_words <= (o.words | set(WORD_RE.findall(offer_title.lower())))
    if brand_words and not brand_ok:
        reasons.append(f"brand '{brand}' not in store title")

    # size / count comparison
    size_ok, conflict = False, []
    for group, a_vals in a.sizes.items():
        o_vals = o.sizes.get(group)
        if not o_vals:
            continue
        if any(_close(x, y) for x in a_vals for y in o_vals):
            size_ok = True
        else:
            conflict.append(group)

    multiplier = 1
    a_pack, o_pack = a.pack or 1, o.pack or 1
    if a_pack != o_pack:
        if o_pack == 1 and a_pack > 1:
            multiplier = a_pack          # buy N singles to make the Amazon pack
        elif a_pack > 1 and o_pack > 1 and a_pack % o_pack == 0:
            multiplier = a_pack // o_pack
        else:
            return MatchResult(MISMATCH, f"pack differs: Amazon {a_pack}-pack vs store {o_pack}-pack")

    if conflict:
        # e.g. Amazon "3 x 12 oz" vs store "12 oz" is fine if multiplier explains it
        if not (multiplier > 1 and size_ok):
            return MatchResult(MISMATCH, "size/count differs (" + ", ".join(conflict) + ")")

    sim = similarity(a.words, o.words)
    if brand_ok and size_ok and sim >= 0.25:
        note = f"brand + size match, title similarity {sim:.2f}"
        if multiplier > 1:
            note += f"; buy {multiplier} singles and bundle as {a_pack}-pack"
        return MatchResult(STRONG, note, multiplier)

    if not a.sizes:
        reasons.append("Amazon title has no size/count to compare")
    elif not size_ok:
        reasons.append("store title has no matching size/count")
    if sim < 0.25:
        reasons.append(f"titles differ (similarity {sim:.2f})")
    return MatchResult(UNCERTAIN, "; ".join(reasons) or "could not confirm", multiplier)
