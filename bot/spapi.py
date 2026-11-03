"""Small Amazon Selling Partner API client (private, self-authorized app).

Uses only the operations the bot needs:
  - Catalog Items 2022-04-01  searchCatalogItems
  - Listings Restrictions     getListingsRestrictions
  - Product Pricing v0        getItemOffersBatch
  - Product Fees v0           getMyFeesEstimates
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

LWA_URL = "https://api.amazon.com/auth/o2/token"
ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
MARKETPLACE_ID = "ATVPDKIKX0DER"       # Amazon.com
AMAZON_RETAIL_SELLER_ID = "ATVPDKIKX0DER"  # Amazon itself as a seller on .com
USER_AGENT = "AmazonBot/1.0 (Language=Python)"

# Seconds between calls per operation (slightly slower than Amazon's limits).
MIN_INTERVAL = {
    "search": 0.55,        # 2 req/s
    "restrictions": 0.22,  # 5 req/s
    "offers_batch": 10.5,  # 0.1 req/s, 20 ASINs each
    "fees_batch": 2.1,     # 0.5 req/s, 20 ASINs each
}


class SPAPIError(Exception):
    pass


class SPAPIAuthError(SPAPIError):
    pass


@dataclass
class CatalogItem:
    asin: str
    title: str = ""
    brand: str = ""
    category: str = ""
    rank: int | None = None
    upcs: list[str] = field(default_factory=list)
    is_variation_parent: bool = False
    length_in: float | None = None
    width_in: float | None = None
    height_in: float | None = None
    weight_lb: float | None = None

    @property
    def amazon_url(self) -> str:
        return f"https://www.amazon.com/dp/{self.asin}"


@dataclass
class Offers:
    asin: str
    buybox_price: float | None = None
    buybox_is_fba: bool | None = None
    lowest_fba_price: float | None = None
    fba_offers: int = 0
    fbm_offers: int = 0
    total_offers: int = 0
    amazon_on_listing: bool = False
    error: str = ""

    @property
    def sell_price(self) -> float | None:
        """Price we expect to sell at as an FBA seller."""
        return self.buybox_price or self.lowest_fba_price


@dataclass
class Fees:
    asin: str
    price: float
    referral: float = 0.0
    fba: float = 0.0
    closing: float = 0.0
    total: float = 0.0
    error: str = ""


@dataclass
class Eligibility:
    asin: str
    status: str            # Eligible / Needs approval / Restricted / Unknown
    message: str = ""
    link: str = ""


# ---------------------------------------------------------------- helpers

def _to_inches(dim: dict | None) -> float | None:
    if not dim or dim.get("value") is None:
        return None
    value, unit = float(dim["value"]), str(dim.get("unit", "inches")).lower()
    if unit.startswith("centim"):
        return value / 2.54
    if unit.startswith("millim"):
        return value / 25.4
    if unit.startswith("meter"):
        return value * 39.37
    if unit.startswith("feet") or unit == "foot":
        return value * 12
    return value


def _to_pounds(w: dict | None) -> float | None:
    if not w or w.get("value") is None:
        return None
    value, unit = float(w["value"]), str(w.get("unit", "pounds")).lower()
    if unit.startswith("ounce"):
        return value / 16
    if unit.startswith("kilogram"):
        return value * 2.20462
    if unit.startswith("gram"):
        return value / 453.592
    return value


def _for_marketplace(entries: list | None) -> dict:
    for entry in entries or []:
        if entry.get("marketplaceId") == MARKETPLACE_ID:
            return entry
    return (entries or [{}])[0] if entries else {}


def parse_catalog_item(item: dict) -> CatalogItem:
    summary = _for_marketplace(item.get("summaries"))
    out = CatalogItem(asin=item.get("asin", ""))
    out.title = summary.get("itemName", "") or ""
    out.brand = summary.get("brand", "") or summary.get("brandName", "") or ""
    out.is_variation_parent = summary.get("itemClassification") == "VARIATION_PARENT"

    ids = _for_marketplace(item.get("identifiers")).get("identifiers", [])
    out.upcs = sorted({
        i.get("identifier", "") for i in ids
        if i.get("identifierType") in ("UPC", "EAN", "GTIN") and i.get("identifier")
    })

    ranks = _for_marketplace(item.get("salesRanks"))
    group = ranks.get("displayGroupRanks") or []
    if group:
        out.rank = group[0].get("rank")
        out.category = group[0].get("title", "")
    elif ranks.get("classificationRanks"):
        out.rank = ranks["classificationRanks"][0].get("rank")
        out.category = ranks["classificationRanks"][0].get("title", "")
    if not out.category:
        out.category = summary.get("websiteDisplayGroupName", "") or ""

    dims = _for_marketplace(item.get("dimensions"))
    pkg = dims.get("package") or dims.get("item") or {}
    out.length_in = _to_inches(pkg.get("length"))
    out.width_in = _to_inches(pkg.get("width"))
    out.height_in = _to_inches(pkg.get("height"))
    out.weight_lb = _to_pounds(pkg.get("weight"))
    return out


def parse_offers(asin: str, body: dict) -> Offers:
    payload = body.get("payload", body) or {}
    summary = payload.get("Summary", {}) or {}
    out = Offers(asin=asin)

    for bb in summary.get("BuyBoxPrices", []) or []:
        if str(bb.get("condition", "")).lower() == "new":
            landed = (bb.get("LandedPrice") or {}).get("Amount")
            listing = (bb.get("ListingPrice") or {}).get("Amount")
            out.buybox_price = float(landed if landed is not None else listing)
            break

    for n in summary.get("NumberOfOffers", []) or []:
        if str(n.get("condition", "")).lower() != "new":
            continue
        if n.get("fulfillmentChannel") == "Amazon":
            out.fba_offers += int(n.get("OfferCount", 0))
        else:
            out.fbm_offers += int(n.get("OfferCount", 0))
    out.total_offers = int(summary.get("TotalOfferCount", out.fba_offers + out.fbm_offers))

    fba_prices = []
    for offer in payload.get("Offers", []) or []:
        price = float((offer.get("ListingPrice") or {}).get("Amount", 0) or 0)
        price += float((offer.get("Shipping") or {}).get("Amount", 0) or 0)
        if offer.get("SellerId") == AMAZON_RETAIL_SELLER_ID:
            out.amazon_on_listing = True
        if offer.get("IsFulfilledByAmazon"):
            fba_prices.append(price)
        if offer.get("IsBuyBoxWinner"):
            out.buybox_is_fba = bool(offer.get("IsFulfilledByAmazon"))
    if fba_prices:
        out.lowest_fba_price = min(fba_prices)
    return out


def parse_fee_result(result: dict, price_by_asin: dict) -> Fees:
    ident = result.get("FeesEstimateIdentifier", {}) or {}
    asin = ident.get("IdValue") or ident.get("SellerInputIdentifier", "")
    fees = Fees(asin=asin, price=price_by_asin.get(asin, 0.0))
    if result.get("Status") != "Success":
        err = result.get("Error") or {}
        fees.error = err.get("Message") or result.get("Status", "fee error")
        return fees
    est = result.get("FeesEstimate", {}) or {}
    fees.total = float((est.get("TotalFeesEstimate") or {}).get("Amount", 0) or 0)
    for d in est.get("FeeDetailList", []) or []:
        amount = float((d.get("FinalFee") or {}).get("Amount", 0) or 0)
        ftype = d.get("FeeType", "")
        if ftype == "ReferralFee":
            fees.referral += amount
        elif ftype == "FBAFees":
            fees.fba += amount
        elif ftype in ("VariableClosingFee", "PerItemFee", "FixedClosingFee"):
            fees.closing += amount
    return fees


def parse_restrictions(asin: str, body: dict) -> Eligibility:
    restrictions = body.get("restrictions", []) or []
    if not restrictions:
        return Eligibility(asin, "Eligible")
    messages, link, approval = [], "", False
    for r in restrictions:
        for reason in r.get("reasons", []) or []:
            if reason.get("message"):
                messages.append(reason["message"])
            if reason.get("reasonCode") == "APPROVAL_REQUIRED":
                approval = True
            for lk in reason.get("links", []) or []:
                link = link or lk.get("resource", "")
    status = "Needs approval" if approval else "Restricted"
    return Eligibility(asin, status, " | ".join(dict.fromkeys(messages))[:300], link)


# ---------------------------------------------------------------- client

class SPAPI:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 seller_id: str, session: requests.Session | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.seller_id = seller_id
        self.http = session or requests.Session()
        self._token = ""
        self._token_expiry = 0.0
        self._last_call: dict[str, float] = {}

    # ---- auth
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = self.http.post(LWA_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }, timeout=30)
        if resp.status_code != 200:
            raise SPAPIAuthError(
                "Amazon login failed (LWA). Check SPAPI_LWA_CLIENT_ID, "
                "SPAPI_LWA_CLIENT_SECRET and SPAPI_REFRESH_TOKEN. If Seller Central "
                "says your client secret expired, generate a new one and update the "
                f"GitHub secret. Amazon said: {resp.text[:300]}")
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600))
        return self._token

    # ---- throttled request with retries
    def _wait(self, op: str) -> None:
        gap = MIN_INTERVAL.get(op, 1.0)
        elapsed = time.time() - self._last_call.get(op, 0)
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_call[op] = time.time()

    def _request(self, method: str, path: str, op: str, params=None, json=None,
                 max_retries: int = 6):
        for attempt in range(max_retries):
            self._wait(op)
            resp = self.http.request(
                method, ENDPOINT + path, params=params, json=json, timeout=60,
                headers={"x-amz-access-token": self._access_token(),
                         "user-agent": USER_AGENT,
                         "content-type": "application/json"})
            if resp.status_code in (429, 500, 502, 503, 504):
                delay = min(60, MIN_INTERVAL.get(op, 1.0) * (2 ** attempt) + 1)
                log.warning("%s throttled/unavailable (%s); retrying in %.0fs",
                            op, resp.status_code, delay)
                time.sleep(delay)
                continue
            if resp.status_code in (401, 403):
                raise SPAPIAuthError(
                    f"Amazon refused {op} ({resp.status_code}). Make sure your app "
                    "has the Product Listing and Pricing roles and was re-authorized "
                    f"after adding them. Amazon said: {resp.text[:300]}")
            if resp.status_code >= 400:
                raise SPAPIError(f"{op} failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()
        raise SPAPIError(f"{op} kept getting throttled; giving up for now")

    # ---- operations
    def search_catalog(self, keywords: str | None = None, brand: str | None = None,
                       identifiers: list[str] | None = None,
                       identifiers_type: str = "UPC",
                       page_token: str | None = None) -> tuple[list[CatalogItem], str | None]:
        params = {
            "marketplaceIds": MARKETPLACE_ID,
            "includedData": "summaries,identifiers,salesRanks,dimensions",
            "pageSize": 20,
        }
        if identifiers:
            params["identifiers"] = ",".join(identifiers[:20])
            params["identifiersType"] = identifiers_type
        else:
            params["keywords"] = keywords or brand or ""
            if brand:
                params["brandNames"] = brand
        if page_token:
            params["pageToken"] = page_token
        body = self._request("GET", "/catalog/2022-04-01/items", "search", params=params)
        items = [parse_catalog_item(i) for i in body.get("items", [])]
        next_token = (body.get("pagination") or {}).get("nextToken")
        return items, next_token

    def get_eligibility(self, asin: str) -> Eligibility:
        params = {"asin": asin, "sellerId": self.seller_id,
                  "marketplaceIds": MARKETPLACE_ID, "conditionType": "new_new"}
        try:
            body = self._request("GET", "/listings/2021-08-01/restrictions",
                                 "restrictions", params=params)
        except SPAPIAuthError:
            raise
        except SPAPIError as exc:
            return Eligibility(asin, "Unknown", str(exc)[:200])
        return parse_restrictions(asin, body)

    def get_offers(self, asins: list[str]) -> dict[str, Offers]:
        out: dict[str, Offers] = {}
        for start in range(0, len(asins), 20):
            chunk = asins[start:start + 20]
            body = self._request("POST", "/batches/products/pricing/v0/itemOffers",
                                 "offers_batch", json={"requests": [{
                                     "uri": f"/products/pricing/v0/items/{a}/offers",
                                     "method": "GET",
                                     "MarketplaceId": MARKETPLACE_ID,
                                     "ItemCondition": "New",
                                     "CustomerType": "Consumer",
                                 } for a in chunk]})
            for i, resp in enumerate(body.get("responses", [])):
                uri = (resp.get("request") or {}).get("uri", "")
                asin = uri.split("/items/")[-1].split("/")[0] if "/items/" in uri else chunk[i]
                status = (resp.get("status") or {}).get("statusCode", 200)
                if status != 200:
                    out[asin] = Offers(asin=asin, error=f"offers HTTP {status}")
                    continue
                out[asin] = parse_offers(asin, resp.get("body", {}))
        return out

    def get_fees(self, prices: dict[str, float]) -> dict[str, Fees]:
        out: dict[str, Fees] = {}
        items = [(a, p) for a, p in prices.items() if p]
        for start in range(0, len(items), 20):
            chunk = items[start:start + 20]
            body = self._request("POST", "/products/fees/v0/feesEstimate", "fees_batch",
                                 json=[{
                                     "IdType": "ASIN",
                                     "IdValue": a,
                                     "FeesEstimateRequest": {
                                         "MarketplaceId": MARKETPLACE_ID,
                                         "IsAmazonFulfilled": True,
                                         "Identifier": a,
                                         "PriceToEstimateFees": {
                                             "ListingPrice": {"CurrencyCode": "USD", "Amount": round(p, 2)},
                                             "Shipping": {"CurrencyCode": "USD", "Amount": 0},
                                         },
                                     },
                                 } for a, p in chunk])
            results = body if isinstance(body, list) else body.get("payload", body.get("FeesEstimateResult", []))
            for result in results or []:
                fees = parse_fee_result(result, dict(chunk))
                if fees.asin:
                    out[fees.asin] = fees
        return out
