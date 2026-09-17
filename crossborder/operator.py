"""Operator-assist: the buy flow that replaces the automation we cannot build.

WHY THERE IS NO AUTOMATED BUYER
-------------------------------
Placing a Blinkit order needs four things. The project holds none of them:

  1. An authenticated consumer account. `session_delhi.json` carries only
     gr_1_lat, gr_1_lon, gr_1_deviceId, __cf_bm, _cfuvid, _gid, _gcl_au, _fbp --
     geolocation, Cloudflare and analytics cookies. There is no account token.
  2. A cart write endpoint. discover.py captures three READ templates
     (tag_collections, listing, search). No cart or checkout call is observed,
     because an anonymous browsing session never makes one.
  3. A saved delivery address bound to that account.
  4. A payment authorization. Indian card and UPI payments require RBI-mandated
     two-factor authentication on every transaction. That factor is delivered to
     a human's device by design, and scripting around it is precisely what the
     regulation exists to prevent.

(1)-(3) are engineering problems. (4) is not: it cannot be automated without
either storing a payment credential and defeating 2FA, or obtaining a genuine
B2B/partner integration from Blinkit. Automating a consumer account against a
payment instrument also breaks their terms of service and puts the account and
its funds at risk.

So the honest answer is: build the operator's job down to a few taps, and leave
a clean seam for a partner API. That is what this module does.

WHAT REPLACES IT
----------------
A pick sheet ordered by stockout risk, one line at a time, with a deep link per
line so the operator never searches. Because no API confirms the purchase, the
system takes three independent, API-free confirmations:

  - PRICE ATTESTATION. The operator types the price actually shown. A mismatch
    against our expected price catches the wrong-pack-size mis-pick, which is the
    most common and most expensive error in this flow.
  - BILL RECONCILIATION. The Blinkit bill total must balance against the sum of
    attested line prices before the run can close.
  - PHYSICAL INTAKE. Goods are re-counted at the hub against the same ledger.

None of the three needs Blinkit's cooperation, and each catches a different class
of error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote_plus

from .db import connect, DB_PATH

BLINKIT = "https://blinkit.com"


def product_link(product_id: str, name: str = "") -> str:
    """Deep link to a product page.

    Blinkit's route is /prn/<slug>/prid/<id>; the slug is cosmetic and the id is
    what resolves, so a link is constructible from what we already store.
    """
    slug = "".join(ch if ch.isalnum() else "-" for ch in (name or "item").lower())
    slug = "-".join(filter(None, slug.split("-")))[:60] or "item"
    return f"{BLINKIT}/prn/{slug}/prid/{product_id}"


def search_link(name: str, brand: str = "") -> str:
    """Robust fallback if the /prn/ route ever changes.

    Brand is dropped when the name already starts with it, which is common in
    this catalogue and would otherwise double the term.
    """
    name = name or ""
    q = name if (brand and name.lower().startswith(brand.lower())) else f"{brand} {name}".strip()
    return f"{BLINKIT}/s/?q={quote_plus(q)}"


@dataclass
class PickLine:
    seq: int
    product_id: str
    name: str
    brand: str
    unit: str
    qty: int
    expected_inr: float
    risk_bucket: str
    risk_score: float
    risk_confidence: str
    state: str
    cart_no: int
    merchant_id: str
    product_url: str
    search_url: str
    image: Optional[str]
    for_customers: int          # how many US orders depend on this line
    priority_note: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class OperatorFlow:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path

    def pick_sheet(self, batch_id: str) -> dict:
        """The operator's screen: risky lines first, grouped by dark store."""
        conn = connect(self.db_path)
        rows = conn.execute("""
            SELECT bl.*, p.name, p.brand, p.unit, p.image,
                   (SELECT COUNT(DISTINCT order_id) FROM batch_allocations
                    WHERE batch_id=bl.batch_id AND product_id=bl.product_id) AS n_orders
            FROM batch_lines bl JOIN products p USING(product_id)
            WHERE bl.batch_id=? ORDER BY bl.cart_no, bl.pick_rank""",
            (batch_id,)).fetchall()
        batch = conn.execute("SELECT * FROM procurement_batches WHERE batch_id=?",
                             (batch_id,)).fetchone()
        conn.close()
        if not rows:
            return {"batch_id": batch_id, "carts": [], "done": 0, "total": 0}

        carts: dict[int, dict] = {}
        done = 0
        for i, r in enumerate(rows, 1):
            if r["state"] in ("BOUGHT", "SHORT", "SKIPPED", "SUBSTITUTED", "RECEIVED"):
                done += 1
            note = ""
            if r["risk_bucket"] == "CRITICAL":
                note = "Buy this first — likely to go out of stock"
            elif (r["n_orders"] or 0) > 1:
                note = f"{r['n_orders']} customers are waiting on this line"
            c = carts.setdefault(r["cart_no"], {
                "cart_no": r["cart_no"], "merchant_id": r["merchant_id"],
                "lines": [], "value_inr": 0.0})
            c["lines"].append(PickLine(
                seq=i, product_id=r["product_id"], name=r["name"],
                brand=r["brand"] or "", unit=r["unit"] or "",
                qty=r["qty_required"], expected_inr=r["expected_inr"] or 0,
                risk_bucket=r["risk_bucket"] or "NORMAL",
                risk_score=round(r["risk_score"] or 0, 2),
                risk_confidence=r["risk_confidence"] or "low",
                state=r["state"], cart_no=r["cart_no"],
                merchant_id=str(r["merchant_id"]),
                product_url=product_link(r["product_id"], r["name"]),
                search_url=search_link(r["name"], r["brand"] or ""),
                image=r["image"], for_customers=r["n_orders"] or 1,
                priority_note=note).as_dict())
            c["value_inr"] += (r["expected_inr"] or 0) * r["qty_required"]

        return {"batch_id": batch_id,
                "state": batch["state"] if batch else "?",
                "done": done, "total": len(rows),
                "carts": [{**c, "value_inr": round(c["value_inr"], 2)}
                          for c in sorted(carts.values(), key=lambda x: x["cart_no"])]}

    def attest_price(self, batch_id: str, product_id: str, shown_inr: float,
                     tolerance_pct: float = 15.0) -> dict:
        """Compare the price the operator sees against what we expected.

        A large gap usually means the wrong pack size is in the cart -- the
        cheapest error to catch here and the most expensive to catch later,
        because by then it has been paid for, packed and flown.
        """
        conn = connect(self.db_path)
        r = conn.execute("SELECT expected_inr FROM batch_lines "
                         "WHERE batch_id=? AND product_id=?",
                         (batch_id, product_id)).fetchone()
        conn.close()
        if not r:
            raise ValueError("no such line")
        expected = r["expected_inr"] or 0
        if not expected:
            return {"ok": True, "delta_pct": 0.0, "message": "no expected price on file"}
        delta = (shown_inr - expected) / expected * 100.0
        ok = abs(delta) <= tolerance_pct
        return {"ok": ok, "expected_inr": expected, "shown_inr": shown_inr,
                "delta_pct": round(delta, 1),
                "message": ("price matches" if ok else
                            f"₹{shown_inr:.0f} vs expected ₹{expected:.0f} "
                            f"({delta:+.0f}%) — check the pack size before buying")}

    def reconcile_bill(self, batch_id: str, cart_no: int,
                       bill_total_inr: float, tolerance_pct: float = 8.0) -> dict:
        """The bill must balance against attested lines before a run closes.

        Delivery fees, surge and instant-discounts move the total legitimately,
        hence a tolerance rather than an exact match.
        """
        conn = connect(self.db_path)
        rows = conn.execute("""SELECT qty_bought, actual_inr, expected_inr
            FROM batch_lines WHERE batch_id=? AND cart_no=?
            AND state IN ('BOUGHT','SHORT','RECEIVED','SUBSTITUTED')""",
            (batch_id, cart_no)).fetchall()
        conn.close()
        expected = sum((r["actual_inr"] or r["expected_inr"] or 0) * (r["qty_bought"] or 0)
                       for r in rows)
        if expected <= 0:
            return {"balanced": False, "expected_inr": 0, "bill_total_inr": bill_total_inr,
                    "message": "nothing marked bought in this cart yet"}
        delta = (bill_total_inr - expected) / expected * 100.0
        balanced = abs(delta) <= tolerance_pct
        return {"balanced": balanced, "expected_inr": round(expected, 2),
                "bill_total_inr": bill_total_inr, "delta_pct": round(delta, 1),
                "message": ("bill balances" if balanced else
                            f"bill is ₹{bill_total_inr:.0f} against ₹{expected:.0f} "
                            f"of attested lines ({delta:+.0f}%) — check for a missed "
                            f"or extra item")}
