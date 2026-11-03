"""Find where to buy: (1) your wholesale price lists, (2) Google Shopping by
UPC via SerpApi. Retail sites are never scraped directly."""
from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)
SERPAPI_URL = "https://serpapi.com/search.json"


@dataclass
class SupplierOffer:
    supplier: str
    price: float
    link: str
    title: str
    match_type: str        # "UPC (price list)", "UPC search", "Weak match - verify"
    match_score: float
    retailer_key: str = ""  # key in retailers.yaml, "" if unknown

    @property
    def verified(self) -> bool:
        return self.match_type.startswith("UPC")


def normalize_upc(code: str) -> str:
    digits = re.sub(r"\D", "", str(code or ""))
    return digits.lstrip("0")


def title_similarity(a: str, b: str) -> float:
    def tokens(s):
        return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 1}
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_retailer(source: str, retailers: dict) -> tuple[str, dict]:
    s = (source or "").lower()
    for key, r in (retailers.get("retailers") or {}).items():
        if any(m in s for m in r.get("match", [])):
            return key, r
    return "", retailers.get("default", {})


# ---------------------------------------------------------- price lists

def load_price_lists(folder: Path) -> dict[str, dict]:
    """CSV files in data/price_lists/ with columns:
       upc, cost, supplier, (optional) name, link, case_pack
    Returns {normalized_upc: row} keeping the cheapest cost per UPC."""
    out: dict[str, dict] = {}
    if not folder.exists():
        return out
    for path in sorted(folder.glob("*.csv")):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                upc = normalize_upc(row.get("upc", ""))
                try:
                    cost = float(row.get("cost", "").replace("$", "").replace(",", ""))
                except ValueError:
                    continue
                if not upc or cost <= 0:
                    continue
                row["cost"] = cost
                row.setdefault("supplier", path.stem)
                if upc not in out or cost < out[upc]["cost"]:
                    out[upc] = row
    return out


def match_price_list(upcs: list[str], price_list: dict[str, dict]) -> SupplierOffer | None:
    for upc in upcs:
        row = price_list.get(normalize_upc(upc))
        if row:
            return SupplierOffer(
                supplier=row.get("supplier", "price list"), price=row["cost"],
                link=row.get("link", ""), title=row.get("name", ""),
                match_type="UPC (price list)", match_score=1.0)
    return None


# ---------------------------------------------------------- SerpApi

def serpapi_lookup(upc: str, api_key: str) -> list[dict]:
    resp = requests.get(SERPAPI_URL, params={
        "engine": "google_shopping", "q": upc, "gl": "us", "hl": "en",
        "api_key": api_key}, timeout=60)
    if resp.status_code != 200:
        log.warning("SerpApi error %s: %s", resp.status_code, resp.text[:200])
        return []
    data = resp.json()
    results = []
    for r in data.get("shopping_results", []) or []:
        price = r.get("extracted_price")
        if price is None:
            continue
        results.append({
            "title": r.get("title", ""),
            "source": r.get("source", ""),
            "price": float(price),
            "link": r.get("product_link") or r.get("link", ""),
        })
    return results


def best_serp_offer(results: list[dict], amazon_title: str, retailers: dict,
                    cfg: dict) -> SupplierOffer | None:
    blocked = [b.lower() for b in cfg["suppliers"]["blocked_sources"]]
    min_match = cfg["suppliers"]["min_title_match"]
    candidates = []
    for r in results:
        src = r["source"].lower()
        if any(b in src for b in blocked):
            continue
        sim = title_similarity(r["title"], amazon_title)
        key, _ = find_retailer(r["source"], retailers)
        match_type = "UPC search" if sim >= min_match else "Weak match - verify"
        candidates.append(SupplierOffer(
            supplier=r["source"], price=r["price"], link=r["link"], title=r["title"],
            match_type=match_type, match_score=round(sim, 2), retailer_key=key))
    if not candidates:
        return None
    # Prefer good matches, then cheapest.
    candidates.sort(key=lambda o: (not o.verified, o.price))
    return candidates[0]
