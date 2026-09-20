"""Daily procurement batching: US orders -> Blinkit baskets an operator can buy.

The problem this solves is not "make a shopping list". It is that a day's US
orders arrive as N independent carts, must be bought as a handful of Blinkit
baskets, and must then come back apart into per-customer parcels -- while some
lines fail to buy and someone has to decide whose order goes short.

Four things make that work:

1. CONSOLIDATION WITH A LEDGER. Three customers ordering the same peanuts become
   ONE Blinkit line of qty 3, but `batch_allocations` records which unit belongs
   to whom. Without that ledger a short has no owner and pack-out is guesswork.

2. SPLIT BY DARK STORE FIRST. A Blinkit basket is served by one merchant. The
   listable catalogue spans merchants 34280 (3,700 SKUs) and 36778 (2,396), so a
   day's batch is essentially never one order. Merchant split precedes cart
   splitting, which then respects per-cart line and value caps.

3. RISK-FIRST PICK ORDER. Lines are bought in descending stockout risk, weighted
   by the USD revenue riding on them. Buying the item likely to vanish last is
   how a batch fails.

4. COMPLETENESS-FIRST SHORT ALLOCATION. When 2 of 3 units arrive, they go to the
   orders closest to being whole rather than spreading thin. One shippable order
   and one refund beats three half-orders, none of which can move.

Order placement itself is NOT automated -- see `operator.py` for why, and for
the pick flow that replaces it. `ProcurementBackend` is the seam where a real
partner API would attach without redesigning any of the above.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

import yaml

from .db import connect, DB_PATH

RULES_PATH = Path(__file__).parent / "rules" / "procurement.yaml"


class ProcurementBackend(Protocol):
    """The seam for a future automated buyer (partner API, B2B account).

    Implement `execute(batch_id, lines)` and the operator flow becomes a
    fallback rather than the only path. Nothing else in this module changes.
    """
    def execute(self, batch_id: str, lines: list[dict]) -> dict: ...


@dataclass
class Cart:
    """One Blinkit basket: a single merchant, within line and value caps."""
    cart_no: int
    merchant_id: str
    lines: list[dict] = field(default_factory=list)

    @property
    def value_inr(self) -> float:
        return sum((l["expected_inr"] or 0) * l["qty_required"] for l in self.lines)

    def as_dict(self) -> dict:
        return {"cart_no": self.cart_no, "merchant_id": self.merchant_id,
                "n_lines": len(self.lines), "value_inr": round(self.value_inr, 2),
                "lines": self.lines}


class ProcurementEngine:
    def __init__(self, db_path=DB_PATH, rules_path: Path | str = RULES_PATH):
        cfg = yaml.safe_load(Path(rules_path).read_text())
        self.cfg = cfg["batching"]
        self.db_path = db_path

    # -- event log -----------------------------------------------------------

    def _event(self, conn, batch_id, product_id, event, prev=None, curr=None,
               actor="system") -> None:
        conn.execute("""INSERT INTO procurement_events
            (batch_id,product_id,event,prev,curr,actor) VALUES (?,?,?,?,?,?)""",
            (batch_id, product_id, event, prev, curr, actor))

    # -- building a batch ----------------------------------------------------

    def build_batch(self, batch_date: str, order_ids: Optional[list[str]] = None,
                    operator: str = "ops") -> dict:
        """Seal a day's orders into a consolidated, risk-ranked, split batch.

        `batch_date` is supplied by the caller rather than read from the clock,
        so a batch is reproducible and testable.
        """
        conn = connect(self.db_path)
        batch_id = f"PB-{batch_date}"

        if conn.execute("SELECT 1 FROM procurement_batches WHERE batch_id=?",
                        (batch_id,)).fetchone():
            conn.close()
            raise ValueError(f"{batch_id} already exists")

        if order_ids is None:
            rows = conn.execute(
                "SELECT order_id FROM orders WHERE batch_id IS NULL "
                "AND status IN ('STOCK_CONFIRMED','PLACED')").fetchall()
            order_ids = [r["order_id"] for r in rows]

        if not order_ids:
            conn.close()
            return {"batch_id": batch_id, "n_orders": 0, "n_lines": 0, "carts": []}

        marks = ",".join("?" * len(order_ids))
        lines = conn.execute(f"""
            SELECT ol.order_id, ol.product_id, ol.qty, ol.unit_price_inr,
                   o.created_at, p.merchant_id, p.price_inr, p.name,
                   COALESCE(r.score, 0.5) AS risk, r.bucket, r.confidence
            FROM order_lines ol
            JOIN orders o USING(order_id)
            JOIN products p ON p.product_id = ol.product_id
            LEFT JOIN stockout_risk r ON r.product_id = ol.product_id
            WHERE ol.order_id IN ({marks})
            ORDER BY o.created_at""", order_ids).fetchall()

        if not lines:
            conn.close()
            return {"batch_id": batch_id, "n_orders": len(order_ids),
                    "n_lines": 0, "carts": []}

        # --- consolidate: many order lines -> one line per SKU, plus a ledger
        agg: dict[str, dict] = {}
        allocations: list[tuple] = []
        for l in lines:
            pid = l["product_id"]
            a = agg.setdefault(pid, {
                "product_id": pid, "name": l["name"],
                "merchant_id": str(l["merchant_id"]),
                "qty_required": 0,
                "expected_inr": l["price_inr"] or l["unit_price_inr"] or 0,
                "risk": float(l["risk"]), "bucket": l["bucket"] or "NORMAL",
                "confidence": l["confidence"] or "low", "value_at_risk": 0.0,
            })
            a["qty_required"] += l["qty"]
            # USD revenue depending on this line, used to weight pick order.
            a["value_at_risk"] += (l["unit_price_inr"] or 0) * l["qty"] / 88.0
            allocations.append((batch_id, pid, l["order_id"], l["qty"]))

        # --- rank: risky first, weighted by the money riding on the line
        ranked = sorted(agg.values(),
                        key=lambda a: -(a["risk"] * (1.0 + a["value_at_risk"])))
        for i, a in enumerate(ranked, 1):
            a["pick_rank"] = i

        carts = self._split_carts(ranked)
        cart_of = {l["product_id"]: c.cart_no for c in carts for l in c.lines}

        conn.execute("""INSERT INTO procurement_batches
            (batch_id,batch_date,state,n_orders,n_lines,planned_inr,operator)
            VALUES (?,?,?,?,?,?,?)""",
            (batch_id, batch_date, "SEALED", len(set(order_ids)), len(ranked),
             sum(a["expected_inr"] * a["qty_required"] for a in ranked), operator))

        conn.executemany("""INSERT INTO batch_lines
            (batch_id,product_id,merchant_id,qty_required,expected_inr,risk_score,
             risk_bucket,risk_confidence,value_at_risk,pick_rank,cart_no,state)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'PENDING')""",
            [(batch_id, a["product_id"], a["merchant_id"], a["qty_required"],
              a["expected_inr"], a["risk"], a["bucket"], a["confidence"],
              round(a["value_at_risk"], 2), a["pick_rank"],
              cart_of.get(a["product_id"], 1)) for a in ranked])

        conn.executemany("""INSERT INTO batch_allocations
            (batch_id,product_id,order_id,qty) VALUES (?,?,?,?)
            ON CONFLICT(batch_id,product_id,order_id)
            DO UPDATE SET qty = qty + excluded.qty""", allocations)

        conn.executemany("UPDATE orders SET batch_id=? WHERE order_id=?",
                         [(batch_id, oid) for oid in set(order_ids)])
        self._event(conn, batch_id, None, "batch_sealed", None,
                    f"{len(ranked)} lines / {len(carts)} carts", operator)
        conn.commit(); conn.close()

        return {"batch_id": batch_id, "n_orders": len(set(order_ids)),
                "n_lines": len(ranked), "n_carts": len(carts),
                "carts": [c.as_dict() for c in carts]}

    def _split_carts(self, ranked: list[dict]) -> list[Cart]:
        """Split by dark store first, then by per-cart line and value caps."""
        by_merchant: dict[str, list[dict]] = {}
        for a in ranked:
            key = a["merchant_id"] if self.cfg["split_by_merchant"] else "ALL"
            by_merchant.setdefault(key, []).append(a)

        carts: list[Cart] = []
        n = 0
        for merchant, items in sorted(by_merchant.items()):
            cur: Optional[Cart] = None
            for a in items:
                # Cap per-SKU quantity: q-commerce apps refuse large multiples.
                a["qty_required"] = min(a["qty_required"], self.cfg["max_qty_per_sku"])
                line_value = a["expected_inr"] * a["qty_required"]
                if (cur is None
                        or len(cur.lines) >= self.cfg["max_lines_per_cart"]
                        or cur.value_inr + line_value > self.cfg["max_value_per_cart_inr"]):
                    n += 1
                    cur = Cart(cart_no=n, merchant_id=merchant)
                    carts.append(cur)
                cur.lines.append(a)
        return carts

    # -- operator actions ----------------------------------------------------

    def mark_line(self, batch_id: str, product_id: str, state: str,
                  qty_bought: Optional[int] = None,
                  actual_inr: Optional[float] = None,
                  actor: str = "operator", note: str = "") -> dict:
        """Record what actually happened to one line at the shelf."""
        valid = {"IN_CART", "BOUGHT", "SHORT", "SUBSTITUTED", "SKIPPED", "RECEIVED"}
        if state not in valid:
            raise ValueError(f"state must be one of {sorted(valid)}")
        conn = connect(self.db_path)
        row = conn.execute("SELECT * FROM batch_lines WHERE batch_id=? AND product_id=?",
                           (batch_id, product_id)).fetchone()
        if not row:
            conn.close(); raise ValueError("no such line")

        qty = row["qty_required"] if qty_bought is None else qty_bought
        if state in ("BOUGHT", "RECEIVED") and qty < row["qty_required"]:
            state = "SHORT"

        conn.execute("""UPDATE batch_lines SET state=?, qty_bought=?, actual_inr=?,
                        note=? WHERE batch_id=? AND product_id=?""",
                     (state, qty, actual_inr, note, batch_id, product_id))
        self._event(conn, batch_id, product_id, f"marked_{state.lower()}",
                    row["state"], state, actor)
        conn.commit(); conn.close()
        return {"batch_id": batch_id, "product_id": product_id,
                "state": state, "qty_bought": qty}

    # -- reconciliation ------------------------------------------------------

    def reconcile(self, batch_id: str, actor: str = "ops") -> dict:
        """Allocate what was actually bought back to customer orders.

        COMPLETENESS-FIRST: scarce units go to the orders closest to being whole.
        One shippable order plus one refund beats three half-orders, because a
        half-order cannot move and still costs a parcel to hold.
        """
        conn = connect(self.db_path)
        lines = conn.execute("SELECT * FROM batch_lines WHERE batch_id=?",
                             (batch_id,)).fetchall()
        if not lines:
            conn.close(); raise ValueError("no such batch")

        bought = {l["product_id"]: (l["qty_bought"] or 0) for l in lines}

        # How short each order already is, so "closest to whole" is measurable.
        allocs = [dict(r) for r in conn.execute(
            "SELECT * FROM batch_allocations WHERE batch_id=?", (batch_id,))]
        order_need: dict[str, int] = {}
        for a in allocs:
            order_need[a["order_id"]] = order_need.get(a["order_id"], 0) + a["qty"]

        order_age = {r["order_id"]: r["created_at"] for r in conn.execute(
            "SELECT order_id, created_at FROM orders WHERE batch_id=?", (batch_id,))}

        by_product: dict[str, list[dict]] = {}
        for a in allocs:
            by_product.setdefault(a["product_id"], []).append(a)

        filled: dict[str, int] = {}
        for pid, rows in by_product.items():
            avail = bought.get(pid, 0)
            # Smallest orders first: they are cheapest to complete, so scarce
            # stock makes the largest number of shippable orders. Ties break on
            # age, so an older customer is never leapfrogged by an equal one.
            rows.sort(key=lambda a: (order_need.get(a["order_id"], 0),
                                     order_age.get(a["order_id"], "")))
            for a in rows:
                give = min(a["qty"], avail)
                avail -= give
                conn.execute("""UPDATE batch_allocations SET qty_filled=?
                                WHERE batch_id=? AND product_id=? AND order_id=?""",
                             (give, batch_id, pid, a["order_id"]))
                filled[a["order_id"]] = filled.get(a["order_id"], 0) + give

        # An order ships only if every one of its lines was filled in full.
        complete, short = [], []
        for oid, need in order_need.items():
            (complete if filled.get(oid, 0) >= need else short).append(oid)

        for oid in complete:
            conn.execute("UPDATE orders SET status='PROCURED' WHERE order_id=?", (oid,))
        for oid in short:
            conn.execute("UPDATE orders SET status='PROCUREMENT_SHORT' WHERE order_id=?", (oid,))

        conn.execute("UPDATE procurement_batches SET state='RECONCILE', actual_inr=? "
                     "WHERE batch_id=?",
                     (sum((l["actual_inr"] or l["expected_inr"] or 0) * (l["qty_bought"] or 0)
                          for l in lines), batch_id))
        self._event(conn, batch_id, None, "reconciled", None,
                    f"{len(complete)} complete / {len(short)} short", actor)
        conn.commit(); conn.close()
        return {"batch_id": batch_id, "complete": complete, "short": short,
                "orders_complete": len(complete), "orders_short": len(short)}

    def packout(self, batch_id: str) -> dict:
        """Per-customer bins: how goods bought in bulk come back apart."""
        conn = connect(self.db_path)
        rows = conn.execute("""
            SELECT a.order_id, a.product_id, a.qty, a.qty_filled, p.name, p.unit,
                   p.est_weight_g, c.email, c.ship_name, c.line1, c.city,
                   c.state, c.zip5
            FROM batch_allocations a
            JOIN products p USING(product_id)
            LEFT JOIN orders o ON o.order_id = a.order_id
            LEFT JOIN customers c ON c.customer_id = o.customer_id
            WHERE a.batch_id=? ORDER BY a.order_id""", (batch_id,)).fetchall()
        conn.close()

        bins: dict[str, dict] = {}
        for r in rows:
            b = bins.setdefault(r["order_id"], {
                "order_id": r["order_id"], "email": r["email"],
                "ship_to": {"name": r["ship_name"], "line1": r["line1"],
                            "city": r["city"], "state": r["state"], "zip5": r["zip5"]},
                "items": [], "weight_g": 0.0, "complete": True})
            b["items"].append({"product_id": r["product_id"], "name": r["name"],
                               "unit": r["unit"], "qty": r["qty"],
                               "qty_filled": r["qty_filled"]})
            b["weight_g"] += (r["est_weight_g"] or 0) * (r["qty_filled"] or 0)
            if (r["qty_filled"] or 0) < r["qty"]:
                b["complete"] = False
        for b in bins.values():
            b["weight_g"] = round(b["weight_g"], 1)
        return {"batch_id": batch_id, "bins": list(bins.values())}
