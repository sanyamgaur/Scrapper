"""Smart basket builder: turn commercially dead carts into viable ones.

This is the highest-leverage engine in the system. A single cheap SKU is dead on
arrival -- 500 g of peanuts is USD 3.92 of goods against USD 26-64 of freight --
while an eight-item basket reaches a 2.7x ratio and 30.8% margin. The business
does not work until carts get bigger, so this engine exists to make them bigger
for reasons the customer actually benefits from.

It is NOT "customers also bought". It is freight arithmetic:

1. FREE HEADROOM. Carriers bill on rounded weight: 0.5 kg steps below 2 kg,
   1 kg steps above. A 1.05 kg cart already bills at 1.5 kg, so the next 450 g
   ship for nothing. Spending that headroom is a pure gain for the customer and
   the single best suggestion available -- more goods, identical freight.

2. VALUE DENSITY. Goods value per gram of chargeable weight. Adding a dense item
   (saffron, INR 615/g) improves the freight ratio; adding a cheap heavy one
   (erasers, INR 0.025/g) makes it worse. The catalogue spans a 25,000x range,
   so this genuinely discriminates.

3. PROCUREMENT REALITY. A suggestion that cannot be bought costs more than no
   suggestion: it becomes a short, a refund and an apology. So only in-stock,
   low-stockout-risk, compliance-ALLOWED SKUs are ever suggested.

   Note what the risk half of that is worth TODAY: with no availability history,
   an in-stock SKU cannot score above ~0.39 against a 0.60 threshold, so the
   risk filter currently excludes nothing and the in-stock flag is doing all the
   work. It starts discriminating on its own once stock_checks accumulates. It
   is documented here rather than removed, because the guard is correct and
   silently inert is exactly how a safety check gets mistaken for protection.

4. ONE DARK STORE. Blinkit serves a basket from a single merchant. An add-on
   from the other dark store silently creates a SECOND order with a second
   delivery fee, which can cost more than the add-on earns. Same-merchant
   suggestions are preferred.

5. CARRIER COMPATIBILITY. An add-on whose handling flags exclude the cart's
   chosen carrier (chocolate needing INSULATED_PACK removes India Post) can
   raise the total. Such items are filtered, not ranked down.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

from .db import connect, DB_PATH
from .pricing import PricingEngine
from .shipping import ShippingEngine

RULES_PATH = Path(__file__).parent / "rules" / "procurement.yaml"


@dataclass
class Suggestion:
    product_id: str
    name: str
    brand: str
    unit: str
    price_inr: float
    weight_g: float
    list_price_usd: float
    value_density: float
    fits_free_headroom: bool
    same_merchant: bool
    risk_score: float
    reason: str
    image: Optional[str] = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class BasketAdvice:
    chargeable_g: float
    billed_g: float
    free_headroom_g: float
    freight_ratio: float
    viable: bool
    suggestions: list[Suggestion]
    headline: str

    def as_dict(self) -> dict:
        return {"chargeable_g": self.chargeable_g, "billed_g": self.billed_g,
                "free_headroom_g": round(self.free_headroom_g, 1),
                "freight_ratio": self.freight_ratio, "viable": self.viable,
                "headline": self.headline,
                "suggestions": [s.as_dict() for s in self.suggestions]}


def billed_weight_g(chargeable_g: float) -> float:
    """Mirror the carrier's rounding so headroom is computed, not guessed."""
    kg = chargeable_g / 1000.0
    step = 0.5 if kg <= 2.0 else 1.0
    return math.ceil(kg / step) * step * 1000.0


class BasketBuilder:
    def __init__(self, db_path=DB_PATH, rules_path: Path | str = RULES_PATH,
                 pricing: Optional[PricingEngine] = None):
        cfg = yaml.safe_load(Path(rules_path).read_text())
        self.cfg = cfg["basket"]
        self.db_path = db_path
        self.pricing = pricing or PricingEngine()
        self.shipping: ShippingEngine = self.pricing.shipping

    def advise(self, lines: list[tuple[dict, int]],
               handling: Optional[list[str]] = None,
               carrier_code: Optional[str] = None) -> BasketAdvice:  # noqa: C901
        """Given a cart, return add-ons that improve its economics."""
        handling = handling or []
        lc, quote = self.pricing.price_cart(lines, handling=handling,
                                            carrier_code=carrier_code)
        chargeable = quote.weight.chargeable_g
        billed = billed_weight_g(chargeable)
        headroom = max(0.0, billed - chargeable)
        ratio = lc.freight_ratio
        target = self.cfg["target_freight_ratio"]
        viable = lc.viable and ratio <= target

        cart_merchants = {str(p.get("merchant_id")) for p, _ in lines if p.get("merchant_id")}
        exclude = {str(p.get("product_id")) for p, _ in lines}

        cart_goods_inr = sum(float(p.get("price_inr") or p.get("price") or 0) * q
                             for p, q in lines)
        cart_supers = {p.get("super_category") for p, _ in lines if p.get("super_category")}
        suggestions = self._candidates(headroom, cart_merchants, exclude, handling,
                                       cart_goods_inr, cart_supers)

        if headroom >= 50 and suggestions:
            headline = (f"You have {headroom:.0f} g of shipping headroom already paid for. "
                        f"Adding these ships free.")
        elif not viable:
            headline = (f"Shipping is {ratio:.1f}x the value of these goods. "
                        f"Adding a few dense items makes this basket worth sending.")
        else:
            headline = "This basket ships efficiently. These add well if you want more."

        return BasketAdvice(chargeable_g=round(chargeable, 1), billed_g=billed,
                            free_headroom_g=headroom, freight_ratio=ratio,
                            viable=viable, suggestions=suggestions, headline=headline)

    def _candidates(self, headroom_g: float, cart_merchants: set[str],
                    exclude: set[str], handling: list[str],
                    cart_goods_inr: float = 0.0,
                    cart_supers: Optional[set] = None) -> list[Suggestion]:
        conn = connect(self.db_path)
        rows = [dict(r) for r in conn.execute("""
            SELECT p.product_id, p.name, p.brand, p.unit, p.price_inr, p.image,
                   p.est_weight_g, p.merchant_id, p.in_stock, p.group_name,
                   p.category_name, p.super_category, c.handling,
                   COALESCE(s.score, 0.5) AS risk
            FROM products p
            JOIN classifications c USING(product_id)
            LEFT JOIN stockout_risk s USING(product_id)
            WHERE c.verdict = 'ALLOWED'
              AND p.price_inr > 0 AND p.est_weight_g > 0
              AND (? = 0 OR p.in_stock = 1)
              AND COALESCE(s.score, 0.5) <= ?
            """, (1 if self.cfg["require_in_stock"] else 0,
                  self.cfg["max_risk_score"]))]
        conn.close()

        scored: list[tuple[float, Suggestion]] = []
        for r in rows:
            pid = str(r["product_id"])
            if pid in exclude:
                continue

            # A handling flag the cart's carrier set cannot absorb is a filter,
            # not a penalty: adding it can remove the cheapest carrier entirely.
            item_handling = [h for h in (r["handling"] or "").split(",") if h]
            if any(h not in handling for h in item_handling):
                continue

            # Relevance guard: a suggestion far more expensive than the cart
            # itself reads as a bait-and-switch, however good its freight maths.
            if cart_goods_inr > 0:
                if float(r["price_inr"]) > cart_goods_inr * self.cfg["max_price_ratio"]:
                    continue

            w = float(r["est_weight_g"])
            density = float(r["price_inr"]) / w
            if density < self.cfg["min_value_density_inr_per_g"]:
                continue

            fits_free = w <= headroom_g and headroom_g >= 50
            same_merchant = (not cart_merchants) or str(r["merchant_id"]) in cart_merchants
            if self.cfg["prefer_same_merchant"] and cart_merchants and not same_merchant:
                # Allowed, but it must clearly outperform to justify a second
                # dark-store order and its extra delivery fee.
                pass

            # Rank: density is the economic core; free-headroom fit is decisive
            # when it applies; same-merchant avoids a second Blinkit order;
            # lower stockout risk means the suggestion can actually be bought.
            rank = math.log10(max(density, 0.01)) + 1.0
            if fits_free:
                rank += 2.0
            if same_merchant:
                rank += self.cfg["same_merchant_bonus"]
            elif self.cfg["prefer_same_merchant"]:
                rank -= self.cfg["same_merchant_bonus"]
            rank += (1.0 - float(r["risk"])) * 0.5
            # Stay in the aisle the shopper is already shopping.
            if cart_supers and r.get("super_category") in cart_supers:
                rank += self.cfg["same_super_category_bonus"]

            p = dict(r); p["price"] = r["price_inr"]
            try:
                lc, _ = self.pricing.price_single(p, 1)
                usd = lc.list_price_usd
            except Exception:
                continue

            reason = ("ships free in your existing weight allowance" if fits_free
                      else f"high value for its weight (₹{density:.0f}/g)")
            if not same_merchant:
                reason += " — from a different store, may ship separately"

            scored.append((rank, Suggestion(
                product_id=pid, name=r["name"], brand=r["brand"] or "",
                unit=r["unit"] or "", price_inr=float(r["price_inr"]),
                weight_g=w, list_price_usd=usd, value_density=round(density, 2),
                fits_free_headroom=fits_free, same_merchant=same_merchant,
                risk_score=round(float(r["risk"]), 2), reason=reason,
                image=r["image"])))

        scored.sort(key=lambda t: -t[0])

        # Spread across shelves so the panel does not become six saffrons.
        out: list[Suggestion] = []
        seen_shelf: dict[str, int] = {}
        by_pid = {str(r["product_id"]): r for r in rows}
        for _rank, s in scored:
            shelf = by_pid[s.product_id].get("group_name") or "?"
            if seen_shelf.get(shelf, 0) >= 2:
                continue
            seen_shelf[shelf] = seen_shelf.get(shelf, 0) + 1
            out.append(s)
            if len(out) >= self.cfg["max_suggestions"]:
                break
        return out
