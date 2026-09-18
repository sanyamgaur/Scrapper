"""Control-tower add-on: run every engine from one web page, no terminal.

This module is ADDITIVE. It imports the existing FastAPI `app` and attaches
new routes to it; it never edits api.py. Importing it (see run_console.py) is
all that is needed to wire everything up.

What it adds:
  - pipeline endpoints the CLI had but the API did not (ingest/classify/llm/risk)
  - a small set of DEMO-ONLY helpers so an end-to-end funnel can be shown live
    without a running scraper: prime live stock, seed sample orders, and
    simulate an operator's buying run
  - the cart run (see cart.py): the batch's lines become one Blinkit cart per
    store, added item by item with a live per-item cap, ending in one link
  - GET /console -> a single page with a button for every step

Everything here drives the SAME engines and the SAME crossborder.db the rest of
the system uses. Nothing is mocked. The demo helpers are clearly labelled and
only write rows the real flow would also write (stock_checks, orders,
order_lines, batch_lines), so a demo leaves the database in a real, inspectable
state.
"""

from __future__ import annotations

import json
import random
import uuid
from pathlib import Path
from typing import Optional

from fastapi.responses import FileResponse
from pydantic import BaseModel

from .api import app, pricing, procurement, risk, restock, drift
from .db import connect
from . import cart                      # noqa: F401  (attaches /api/ops/cart/*)

WEB = Path(__file__).parent / "web"


# --------------------------------------------------------------- models -----

class IngestReq(BaseModel):
    csv: str = "inventory_delhi.csv"


class LlmReq(BaseModel):
    limit: Optional[int] = None


class PrimeReq(BaseModel):
    # Record a live stock reading for every ALLOWED SKU, mirroring the crawl
    # snapshot's in_stock flag. This is what lets the checkout gate pass in a
    # demo: without any stock_checks row, every order fails closed (by design).
    only_allowed: bool = True


class SeedReq(BaseModel):
    n_orders: int = 4
    items_per_order: int = 3


class SimulateReq(BaseModel):
    batch_id: str
    short_one: bool = True     # leave one high-qty line one unit short, to show
                               # completeness-first reconciliation doing its job


# ----------------------------------------------------- pipeline endpoints ---

@app.post("/api/ops/pipeline/ingest")
def pipeline_ingest(req: IngestReq) -> dict:
    from .ingest import ingest_csv
    try:
        n = ingest_csv(req.csv)
    except FileNotFoundError:
        return {"ok": False, "error": f"CSV not found: {req.csv}"}
    return {"ok": True, "ingested": n}


@app.post("/api/ops/pipeline/classify")
def pipeline_classify() -> dict:
    from .classify_run import run
    s = run()
    return {"ok": True, **s}


@app.post("/api/ops/pipeline/llm")
def pipeline_llm(req: LlmReq) -> dict:
    from .llm import apply_to_db
    r = apply_to_db(limit=req.limit)
    r["ok"] = True
    if not r.get("classified"):
        r["note"] = ("No verdicts written. Set ANTHROPIC_API_KEY and "
                     "`pip install anthropic` to classify the no-rule tail.")
    return r


@app.post("/api/ops/pipeline/risk")
def pipeline_risk() -> dict:
    return {"ok": True, **risk.score_all()}


@app.post("/api/ops/pipeline/restock")
def pipeline_restock() -> dict:
    return {"ok": True, **restock.detect()}


@app.post("/api/ops/pipeline/drift")
def pipeline_drift(limit: int = 500) -> dict:
    return {"ok": True, **drift.sweep(limit=limit)}


# -------------------------------------------------------- demo endpoints ----

@app.post("/api/ops/demo/prime-stock")
def demo_prime_stock(req: PrimeReq) -> dict:
    """Write a fresh live stock reading for listable SKUs.

    Mirrors each SKU's crawl in_stock flag into stock_checks with source
    'demo-prime'. In-stock items then pass the checkout gate; out-of-stock
    items still (correctly) block. This is the one thing a live demo needs
    that a running availability engine would otherwise provide.
    """
    conn = connect()
    where = "WHERE c.verdict='ALLOWED'" if req.only_allowed else ""
    rows = conn.execute(
        f"""SELECT p.product_id, p.in_stock, p.price_inr FROM products p
            JOIN classifications c USING(product_id) {where}""").fetchall()
    conn.executemany(
        "INSERT INTO stock_checks (product_id,in_stock,price_inr,source) "
        "VALUES (?,?,?,'demo-prime')",
        [(r["product_id"], int(bool(r["in_stock"])), r["price_inr"]) for r in rows])
    conn.commit()
    in_stock = sum(1 for r in rows if r["in_stock"])
    conn.close()
    return {"ok": True, "primed": len(rows), "in_stock": in_stock,
            "note": "Checkout will now succeed for in-stock items."}


@app.post("/api/ops/demo/seed-orders")
def demo_seed_orders(req: SeedReq) -> dict:
    """Create sample US orders so the fulfilment funnel has something to batch.

    Picks listable, in-stock SKUs and deliberately shares one SKU across
    several orders, so consolidation and short-allocation are visible. Writes
    real customers/orders/order_lines rows, exactly as a storefront checkout
    would. Idempotent-safe: each call makes new orders with fresh ids.
    """
    conn = connect()
    pool = [dict(r) for r in conn.execute(
        """SELECT p.product_id, p.price_inr, p.merchant_id FROM products p
           JOIN classifications c USING(product_id)
           WHERE c.verdict='ALLOWED' AND p.in_stock=1 AND p.price_inr>0
           ORDER BY p.price_inr DESC LIMIT 400""")]
    if len(pool) < req.items_per_order + 1:
        conn.close()
        return {"ok": False, "error": "not enough listable in-stock SKUs; "
                "run classify (and prime stock) first"}

    rng = random.Random(len(pool))                 # deterministic for demos
    shared = pool[0]                                # one SKU every order shares
    cities = [("New York", "NY", "10001"), ("Austin", "TX", "78701"),
              ("Seattle", "WA", "98101"), ("Miami", "FL", "33101"),
              ("Chicago", "IL", "60601")]

    created = []
    for k in range(req.n_orders):
        picks = [shared] + rng.sample(pool[1:], max(0, req.items_per_order - 1))
        city, st, zip5 = cities[k % len(cities)]
        email = f"demo{k+1}@example.com"
        cid = f"CUST-{uuid.uuid5(uuid.NAMESPACE_DNS, email).hex[:10].upper()}"
        oid = f"ORD-{uuid.uuid4().hex[:10].upper()}"
        conn.execute(
            """INSERT INTO customers (customer_id,email,name,ship_name,line1,city,state,zip5)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(customer_id) DO UPDATE SET city=excluded.city""",
            (cid, email, f"Demo Customer {k+1}", f"Demo Customer {k+1}",
             f"{100+k} Main St", city, st, zip5))
        lines = [(p["product_id"], 1) for p in picks]   # one of each; the shared
                                                          # SKU then consolidates to qty = n_orders
        conn.execute(
            """INSERT INTO orders (order_id,customer_id,customer_zip,status,lines_json,total_usd)
               VALUES (?,?,?,?,?,?)""",
            (oid, cid, zip5, "STOCK_CONFIRMED",
             json.dumps([{"product_id": pid, "qty": q} for pid, q in lines]), 0.0))
        conn.executemany(
            "INSERT OR REPLACE INTO order_lines (order_id,product_id,qty,unit_price_inr) "
            "VALUES (?,?,?,?)",
            [(oid, pid, q, next(pp["price_inr"] for pp in picks if pp["product_id"] == pid))
             for pid, q in lines])
        created.append(oid)
    conn.commit()
    conn.close()
    return {"ok": True, "orders": created, "shared_sku": shared["product_id"],
            "note": f"{len(created)} orders created, all sharing one SKU."}


@app.post("/api/ops/demo/simulate-buys")
def demo_simulate_buys(req: SimulateReq) -> dict:
    """Mark a batch's lines as bought, as if the operator finished the run.

    Optionally leaves the single highest-quantity line one unit short, so the
    reconcile step visibly produces one complete order and one short order
    rather than everything succeeding.
    """
    conn = connect()
    lines = [dict(r) for r in conn.execute(
        "SELECT product_id, qty_required FROM batch_lines WHERE batch_id=? "
        "ORDER BY qty_required DESC", (req.batch_id,))]
    conn.close()
    if not lines:
        return {"ok": False, "error": f"no such batch or no lines: {req.batch_id}"}
    short_pid = lines[0]["product_id"] if (req.short_one and lines[0]["qty_required"] > 1) else None
    bought = shorted = 0
    for l in lines:
        qty = l["qty_required"] - 1 if l["product_id"] == short_pid else l["qty_required"]
        procurement.mark_line(req.batch_id, l["product_id"], "BOUGHT",
                              qty_bought=qty, actor="demo")
        if qty < l["qty_required"]:
            shorted += 1
        else:
            bought += 1
    return {"ok": True, "bought": bought, "short": shorted,
            "short_sku": short_pid,
            "note": "Now Reconcile to see completeness-first allocation."}


# ------------------------------------------------------- funnel snapshot ----

@app.get("/api/ops/console/state")
def console_state() -> dict:
    """One call the console polls for the live funnel strip and status lights."""
    conn = connect()
    q = lambda s, *a: conn.execute(s, a).fetchone()[0]
    verdicts = {r["verdict"]: r["n"] for r in conn.execute(
        "SELECT verdict, COUNT(*) n FROM classifications GROUP BY 1")}
    state = {
        "products": q("SELECT COUNT(*) FROM products"),
        "verdicts": verdicts,
        "listable_in_stock": q(
            "SELECT COUNT(*) FROM products p JOIN classifications c USING(product_id) "
            "WHERE c.verdict='ALLOWED' AND p.in_stock=1"),
        "queue_pending_clusters": q(
            "SELECT COUNT(*) FROM review_queue WHERE status='PENDING'"),
        "queue_pending_skus": q(
            "SELECT COALESCE(SUM(sku_count),0) FROM review_queue WHERE status='PENDING'"),
        "stock_checks": q("SELECT COUNT(*) FROM stock_checks"),
        "risk_scored": q("SELECT COUNT(*) FROM stockout_risk"),
        "risk_confidence": (conn.execute(
            "SELECT confidence FROM stockout_risk LIMIT 1").fetchone() or [None])[0],
        "orders": q("SELECT COUNT(*) FROM orders"),
        "orders_unbatched": q(
            "SELECT COUNT(*) FROM orders WHERE batch_id IS NULL"),
        "batches": q("SELECT COUNT(*) FROM procurement_batches"),
        "drift_recorded": q("SELECT COUNT(*) FROM price_drift"),
        "relist_ready": q("SELECT COUNT(*) FROM relist_queue WHERE status='READY'"),
    }
    # Cart runs live in tables cart.py creates on first use, so the funnel has
    # to survive their absence on a database that has never built one.
    try:
        state["cart_runs_open"] = q(
            "SELECT COUNT(*) FROM cart_sessions WHERE status='OPEN'")
        state["cart_items_over_limit"] = q(
            """SELECT COUNT(*) FROM cart_session_items
               WHERE qty_added > max_qty""")
    except Exception:
        state["cart_runs_open"] = state["cart_items_over_limit"] = 0
    state["batch_list"] = [dict(r) for r in conn.execute(
        "SELECT batch_id, state, n_orders, n_lines FROM procurement_batches "
        "ORDER BY batch_date DESC LIMIT 8")]
    conn.close()
    return state


# --------------------------------------------------------------- the page ---

@app.get("/console")
def console_page():
    return FileResponse(WEB / "console.html")
