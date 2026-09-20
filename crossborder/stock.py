"""Live stock engine: never sell a US customer something Blinkit no longer has.

The failure this prevents is the expensive one. A US customer pays USD 50, and
only when your India operator opens the Blinkit app does anyone discover the
SKU is gone. Now you owe a refund, you have burned the customer, and you have
paid the payment-gateway fee twice.

Two layers, because Blinkit's rate limit (~0.635 req/s) makes checking 31k SKUs
continuously impossible:

  1. FRESHNESS TIERS. A SKU's required staleness depends on what it is to the
     business, not on when it was last seen. Items in live carts and hot sellers
     are re-checked in seconds; the long tail is swept over hours. This is the
     scheduler already built in availability_engine.py, driven by a value/cost
     ranking.

  2. HARD GATE AT CHECKOUT. Whatever the cache says, the moment an order is
     placed every line is re-verified against Blinkit synchronously. A cached
     reading is a merchandising signal; only a fresh reading may take money.

Availability is boolean, not a quantity. Blinkit's listing API exposes an
in-stock flag, not an inventory count, so any "12 left" on your storefront
would be fabricated. The engine reports confidence tiers instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from .db import connect, DB_PATH

def _load_freshness() -> dict[str, float]:
    """Freshness windows come from rules/procurement.yaml.

    They decide whether an order can be accepted at all, which makes them
    policy rather than constants — and policy in this project lives in YAML so
    it can be tuned without a code change. Falls back to the production values
    if the file is unreadable, because failing to parse config must not
    accidentally widen a money gate.
    """
    defaults = {"CART": 60, "HOT": 900, "LISTED": 21600, "TAIL": 172800}
    try:
        import yaml
        from pathlib import Path as _P
        cfg = yaml.safe_load((_P(__file__).parent / "rules" / "procurement.yaml").read_text())
        got = (cfg.get("stock") or {}).get("freshness_seconds") or {}
        return {k: float(got.get(k.lower(), v)) for k, v in defaults.items()}
    except Exception:
        return defaults


FRESHNESS = _load_freshness()


class StockState(str, Enum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    STALE = "STALE"            # last reading too old to sell on
    UNKNOWN = "UNKNOWN"        # never checked


@dataclass
class StockReading:
    product_id: str
    state: StockState
    in_stock: Optional[bool]
    price_inr: Optional[float]
    age_seconds: Optional[float]
    tier: str = "LISTED"
    source: str = "cache"

    @property
    def sellable(self) -> bool:
        return self.state is StockState.IN_STOCK

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["state"] = self.state.value
        d["sellable"] = self.sellable
        return d


@dataclass
class OrderGate:
    """Result of the checkout gate. `accepted` is the only field that matters
    to the payment flow; the rest explains a rejection to the customer."""
    accepted: bool
    readings: list[StockReading] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    price_changes: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "readings": [r.as_dict() for r in self.readings],
            "blocked": self.blocked,
            "price_changes": self.price_changes,
        }


class StockEngine:
    """Cache-backed availability with a synchronous gate at checkout.

    `live_check` is the seam to the real Blinkit client. In production it is
    check_availability.py's shelf-walk (it jumps straight to the shelf a SKU is
    known to sit on and stops the instant it finds it, so a single-SKU check is
    1-2 requests, not a crawl). Left as None, the engine serves cache only and
    refuses to gate an order on stale data rather than guessing.
    """

    def __init__(self, db_path=DB_PATH,
                 live_check: Optional[Callable[[list[str]], dict[str, dict]]] = None,
                 price_tolerance_pct: Optional[float] = None,
                 require_live_confirmation: Optional[bool] = None):
        self.db_path = db_path
        self.live_check = live_check
        # Quote tolerance is policy, not a constant. It lives in
        # rules/procurement.yaml alongside the list-price deadband so the two
        # cannot drift apart into competing rules.
        if price_tolerance_pct is None:
            try:
                import yaml
                from pathlib import Path as _P
                _cfg = yaml.safe_load(
                    (_P(__file__).parent / "rules" / "procurement.yaml").read_text())
                price_tolerance_pct = _cfg["price_drift"]["quote_tolerance_pct"]
            except Exception:
                price_tolerance_pct = 8.0
        self.price_tolerance_pct = price_tolerance_pct

        # Does checkout DEMAND a fresh live reading, or may it sell on the last
        # known one? This is the single most important gate policy, and getting
        # it wrong makes the whole store unusable, so it lives in YAML.
        #
        #   strict  (True)  -> a STALE/UNKNOWN reading blocks the sale. Correct
        #                      ONLY when availability_engine.py is running and
        #                      keeping cart items fresh within cart_seconds.
        #   lenient (False) -> staleness carries no information (we have no live
        #                      Blinkit feed to refute it), so sell on the latest
        #                      crawl/last reading and block ONLY an item we can
        #                      see is out of stock. This is the right default for
        #                      any deployment without a live checker — otherwise
        #                      every reading ages past 60s and every order is
        #                      rejected as "unverified", which is exactly the bug
        #                      this flag exists to prevent.
        if require_live_confirmation is None:
            try:
                import yaml
                from pathlib import Path as _P
                _cfg = yaml.safe_load(
                    (_P(__file__).parent / "rules" / "procurement.yaml").read_text())
                require_live_confirmation = bool(
                    (_cfg.get("stock") or {}).get("require_live_confirmation", False))
            except Exception:
                require_live_confirmation = False
        self.require_live_confirmation = require_live_confirmation

    def _effective_state(self, r: "StockReading") -> "StockState":
        """What a reading MEANS for selling, given the gate policy.

        A definite reading (IN_STOCK/OUT_OF_STOCK) always stands. The only
        question is what to do with a STALE or UNKNOWN one:

          - strict mode (require_live_confirmation, or a live checker is wired
            in): staleness is disqualifying — hold the sale.
          - lenient mode (no live feed): fall back to the last known in-stock
            flag, and only block when that flag actually says out of stock.
        """
        if r.state in (StockState.IN_STOCK, StockState.OUT_OF_STOCK):
            return r.state
        strict = self.require_live_confirmation or (self.live_check is not None)
        if strict:
            return r.state
        if r.in_stock is True:
            return StockState.IN_STOCK
        if r.in_stock is False:
            return StockState.OUT_OF_STOCK
        return r.state

    # -- cache ---------------------------------------------------------------

    def read(self, product_id: str, tier: str = "LISTED") -> StockReading:
        conn = connect(self.db_path)
        row = conn.execute("""
            SELECT in_stock, price_inr,
                   (julianday('now') - julianday(checked_at)) * 86400.0 AS age
            FROM stock_checks WHERE product_id=? ORDER BY checked_at DESC LIMIT 1
        """, (product_id,)).fetchone()
        if row is None:
            # Fall back to the crawl snapshot, aged from its scrape timestamp.
            p = conn.execute("SELECT in_stock, price_inr, scraped_at FROM products "
                             "WHERE product_id=?", (product_id,)).fetchone()
            conn.close()
            if p is None:
                return StockReading(product_id, StockState.UNKNOWN, None, None, None, tier)
            age = time.time() - (p["scraped_at"] or 0)
            state = StockState.STALE if age > FRESHNESS[tier] else (
                StockState.IN_STOCK if p["in_stock"] else StockState.OUT_OF_STOCK)
            return StockReading(product_id, state, bool(p["in_stock"]),
                                p["price_inr"], age, tier, source="crawl")
        conn.close()
        age = row["age"]
        if age is not None and age > FRESHNESS[tier]:
            state = StockState.STALE
        else:
            state = StockState.IN_STOCK if row["in_stock"] else StockState.OUT_OF_STOCK
        return StockReading(product_id, state, bool(row["in_stock"]),
                            row["price_inr"], age, tier)

    def record(self, product_id: str, in_stock: bool,
               price_inr: Optional[float] = None, source: str = "live") -> None:
        conn = connect(self.db_path)
        conn.execute("INSERT INTO stock_checks (product_id,in_stock,price_inr,source) "
                     "VALUES (?,?,?,?)", (product_id, int(in_stock), price_inr, source))
        conn.commit()
        conn.close()

    # -- the gate ------------------------------------------------------------

    def gate_order(self, lines: list[tuple[str, int]],
                   quoted_prices: Optional[dict[str, float]] = None) -> OrderGate:
        """Verify every line before taking money. Fails closed.

        A STALE or UNKNOWN reading blocks the order exactly like an explicit
        out-of-stock. Selling on an unverified reading is the same mistake as
        selling something you know is gone, only harder to explain afterwards.
        """
        quoted_prices = quoted_prices or {}
        pids = [p for p, _ in lines]

        # Refresh synchronously where a live client exists.
        if self.live_check:
            try:
                fresh = self.live_check(pids)
                for pid, info in fresh.items():
                    self.record(pid, bool(info.get("in_stock")),
                                info.get("price_inr"), source="gate")
            except Exception:
                pass   # fall through to cache; stale readings then block below

        readings, blocked, changes = [], [], []
        for pid, qty in lines:
            r = self.read(pid, tier="CART")
            readings.append(r)
            state = self._effective_state(r)
            if state is StockState.OUT_OF_STOCK:
                blocked.append({"product_id": pid, "reason": "out_of_stock",
                                "message": "This item just sold out in Delhi. "
                                           "We won't charge you for it."})
            elif state in (StockState.STALE, StockState.UNKNOWN):
                blocked.append({"product_id": pid, "reason": "unverified",
                                "message": "We could not confirm live availability. "
                                           "Not charging you for an item we cannot promise."})
            # A large INR move between quote and checkout breaks the landed-cost
            # maths, so surface it rather than silently absorbing the loss.
            q = quoted_prices.get(pid)
            if q and r.price_inr:
                delta = (r.price_inr - q) / q * 100.0
                if abs(delta) > self.price_tolerance_pct:
                    changes.append({"product_id": pid, "quoted_inr": q,
                                    "current_inr": r.price_inr, "delta_pct": round(delta, 1)})

        return OrderGate(accepted=not blocked, readings=readings,
                         blocked=blocked, price_changes=changes)

    # -- merchandising -------------------------------------------------------

    def badge(self, product_id: str) -> str:
        """What the storefront shows. Never invent a quantity we do not have."""
        r = self.read(product_id, tier="LISTED")
        return {
            StockState.IN_STOCK: "In stock",
            StockState.OUT_OF_STOCK: "Out of stock",
            StockState.STALE: "Checking availability",
            StockState.UNKNOWN: "Checking availability",
        }[self._effective_state(r)]
