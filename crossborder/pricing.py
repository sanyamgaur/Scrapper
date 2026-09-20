"""Landed-cost engine: INR shelf price -> USD list price, with every line shown.

This is the engine that tells you whether the business works on a given SKU.
It deliberately itemizes rather than returning a single number, because when a
product is unsellable you need to see WHICH line killed it -- freight, duty, or
the goods themselves.

The viability check matters more than the price. Shipping 1.5 kg of peanuts
costs ~USD 26-64 against roughly USD 3.40 of peanuts. The engine flags that
ratio instead of quietly printing a USD 70 list price for peanuts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .shipping import ShippingEngine, CartQuote

RULES_PATH = Path(__file__).parent / "rules" / "pricing.yaml"
SHIP_RULES = Path(__file__).parent / "rules" / "shipping.yaml"


@dataclass
class LandedCost:
    """Every line between the Blinkit shelf and the customer's card."""
    goods_inr: float = 0.0
    sourcing_inr: float = 0.0
    goods_usd: float = 0.0
    sourcing_usd: float = 0.0
    freight_usd: float = 0.0
    duty_usd: float = 0.0
    mpf_usd: float = 0.0
    payments_usd: float = 0.0
    total_cost_usd: float = 0.0
    list_price_usd: float = 0.0
    margin_usd: float = 0.0
    margin_pct: float = 0.0
    carrier: str = ""
    transit_days: tuple[int, int] = (0, 0)
    viable: bool = True
    warnings: list[str] = field(default_factory=list)
    freight_ratio: float = 0.0

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["transit_days"] = list(self.transit_days)
        return d


class PricingEngine:
    def __init__(self, rules_path: Path | str = RULES_PATH,
                 shipping: Optional[ShippingEngine] = None):
        self.cfg = yaml.safe_load(Path(rules_path).read_text())
        self.ship_cfg = yaml.safe_load(Path(SHIP_RULES).read_text())
        self.customs = self.ship_cfg["customs"]

        fx = self.cfg["fx"]
        # Quote at a rate slightly worse than spot so FX drift cannot turn a
        # thin-margin order into a loss between quote and purchase.
        self.usd_inr = fx["usd_inr_spot"] * (1 - fx["buffer_pct"] / 100.0)
        self.shipping = shipping or ShippingEngine(usd_inr=self.usd_inr)

    def _duty_pct(self, category: Optional[str]) -> float:
        return self.customs["duty_pct_by_category"].get(
            category or "", self.customs["duty_pct_default"])

    def _margin_pct(self, category: Optional[str]) -> float:
        m = self.cfg["margin"]
        return m["by_category"].get(category or "", m["target_gross_margin_pct"])

    def _round_price(self, usd: float) -> float:
        m = self.cfg["margin"]
        # Round up to the next .99 so the displayed price never sits below cost.
        base = max(usd, m["min_price_usd"])
        whole = int(base)
        cand = whole + m["round_to"]
        if cand < base:
            cand = whole + 1 + m["round_to"]
        return round(cand, 2)

    def price_cart(self, lines: list[tuple[dict, int]],
                   handling: Optional[list[str]] = None,
                   carrier_code: Optional[str] = None) -> tuple[LandedCost, CartQuote]:
        """Price a full cart. Returns the cost breakdown and the raw quote."""
        src = self.cfg["sourcing"]
        pay = self.cfg["payments"]
        via = self.cfg["viability"]

        goods_inr = sum(float(p.get("price") or 0) * q for p, q in lines)
        goods_inr *= (1 + src["blinkit_handling_pct"] / 100.0)

        sourcing_inr = (src["blinkit_delivery_fee_inr"] + src["india_pick_pack_inr"])
        # Failed procurement is a real, recurring cost: price it in rather than
        # discovering it as a refund line at the end of the month.
        sourcing_inr += goods_inr * (src["procurement_failure_pct"] / 100.0)

        quote = self.shipping.quote_cart(lines, handling=handling)
        if not quote.options:
            lc = LandedCost(viable=False,
                            warnings=["No carrier will accept this parcel."])
            return lc, quote

        chosen = next((o for o in quote.options if o.carrier_code == carrier_code),
                      quote.cheapest)

        goods_usd = goods_inr / self.usd_inr
        sourcing_usd = sourcing_inr / self.usd_inr
        freight_usd = chosen.total_usd

        # Duty is assessed on the customs value of the goods. Use the dominant
        # category in the cart; a real broker would classify line by line.
        cat = lines[0][0].get("category_name") if lines else None
        dutiable = goods_usd
        de_minimis = self.customs.get("de_minimis_usd", 0.0)
        duty_usd = 0.0 if (de_minimis and dutiable <= de_minimis) \
            else dutiable * self._duty_pct(cat) / 100.0

        mpf = dutiable * self.customs["merchandise_processing_fee_pct"] / 100.0
        mpf = min(max(mpf, self.customs["mpf_min_usd"]), self.customs["mpf_max_usd"])

        subtotal = goods_usd + sourcing_usd + freight_usd + duty_usd + mpf

        # Payment costs are charged on the final price, which depends on them.
        # Solve directly instead of iterating: price = (cost + flat) / (1 - r).
        margin_pct = self._margin_pct(cat)
        pay_rate = (pay["gateway_pct"] + pay["fx_conversion_pct"]
                    + pay["chargeback_reserve_pct"]) / 100.0
        pre_margin = (subtotal + pay["gateway_flat_usd"]) / (1 - pay_rate)
        target = pre_margin / (1 - margin_pct / 100.0)
        list_price = self._round_price(target)

        payments_usd = list_price * pay_rate + pay["gateway_flat_usd"]
        total_cost = subtotal + payments_usd
        margin_usd = list_price - total_cost

        warnings: list[str] = []
        ratio = freight_usd / goods_usd if goods_usd > 0 else 999.0
        viable = True
        if goods_inr < via["min_goods_value_inr"]:
            warnings.append(f"Goods value INR {goods_inr:.0f} is below the "
                            f"INR {via['min_goods_value_inr']:.0f} floor; not worth shipping alone.")
            viable = False
        if ratio > via["max_freight_to_goods_ratio"]:
            warnings.append(f"Freight is {ratio:.1f}x the goods value. "
                            f"Unsellable as a standalone order - bundle it or delist.")
            viable = False
        elif ratio > via["warn_freight_to_goods_ratio"]:
            warnings.append(f"Freight is {ratio:.1f}x the goods value. "
                            f"Only viable inside a larger basket.")
        if margin_usd < 0:
            warnings.append("Negative margin at the rounded list price.")
            viable = False
        # Backstop on the configured floor. It cannot fire while _round_price
        # rounds UP and the target margin sits above the floor, so today this is
        # a guard rather than a live check -- but min_gross_margin_pct existed in
        # policy while nothing enforced it, which reads as a safety net that is
        # not there. It becomes real the moment a per-category target is set
        # below the floor, or the rounding rule changes.
        min_margin = self.cfg["margin"]["min_gross_margin_pct"]
        realized = (margin_usd / list_price * 100) if list_price else 0.0
        if 0 <= realized < min_margin:
            warnings.append(f"Gross margin {realized:.1f}% is below the "
                            f"{min_margin:.0f}% floor.")
            viable = False
        if quote.weight.confidence != "high":
            warnings.append("Weight partly estimated; freight may be understated.")

        lc = LandedCost(
            goods_inr=round(goods_inr, 2), sourcing_inr=round(sourcing_inr, 2),
            goods_usd=round(goods_usd, 2), sourcing_usd=round(sourcing_usd, 2),
            freight_usd=round(freight_usd, 2), duty_usd=round(duty_usd, 2),
            mpf_usd=round(mpf, 2), payments_usd=round(payments_usd, 2),
            total_cost_usd=round(total_cost, 2), list_price_usd=list_price,
            margin_usd=round(margin_usd, 2),
            margin_pct=round(margin_usd / list_price * 100, 1) if list_price else 0.0,
            carrier=chosen.carrier_code, transit_days=chosen.transit_days,
            viable=viable, warnings=warnings, freight_ratio=round(ratio, 2),
        )
        return lc, quote

    def price_single(self, product: dict[str, Any], qty: int = 1,
                     handling: Optional[list[str]] = None) -> tuple[LandedCost, CartQuote]:
        return self.price_cart([(product, qty)], handling=handling)
