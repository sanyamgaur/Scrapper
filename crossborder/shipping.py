"""Shipping cost engine: a cart of Indian SKUs -> comparable US delivery quotes.

The hard part of cross-border shipping is not the rate table, it is the weight.
Carriers bill on max(actual, volumetric), the catalogue only gives a free-text
pack string, and a large share of SKUs are sold by count with no mass at all.
Every gram this engine under-estimates is margin lost on every order that ships,
so unknown weights resolve upward, never downward.

Quote shape:
    quote_cart([(product, qty), ...]) -> CartQuote
        .options   one Quote per carrier that will accept the parcel
        .cheapest / .fastest
        .range_usd (low, high) for the "from $X to $Y" line on a product page

Carrier APIs are not wired in (no accounts yet). `LiveRateProvider` is the seam
where they attach: implement `quote()` and the rate card becomes the fallback
rather than the source of truth. Nothing else changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

import yaml

from .packparse import parse_pack

RULES_PATH = Path(__file__).parent / "rules" / "shipping.yaml"


@dataclass
class Quote:
    carrier_code: str
    carrier_name: str
    service: str
    chargeable_kg: float
    freight_inr: float
    fuel_inr: float
    total_inr: float
    total_usd: float
    transit_days: tuple[int, int]
    tracking: str
    notes: str = ""

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["transit_days"] = list(self.transit_days)
        return d


@dataclass
class WeightBreakdown:
    """Kept separate from the quote because ops needs to audit weight disputes."""
    goods_g: float
    packaging_g: float
    actual_g: float
    volumetric_g: float
    chargeable_g: float
    estimated_items: int = 0      # how many line items fell back to a default weight
    confidence: str = "high"      # high if every item had a parsed mass

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class CartQuote:
    weight: WeightBreakdown
    options: list[Quote] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)   # carrier -> why filtered out

    @property
    def cheapest(self) -> Optional[Quote]:
        return min(self.options, key=lambda q: q.total_usd) if self.options else None

    @property
    def fastest(self) -> Optional[Quote]:
        return min(self.options, key=lambda q: q.transit_days[1]) if self.options else None

    @property
    def range_usd(self) -> Optional[tuple[float, float]]:
        if not self.options:
            return None
        lo = min(q.total_usd for q in self.options)
        hi = max(q.total_usd for q in self.options)
        return (round(lo, 2), round(hi, 2))

    def as_dict(self) -> dict:
        return {
            "weight": self.weight.as_dict(),
            "options": [q.as_dict() for q in self.options],
            "excluded": self.excluded,
            "cheapest": self.cheapest.as_dict() if self.cheapest else None,
            "fastest": self.fastest.as_dict() if self.fastest else None,
            "range_usd": list(self.range_usd) if self.range_usd else None,
        }


class LiveRateProvider(Protocol):
    """Seam for a real carrier API (Shippo/EasyPost/DHL). Not yet implemented."""
    def quote(self, chargeable_kg: float, dest_zip: str) -> list[Quote]: ...


class ShippingEngine:
    def __init__(self, rules_path: Path | str = RULES_PATH,
                 usd_inr: float = 88.0,
                 live: Optional[LiveRateProvider] = None):
        self.cfg = yaml.safe_load(Path(rules_path).read_text())
        self.usd_inr = usd_inr
        self.live = live
        self.pkg = self.cfg["packaging"]
        self.pw = self.cfg["piece_weights_g"]
        self.carriers = self.cfg["carriers"]
        self.divisor = self.cfg["meta"]["volumetric_divisor"]

    # -- weight --------------------------------------------------------------

    def item_weight_g(self, product: dict[str, Any]) -> tuple[float, bool]:
        """Weight of ONE unit of this SKU. Returns (grams, was_estimated).

        Order of preference: parsed mass -> parsed volume (at water density) ->
        piece count x a per-shelf default -> a global default. Each fallback is
        less accurate, so `was_estimated` propagates into quote confidence.
        """
        pack = parse_pack(product.get("unit"))
        if pack.billable_g:
            return pack.billable_g, False

        pieces = pack.pieces or 1

        # A named consumable has a real per-piece weight: 100 tissue pulls is
        # 220 g, not 100 retail units. Check this before any shelf default.
        cw = self.pw.get("by_piece_word", {}).get((pack.piece_word or "").lower())
        if cw is not None:
            return min(cw * pieces, self.pw["sanity_ceiling_g"]), True

        # Otherwise fall back to a per-shelf default. That default is the
        # weight of one typical RETAIL PACK on that shelf, and "10 pcs" is the
        # contents of one such pack -- not ten of them. So it is applied once,
        # never multiplied by the piece count. Ordering several packs is the
        # cart quantity's job, which is applied by the caller.
        pack_g = (
            self.pw["by_group"].get(product.get("group_name"))
            or self.pw["by_category"].get(product.get("category_name"))
            or self.pw["default"]
        )
        return min(pack_g, self.pw["sanity_ceiling_g"]), True

    def cart_weight(self, lines: list[tuple[dict, int]],
                    handling: Optional[list[str]] = None,
                    dims_cm: Optional[tuple[float, float, float]] = None) -> WeightBreakdown:
        handling = handling or []
        goods = 0.0
        estimated = 0
        for product, qty in lines:
            g, est = self.item_weight_g(product)
            goods += g * qty
            if est:
                estimated += 1

        packaging = goods * (self.pkg["weight_factor"] - 1.0) + self.pkg["box_tare_g"]
        if "FRAGILE_PACK" in handling:
            packaging += self.pkg["fragile_extra_g"]
        if "INSULATED_PACK" in handling:
            packaging += self.pkg["insulated_extra_g"]

        actual = goods + packaging

        # Without real carton dimensions, infer a plausible box from the weight
        # at an assumed loose-pack density of ~200 g/litre. Groceries are bulky
        # relative to their mass, and volumetric weight frequently governs.
        if dims_cm:
            vol_cm3 = dims_cm[0] * dims_cm[1] * dims_cm[2]
        else:
            vol_cm3 = max(actual / 0.20, 1000.0)   # g / (g per cm^3)
        volumetric = vol_cm3 / self.divisor * 1000.0

        chargeable = max(actual, volumetric, self.pkg["min_billable_g"])
        return WeightBreakdown(
            goods_g=round(goods, 1), packaging_g=round(packaging, 1),
            actual_g=round(actual, 1), volumetric_g=round(volumetric, 1),
            chargeable_g=round(chargeable, 1), estimated_items=estimated,
            confidence="high" if estimated == 0 else "estimated",
        )

    # -- rating --------------------------------------------------------------

    def _rate_carrier(self, carrier: dict, chargeable_kg: float) -> Optional[Quote]:
        if chargeable_kg > carrier["max_kg"]:
            return None

        # Carriers bill in half-kilo steps below 2 kg and whole kilos above.
        step = 0.5 if chargeable_kg <= 2.0 else 1.0
        billed_kg = math.ceil(chargeable_kg / step) * step

        slab = next((s for s in carrier["slabs"] if billed_kg <= s["up_to_kg"]), None)
        if slab is None:
            return None

        freight = slab["base_inr"] + slab["per_kg_inr"] * billed_kg
        fuel = freight * carrier["fuel_surcharge_pct"] / 100.0
        total_inr = freight + fuel
        total_usd = total_inr / self.usd_inr
        # The consolidator's US-side last mile is billed in USD, not INR.
        total_usd += carrier.get("last_mile_usd", 0.0)

        return Quote(
            carrier_code=carrier["code"], carrier_name=carrier["name"],
            service=carrier["service"], chargeable_kg=round(billed_kg, 2),
            freight_inr=round(freight, 2), fuel_inr=round(fuel, 2),
            total_inr=round(total_inr, 2), total_usd=round(total_usd, 2),
            transit_days=tuple(carrier["transit_days"]),
            tracking=carrier["tracking"], notes=carrier.get("notes", ""),
        )

    def quote_cart(self, lines: list[tuple[dict, int]],
                   handling: Optional[list[str]] = None,
                   dims_cm: Optional[tuple[float, float, float]] = None) -> CartQuote:
        """Quote every carrier that will accept this parcel's handling needs."""
        handling = handling or []
        w = self.cart_weight(lines, handling, dims_cm)
        kg = w.chargeable_g / 1000.0

        options: list[Quote] = []
        excluded: list[dict] = []
        for c in self.carriers:
            # A carrier that cannot honour a required handling flag is not a
            # cheaper option, it is a failed delivery. Filter, never rank.
            missing = [h for h in handling if h not in c.get("accepts", [])]
            if missing:
                excluded.append({"carrier": c["code"],
                                 "reason": f"does not accept {', '.join(missing)}"})
                continue
            q = self._rate_carrier(c, kg)
            if q is None:
                excluded.append({"carrier": c["code"],
                                 "reason": f"over {c['max_kg']} kg limit"})
                continue
            options.append(q)

        options.sort(key=lambda q: q.total_usd)

        if self.live:
            try:
                options = self.live.quote(kg, "") or options
            except Exception:
                pass   # live provider down -> rate card stands in, silently

        return CartQuote(weight=w, options=options, excluded=excluded)
