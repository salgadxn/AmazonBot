import datetime as dt
from pathlib import Path

from bot.config import load_config, load_retailers
from bot.matching import compare
from bot.profit import ProductCosts, max_buy_price
from bot.providers import BestBuyProvider, SerpApiProvider, SerperProvider, TavilyProvider
from bot.sourcing import Candidate, decide, evaluate_offer, is_lead, needs_verification
from bot.spapi import CatalogItem, Fees, Offers
from bot.store import Store
from bot.suppliers import (SupplierOffer, dedupe, discover, free_offers, load_price_lists,
                           offer_from_walmart, offers_from_stores, verify)

CFG = load_config()
RET = load_retailers()
TODAY = dt.date.today().isoformat()


def test_matching_levels():
    assert compare("Great Value Oats, 42 oz", "Great Value", "Great Value Old Fashioned Oats 42 oz").level == "Strong"
    assert compare("Great Value Oats, 42 oz", "Great Value", "Great Value Oats 18 oz").level == "Mismatch"
    m = compare("Great Value Oats, 42 oz (Pack of 3)", "Great Value", "Great Value Oats 42 oz")
    assert m.level == "Strong" and m.pack_multiplier == 3
    assert compare("CVS A1C Test Kit", "CVS Health", "CVS Health A1C kit").level == "Uncertain"


def candidate(title="Great Value Oats, 42 oz", sell=24.99, upc="078742000001"):
    it = CatalogItem(asin="B0TEST0001", title=title, brand="Great Value", category="Home & Kitchen",
                     rank=2000, upcs=[upc], weight_lb=1, length_in=6, width_in=4, height_in=3)
    o = Offers(asin=it.asin, buybox_price=sell, buybox_is_fba=True, fba_offers=3, fbm_offers=1, total_offers=4)
    f = Fees(asin=it.asin, price=sell, referral=sell * 0.15, fba=4.2)
    pc = ProductCosts(sell, f.referral, f.fba, 0, it.category, 1, 6, 4, 3)
    return Candidate(it=it, o=o, f=f, pc=pc, est=300, flags=[], max_buy=max_buy_price(pc, CFG, 3, 0.30),
                     min_profit=3, min_roi=0.30, rule="Starter")


def offer(**kw):
    base = dict(retailer="Walmart", price=6.0, link="https://www.walmart.com/ip/1", title="Great Value Oats 42 oz",
                source="test", checked=TODAY, retailer_key="Walmart", availability="In stock",
                match="Exact", match_reason="UPC", found_by_upc=True)
    base.update(kw)
    return SupplierOffer(**base)


def test_lead_requires_full_verification():
    c = candidate()
    lead, rev, opts, none, outcome = decide(c, [offer(), offer(retailer="Target", retailer_key="Target", price=7)],
                                            "", RET, CFG, "now")
    assert outcome == "lead" and lead["Buy link"].startswith("https://www.walmart")
    cases = {
        "UPC not verified": offer(match="Strong", found_by_upc=False),
        "stock not confirmed": offer(availability="Unknown"),
        "no direct store link": offer(link="https://www.google.com/shopping/product/1"),
        "trusted retailers": offer(retailer="Joe's Deals", retailer_key=""),
        "page text": offer(price_verified=False),
        "uncertain match": offer(match="Uncertain", match_reason="no size"),
        "marketplace": offer(third_party=True, seller_note="sold by X"),
    }
    for expect, o in cases.items():
        lead, rev, _, _, outcome = decide(c, [o], "", RET, CFG, "now")
        assert lead is None and expect in rev["Why not a Lead"], expect


def test_no_lead_reasons():
    c = candidate()
    *_, none, outcome = decide(c, [offer(price=20)], "", RET, CFG, "now")
    assert outcome == "too_expensive" and none["Why no lead"].startswith("Too expensive")
    *_, none, outcome = decide(c, [], "No store offers found (x)", RET, CFG, "now")
    assert outcome == "no_offers" and none["Why no lead"].startswith("No store offers")
    *_, none, _ = decide(c, [offer(availability="Out of stock")], "", RET, CFG, "now")
    assert "out of stock" in none["Why no lead"]


def test_needs_verification():
    c = candidate()
    ev = [evaluate_offer(c, offer(match="Strong", found_by_upc=True, availability="Unknown",
                                  link="https://google.com/x"), RET, CFG)]
    assert not is_lead(ev) and needs_verification(ev)
    ev = [evaluate_offer(c, offer(retailer="Joe", retailer_key=""), RET, CFG)]
    assert not needs_verification(ev)


def test_parsers():
    stores = {"product_results": {"title": "Great Value Oats 42 oz", "stores": [
        {"name": "Target", "link": "https://target.com/p", "extracted_price": 5.79,
         "details_and_offers": ["In stock online", "Free delivery"]},
        {"name": "Kroger", "link": "https://kroger.com/p", "extracted_price": 5.2,
         "shipping": "+ $6.99", "details_and_offers": ["Out of stock"]}]}}
    s = offers_from_stores(stores, TODAY, RET, CFG, True)
    assert s[0].availability == "In stock" and s[0].shipping == 0 and s[0].found_by_upc
    assert s[1].availability == "Out of stock" and s[1].shipping == 6.99
    wm = offer_from_walmart({"product_result": {"title": "GV Oats", "price_map": {"price": 5.48},
                                                "upc": "078742000001", "in_stock": True,
                                                "us_item_id": "123", "seller_name": "Walmart.com"}},
                            TODAY, "078742000001", RET)
    assert wm.match == "Exact" and wm.price == 5.48 and not wm.third_party and wm.direct_link


def test_price_list_aliases(tmp_path: Path):
    (tmp_path / "kehe.csv").write_text(
        "Item UPC,Description,Case Cost,Case Pack,Vendor,Qty Available\n"
        "0078742000001,Great Value Oats 42 oz,48.00,12,KeHE,40\n")
    pl = load_price_lists(tmp_path)
    o = free_offers(candidate().it, pl, {}, RET)[0]
    assert o.price == 4.0 and o.min_order_units == 12 and o.availability == "In stock" and o.match == "Exact"


class R:
    def __init__(self, data, code=200):
        self.data, self.status_code, self.content = data, code, b"x"

    def json(self):
        return self.data


class FakeHTTP:
    def __init__(self):
        self.gets, self.posts = [], []

    def get(self, url, params=None, timeout=None):
        self.gets.append((url, params))
        if "account.json" in url:
            return R({"total_searches_left": 240, "searches_per_month": 250})
        if "bestbuy" in url:
            return R({"products": [{"upc": "078742000001", "name": "Great Value Oats 42 oz", "salePrice": 5.0,
                                    "onlineAvailability": True, "url": "https://www.bestbuy.com/site/1"}]})
        e = params.get("engine")
        if e == "google_shopping":
            return R({"shopping_results": [{"title": "Great Value Oats 42 oz", "source": "Target",
                                            "extracted_price": 5.6, "product_link": "https://google.com/p/1",
                                            "immersive_product_page_token": "tok"}]})
        if e == "google_immersive_product":
            return R({"product_results": {"title": "Great Value Oats 42 oz", "stores": [
                {"name": "Target", "link": "https://www.target.com/p/1", "extracted_price": 5.6,
                 "details_and_offers": ["In stock online"]}]}})
        if e == "walmart_product":
            return R({"product_result": {"title": "Great Value Oats 42 oz", "price_map": {"price": 5.48},
                                         "upc": "078742000001", "in_stock": True, "us_item_id": "1"}})
        return R({})

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json))
        if "serper" in url:
            return R({"shopping": [{"title": "Great Value Oats 42 oz", "source": "Walmart", "price": "$5.48",
                                    "link": "https://google.com/shopping/1"},
                                   {"title": "Great Value Oats 42 oz", "source": "Target", "price": "$5.60",
                                    "link": "https://google.com/shopping/2"}], "credits": 1})
        if "tavily" in url:
            return R({"results": [{"title": "Great Value Oats 42 oz - Walmart.com", "url": "https://www.walmart.com/ip/5",
                                   "content": "Now $5.97 Great Value oats"}]})
        return R({})


def providers(store, http, **keys):
    keys = {"serper": "k", "serpapi": "k", "tavily": "k", "bestbuy": "k", **keys}
    return {"serper": SerperProvider(keys["serper"], CFG, store, http),
            "serpapi": SerpApiProvider(keys["serpapi"], CFG, store, http),
            "tavily": TavilyProvider(keys["tavily"], CFG, store, http),
            "bestbuy": BestBuyProvider(keys["bestbuy"], CFG, store, http)}


def test_discover_then_verify_makes_lead(tmp_path):
    store, http = Store(tmp_path / "t.db"), FakeHTTP()
    p = providers(store, http)
    c = candidate()
    found = discover(c.it, p, RET, CFG)
    assert p["serper"].calls == 1 and p["serpapi"].calls == 0       # Serper used first
    evals = [evaluate_offer(c, o, RET, CFG) for o in found.offers]
    assert not is_lead(evals) and needs_verification(evals)          # Google links, stock unknown
    extra = verify(c.it, found.offers, p, RET, CFG)
    assert p["serpapi"].calls == 3                                   # Walmart UPC + shopping + store compare
    offers = dedupe(found.offers + extra)
    lead, *_ , outcome = decide(c, offers, "", RET, CFG, "now")
    assert outcome == "lead" and lead["Buy link"].startswith("https://www.")
    # usage persisted
    assert store.usage_get("Serper", "lifetime") == 1
    # second time: everything from cache
    p2 = providers(store, http)
    discover(c.it, p2, RET, CFG)
    assert p2["serper"].calls == 0


def test_failover_when_serper_missing(tmp_path):
    store, http = Store(tmp_path / "t.db"), FakeHTTP()
    p = providers(store, http, serper="")
    discover(candidate().it, p, RET, CFG)
    assert p["serper"].disabled_reason and p["serpapi"].calls == 1


def test_tavily_last_resort_and_bestbuy(tmp_path):
    store, http = Store(tmp_path / "t.db"), FakeHTTP()
    p = providers(store, http, serper="", serpapi="")
    found = discover(candidate().it, p, RET, CFG)
    assert found.offers and not found.offers[0].price_verified
    hits = p["bestbuy"].lookup_upcs(["078742000001", "000000000000"], 2)
    assert "078742000001" in hits and p["bestbuy"].calls == 1
    o = free_offers(candidate().it, {}, hits, RET)[0]
    assert o.retailer == "Best Buy" and o.match == "Exact" and o.availability == "In stock"


def test_no_keys_explains(tmp_path):
    p = providers(Store(tmp_path / "t.db"), FakeHTTP(), serper="", serpapi="", tavily="", bestbuy="")
    found = discover(candidate().it, p, RET, CFG)
    assert not found.offers and "SERPER" in found.status.upper()
