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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .basket import BasketBuilder
from .db import connect, DB_PATH
from .images import cache_path
from .operator import OperatorFlow
from .pricedrift import PriceDriftEngine
from .pricing import PricingEngine
from .procurement import ProcurementEngine
from .restock import RestockEngine
from .shipping import ShippingEngine
from .stock import StockEngine
from .stockout_risk import StockoutRiskEngine

app = FastAPI(title="Cross-border Catalogue API", version="0.1.0")

WEB = Path(__file__).parent / "web"
pricing = PricingEngine()
shipping = ShippingEngine(usd_inr=pricing.usd_inr)
stock = StockEngine()
basket = BasketBuilder(pricing=pricing)
procurement = ProcurementEngine()
operator = OperatorFlow()
restock = RestockEngine()
risk = StockoutRiskEngine()
drift = PriceDriftEngine(pricing=pricing)


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
    email: str = ""
    name: str = ""
    address1: str = ""
    city: str = ""
    state: str = ""


class BasketRequest(BaseModel):
    lines: list[CartLine]
    carrier: Optional[str] = None


class BatchRequest(BaseModel):
    batch_date: str
    operator: str = "ops"


class MarkLineRequest(BaseModel):
    batch_id: str
    product_id: str
    state: str
    qty_bought: Optional[int] = None
    actual_inr: Optional[float] = None
    actor: str = "operator"


class AttestRequest(BaseModel):
    batch_id: str
    product_id: str
    shown_inr: float


class BillRequest(BaseModel):
    batch_id: str
    cart_no: int
    bill_total_inr: float


class RelistDecision(BaseModel):
    product_id: str
    decision: str
    actor: str = "ops"
    note: str = ""


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

# Sort keys are whitelisted rather than interpolated: `sort` arrives from the
# query string and would otherwise be an injection point in the ORDER BY.
SORT_SQL = {
    # Category-NEUTRAL by design. An earlier version ranked food categories
    # first, which misrepresented the catalogue: only 33% of listable SKUs are
    # food, while Household Essentials alone is 64% (Home & Lifestyle 2,923,
    # Stationery & Games 1,024). This is a general store, not a grocer.
    #
    # Ranking by discount PERCENT surfaced cheap items with big markdowns, and
    # by value-per-gram it surfaced ball pens. Absolute rupee saving favours
    # substantial products with real markdowns, in whatever department they
    # happen to sit, and "biggest savings" is a claim the data can back.
    "relevance":  ("p.in_stock DESC, "
                   "(COALESCE(p.mrp_inr,0) - p.price_inr) DESC, "
                   "COALESCE(p.discount_pct,0) DESC, p.name"),
    "price_asc":  "p.in_stock DESC, p.price_inr ASC",
    "price_desc": "p.in_stock DESC, p.price_inr DESC",
    "name":       "p.name",
    "light":      "p.in_stock DESC, p.est_weight_g ASC",   # cheapest to ship
    "value":      "p.in_stock DESC, (p.price_inr / NULLIF(p.est_weight_g,0)) DESC",
}


@app.get("/api/catalog")
def catalog(q: str = "", category: str = "", group: str = "", brand: str = "",
            in_stock_only: bool = False, sort: str = "relevance",
            min_usd: float = 0, max_usd: float = 0,
            limit: int = Query(60, le=200), offset: int = 0) -> dict:
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
    if brand:
        where.append("p.brand = ?"); args.append(brand)
    if in_stock_only:
        where.append("p.in_stock = 1")
    clause = " AND ".join(where)
    order = SORT_SQL.get(sort, SORT_SQL["relevance"])

    # Collapse same-name variants to one card, keeping the cheapest. The
    # catalogue carries the same product under several ids (five "Limestone
    # Analog Watch" rows at four prices), and they clustered at the top of the
    # savings sort — the homepage opened with the same watch twice. The subquery
    # is aliased `p` so the ORDER BY clauses above keep working unchanged.
    dedup = f"""
        SELECT p.product_id,p.name,p.brand,p.unit,p.price_inr,p.mrp_inr,
               p.in_stock,p.image,p.category_name,p.group_name,p.est_weight_g,
               p.discount_pct,
               ROW_NUMBER() OVER (PARTITION BY p.name
                                  ORDER BY p.in_stock DESC, p.price_inr ASC) AS rn
        FROM products p JOIN classifications c USING(product_id)
        WHERE {clause}"""

    total = conn.execute(
        f"SELECT COUNT(*) FROM ({dedup}) p WHERE p.rn = 1", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM ({dedup}) p WHERE p.rn = 1 ORDER BY {order} LIMIT ? OFFSET ?",
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
        if min_usd and d["list_price_usd"] < min_usd:
            continue
        if max_usd and d["list_price_usd"] > max_usd:
            continue
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
    brands = [dict(r) for r in conn.execute("""
        SELECT p.brand AS name, COUNT(*) n FROM products p
        JOIN classifications c USING(product_id)
        WHERE c.verdict='ALLOWED' AND p.brand IS NOT NULL AND p.brand != ''
        GROUP BY 1 ORDER BY n DESC LIMIT 80""")]
    conn.close()
    return {"categories": cats, "groups": groups, "brands": brands}


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

    # A shipping label needs a person, not a zip code. Orders placed without
    # customer details are still accepted, but they are recorded as such rather
    # than silently producing an unshippable order later.
    cid = None
    if req.email:
        cid = f"CUST-{uuid.uuid5(uuid.NAMESPACE_DNS, req.email).hex[:10].upper()}"
        conn.execute("""INSERT INTO customers
            (customer_id,email,name,ship_name,line1,city,state,zip5)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(customer_id) DO UPDATE SET
                name=excluded.name, ship_name=excluded.ship_name,
                line1=excluded.line1, city=excluded.city,
                state=excluded.state, zip5=excluded.zip5""",
            (cid, req.email, req.name, req.name, req.address1,
             req.city, req.state, req.customer_zip))

    conn.execute("""INSERT INTO orders (order_id,customer_id,customer_zip,status,
                    lines_json,quote_json,total_usd,carrier)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (oid, cid, req.customer_zip, "STOCK_CONFIRMED",
                  json.dumps([{"product_id": p["product_id"], "qty": q} for p, q in lines]),
                  json.dumps(lc.as_dict()), lc.list_price_usd, lc.carrier))

    # Normalized lines: a physical unit cannot be allocated against a JSON blob.
    conn.executemany("""INSERT OR REPLACE INTO order_lines
        (order_id,product_id,qty,unit_price_inr) VALUES (?,?,?,?)""",
        [(oid, p["product_id"], q, p.get("price_inr")) for p, q in lines])
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


@app.get("/api/product/{product_id}/related")
def related(product_id: str, limit: int = Query(8, le=24)) -> dict:
    """Same shelf, in stock, listable — and cheap to add to an existing parcel.

    Ordered by value density, because on this lane the useful "you might also
    like" is the one that improves the customer's freight ratio rather than the
    one an engagement metric would pick.
    """
    conn = connect()
    me = conn.execute("SELECT group_name, category_name, merchant_id "
                      "FROM products WHERE product_id=?", (product_id,)).fetchone()
    if not me:
        conn.close(); raise HTTPException(404, "unknown product")
    rows = [dict(r) for r in conn.execute("""
        SELECT p.product_id,p.name,p.brand,p.unit,p.price_inr,p.in_stock,p.image,
               p.est_weight_g,p.group_name
        FROM products p JOIN classifications c USING(product_id)
        WHERE c.verdict='ALLOWED' AND p.product_id != ?
          AND (p.group_name = ? OR p.category_name = ?)
          AND p.price_inr > 0 AND p.est_weight_g > 0
        ORDER BY p.in_stock DESC,
                 (p.group_name = ?) DESC,
                 (p.price_inr / p.est_weight_g) DESC
        LIMIT ?""", (product_id, me["group_name"], me["category_name"],
                     me["group_name"], limit))]
    conn.close()
    for r in rows:
        pr = dict(r); pr["price"] = r["price_inr"]
        try:
            lc, _ = pricing.price_single(pr, 1)
            r["list_price_usd"] = lc.list_price_usd
        except Exception:
            r["list_price_usd"] = None
    return {"items": rows}


@app.get("/api/order/{order_id}")
def order_status(order_id: str) -> dict:
    """Where an order actually is. The states are the real procurement ladder,
    not a decorative progress bar."""
    conn = connect()
    o = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if not o:
        conn.close(); raise HTTPException(404, "unknown order")
    o = dict(o)
    lines = [dict(r) for r in conn.execute("""
        SELECT ol.product_id, ol.qty, p.name, p.unit, p.image,
               COALESCE(a.qty_filled, 0) AS qty_filled
        FROM order_lines ol JOIN products p USING(product_id)
        LEFT JOIN batch_allocations a
          ON a.order_id = ol.order_id AND a.product_id = ol.product_id
        WHERE ol.order_id=?""", (order_id,))]
    conn.close()
    o["lines"] = lines
    o["quote"] = json.loads(o.get("quote_json") or "{}")
    # A customer-readable stage, derived from the internal status.
    stage = {
        "PLACED": 1, "STOCK_CONFIRMED": 1, "PROCURED": 2,
        "PROCUREMENT_SHORT": 2, "PACKED": 3, "SHIPPED": 4, "DELIVERED": 5,
    }.get(o["status"], 1)
    o["stage"] = stage
    o["stage_labels"] = ["Order placed", "Buying in Delhi", "Packed",
                         "In transit to the US", "Delivered"]
    return o


# ------------------------------------------------------------- basket ------

@app.post("/api/basket/advise")
def basket_advise(req: BasketRequest) -> dict:
    """Freight-aware add-on suggestions for a cart."""
    got = _fetch([l.product_id for l in req.lines])
    lines = [(got[l.product_id], l.qty) for l in req.lines if l.product_id in got]
    if not lines:
        raise HTTPException(400, "no valid lines")
    handling = _handling_for([p for p, _ in lines])
    return basket.advise(lines, handling=handling, carrier_code=req.carrier).as_dict()


# -------------------------------------------------------- procurement ------

@app.post("/api/ops/batch/build")
def batch_build(req: BatchRequest) -> dict:
    try:
        return procurement.build_batch(req.batch_date, operator=req.operator)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/ops/batch/{batch_id}/picksheet")
def batch_picksheet(batch_id: str) -> dict:
    return operator.pick_sheet(batch_id)


@app.post("/api/ops/batch/mark")
def batch_mark(req: MarkLineRequest) -> dict:
    try:
        return procurement.mark_line(req.batch_id, req.product_id, req.state,
                                     qty_bought=req.qty_bought,
                                     actual_inr=req.actual_inr, actor=req.actor)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/ops/batch/attest")
def batch_attest(req: AttestRequest) -> dict:
    try:
        return operator.attest_price(req.batch_id, req.product_id, req.shown_inr)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/ops/batch/bill")
def batch_bill(req: BillRequest) -> dict:
    """The Blinkit bill must balance against attested lines before a run closes."""
    return operator.reconcile_bill(req.batch_id, req.cart_no, req.bill_total_inr)


@app.post("/api/ops/batch/{batch_id}/reconcile")
def batch_reconcile(batch_id: str) -> dict:
    try:
        return procurement.reconcile(batch_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/ops/batch/{batch_id}/packout")
def batch_packout(batch_id: str) -> dict:
    return procurement.packout(batch_id)


@app.get("/api/ops/batches")
def batches() -> dict:
    conn = connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM procurement_batches ORDER BY batch_date DESC LIMIT 30")]
    conn.close()
    return {"batches": rows}


# --------------------------------------------------------------- risk ------

@app.post("/api/ops/risk/score")
def risk_score() -> dict:
    return risk.score_all()


@app.get("/api/ops/risk")
def risk_list(bucket: str = "CRITICAL", limit: int = Query(50, le=300)) -> dict:
    conn = connect()
    rows = [dict(r) for r in conn.execute("""
        SELECT s.*, p.name, p.group_name, p.in_stock FROM stockout_risk s
        JOIN products p USING(product_id)
        JOIN classifications c USING(product_id)
        WHERE s.bucket=? AND c.verdict='ALLOWED'
        ORDER BY s.score DESC LIMIT ?""", (bucket, limit))]
    conn.close()
    return {"bucket": bucket, "items": rows}


# ------------------------------------------------------------ restock ------

@app.post("/api/ops/restock/detect")
def restock_detect() -> dict:
    return restock.detect()


@app.get("/api/ops/restock")
def restock_list(limit: int = Query(50, le=300)) -> dict:
    return {"candidates": [c.as_dict() for c in restock.ready(limit)]}


@app.post("/api/ops/restock/decide")
def restock_decide(req: RelistDecision) -> dict:
    try:
        return restock.decide(req.product_id, req.decision, req.actor, req.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


# -------------------------------------------------------- price drift ------

@app.post("/api/ops/drift/sweep")
def drift_sweep(limit: Optional[int] = None) -> dict:
    return drift.sweep(limit=limit)


@app.get("/api/ops/drift")
def drift_list(limit: int = Query(50, le=300)) -> dict:
    conn = connect()
    rows = [dict(r) for r in conn.execute("""
        SELECT d.*, p.name FROM price_drift d JOIN products p USING(product_id)
        WHERE d.action != 'IGNORED' ORDER BY d.detected_at DESC LIMIT ?""", (limit,))]
    conn.close()
    return {"drift": rows}


@app.get("/img/{product_id}")
def product_image(product_id: str):
    """Serve a product image from our own origin.

    Falls back to redirecting at the source CDN when the local cache has not
    been warmed, so the storefront works immediately and simply gets faster as
    `crossborder.cli images` fills the cache.
    """
    conn = connect()
    row = conn.execute("SELECT image FROM products WHERE product_id=?",
                       (product_id,)).fetchone()
    conn.close()
    url = row["image"] if row else None
    if not url:
        raise HTTPException(404, "no image")
    local = cache_path(url)
    if local:
        return FileResponse(local, headers={"Cache-Control": "public, max-age=604800"})
    return RedirectResponse(url, status_code=307)


# ------------------------------------------------------------------ pages ---

# The storefront is a small ES-module app rather than one large HTML file, so
# views can be edited independently. Mounted before the routes below so its
# assets resolve without a catch-all rewrite.
app.mount("/app", StaticFiles(directory=str(WEB / "app")), name="app")


@app.get("/")
def storefront():
    return FileResponse(WEB / "app" / "index.html")


@app.get("/legacy")
def storefront_legacy():
    """The original single-file prototype, kept for reference."""
    return FileResponse(WEB / "storefront.html")


@app.get("/ops")
def ops_page():
    return FileResponse(WEB / "ops.html")


@app.get("/operator")
def operator_page():
    return FileResponse(WEB / "operator.html")
