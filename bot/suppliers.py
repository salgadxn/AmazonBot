"""Supplier discovery. Legitimate sources only — no direct scraping of store sites.

  1. Your wholesale price lists (data/price_lists/*.csv)       -> free, exact UPC
  2. Google Shopping search by UPC (SerpApi)                   -> 1 search
  3. Google "compare stores" list for the best match (SerpApi) -> 1 search, every store
     with price, shipping and stock text
  4. Walmart product lookup by UPC (SerpApi)                   -> 1 search, confirms UPC,
     price, stock and whether Walmart itself is the seller
  5. If the UPC finds nothing: Google Shopping by brand + title -> 1 search

Every SerpApi call is counted against a per-run budget derived from what's left on
your SerpApi plan this month, so the bot never burns the whole month in one day.
"""
from __future__ import annotations

import calendar
import csv
import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .matching import EXACT, MISMATCH, STRONG, UNCERTAIN, compare

log = logging.getLogger(__name__)
SERPAPI_URL = "https://serpapi.com/search.json"
SERPAPI_ACCOUNT_URL = "https://serpapi.com/account.json"


# ---------------------------------------------------------------- data types

@dataclass
class SupplierOffer:
    retailer: str
    price: float
    link: str
    title: str
    source: str                      # where we learned about it
    checked: str                     # date the price was fetched
    retailer_key: str = ""           # key in retailers.yaml ("" = unknown store)
    shipping: float | None = None    # None = not shown
    availability: str = "Unknown"    # In stock / Out of stock / Unknown
    seller_note: str = ""            # e.g. marketplace seller on Walmart
    third_party: bool = False
    match: str = UNCERTAIN
    match_reason: str = ""
    pack_multiplier: int = 1

    @property
    def trusted(self) -> bool:
        return bool(self.retailer_key)

    @property
    def unit_cost(self) -> float:
        """Price to make ONE Amazon unit (handles 'buy 2 singles for a 2-pack')."""
        return self.price * self.pack_multiplier


@dataclass
class SourcingResult:
    offers: list[SupplierOffer] = field(default_factory=list)
    status: str = ""                 # plain-English explanation when nothing usable


def normalize_upc(code: str) -> str:
    return re.sub(r"\D", "", str(code or "")).lstrip("0")


def upc12(code: str) -> str:
    d = re.sub(r"\D", "", str(code or ""))
    if len(d) == 13 and d.startswith("0"):
        d = d[1:]
    if len(d) == 14 and d.startswith("00"):
        d = d[2:]
    return d


def find_retailer(source: str, retailers: dict) -> tuple[str, dict]:
    s = (source or "").lower()
    default = retailers.get("default", {}) or {}
    for key, r in (retailers.get("retailers") or {}).items():
        if any(m in s for m in (r or {}).get("match", [])):
            return key, {**default, **r}
    return "", dict(default)


def blocked(source: str, cfg: dict) -> bool:
    s = (source or "").lower()
    return any(b in s for b in cfg["suppliers"]["blocked_sources"])


def availability_from(texts: list[str]) -> str:
    joined = " ".join(t.lower() for t in texts if isinstance(t, str))
    if "out of stock" in joined or "sold out" in joined or "unavailable" in joined:
        return "Out of stock"
    if "in stock" in joined or "in-stock" in joined or "available" in joined:
        return "In stock"
    return "Unknown"


def shipping_from(store: dict) -> float | None:
    for key in ("shipping_extracted", "extracted_shipping"):
        if store.get(key) is not None:
            try:
                return float(store[key])
            except (TypeError, ValueError):
                pass
    texts = [store.get("shipping", "")] + list(store.get("details_and_offers") or [])
    texts += [store.get("delivery", "")]
    joined = " ".join(t.lower() for t in texts if isinstance(t, str))
    if "free" in joined and ("deliver" in joined or "shipping" in joined):
        return 0.0
    m = re.search(r"\+\s*\$(\d+(?:\.\d+)?)\s*(shipping|delivery)?", joined)
    if m:
        return float(m.group(1))
    return None


# ---------------------------------------------------------------- price lists

def load_price_lists(folder: Path) -> dict[str, dict]:
    """CSV files in data/price_lists/: upc, cost, supplier (+ optional name, link, in_stock)."""
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
                row["file_date"] = dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
                if upc not in out or cost < out[upc]["cost"]:
                    out[upc] = row
    return out


def price_list_offer(upcs: list[str], price_list: dict) -> SupplierOffer | None:
    for upc in upcs:
        row = price_list.get(normalize_upc(upc))
        if row:
            stock = row.get("in_stock", "").lower()
            return SupplierOffer(
                retailer=row.get("supplier", "price list"), price=row["cost"],
                link=row.get("link", ""), title=row.get("name", ""),
                source="Your price list", checked=row["file_date"],
                retailer_key="price list", shipping=None,
                availability="In stock" if stock in ("yes", "true", "1") else
                "Out of stock" if stock in ("no", "false", "0") else "Unknown",
                match=EXACT, match_reason="UPC in your price list")
    return None


# ---------------------------------------------------------------- SerpApi

class SerpApi:
    """Budgeted SerpApi client with clear diagnostics."""

    def __init__(self, api_key: str, cfg: dict, store, http=None):
        self.key = api_key
        self.cfg = cfg["suppliers"]
        self.store = store
        self.http = http or requests.Session()
        self.calls = 0
        self.cache_hits = 0
        self.errors: list[str] = []
        self.searches_left: int | None = None
        self.budget = 0
        self.disabled_reason = "" if api_key else "SERPAPI_KEY secret is not set in GitHub"
        if api_key:
            self._set_budget()

    def _set_budget(self) -> None:
        cap = int(self.cfg["serpapi_max_calls_per_run"])
        try:
            r = self.http.get(SERPAPI_ACCOUNT_URL, params={"api_key": self.key}, timeout=30)
            data = r.json()
            if r.status_code != 200 or data.get("error"):
                self.disabled_reason = f"SerpApi rejected the key: {data.get('error', r.status_code)}"
                return
            left = data.get("total_searches_left", data.get("plan_searches_left"))
            self.searches_left = int(left) if left is not None else None
        except Exception as exc:  # network hiccup: fall back to config cap
            self.errors.append(f"account check failed: {exc}")
        if self.searches_left is None:
            self.budget = cap
            return
        if self.searches_left <= 0:
            self.disabled_reason = "SerpApi plan has 0 searches left this month"
            return
        today = dt.date.today()
        days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
        reserve = int(self.cfg.get("serpapi_reserve", 5))
        daily = max(1, (self.searches_left - reserve) // max(days_left, 1))
        self.budget = min(cap, daily)

    @property
    def active(self) -> bool:
        return not self.disabled_reason and self.budget > 0

    def remaining(self) -> int:
        return max(self.budget - self.calls, 0) if self.active else 0

    def search(self, params: dict, cache_key: str) -> tuple[dict | None, str]:
        """Returns (json, fetched_date). Uses the local cache first (free)."""
        cached = self.store.cached_serp(cache_key, int(self.cfg["serpapi_cache_days"]))
        if cached is not None:
            self.cache_hits += 1
            return cached
        if not self.active:
            return None, ""
        if self.calls >= self.budget:
            return None, ""
        self.calls += 1
        try:
            r = self.http.get(SERPAPI_URL, params={**params, "api_key": self.key}, timeout=90)
            data = r.json()
        except Exception as exc:
            self.errors.append(f"{params.get('engine')}: {exc}")
            return None, ""
        if data.get("error"):
            err = data["error"]
            # "no results" is a normal answer, not a failure
            if "hasn't returned any results" in err or "no results" in err.lower():
                data = {}
            else:
                self.errors.append(f"{params.get('engine')}: {err}")
                if "run out" in err.lower() or "invalid api key" in err.lower():
                    self.disabled_reason = f"SerpApi: {err}"
                return None, ""
        fetched = dt.date.today().isoformat()
        self.store.save_serp(cache_key, [data, fetched])
        return data, fetched

    # ---- engines
    def shopping(self, query: str):
        return self.search({"engine": "google_shopping", "q": query, "gl": "us", "hl": "en",
                            "location": "United States"}, f"gs:{query}")

    def compare_stores(self, page_token: str):
        return self.search({"engine": "google_immersive_product", "page_token": page_token,
                            "more_stores": "true"}, f"im:{page_token[:120]}")

    def walmart_upc(self, upc: str):
        return self.search({"engine": "walmart_product", "upc": upc}, f"wm:{upc}")


# ---------------------------------------------------------------- parsing

def offers_from_shopping(data: dict, fetched: str, retailers: dict, cfg: dict) -> tuple[list[SupplierOffer], list[dict]]:
    """Returns (offers, raw results) — raw results carry immersive page tokens."""
    offers, raw = [], []
    for r in (data or {}).get("shopping_results", []) or []:
        title = r.get("title", "")
        if r.get("second_hand_condition") or re.search(r"\b(used|refurbished|pre-owned|open box)\b", title, re.I):
            continue
        price = r.get("extracted_price")
        src = r.get("source", "")
        if price is None or blocked(src, cfg):
            continue
        key, _ = find_retailer(src, retailers)
        raw.append(r)
        offers.append(SupplierOffer(
            retailer=src, price=float(price), link=r.get("product_link") or r.get("link", ""),
            title=title, source="Google Shopping", checked=fetched, retailer_key=key,
            shipping=shipping_from(r), availability=availability_from(
                [r.get("delivery", "")] + list(r.get("extensions") or []))))
    return offers, raw


def offers_from_stores(data: dict, fetched: str, retailers: dict, cfg: dict) -> list[SupplierOffer]:
    pr = (data or {}).get("product_results", {}) or {}
    product_title = pr.get("title", "")
    offers = []
    for s in pr.get("stores", []) or []:
        name = s.get("name", "")
        price = s.get("extracted_price")
        if price is None or blocked(name, cfg):
            continue
        texts = list(s.get("details_and_offers") or []) + [s.get("tag", "") or ""]
        key, _ = find_retailer(name, retailers)
        offers.append(SupplierOffer(
            retailer=name, price=float(price), link=s.get("link", ""),
            title=s.get("title") or product_title, source="Google store comparison",
            checked=fetched, retailer_key=key, shipping=shipping_from(s),
            availability=availability_from(texts)))
    return offers


def offer_from_walmart(data: dict, fetched: str, upc: str, retailers: dict) -> SupplierOffer | None:
    p = (data or {}).get("product_result") or {}
    if not p:
        return None
    price = None
    for path in (("price_map", "price"), ("offer_price",), ("price",), ("price_map", "current_price")):
        v = p
        for k in path:
            v = v.get(k) if isinstance(v, dict) else None
        if isinstance(v, (int, float)):
            price = float(v)
            break
        if isinstance(v, str):
            m = re.search(r"\d+(?:\.\d+)?", v.replace(",", ""))
            if m:
                price = float(m.group())
                break
    if price is None:
        return None
    seller = p.get("seller_name") or ""
    if not seller and isinstance(p.get("seller"), dict):
        seller = p["seller"].get("name", "")
    elif not seller and isinstance(p.get("seller"), str):
        seller = p["seller"]
    third_party = bool(seller) and "walmart" not in str(seller).lower()
    stock = p.get("in_stock")
    availability = "In stock" if stock is True else "Out of stock" if stock is False else \
        availability_from([str(p.get("availability_status", ""))])
    returned_upc = normalize_upc(p.get("upc", ""))
    item_id = p.get("us_item_id") or p.get("product_id") or ""
    link = p.get("product_page_url") or (f"https://www.walmart.com/ip/{item_id}" if item_id else "")
    key, _ = find_retailer("walmart", retailers)
    offer = SupplierOffer(
        retailer="Walmart" + (f" (marketplace: {seller})" if third_party else ""),
        price=price, link=link, title=p.get("title", ""), source="Walmart UPC lookup",
        checked=fetched, retailer_key=key, shipping=None, availability=availability,
        seller_note=f"sold by {seller}" if seller else "", third_party=third_party)
    if returned_upc and returned_upc == normalize_upc(upc):
        offer.match, offer.match_reason = EXACT, "Walmart listing has the same UPC"
    return offer


# ---------------------------------------------------------------- orchestration

def source_product(item, price_list: dict, serp: SerpApi, retailers: dict, cfg: dict) -> SourcingResult:
    """Find every supplier offer for one Amazon product and grade each match."""
    res = SourcingResult()
    pl = price_list_offer(item.upcs, price_list)
    if pl:
        res.offers.append(pl)

    upc = upc12(item.upcs[0]) if item.upcs else ""
    tried: list[str] = []
    skipped: list[str] = []
    errors_before = len(serp.errors)

    def run(label, fn, *args):
        data, fetched = fn(*args)
        if data is None:
            skipped.append(label)
            return None, ""
        tried.append(label)
        return data, fetched

    raw_results: list[dict] = []
    if upc:
        data, fetched = run("Google Shopping (UPC)", serp.shopping, upc)
        if data is not None:
            offers, raw_results = offers_from_shopping(data, fetched, retailers, cfg)
            res.offers += offers
    if not raw_results and cfg["suppliers"].get("title_search_fallback", True):
        q = " ".join(f"{item.brand} {item.title}".split()[:12])
        data, fetched = run("Google Shopping (title)", serp.shopping, q)
        if data is not None:
            offers, raw_results = offers_from_shopping(data, fetched, retailers, cfg)
            res.offers += offers

    _grade(res.offers, item)
    best_raw = _best_raw(raw_results, res.offers)
    if best_raw and best_raw.get("immersive_product_page_token"):
        data, fetched = run("store comparison", serp.compare_stores,
                            best_raw["immersive_product_page_token"])
        if data is not None:
            res.offers += offers_from_stores(data, fetched, retailers, cfg)

    if upc and cfg["suppliers"].get("walmart_upc_check", True):
        data, fetched = run("Walmart UPC", serp.walmart_upc, upc)
        if data is not None:
            wm = offer_from_walmart(data, fetched, upc, retailers)
            if wm:
                res.offers.append(wm)

    _grade(res.offers, item)
    res.offers = _dedupe(res.offers)
    res.offers.sort(key=lambda o: (o.match not in (EXACT, STRONG), o.availability == "Out of stock",
                                   not o.trusted, o.unit_cost + (o.shipping or 0)))

    if not res.offers:
        new_errors = serp.errors[errors_before:]
        if new_errors:
            res.status = "SerpApi error: " + new_errors[-1][:150]
        elif not tried and serp.disabled_reason:
            res.status = f"Supplier search not run: {serp.disabled_reason}"
        elif not tried:
            res.status = "Not searched yet: today's SerpApi budget ran out (will retry next run)"
        else:
            res.status = f"No store offers found ({', '.join(tried)})" + \
                ("" if upc else "; Amazon listing has no UPC")
    elif skipped and not serp.disabled_reason:
        res.status = "Partial search (budget ran out before: " + ", ".join(skipped) + ")"
    return res


def _grade(offers: list[SupplierOffer], item) -> None:
    for o in offers:
        if o.match == EXACT:
            continue
        m = compare(item.title, item.brand, o.title)
        o.match, o.match_reason, o.pack_multiplier = m.level, m.reason, m.pack_multiplier
        if o.source == "Google store comparison" and m.level == UNCERTAIN and o.title:
            # Google grouped this store under the same product; still needs a size check
            o.match_reason += " (Google lists it as the same product)"


def _best_raw(raw: list[dict], offers: list[SupplierOffer]) -> dict | None:
    by_title = {o.title: o for o in offers}
    ranked = sorted(raw, key=lambda r: {EXACT: 0, STRONG: 1, UNCERTAIN: 2, MISMATCH: 3}
                    .get(by_title.get(r.get("title", ""), SupplierOffer("", 0, "", "", "", "")).match, 3))
    for r in ranked:
        o = by_title.get(r.get("title", ""))
        if o and o.match != MISMATCH:
            return r
    return None


def _dedupe(offers: list[SupplierOffer]) -> list[SupplierOffer]:
    best: dict[tuple, SupplierOffer] = {}
    rank = {EXACT: 0, STRONG: 1, UNCERTAIN: 2, MISMATCH: 3}
    for o in offers:
        k = (o.retailer.lower().split(" (")[0], round(o.price, 2))
        cur = best.get(k)
        if cur is None or rank[o.match] < rank[cur.match] or \
                (rank[o.match] == rank[cur.match] and cur.availability == "Unknown"):
            if cur and cur.availability != "Unknown" and o.availability == "Unknown":
                o.availability = cur.availability
            best[k] = o
    return list(best.values())
