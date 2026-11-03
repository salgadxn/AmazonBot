"""Supplier discovery and verification. Legitimate sources only — no direct
scraping of store websites.

Order, cheapest first:
  FREE      your price lists (exact UPC) and Best Buy's API (exact UPC, stock)
  DISCOVER  Google Shopping by UPC  — Serper first, SerpApi if Serper is out
            Google Shopping by brand+title if the UPC finds nothing
            Tavily page search on trusted store sites as a last resort
  VERIFY    only for offers that look profitable but aren't confirmed yet:
            SerpApi Walmart UPC lookup  (UPC + price + stock + who sells it)
            SerpApi Google store comparison (direct store links + stock text)
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .matching import EXACT, MISMATCH, STRONG, UNCERTAIN, compare
from .providers import parse_money

log = logging.getLogger(__name__)

RANK = {EXACT: 0, STRONG: 1, UNCERTAIN: 2, MISMATCH: 3}


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
    seller_note: str = ""
    third_party: bool = False
    match: str = UNCERTAIN
    match_reason: str = ""
    pack_multiplier: int = 1
    found_by_upc: bool = False       # result came from searching the exact UPC
    price_verified: bool = True      # False = price read from page text (Tavily)
    min_order_units: int = 1         # from price lists (case pack / MOQ)

    @property
    def trusted(self) -> bool:
        return bool(self.retailer_key)

    @property
    def unit_cost(self) -> float:
        """Price to make ONE Amazon unit (handles 'buy 2 singles for a 2-pack')."""
        return self.price * self.pack_multiplier

    @property
    def direct_link(self) -> bool:
        host = urlparse(self.link or "").netloc.lower()
        return bool(host) and "google." not in host

    @property
    def upc_verified(self) -> bool:
        return self.match == EXACT or (self.match == STRONG and self.found_by_upc)


@dataclass
class SourcingResult:
    offers: list[SupplierOffer] = field(default_factory=list)
    status: str = ""
    tried: list[str] = field(default_factory=list)


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


def availability_from(texts: list) -> str:
    joined = " ".join(t.lower() for t in texts if isinstance(t, str))
    if "out of stock" in joined or "sold out" in joined or "unavailable" in joined:
        return "Out of stock"
    if "in stock" in joined or "in-stock" in joined:
        return "In stock"
    return "Unknown"


def shipping_from(store: dict) -> float | None:
    for key in ("shipping_extracted", "extracted_shipping"):
        if store.get(key) is not None:
            try:
                return float(store[key])
            except (TypeError, ValueError):
                pass
    texts = [store.get("shipping", ""), store.get("delivery", "")] + list(store.get("details_and_offers") or [])
    joined = " ".join(t.lower() for t in texts if isinstance(t, str))
    if "free" in joined and ("deliver" in joined or "shipping" in joined):
        return 0.0
    m = re.search(r"\+\s*\$(\d+(?:\.\d+)?)", joined)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- price lists

ALIASES = {
    "upc": ["upc", "upc code", "gtin", "ean", "barcode", "item upc", "unit upc", "upc/ean", "gtin-12", "gtin12"],
    "cost": ["cost", "unit cost", "unit price", "price", "wholesale", "wholesale price", "each cost",
             "cost per unit", "net cost", "your cost"],
    "case_cost": ["case cost", "case price", "cost per case", "carton cost"],
    "case_pack": ["case pack", "pack", "casepack", "units per case", "case qty", "inner pack", "qty per case"],
    "moq": ["moq", "min qty", "minimum", "minimum order", "min order qty"],
    "supplier": ["supplier", "vendor", "distributor", "source"],
    "name": ["name", "description", "item description", "product", "product name", "title", "item"],
    "link": ["link", "url", "product url"],
    "in_stock": ["in stock", "in_stock", "stock", "availability", "available", "qty available", "on hand"],
}


def _canon(header: str) -> str:
    h = re.sub(r"[_\s]+", " ", (header or "").strip().lower())
    for canon, names in ALIASES.items():
        if h in names:
            return canon
    return h


def _read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            log.warning("openpyxl not installed; skipping %s", path.name)
            return []
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        head = [str(h or "") for h in rows[0]]
        return [{head[i]: ("" if v is None else str(v)) for i, v in enumerate(r) if i < len(head)}
                for r in rows[1:]]
    return []


def _num(v) -> float | None:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def load_price_lists(folder: Path) -> dict[str, dict]:
    """Reads every .csv / .xlsx in data/price_lists/. Understands common header
    names (UPC/GTIN/EAN, Unit Cost/Price, Case Cost + Case Pack, MOQ, Vendor...).
    Returns {normalized_upc: row}, keeping the cheapest unit cost per UPC."""
    out: dict[str, dict] = {}
    if not folder.exists():
        return out
    for path in sorted(folder.iterdir()):
        for raw in _read_rows(path):
            row = {_canon(k): (str(v).strip() if v is not None else "") for k, v in raw.items() if k}
            upc = normalize_upc(row.get("upc", ""))
            cost = _num(row.get("cost"))
            pack = int(_num(row.get("case_pack")) or 0) or 1
            if cost is None and _num(row.get("case_cost")):
                cost = _num(row.get("case_cost")) / pack
            if not upc or not cost or cost <= 0:
                continue
            row["cost"] = round(cost, 4)
            row["moq"] = int(_num(row.get("moq")) or pack or 1)
            row.setdefault("supplier", path.stem)
            row["supplier"] = row.get("supplier") or path.stem
            row["file_date"] = dt.date.fromtimestamp(path.stat().st_mtime).isoformat()
            if upc not in out or cost < out[upc]["cost"]:
                out[upc] = row
    return out


def price_list_offer(upcs: list[str], price_list: dict) -> SupplierOffer | None:
    for upc in upcs:
        row = price_list.get(normalize_upc(upc))
        if not row:
            continue
        stock = str(row.get("in_stock", "")).lower()
        qty = _num(stock)
        availability = ("In stock" if stock in ("yes", "true", "y", "in stock") or (qty and qty > 0) else
                        "Out of stock" if stock in ("no", "false", "n", "0", "out of stock") else "Unknown")
        return SupplierOffer(
            retailer=row.get("supplier", "price list"), price=row["cost"], link=row.get("link", ""),
            title=row.get("name", ""), source="Your price list", checked=row["file_date"],
            retailer_key="price list", availability=availability, match=EXACT,
            match_reason="UPC in your price list", found_by_upc=True,
            min_order_units=row.get("moq", 1))
    return None


# ---------------------------------------------------------------- converters

def offers_from_results(results: list[dict], fetched: str, retailers: dict, cfg: dict,
                        source: str, found_by_upc: bool) -> list[SupplierOffer]:
    offers = []
    for r in results or []:
        title, src, price = r.get("title", ""), r.get("source", ""), r.get("price")
        if r.get("used") or re.search(r"\b(used|refurbished|pre-owned|open box)\b", title, re.I):
            continue
        if price is None or blocked(src, cfg):
            continue
        key, _ = find_retailer(src, retailers)
        retailer, third_party, note = src, False, ""
        m = re.match(r"^(.+?)\s+-\s+(.+)$", src)
        if key and m and find_retailer(m.group(1), retailers)[0] == key:
            # Google shows marketplace sellers as "Walmart - Some Seller LLC"
            retailer, third_party, note = f"{key} (marketplace: {m.group(2)})", True, f"sold by {m.group(2)}"
        offers.append(SupplierOffer(
            retailer=retailer, price=float(price), link=r.get("link", ""), title=title, source=source,
            checked=fetched, retailer_key=key, shipping=shipping_from(r),
            availability=availability_from([r.get("delivery", "")] + list(r.get("extensions") or [])),
            found_by_upc=found_by_upc, third_party=third_party, seller_note=note))
    return offers


def offers_from_stores(data: dict, fetched: str, retailers: dict, cfg: dict,
                       found_by_upc: bool) -> list[SupplierOffer]:
    pr = (data or {}).get("product_results", {}) or {}
    product_title = pr.get("title", "")
    offers = []
    for s in pr.get("stores", []) or []:
        name, price = s.get("name", ""), s.get("extracted_price")
        if price is None or blocked(name, cfg):
            continue
        key, _ = find_retailer(name, retailers)
        offers.append(SupplierOffer(
            retailer=name, price=float(price), link=s.get("link", ""),
            title=s.get("title") or product_title, source="Google store comparison (SerpApi)",
            checked=fetched, retailer_key=key, shipping=shipping_from(s),
            availability=availability_from(list(s.get("details_and_offers") or []) + [s.get("tag") or ""]),
            found_by_upc=found_by_upc))
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
        price = parse_money(v)
        if price is not None:
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
    availability = ("In stock" if stock is True else "Out of stock" if stock is False else
                    availability_from([str(p.get("availability_status", ""))]))
    item_id = p.get("us_item_id") or p.get("product_id") or ""
    link = p.get("product_page_url") or (f"https://www.walmart.com/ip/{item_id}" if item_id else "")
    key, _ = find_retailer("walmart", retailers)
    offer = SupplierOffer(
        retailer="Walmart" + (f" (marketplace: {seller})" if third_party else ""),
        price=price, link=link, title=p.get("title", ""), source="Walmart UPC lookup (SerpApi)",
        checked=fetched, retailer_key=key, availability=availability,
        seller_note=f"sold by {seller}" if seller else "", third_party=third_party, found_by_upc=True)
    returned = normalize_upc(p.get("upc", ""))
    if returned and returned == normalize_upc(upc):
        offer.match, offer.match_reason = EXACT, "Walmart listing has the same UPC"
    return offer


def offer_from_bestbuy(p: dict, fetched: str, retailers: dict) -> SupplierOffer | None:
    price = parse_money(p.get("salePrice") or p.get("regularPrice"))
    if price is None:
        return None
    key, _ = find_retailer("best buy", retailers)
    online = p.get("onlineAvailability")
    return SupplierOffer(
        retailer="Best Buy", price=price, link=p.get("url", ""), title=p.get("name", ""),
        source="Best Buy API", checked=fetched, retailer_key=key,
        availability="In stock" if online else "Out of stock" if online is False else "Unknown",
        match=EXACT, match_reason="Best Buy UPC lookup", found_by_upc=True)


PRICE_RE = re.compile(r"\$\s?(\d{1,4}(?:\.\d{2}))")


def offers_from_tavily(results: list[dict], fetched: str, retailers: dict, cfg: dict) -> list[SupplierOffer]:
    offers = []
    for r in results or []:
        url = r.get("url", "")
        host = urlparse(url).netloc.lower()
        m = PRICE_RE.search(r.get("content", "") or "")
        if not m or blocked(host, cfg):
            continue
        key, _ = find_retailer(host, retailers)
        offers.append(SupplierOffer(
            retailer=key or host, price=float(m.group(1)), link=url, title=r.get("title", ""),
            source="Tavily page search", checked=fetched, retailer_key=key,
            price_verified=False))
    return offers


# ---------------------------------------------------------------- grading

def grade(offers: list[SupplierOffer], item) -> None:
    for o in offers:
        if o.match == EXACT:
            continue
        m = compare(item.title, item.brand, o.title)
        o.match, o.match_reason, o.pack_multiplier = m.level, m.reason, m.pack_multiplier
        if o.found_by_upc and m.level == STRONG:
            o.match_reason = "found by UPC search; " + o.match_reason


def dedupe(offers: list[SupplierOffer]) -> list[SupplierOffer]:
    """One offer per store+price, keeping the best-verified version of it."""
    best: dict[tuple, SupplierOffer] = {}

    def quality(o):
        return (RANK[o.match], not o.found_by_upc, o.availability == "Unknown", not o.direct_link,
                not o.price_verified)

    for o in offers:
        k = (o.retailer.lower().split(" (")[0], round(o.price, 2))
        if k not in best or quality(o) < quality(best[k]):
            best[k] = o
    out = list(best.values())
    out.sort(key=lambda o: (RANK[o.match], o.availability == "Out of stock", not o.trusted,
                            o.unit_cost + (o.shipping or 0)))
    return out


# ---------------------------------------------------------------- discovery

def free_offers(item, price_list: dict, bby_hits: dict, retailers: dict) -> list[SupplierOffer]:
    offers = []
    pl = price_list_offer(item.upcs, price_list)
    if pl:
        offers.append(pl)
    for u in item.upcs:
        hit = bby_hits.get(upc12(u))
        if hit:
            o = offer_from_bestbuy(hit[0], hit[1], retailers)
            if o:
                offers.append(o)
            break
    return offers


def google_shopping(query: str, providers: dict, cfg: dict, for_discovery: bool = True):
    """Try Serper, then SerpApi (SerpApi keeps most of its budget for verification).
    Returns (results, fetched, provider_name) or (None, "", reason)."""
    days = int(cfg["suppliers"]["cache_days"])
    serper, serp = providers.get("serper"), providers.get("serpapi")
    # free cache hits from either provider first
    for p, prefix in ((serper, "serper-gs:"), (serp, "serpapi-gs:")):
        if p and p.store.cached_serp(prefix + query, days) is not None:
            res, fetched = p.shopping(query, days)
            return res, fetched, p.name + " (cache)"
    if serper and serper.active:
        res, fetched = serper.shopping(query, days)
        if res is not None:
            return res, fetched, "Serper"
    if serp and serp.active:
        share = float(cfg["suppliers"].get("serpapi_discovery_share", 0.4))
        if not for_discovery or serp.calls < max(1, int(serp.budget * share)):
            res, fetched = serp.shopping(query, days)
            if res is not None:
                return res, fetched, "SerpApi"
    reasons = [f"{p.name}: {p.disabled_reason or 'run budget used'}" for p in (serper, serp) if p]
    return None, "", "; ".join(reasons) or "no Google Shopping provider set up"


TAVILY_DOMAINS = ["walmart.com", "target.com", "cvs.com", "walgreens.com", "vitacost.com", "iherb.com",
                  "homedepot.com", "lowes.com", "kohls.com", "ulta.com", "michaels.com", "samsclub.com",
                  "costco.com", "kroger.com", "meijer.com", "staples.com", "officedepot.com", "petsmart.com",
                  "petco.com", "chewy.com", "bestbuy.com", "riteaid.com", "gnc.com", "vitaminshoppe.com"]


def discover(item, providers: dict, retailers: dict, cfg: dict) -> SourcingResult:
    res = SourcingResult()
    upc = upc12(item.upcs[0]) if item.upcs else ""
    found_any = False
    why_not: list[str] = []
    if upc:
        results, fetched, who = google_shopping(upc, providers, cfg)
        if results is not None:
            res.tried.append(f"Google Shopping by UPC via {who}")
            offers = offers_from_results(results, fetched, retailers, cfg, f"Google Shopping ({who})", True)
            res.offers += offers
            found_any = bool(offers)
        else:
            why_not.append(who)
    if not found_any and cfg["suppliers"].get("title_search_fallback", True) and not why_not:
        q = " ".join(f"{item.brand} {item.title}".split()[:12])
        results, fetched, who = google_shopping(q, providers, cfg)
        if results is not None:
            res.tried.append(f"Google Shopping by title via {who}")
            offers = offers_from_results(results, fetched, retailers, cfg, f"Google Shopping ({who})", False)
            res.offers += offers
            found_any = bool(offers)
        else:
            why_not.append(who)
    tav = providers.get("tavily")
    if not found_any and tav and tav.active:
        q = f"{item.brand} {item.title} {upc}".strip()
        results, fetched = tav.search(" ".join(q.split()[:16]), TAVILY_DOMAINS,
                                      int(cfg["suppliers"]["cache_days"]))
        if results is not None:
            res.tried.append("Tavily page search")
            res.offers += offers_from_tavily(results, fetched, retailers, cfg)
    grade(res.offers, item)
    if not res.offers:
        if res.tried:
            res.status = f"No store offers found ({'; '.join(res.tried)})" + ("" if upc else "; listing has no UPC")
        else:
            res.status = "Not searched: " + ("; ".join(why_not) or "no search provider available")
    return res


# ---------------------------------------------------------------- verification

def verify(item, offers: list[SupplierOffer], providers: dict, retailers: dict, cfg: dict) -> list[SupplierOffer]:
    """Spend SerpApi only on products that already look profitable, to confirm UPC,
    price, stock and get direct store links. Returns new offers to merge."""
    serp = providers.get("serpapi")
    if not serp or not serp.active or not item.upcs:
        return []
    upc = upc12(item.upcs[0])
    days = int(cfg["suppliers"]["cache_days"])
    new: list[SupplierOffer] = []
    if any(o.retailer_key == "Walmart" and o.match != EXACT for o in offers):
        data, fetched = serp.walmart_upc(upc, days)
        wm = offer_from_walmart(data, fetched, upc, retailers) if data else None
        if wm:
            new.append(wm)
    needs_store_check = any(o.retailer_key != "Walmart" and (o.availability == "Unknown" or not o.direct_link
                                                             or not o.price_verified) for o in offers)
    if needs_store_check and serp.active:
        results, fetched = serp.shopping(upc, days)
        token = ""
        for r in results or []:
            if r.get("token") and compare(item.title, item.brand, r.get("title", "")).level != MISMATCH:
                token = r["token"]
                break
        if token and serp.active:
            data, fetched = serp.compare_stores(token, days)
            if data:
                new += offers_from_stores(data, fetched, retailers, cfg, found_by_upc=True)
    grade(new, item)
    return new
