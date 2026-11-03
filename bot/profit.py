"""Per-unit profit model. Every cost is listed separately so the sheet can
show exactly where the money goes."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class ProductCosts:
    """Inputs about the Amazon side of one product."""
    sell_price: float
    referral_fee: float
    fba_fee: float
    closing_fee: float = 0.0
    category: str = ""
    weight_lb: float | None = None
    length_in: float | None = None
    width_in: float | None = None
    height_in: float | None = None


@dataclass
class ProfitResult:
    buy_price: float
    sell_price: float
    tax: float
    shipping_in: float
    inbound: float
    prep_misc: float
    placement: float
    referral: float
    fba: float
    closing: float
    storage: float
    returns: float
    landed_cost: float
    amazon_costs: float
    profit: float
    roi: float
    margin: float
    profit_at_drop: float
    savings_certain: float = 0.0
    savings_all: float = 0.0
    profit_with_savings: float = 0.0
    roi_with_savings: float = 0.0
    notes: list[str] = field(default_factory=list)


def _is_grocery(category: str, cfg: dict) -> bool:
    groceries = [c.lower() for c in cfg["costs"].get("grocery_categories", [])]
    return category.lower() in groceries


def tax_rate(category: str, cfg: dict, retailer: dict | None = None) -> float:
    if retailer and retailer.get("tax_exempt"):
        return 0.0
    costs = cfg["costs"]
    return costs["grocery_tax_rate"] if _is_grocery(category, cfg) else costs["sales_tax_rate"]


def billable_weight(p: ProductCosts, cfg: dict) -> float:
    weight = p.weight_lb or 1.0  # unknown weight -> assume 1 lb (flagged by caller)
    if p.length_in and p.width_in and p.height_in:
        dim = p.length_in * p.width_in * p.height_in / cfg["costs"]["dim_weight_divisor"]
        weight = max(weight, dim)
    return weight


def storage_cost(p: ProductCosts, cfg: dict, today: dt.date | None = None) -> float:
    costs = cfg["costs"]
    if not (p.length_in and p.width_in and p.height_in):
        cubic_ft = 0.05  # small item guess
    else:
        cubic_ft = p.length_in * p.width_in * p.height_in / 1728
    today = today or dt.date.today()
    total, month = 0.0, today.month
    for i in range(int(costs["storage_months"])):
        m = (month - 1 + i) % 12 + 1
        rate = costs["storage_rate_oct_dec"] if m >= 10 else costs["storage_rate_jan_sep"]
        total += cubic_ft * rate
    return total


def returns_rate(category: str, cfg: dict) -> float:
    table = cfg["costs"]["returns_reserve"]
    return table.get(category, table["default"])


def fixed_costs_per_unit(p: ProductCosts, cfg: dict) -> tuple[float, float, float]:
    """(inbound shipping, prep+misc, placement) — costs that don't depend on buy price."""
    c = cfg["costs"]
    return (billable_weight(p, cfg) * c["inbound_per_lb"],
            c["prep_per_unit"] + c["misc_per_unit"],
            c["placement_fee"])


def amazon_side_costs(p: ProductCosts, cfg: dict, price: float | None = None,
                      today: dt.date | None = None) -> float:
    """Referral + FBA + closing + storage + returns at a given sell price."""
    price = p.sell_price if price is None else price
    ref_rate = p.referral_fee / p.sell_price if p.sell_price else 0.15
    referral = max(0.30, ref_rate * price) if p.referral_fee else 0.0
    return (referral + p.fba_fee + p.closing_fee + storage_cost(p, cfg, today)
            + returns_rate(p.category, cfg) * price)


def calc_profit(p: ProductCosts, buy_price: float, cfg: dict,
                retailer: dict | None = None, shipping_in: float = 0.0,
                today: dt.date | None = None) -> ProfitResult:
    t_rate = tax_rate(p.category, cfg, retailer)
    tax = buy_price * t_rate
    inbound, prep_misc, placement = fixed_costs_per_unit(p, cfg)
    storage = storage_cost(p, cfg, today)
    returns = returns_rate(p.category, cfg) * p.sell_price
    landed = buy_price + tax + shipping_in + inbound + prep_misc + placement
    amazon = p.referral_fee + p.fba_fee + p.closing_fee + storage + returns
    profit = p.sell_price - amazon - landed

    drop_price = p.sell_price * (1 - cfg["costs"]["price_drop_test"])
    profit_drop = drop_price - amazon_side_costs(p, cfg, drop_price, today) - landed

    res = ProfitResult(
        buy_price=buy_price, sell_price=p.sell_price, tax=tax, shipping_in=shipping_in,
        inbound=inbound, prep_misc=prep_misc, placement=placement,
        referral=p.referral_fee, fba=p.fba_fee, closing=p.closing_fee,
        storage=storage, returns=returns, landed_cost=landed, amazon_costs=amazon,
        profit=profit, roi=profit / landed if landed else 0.0,
        margin=profit / p.sell_price if p.sell_price else 0.0,
        profit_at_drop=profit_drop,
    )
    if p.weight_lb is None:
        res.notes.append("weight unknown (assumed 1 lb)")
    if not (p.length_in and p.width_in and p.height_in):
        res.notes.append("size unknown (storage estimated)")

    if retailer:
        apply_savings(res, retailer, cfg)
    else:
        res.profit_with_savings, res.roi_with_savings = res.profit, res.roi
    return res


def apply_savings(res: ProfitResult, r: dict, cfg: dict) -> None:
    """Discounts in real-world order:
       coupon (already in buy price) -> tax on discounted price ->
       ONE payment discount (gift card OR store card) on the total paid ->
       cashback on the pre-tax subtotal."""
    paid = res.buy_price + res.tax + res.shipping_in
    gc, card = r.get("giftcard_pct", 0) or 0, r.get("store_card_pct", 0) or 0

    best_pay = max(gc, card)
    certain_pay = max(gc if r.get("giftcard_owned") else 0,
                      card if r.get("store_card_owned") else 0)
    cashback = (r.get("cashback_pct", 0) or 0) * res.buy_price

    res.savings_all = paid * best_pay + cashback
    res.savings_certain = paid * certain_pay
    if cfg["savings"].get("count_cashback_toward_pass"):
        res.savings_certain += cashback

    res.profit_with_savings = res.profit + res.savings_all
    cost_after = res.landed_cost - paid * best_pay
    res.roi_with_savings = res.profit_with_savings / cost_after if cost_after else 0.0

    if r.get("signup_coupon_pct"):
        res.notes.append(f"one-time signup coupon {r['signup_coupon_pct']:.0%} available (not included)")


def max_buy_price(p: ProductCosts, cfg: dict, min_profit: float, min_roi: float,
                  retailer: dict | None = None, today: dt.date | None = None) -> float:
    """Highest purchase price (before tax) that still meets both targets.

    profit(b) = K - b(1+t)            where K = sell - amazon - fixed
    ROI(b)    = profit / (b(1+t) + F) where F = fixed costs
    """
    t = tax_rate(p.category, cfg, retailer)
    inbound, prep_misc, placement = fixed_costs_per_unit(p, cfg)
    fixed = inbound + prep_misc + placement
    amazon = p.referral_fee + p.fba_fee + p.closing_fee + storage_cost(p, cfg, today) \
        + returns_rate(p.category, cfg) * p.sell_price
    k = p.sell_price - amazon - fixed
    x = min(k - min_profit, (k - min_roi * fixed) / (1 + min_roi))
    return max(0.0, x / (1 + t))
