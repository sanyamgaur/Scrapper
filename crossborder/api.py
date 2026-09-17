"""FastAPI backend: the four engines behind one HTTP surface.

    GET  /api/catalog          browse listable SKUs (compliance-filtered)
    GET  /api/product/{id}     one SKU with live price, stock badge, shipping
    POST /api/quote            cart -> carrier comparison + landed cost
    POST /api/order            checkout, gated on live stock
    GET  /api/ops/summary      funnel counts for the ops dashboard
    GET  /api/ops/queue        clustered review queue
    POST /api/ops/decide       clear or kill a whole cluster
    GET  /api/ops/explain/{id} every rule that fired on one SKU

The storefront only ever sees ALLOWED SKUs. Compliance filtering happens in
SQL, not in the frontend, so a UI bug cannot list a blocked product.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .db import connect, DB_PATH
from .pricing import PricingEngine
from .shipping import ShippingEngine
from .stock import StockEngine

app = FastAPI(title="Cross-border Catalogue API", version="0.1.0")

WEB = Path(__file__).parent / "web"
pricing = PricingEngine()
shipping = ShippingEngine(usd_inr=pricing.usd_inr)
stock = StockEngine()


# ---------------------------------------------------------------- models ----

class CartLine(BaseModel):
    product_id: str
    qty: int = Field(default=1, ge=1, le=99)


class QuoteRequest(BaseModel):
    lines: list[CartLine]
    carrier: Optional[str] = None


class OrderRequest(BaseModel):
    lines: list[CartLine]
    carrier: Optional[str] = None
    customer_zip: str = ""


class DecisionRequest(BaseModel):
    cluster_id: str
    decision: str          # CLEARED | KILLED
    reviewer: str = "ops"
    note: str = ""


# ------------------------------------------------------------- helpers ------

def _fetch(pids: list[str]) -> dict[str, dict]:
    if not pids:
        return {}
    conn = connect()
    marks = ",".join("?" * len(pids))
    rows = conn.execute(
        f"""SELECT p.*, c.verdict, c.handling, c.reason, c.primary_rule
            FROM products p LEFT JOIN classifications c USING(product_id)
            WHERE p.product_id IN ({marks})""", pids).fetchall()
    conn.close()
    out = {}
    for r in rows:
        d = dict(r)
        d["price"] = d.get("price_inr")   # engines read `price`
        out[d["product_id"]] = d
    return out


def _handling_for(products: list[dict]) -> list[str]:
    flags: set[str] = set()
    for p in products:
        for f in (p.get("handling") or "").split(","):
            if f:
                flags.add(f)
    return sorted(flags)


# ------------------------------------------------------------ storefront ----

@app.get("/api/catalog")
def catalog(q: str = "", category: str = "", group: str = "",
            in_stock_only: bool = False, limit: int = Query(60, le=200),
            offset: int = 0) -> dict:
    """Listable catalogue only. The compliance filter is in the SQL on purpose."""
    conn = connect()
    where = ["c.verdict = 'ALLOWED'"]
    args: list[Any] = []
    if q:
        where.append("(p.name LIKE ? OR p.brand LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("p.category_name = ?"); args.append(category)
    if group:
        where.append("p.group_name = ?"); args.append(group)
    if in_stock_only:
        where.append("p.in_stock = 1")
    clause = " AND ".join(where)

    total = conn.execute(f"""SELECT COUNT(*) FROM products p
        JOIN classifications c USING(product_id) WHERE {clause}""", args).fetchone()[0]
    rows = conn.execute(f"""
        SELECT p.product_id,p.name,p.brand,p.unit,p.price_inr,p.mrp_inr,
               p.in_stock,p.image,p.category_name,p.group_name,p.est_weight_g
        FROM products p JOIN classifications c USING(product_id)
        WHERE {clause} ORDER BY p.in_stock DESC, p.name LIMIT ? OFFSET ?""",
        args + [limit, offset]).fetchall()
    conn.close()

    items = []
    for r in rows:
        d = dict(r)
        # A list price per SKU, computed at qty 1 so the grid shows real money.
        p = dict(d); p["price"] = d["price_inr"]
        lc, _ = pricing.price_single(p, 1)
        d["list_price_usd"] = lc.list_price_usd
        d["viable"] = lc.viable
        d["freight_ratio"] = lc.freight_ratio
        items.append(d)
    return {"total": total, "items": items}


@app.get("/api/facets")
def facets() -> dict:
    conn = connect()
    cats = [dict(r) for r in conn.execute("""
        SELECT p.category_name AS name, COUNT(*) n FROM products p
        JOIN classifications c USING(product_id) WHERE c.verdict='ALLOWED'
        GROUP BY 1 ORDER BY n DESC""")]
    groups = [dict(r) for r in conn.execute("""
        SELECT p.group_name AS name, COUNT(*) n FROM products p
        JOIN classifications c USING(product_id) WHERE c.verdict='ALLOWED'
        GROUP BY 1 ORDER BY n DESC LIMIT 60""")]
    conn.close()
    return {"categories": cats, "groups": groups}


@app.get("/api/product/{product_id}")
def product(product_id: str) -> dict:
    got = _fetch([product_id])
    if product_id not in got:
        raise HTTPException(404, "unknown product")
    p = got[product_id]
    if p.get("verdict") != "ALLOWED":
        raise HTTPException(403, {"reason": p.get("reason"), "rule": p.get("primary_rule")})
    lc, quote = pricing.price_single(p, 1, handling=_handling_for([p]))
    return {
        "product": p,
        "stock_badge": stock.badge(product_id),
        "pricing": lc.as_dict(),
        "shipping": quote.as_dict(),
    }


@app.post("/api/quote")
def quote(req: QuoteRequest) -> dict:
    got = _fetch([l.product_id for l in req.lines])
    lines = [(got[l.product_id], l.qty) for l in req.lines if l.product_id in got]
    if not lines:
        raise HTTPException(400, "no valid lines")
    blocked = [p["product_id"] for p, _ in lines if p.get("verdict") != "ALLOWED"]
    if blocked:
        raise HTTPException(403, f"not listable: {blocked}")
    handling = _handling_for([p for p, _ in lines])
    lc, cq = pricing.price_cart(lines, handling=handling, carrier_code=req.carrier)
    return {"pricing": lc.as_dict(), "shipping": cq.as_dict(), "handling": handling}


@app.post("/api/order")
def order(req: OrderRequest) -> JSONResponse:
    """Checkout. Live stock gate runs BEFORE any money is taken."""
    got = _fetch([l.product_id for l in req.lines])
    lines = [(got[l.product_id], l.qty) for l in req.lines if l.product_id in got]
    if not lines:
        raise HTTPException(400, "no valid lines")

    quoted = {p["product_id"]: p.get("price_inr") for p, _ in lines}
    gate = stock.gate_order([(p["product_id"], q) for p, q in lines], quoted)
    if not gate.accepted:
        return JSONResponse(status_code=409,
                            content={"accepted": False, "gate": gate.as_dict()})

    lc, cq = pricing.price_cart(lines, handling=_handling_for([p for p, _ in lines]),
                               carrier_code=req.carrier)
    oid = f"ORD-{uuid.uuid4().hex[:10].upper()}"
    conn = connect()
    conn.execute("""INSERT INTO orders (order_id,customer_zip,status,lines_json,
                    quote_json,total_usd,carrier) VALUES (?,?,?,?,?,?,?)""",
                 (oid, req.customer_zip, "STOCK_CONFIRMED",
                  json.dumps([{"product_id": p["product_id"], "qty": q} for p, q in lines]),
                  json.dumps(lc.as_dict()), lc.list_price_usd, lc.carrier))
    conn.commit(); conn.close()
    return JSONResponse({"accepted": True, "order_id": oid,
                         "total_usd": lc.list_price_usd, "carrier": lc.carrier,
                         "transit_days": list(lc.transit_days),
                         "gate": gate.as_dict()})


# ------------------------------------------------------------------ ops -----

@app.get("/api/ops/summary")
def ops_summary() -> dict:
    conn = connect()
    verdicts = {r["verdict"]: r["n"] for r in conn.execute(
        "SELECT verdict, COUNT(*) n FROM classifications GROUP BY 1")}
    dims = [dict(r) for r in conn.execute("""
        SELECT dimension, COUNT(*) n FROM fired_rules
        WHERE dimension IS NOT NULL AND dimension != '' GROUP BY 1 ORDER BY n DESC""")]
    queue = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM review_queue GROUP BY 1")}
    pending_skus = conn.execute(
        "SELECT COALESCE(SUM(sku_count),0) FROM review_queue WHERE status='PENDING'").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    orders = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    conn.close()
    return {"total_products": total, "verdicts": verdicts, "dimensions": dims,
            "queue": queue, "pending_review_skus": pending_skus, "orders": orders}


@app.get("/api/ops/queue")
def ops_queue(status: str = "PENDING", limit: int = Query(80, le=600)) -> dict:
    conn = connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM review_queue WHERE status=? ORDER BY sku_count DESC LIMIT ?",
        (status, limit))]
    conn.close()
    return {"clusters": rows}


@app.post("/api/ops/decide")
def ops_decide(req: DecisionRequest) -> dict:
    """One decision moves an entire cluster. This is what makes 21k tractable."""
    if req.decision not in ("CLEARED", "KILLED", "PENDING"):
        raise HTTPException(400, "decision must be CLEARED, KILLED or PENDING")
    conn = connect()
    cur = conn.execute("""UPDATE review_queue SET status=?, decided_by=?,
                          decided_at=datetime('now'), note=? WHERE cluster_id=?""",
                       (req.decision, req.reviewer, req.note, req.cluster_id))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(404, "unknown cluster")
    conn.commit()
    from .classify_run import _apply_cluster_decisions
    _apply_cluster_decisions(conn)
    moved = conn.execute("SELECT sku_count FROM review_queue WHERE cluster_id=?",
                         (req.cluster_id,)).fetchone()["sku_count"]
    conn.close()
    return {"ok": True, "cluster_id": req.cluster_id,
            "decision": req.decision, "skus_affected": moved}


@app.get("/api/ops/explain/{product_id}")
def ops_explain(product_id: str) -> dict:
    conn = connect()
    p = conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
    if not p:
        conn.close(); raise HTTPException(404, "unknown product")
    c = conn.execute("SELECT * FROM classifications WHERE product_id=?", (product_id,)).fetchone()
    rules = [dict(r) for r in conn.execute(
        "SELECT * FROM fired_rules WHERE product_id=?", (product_id,))]
    conn.close()
    return {"product": dict(p), "classification": dict(c) if c else None,
            "fired_rules": rules}


@app.get("/api/ops/blocked")
def ops_blocked(limit: int = Query(100, le=500)) -> dict:
    """What we refuse to sell and exactly why -- the compliance audit view."""
    conn = connect()
    rows = [dict(r) for r in conn.execute("""
        SELECT c.primary_rule, c.reason, c.authority, COUNT(*) n,
               GROUP_CONCAT(p.name, ' | ') AS samples
        FROM classifications c JOIN products p USING(product_id)
        WHERE c.verdict='BLOCKED' GROUP BY c.primary_rule ORDER BY n DESC LIMIT ?""",
        (limit,))]
    conn.close()
    for r in rows:
        r["samples"] = " | ".join((r["samples"] or "").split(" | ")[:4])
    return {"rules": rows}


# ------------------------------------------------------------------ pages ---

@app.get("/")
def storefront():
    return FileResponse(WEB / "storefront.html")


@app.get("/ops")
def ops_page():
    return FileResponse(WEB / "ops.html")
