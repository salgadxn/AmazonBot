import datetime as dt

from bot.config import load_config, load_retailers
from bot.matching import compare
from bot.profit import ProductCosts, max_buy_price
from bot.sourcing import Candidate, decide
from bot.spapi import CatalogItem, Fees, Offers
from bot.suppliers import (SerpApi, SourcingResult, SupplierOffer, offer_from_walmart,
                           offers_from_shopping, offers_from_stores, source_product)
from bot.store import Store

CFG = load_config()
RET = load_retailers()
TODAY = dt.date.today().isoformat()


def test_matching_levels():
    assert compare("Great Value Oats, 42 oz", "Great Value", "Great Value Old Fashioned Oats 42 oz").level == "Strong"
    assert compare("Great Value Oats, 42 oz", "Great Value", "Great Value Oats 18 oz").level == "Mismatch"
    m = compare("Great Value Oats, 42 oz (Pack of 3)", "Great Value", "Great Value Oats 42 oz")
    assert m.level == "Strong" and m.pack_multiplier == 3
    assert compare("CVS A1C Test Kit", "CVS Health", "CVS Health A1C kit").level == "Uncertain"
    assert compare("x", "", "y", upc_confirmed=True).level == "Exact"


def candidate(title="Great Value Oats, 42 oz", sell=24.99):
    it = CatalogItem(asin="B0TEST0001", title=title, brand="Great Value", category="Home & Kitchen",
                     rank=2000, upcs=["078742000001"], weight_lb=1, length_in=6, width_in=4, height_in=3)
    o = Offers(asin=it.asin, buybox_price=sell, buybox_is_fba=True, fba_offers=3, fbm_offers=1, total_offers=4)
    f = Fees(asin=it.asin, price=sell, referral=sell * 0.15, fba=4.2)
    pc = ProductCosts(sell, f.referral, f.fba, 0, it.category, 1, 6, 4, 3)
    mb = max_buy_price(pc, CFG, 3, 0.30)
    return Candidate(it=it, o=o, f=f, pc=pc, est=300, flags=[], max_buy=mb, min_profit=3,
                     min_roi=0.30, rule="Starter")


def offer(**kw):
    base = dict(retailer="Walmart", price=6.0, link="https://walmart.com/ip/1", title="Great Value Oats 42 oz",
                source="Google Shopping", checked=TODAY, retailer_key="Walmart", availability="In stock",
                match="Strong", match_reason="brand + size")
    base.update(kw)
    return SupplierOffer(**base)


def test_decide_lead_review_and_none():
    c = candidate()
    lead, rev, opts, none = decide(c, SourcingResult([offer(), offer(retailer="Target", retailer_key="Target", price=7)]),
                                   RET, CFG, "now")
    assert lead and lead["Retailer"] == "Walmart" and lead["Buy link"].startswith("https://")
    assert "Target" in lead["Other suppliers"] and len(opts) == 2

    lead, rev, _, _ = decide(c, SourcingResult([offer(retailer="Joe's Deals", retailer_key="")]), RET, CFG, "now")
    assert lead is None and "trusted" in rev["Why not a Lead"]

    lead, rev, _, _ = decide(c, SourcingResult([offer(match="Uncertain", match_reason="no size")]), RET, CFG, "now")
    assert lead is None and "Uncertain" in rev["Why not a Lead"]

    lead, rev, _, none = decide(c, SourcingResult([offer(price=20)]), RET, CFG, "now")
    assert none and none["Why no lead"].startswith("Too expensive")

    lead, rev, _, none = decide(c, SourcingResult([], status="No store offers found (Google Shopping (UPC))"), RET, CFG, "now")
    assert none["Why no lead"].startswith("No store offers")

    lead, rev, _, none = decide(c, SourcingResult([offer(availability="Out of stock")]), RET, CFG, "now")
    assert none and "out of stock" in none["Why no lead"]


def test_parsers():
    data = {"shopping_results": [
        {"title": "Great Value Oats 42 oz", "source": "Walmart", "extracted_price": 5.5,
         "product_link": "https://g/1", "delivery": "Free delivery", "immersive_product_page_token": "tok"},
        {"title": "Oats used", "source": "eBay", "extracted_price": 1}]}
    offers, raw = offers_from_shopping(data, TODAY, RET, CFG)
    assert len(offers) == 1 and offers[0].shipping == 0 and offers[0].retailer_key == "Walmart"
    stores = {"product_results": {"title": "Great Value Oats 42 oz", "stores": [
        {"name": "Target", "link": "https://target.com/p", "extracted_price": 5.79,
         "details_and_offers": ["In stock online", "Free delivery"]},
        {"name": "Kroger", "link": "https://kroger.com/p", "extracted_price": 5.2,
         "shipping": "+ $6.99", "details_and_offers": ["Out of stock"]}]}}
    s = offers_from_stores(stores, TODAY, RET, CFG)
    assert s[0].availability == "In stock" and s[0].shipping == 0
    assert s[1].availability == "Out of stock" and s[1].shipping == 6.99
    wm = offer_from_walmart({"product_result": {"title": "GV Oats", "price_map": {"price": 5.48},
                                                "upc": "078742000001", "in_stock": True,
                                                "us_item_id": "123", "seller_name": "Walmart.com"}},
                            TODAY, "078742000001", RET)
    assert wm.match == "Exact" and wm.price == 5.48 and not wm.third_party
    wm3 = offer_from_walmart({"product_result": {"title": "GV Oats", "price_map": {"price": 4.0},
                                                 "in_stock": True, "us_item_id": "9", "seller_name": "Deals LLC"}},
                             TODAY, "078742000001", RET)
    assert wm3.third_party and "marketplace" in wm3.retailer


class FakeHTTP:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        engine = params.get("engine", "account")
        return type("R", (), {"status_code": 200, "json": lambda s: self.responses[engine]})()


def test_source_product_end_to_end(tmp_path):
    http = FakeHTTP({
        "account": {"total_searches_left": 900},
        "google_shopping": {"shopping_results": [
            {"title": "Great Value Oats, 42 oz", "source": "Walmart", "extracted_price": 5.5,
             "product_link": "https://g/1", "immersive_product_page_token": "tok"}]},
        "google_immersive_product": {"product_results": {"title": "Great Value Oats 42 oz", "stores": [
            {"name": "Target", "link": "https://target.com/p", "extracted_price": 5.79,
             "details_and_offers": ["In stock online"]}]}},
        "walmart_product": {"product_result": {"title": "Great Value Oats 42 oz", "price_map": {"price": 5.48},
                                               "upc": "078742000001", "in_stock": True, "us_item_id": "1"}},
    })
    store = Store(tmp_path / "t.db")
    serp = SerpApi("key", CFG, store, http=http)
    assert serp.active and serp.budget > 0
    res = source_product(candidate().it, {}, serp, RET, CFG)
    names = [o.retailer for o in res.offers]
    assert "Target" in names and any(o.match == "Exact" for o in res.offers)
    assert serp.calls == 3
    # second run uses cache (no new searches)
    serp2 = SerpApi("key", CFG, store, http=http)
    source_product(candidate().it, {}, serp2, RET, CFG)
    assert serp2.calls == 0 and serp2.cache_hits == 3


def test_no_key_explains(tmp_path):
    serp = SerpApi("", CFG, Store(tmp_path / "t.db"))
    res = source_product(candidate().it, {}, serp, RET, CFG)
    assert not res.offers and "SERPAPI_KEY" in res.status
