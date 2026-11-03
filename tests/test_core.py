import datetime as dt
from pathlib import Path

import pytest

from bot import rules
from bot.config import load_config, load_retailers
from bot.profit import ProductCosts, calc_profit, max_buy_price
from bot.spapi import parse_catalog_item, parse_fee_result, parse_offers, parse_restrictions
from bot.store import Store
from bot.suppliers import best_serp_offer, load_price_lists, match_price_list

CFG = load_config()
RET = load_retailers()
JAN = dt.date(2027, 1, 15)


def product(**kw):
    base = dict(sell_price=25.0, referral_fee=3.75, fba_fee=4.50, category="Health & Household",
                weight_lb=1.0, length_in=6, width_in=4, height_in=2)
    base.update(kw)
    return ProductCosts(**base)


def test_profit_breakdown_adds_up():
    r = calc_profit(product(), 10.0, CFG, today=JAN)
    assert r.tax == pytest.approx(1.05)
    assert r.inbound == pytest.approx(1.60)
    assert r.prep_misc == pytest.approx(1.15)
    assert r.landed_cost == pytest.approx(10 + 1.05 + 1.60 + 1.15)
    assert r.returns == pytest.approx(0.50)
    expected = 25 - (3.75 + 4.50 + r.storage + 0.50) - r.landed_cost
    assert r.profit == pytest.approx(expected)
    assert r.roi == pytest.approx(r.profit / r.landed_cost)
    assert r.profit_at_drop < r.profit


def test_grocery_tax_and_peak_storage():
    g = calc_profit(product(category="Grocery & Gourmet Food"), 10.0, CFG, today=JAN)
    assert g.tax == pytest.approx(10 * CFG["costs"]["grocery_tax_rate"])
    nov = calc_profit(product(), 10.0, CFG, today=dt.date(2026, 11, 2))
    jan = calc_profit(product(), 10.0, CFG, today=JAN)
    assert nov.storage > jan.storage


def test_dimensional_weight_used_for_bulky_items():
    r = calc_profit(product(weight_lb=0.5, length_in=12, width_in=12, height_in=10), 10, CFG, today=JAN)
    assert r.inbound == pytest.approx(12 * 12 * 10 / 139 * 1.60)


def test_max_buy_price_hits_targets_exactly():
    p = product(sell_price=40, referral_fee=6.0, fba_fee=5.0)
    b = max_buy_price(p, CFG, 5.0, 0.35, today=JAN)
    r = calc_profit(p, b, CFG, today=JAN)
    assert r.profit >= 5.0 - 1e-6 and r.roi >= 0.35 - 1e-6
    assert r.profit == pytest.approx(5.0, abs=1e-6) or r.roi == pytest.approx(0.35, abs=1e-6)


def test_savings_do_not_stack_giftcard_and_store_card():
    retailer = dict(RET["retailers"]["Target"], giftcard_pct=0.08, store_card_pct=0.05,
                    store_card_owned=True, cashback_pct=0.02)
    r = calc_profit(product(), 10.0, CFG, retailer=retailer, today=JAN)
    paid = 10 + r.tax
    assert r.savings_all == pytest.approx(paid * 0.08 + 0.02 * 10)
    assert r.savings_certain == pytest.approx(paid * 0.05)   # only the owned store card


def test_tax_exempt_retailer():
    r = calc_profit(product(), 10.0, CFG, retailer={"tax_exempt": True}, today=JAN)
    assert r.tax == 0


def test_starter_mode_only_for_fast_sellers():
    assert rules.targets(150, CFG)[2] == "Starter"
    assert rules.targets(20, CFG)[2] == "Standard"


def test_suggested_qty_caps():
    assert rules.suggested_qty(300, 2, 10.0, CFG) == CFG["buying"]["max_units_first_buy"]
    assert rules.suggested_qty(30, 2, 10.0, CFG) == 10
    assert rules.suggested_qty(300, 0, 100.0, CFG) == 1


def test_parse_catalog_item():
    item = {"asin": "B000TEST01",
            "summaries": [{"marketplaceId": "ATVPDKIKX0DER", "itemName": "Great Value Oats",
                           "brand": "Great Value", "itemClassification": "BASE_PRODUCT"}],
            "identifiers": [{"marketplaceId": "ATVPDKIKX0DER", "identifiers": [
                {"identifierType": "UPC", "identifier": "078742000000"}]}],
            "salesRanks": [{"marketplaceId": "ATVPDKIKX0DER", "displayGroupRanks": [
                {"title": "Grocery & Gourmet Food", "rank": 1234}]}],
            "dimensions": [{"marketplaceId": "ATVPDKIKX0DER", "package": {
                "length": {"unit": "inches", "value": 8}, "width": {"unit": "inches", "value": 4},
                "height": {"unit": "inches", "value": 2},
                "weight": {"unit": "ounces", "value": 16}}}]}
    it = parse_catalog_item(item)
    assert (it.brand, it.rank, it.category, it.weight_lb) == ("Great Value", 1234, "Grocery & Gourmet Food", 1.0)
    assert it.upcs == ["078742000000"]


def test_parse_offers_detects_amazon_and_counts():
    body = {"payload": {"Summary": {
        "TotalOfferCount": 4,
        "NumberOfOffers": [{"condition": "new", "fulfillmentChannel": "Amazon", "OfferCount": 3},
                           {"condition": "new", "fulfillmentChannel": "Merchant", "OfferCount": 1}],
        "BuyBoxPrices": [{"condition": "New", "LandedPrice": {"Amount": 19.99}}]},
        "Offers": [{"SellerId": "ATVPDKIKX0DER", "IsFulfilledByAmazon": True, "IsBuyBoxWinner": True,
                    "ListingPrice": {"Amount": 19.99}, "Shipping": {"Amount": 0}},
                   {"SellerId": "X", "IsFulfilledByAmazon": True,
                    "ListingPrice": {"Amount": 21.00}, "Shipping": {"Amount": 0}}]}}
    o = parse_offers("B1", body)
    assert o.amazon_on_listing and o.fba_offers == 3 and o.fbm_offers == 1
    assert o.sell_price == 19.99 and o.lowest_fba_price == 19.99


def test_parse_fees_and_restrictions():
    fee = parse_fee_result({"Status": "Success",
                            "FeesEstimateIdentifier": {"IdValue": "B1"},
                            "FeesEstimate": {"TotalFeesEstimate": {"Amount": 8.0}, "FeeDetailList": [
                                {"FeeType": "ReferralFee", "FinalFee": {"Amount": 3.0}},
                                {"FeeType": "FBAFees", "FinalFee": {"Amount": 5.0}}]}}, {"B1": 20})
    assert (fee.referral, fee.fba, fee.total) == (3.0, 5.0, 8.0)
    assert parse_restrictions("B1", {"restrictions": []}).status == "Eligible"
    e = parse_restrictions("B1", {"restrictions": [{"reasons": [{
        "reasonCode": "APPROVAL_REQUIRED", "message": "Approval required",
        "links": [{"resource": "https://sellercentral.amazon.com/hz/approvalrequest"}]}]}]})
    assert e.status == "Needs approval" and e.link.startswith("https://")
    assert parse_restrictions("B1", {"restrictions": [{"reasons": [
        {"reasonCode": "NOT_ELIGIBLE", "message": "no"}]}]}).status == "Restricted"


def test_history_flags_ip_drop_and_amazon(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    today = dt.date.today()
    for i, offers in enumerate([20, 20, 21, 20]):
        day = (today - dt.timedelta(days=4 - i)).isoformat()
        s.save_snapshot("B1", 20.0, offers, 0, offers, i == 0, 1000, day=day)
    rep = s.analyze("B1", 20.0, 8)
    assert any("IP complaint" in f for f in rep.flags)
    assert any("Amazon sold" in f for f in rep.flags)


def test_alert_dedupe(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    a = CFG["alerts"]
    assert s.should_alert("B1", 10.0, "Eligible", a)
    s.mark_alerted("B1", 10.0, "Eligible")
    assert not s.should_alert("B1", 10.5, "Eligible", a)
    assert s.should_alert("B1", 13.0, "Eligible", a)


def test_supplier_matching(tmp_path: Path):
    (tmp_path / "list.csv").write_text("UPC,Cost,Supplier\n078742000000,4.25,KeHE\n")
    pl = load_price_lists(tmp_path)
    offer = match_price_list(["078742000000"], pl)
    assert offer and offer.price == 4.25 and offer.verified
    results = [{"title": "Great Value Old Fashioned Oats 42 oz", "source": "Walmart", "price": 4.0, "link": "w"},
               {"title": "Oats", "source": "eBay - seller", "price": 1.0, "link": "e"}]
    best = best_serp_offer(results, "Great Value Old Fashioned Oats, 42 oz", RET, CFG)
    assert best.supplier == "Walmart" and best.retailer_key == "Walmart" and best.verified
