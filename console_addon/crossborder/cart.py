"""One Blinkit cart per store, built item by item, with a live per-item cap.

Before this module the buy sheet handed the operator a page of independent
product links: one link per SKU, each opened on its own, with nothing tying the
run together and nothing saying how many units of a given SKU Blinkit will
actually accept. Two things went wrong with that. Blinkit's cart is
session-side -- a link never carries a quantity, so the quantity on the sheet
was advisory at best -- and every SKU has its own ceiling (stock on hand, a
per-item cart cap, the money and weight a single cart may carry), so an
operator could tap "+" past the point where the order stops being fillable and
only find out at checkout.

What this module adds instead is a *cart run*: the batch's lines are
consolidated into one ordered walk per store, the operator adds one item at a
time in a single Blinkit tab, each add is recorded against that item's own
ceiling, and the run ends with one permalink listing everything that went in
plus the link to the Blinkit cart itself.

The pieces:

  cap_for()        the per-item ceiling. Live from Blinkit when a scraper
                   session is configured, else cached, else derived from the
                   data the system already holds. Always says which input bound
                   it, so the number is arguable rather than magic.
  plan_batch()     batch lines -> one cart run per cart, riskiest item first,
                   each line carrying its cap and how many passes it needs.
  the /api/ops/cart/* endpoints  drive the run and flag an over-limit quantity
                   the moment it is typed.
  GET /cart-run/{id}  the final link: every product added, with quantity.
                   (namespaced away from the storefront's own /cart)

Additive, like the rest of the add-on: it creates its own three tables on
first use and never touches the schema the main system owns.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .api import app
from .db import connect

WEB = Path(__file__).parent / "web"

BLINKIT = "https://blinkit.com"
BLINKIT_CART_URL = f"{BLINKIT}/cart"

# ---- the caps a single cart is subject to -----------------------------------
# These mirror the rules the batch splitter already enforces, so the number the
# operator sees on screen is the same number the batch was built against.
PER_SKU_CART_CAP = 6          # <=6 units of one SKU per cart (batch rule)
CART_VALUE_CAP_INR = 15_000   # <=Rs.15k per cart (batch rule)
MAX_PARCEL_G = 20_000         # a single onward parcel; caps heavy/bulky packs
RISK_CAP = {"CRITICAL": 2, "HIGH": 4, "NORMAL": PER_SKU_CART_CAP}
LIMIT_TTL_S = 6 * 3600        # how long a cap Blinkit gave us stays fresh
FALLBACK_TTL_S = 10 * 60      # a derived cap is a stand-in, so it expires fast
                              # and the next plan gets another go at the live one
LIVE_PROBE_BUDGET_S = 10.0    # per plan call; a cart of 40 lines must not turn
                              # into 40 sequential round trips to Blinkit

SCHEMA = """
CREATE TABLE IF NOT EXISTS cart_limits (
    product_id  TEXT PRIMARY KEY,
    max_qty     INTEGER NOT NULL,
    source      TEXT,          -- live | derived
    reason      TEXT,          -- which input bound the cap
    detail_json TEXT,          -- every candidate cap, for the tooltip
    computed_at INTEGER
);
CREATE TABLE IF NOT EXISTS cart_sessions (
    cart_session_id TEXT PRIMARY KEY,
    batch_id    TEXT,
    cart_no     INTEGER,
    merchant_id TEXT,
    status      TEXT,          -- OPEN | FINALIZED
    created_at  INTEGER,
    finalized_at INTEGER
);
CREATE TABLE IF NOT EXISTS cart_session_items (
    cart_session_id TEXT,
    product_id  TEXT,
    seq         INTEGER,
    name        TEXT,
    qty_planned INTEGER,
    qty_added   INTEGER DEFAULT 0,
    max_qty     INTEGER,
    limit_source TEXT,
    limit_reason TEXT,
    risk_bucket TEXT,
    unit_price_inr REAL,
    status      TEXT,          -- PENDING | ADDED | SHORT
    updated_at  INTEGER,
    PRIMARY KEY (cart_session_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_cart_items_seq
    ON cart_session_items(cart_session_id, seq);
"""


# ------------------------------------------------------------- schema glue ---

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _cols(conn: sqlite3.Connection, table: str) -> set:
    """Column names of `table`, or an empty set if it does not exist.

    The add-on ships separately from the schema it reads, and the main project
    has renamed columns before. Every query below is built from what is
    actually there, so a rename degrades into a missing optional input (a cap
    that stops considering weight, say) instead of a 500.
    """
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _pick(cols: set, *candidates: str) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


# ------------------------------------------------------------------- links ---

def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or "item"


def product_url(product_id: str, name: str) -> str:
    return f"{BLINKIT}/prn/{slug(name)}/prid/{product_id}"


def search_url(name: str) -> str:
    return f"{BLINKIT}/s/?q=" + urllib.parse.quote((name or "")[:80])


# ------------------------------------------------------- the per-item cap ----

def _live_cap(product_id: str, name: str) -> Optional[dict]:
    """Ask Blinkit how many of this SKU it will take, if we can reach it.

    Blinkit states a per-item ceiling in the product payload itself
    (`max_quantity`, or `inventory`/`available_quantity` when the shelf is
    nearly empty). The scraper session captured by discover.py is what makes
    that payload reachable, so this only runs when one is configured via
    BLINKIT_SESSION, and it fails quiet: outside India the endpoint refuses,
    and a refused probe simply falls through to the derived cap.
    """
    path = os.environ.get("BLINKIT_SESSION", "")
    if not path or not Path(path).exists():
        return None
    try:
        import httpx
    except ImportError:
        return None
    try:
        session = json.loads(Path(path).read_text())
        tmpl = (session.get("templates") or {}).get("search")
        if not tmpl:
            return None
        headers = dict(tmpl["headers"])
        headers["content-type"] = "application/json"
        headers["lat"], headers["lon"] = str(session["lat"]), str(session["lon"])
        url = (f"{BLINKIT}/v1/layout/search?q="
               f"{urllib.parse.quote((name or product_id)[:60])}"
               "&search_type=type_to_search")
        r = httpx.post(url, json={}, headers=headers,
                       cookies=session.get("cookies") or {}, timeout=6.0)
        if r.status_code != 200:
            return None
        cap = _find_cap(r.json(), str(product_id))
    except Exception:
        return None
    if cap is None:
        return None
    return {"max_qty": int(cap), "source": "live",
            "reason": "Blinkit's own per-item cap"}


def _find_cap(obj: Any, product_id: str, depth: int = 0) -> Optional[int]:
    """Walk a search payload for the cap Blinkit publishes on one product.

    Same shape-agnostic approach the scraper's parser takes: the snippet tree
    moves between releases, so rather than pin a path we look for any object
    that identifies this product and read whichever ceiling key it carries.
    """
    if depth > 40:
        return None
    if isinstance(obj, dict):
        merged = dict(obj)
        inner = obj.get("data")
        if isinstance(inner, dict):
            merged = {**inner, **{k: v for k, v in obj.items() if k not in inner}}
        ident = merged.get("identity")
        if isinstance(ident, dict) and "id" in ident:
            merged.setdefault("product_id", ident["id"])
        ids = {str(merged.get(k)) for k in
               ("product_id", "productId", "item_id", "sku_id", "variant_id")
               if merged.get(k) is not None}
        if product_id in ids:
            for k in ("max_quantity", "max_allowed_quantity", "inventory",
                      "available_quantity"):
                v = merged.get(k)
                if isinstance(v, dict):
                    v = v.get("value", v.get("text"))
                if isinstance(v, str) and v.strip().isdigit():
                    v = int(v)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return max(0, int(v))
        for v in merged.values():
            got = _find_cap(v, product_id, depth + 1)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _find_cap(v, product_id, depth + 1)
            if got is not None:
                return got
    return None


def _derived_cap(row: dict) -> dict:
    """The cap we can defend from data already in the database.

    Every input is a ceiling the system enforces somewhere else; the cap is the
    tightest of them, and the binding one is named so the operator can see why
    an item stops at 3 while its neighbour goes to 6.
    """
    caps: dict[str, int] = {}

    if not row:
        # A batch line whose SKU is not in the catalogue: fail closed rather
        # than let the operator buy something the system cannot price or ship.
        return {"max_qty": 0, "source": "derived",
                "reason": "not in the catalogue", "detail": {}}
    if not row.get("in_stock"):
        caps["out of stock"] = 0
    caps["per-SKU cart rule"] = PER_SKU_CART_CAP

    # Only when the risk engine actually scored this SKU. Defaulting an
    # unscored item to NORMAL and then blaming the cap on "NORMAL stockout
    # risk" would put a reason on screen that nothing computed.
    bucket = (row.get("risk_bucket") or "").upper()
    if bucket in RISK_CAP:
        caps[f"{bucket} stockout risk"] = RISK_CAP[bucket]

    price = row.get("unit_price_inr") or 0
    if price and price > 0:
        caps["Rs.15k cart value cap"] = max(0, int(CART_VALUE_CAP_INR // price))

    weight = row.get("weight_g") or 0
    if weight and weight > 0:
        caps["parcel weight cap"] = max(0, int(MAX_PARCEL_G // weight))

    reason = min(caps, key=lambda k: (caps[k], k))
    return {"max_qty": max(0, caps[reason]), "source": "derived",
            "reason": reason, "detail": caps}


def _product_rows(conn: sqlite3.Connection,
                  product_ids: Iterable[str]) -> dict[str, dict]:
    """Everything the cap needs about a set of SKUs, in one query."""
    ids = [str(p) for p in product_ids]
    if not ids:
        return {}
    pc = _cols(conn, "products")
    rc = _cols(conn, "stockout_risk")
    price_c = _pick(pc, "price_inr", "price")
    weight_c = _pick(pc, "weight_g", "pack_weight_g", "net_weight_g", "weight_grams")
    stock_c = _pick(pc, "in_stock")
    name_c = _pick(pc, "name", "product_name")
    merch_c = _pick(pc, "merchant_id")
    bucket_c = _pick(rc, "bucket", "risk_bucket")

    sel = ["p.product_id AS product_id"]
    sel.append(f"p.{name_c} AS name" if name_c else "'' AS name")
    sel.append(f"p.{price_c} AS unit_price_inr" if price_c else "0 AS unit_price_inr")
    sel.append(f"p.{weight_c} AS weight_g" if weight_c else "NULL AS weight_g")
    sel.append(f"p.{stock_c} AS in_stock" if stock_c else "1 AS in_stock")
    sel.append(f"p.{merch_c} AS merchant_id" if merch_c else "NULL AS merchant_id")
    join = ""
    if bucket_c:
        sel.append(f"r.{bucket_c} AS risk_bucket")
        join = "LEFT JOIN stockout_risk r USING(product_id)"
    else:
        sel.append("NULL AS risk_bucket")

    out: dict[str, dict] = {}
    for chunk in (ids[i:i + 400] for i in range(0, len(ids), 400)):
        q = (f"SELECT {', '.join(sel)} FROM products p {join} "
             f"WHERE p.product_id IN ({','.join('?' * len(chunk))})")
        for r in conn.execute(q, chunk):
            out[str(r["product_id"])] = dict(r)
    return out


def cap_for(conn: sqlite3.Connection, product_id: str,
            row: Optional[dict] = None, refresh: bool = False,
            deadline: Optional[float] = None) -> dict:
    """Max units of one SKU a single cart may take, with its provenance.

    Live cap if Blinkit answers, otherwise the cached one while it is fresh,
    otherwise derived. Caching matters twice over: the console re-checks caps
    as quantities are typed and none of those checks should reach Blinkit, and
    a derived cap is only a stand-in, so it expires quickly and the next plan
    tries the live number again. `deadline` (monotonic) is the caller's probe
    budget -- past it, this stops reaching out and derives.
    """
    pid = str(product_id)
    now = int(time.time())
    if not refresh:
        c = conn.execute(
            "SELECT max_qty, source, reason, detail_json, computed_at "
            "FROM cart_limits WHERE product_id=?", (pid,)).fetchone()
        ttl = LIMIT_TTL_S if c and c["source"] == "live" else FALLBACK_TTL_S
        if c and now - (c["computed_at"] or 0) < ttl:
            return {"product_id": pid, "max_qty": c["max_qty"],
                    "source": c["source"], "reason": c["reason"],
                    "detail": json.loads(c["detail_json"] or "{}"),
                    "cached": True}

    if row is None:
        row = _product_rows(conn, [pid]).get(pid, {})
    live = None
    if deadline is None or time.monotonic() < deadline:
        live = _live_cap(pid, row.get("name") or "")
    cap = live or _derived_cap(row)
    cap.setdefault("detail", {})
    conn.execute(
        "INSERT INTO cart_limits (product_id,max_qty,source,reason,detail_json,computed_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET "
        "max_qty=excluded.max_qty, source=excluded.source, reason=excluded.reason, "
        "detail_json=excluded.detail_json, computed_at=excluded.computed_at",
        (pid, int(cap["max_qty"]), cap["source"], cap["reason"],
         json.dumps(cap["detail"]), now))
    conn.commit()
    return {"product_id": pid, "cached": False, **cap}


# ------------------------------------------------------------ the cart run ---

def _batch_lines(conn: sqlite3.Connection, batch_id: str) -> list[dict]:
    """The batch's lines, consolidated per SKU and grouped by cart."""
    bc = _cols(conn, "batch_lines")
    if not bc:
        return []
    qty_c = _pick(bc, "qty_required", "qty") or "qty_required"
    cart_c = _pick(bc, "cart_no", "cart")
    merch_c = _pick(bc, "merchant_id")
    sel = ["b.product_id AS product_id", f"SUM(b.{qty_c}) AS qty_planned"]
    sel.append(f"b.{cart_c} AS cart_no" if cart_c else "1 AS cart_no")
    sel.append(f"b.{merch_c} AS merchant_id" if merch_c else "NULL AS merchant_id")
    group = "b.product_id" + (f", b.{cart_c}" if cart_c else "")
    if merch_c:
        group += f", b.{merch_c}"
    rows = conn.execute(
        f"SELECT {', '.join(sel)} FROM batch_lines b WHERE b.batch_id=? "
        f"GROUP BY {group}", (batch_id,)).fetchall()
    return [dict(r) for r in rows]


def plan_batch(conn: sqlite3.Connection, batch_id: str,
               refresh_limits: bool = False) -> list[dict]:
    """Turn a batch into one ordered cart run per cart.

    Riskiest item first, same order the buy sheet used, so a run that has to be
    abandoned half way still got the items most likely to disappear. Each line
    carries its own cap and, when the batch wants more units than one cart
    takes, the number of passes that needs.
    """
    _ensure_schema(conn)
    lines = _batch_lines(conn, batch_id)
    if not lines:
        return []
    rows = _product_rows(conn, [l["product_id"] for l in lines])
    rank = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2}
    deadline = time.monotonic() + LIVE_PROBE_BUDGET_S

    carts: dict[Any, dict] = {}
    for l in lines:
        pid = str(l["product_id"])
        row = rows.get(pid, {})
        cap = cap_for(conn, pid, row, refresh=refresh_limits, deadline=deadline)
        qty = int(l["qty_planned"] or 0)
        max_qty = int(cap["max_qty"])
        item = {
            "product_id": pid,
            "name": row.get("name") or pid,
            "qty_planned": qty,
            "max_qty": max_qty,
            "limit_source": cap["source"],
            "limit_reason": cap["reason"],
            "limit_detail": cap.get("detail") or {},
            "risk_bucket": (row.get("risk_bucket") or "NORMAL").upper(),
            "unit_price_inr": row.get("unit_price_inr") or 0,
            "plan_over_limit": qty > max_qty,   # wants more than one cart takes
            "runs_needed": math.ceil(qty / max_qty) if max_qty > 0 else None,
            "product_url": product_url(pid, row.get("name") or ""),
            "search_url": search_url(row.get("name") or pid),
        }
        key = (l.get("cart_no") or 1, l.get("merchant_id") or row.get("merchant_id"))
        cart = carts.setdefault(key, {"cart_no": key[0], "merchant_id": key[1],
                                      "items": []})
        cart["items"].append(item)

    out = []
    for key in sorted(carts, key=lambda k: (k[0] or 0, str(k[1] or ""))):
        cart = carts[key]
        cart["items"].sort(key=lambda i: (rank.get(i["risk_bucket"], 3),
                                          -(i["unit_price_inr"] or 0) * i["qty_planned"],
                                          i["name"]))
        for n, item in enumerate(cart["items"], 1):
            item["seq"] = n
        cart["value_inr"] = round(sum((i["unit_price_inr"] or 0) * i["qty_planned"]
                                      for i in cart["items"]), 2)
        cart["n_items"] = len(cart["items"])
        cart["n_multi_pass"] = sum(1 for i in cart["items"] if i["plan_over_limit"])
        out.append(cart)
    return out


def _session_row(conn: sqlite3.Connection, sid: str) -> dict:
    row = conn.execute("SELECT * FROM cart_sessions WHERE cart_session_id=?",
                       (sid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"no such cart run: {sid}")
    return dict(row)


def _session_items(conn: sqlite3.Connection, sid: str) -> list[dict]:
    """A run's items in the one shape the console and the final page both read.

    Two different things can be "over the limit" and the UI treats them very
    differently, so they are separate fields: `over_limit` is a quantity that
    was actually recorded above the cap, `plan_over_limit` is the batch wanting
    more units than one cart takes -- not an error, just more passes.
    """
    items = [dict(r) for r in conn.execute(
        """SELECT i.*, l.detail_json AS limit_detail_json
           FROM cart_session_items i
           LEFT JOIN cart_limits l USING(product_id)
           WHERE i.cart_session_id=? ORDER BY i.seq""", (sid,))]
    for i in items:
        planned, added = i["qty_planned"] or 0, i["qty_added"] or 0
        cap = i["max_qty"] or 0
        i["over_limit"] = added > cap
        i["plan_over_limit"] = planned > cap
        i["runs_needed"] = math.ceil(planned / cap) if cap > 0 else None
        i["short_by"] = max(0, planned - added)
        i["limit_detail"] = json.loads(i.pop("limit_detail_json", None) or "{}")
        i["product_url"] = product_url(i["product_id"], i["name"] or "")
        i["search_url"] = search_url(i["name"] or i["product_id"])
    return items


def _totals(items: list[dict]) -> dict:
    added = [i for i in items if (i["qty_added"] or 0) > 0]
    return {
        "n_items": len(items),
        "n_added": len(added),
        "n_pending": len(items) - len(added),
        "qty_planned": sum(i["qty_planned"] or 0 for i in items),
        "qty_added": sum(i["qty_added"] or 0 for i in items),
        "n_over_limit": sum(1 for i in items if i["over_limit"]),
        "n_short": sum(1 for i in items if i["short_by"] > 0),
        "value_inr": round(sum((i["unit_price_inr"] or 0) * (i["qty_added"] or 0)
                               for i in items), 2),
    }


# --------------------------------------------------------------- endpoints ---

class PlanReq(BaseModel):
    batch_id: str
    refresh_limits: bool = False


class AddReq(BaseModel):
    product_id: str
    qty: int
    dry_run: bool = False      # the console validates on every keystroke; a
                               # dry run checks the cap without recording it


@app.get("/api/ops/cart/limits")
def cart_limits(product_ids: str, refresh: bool = False) -> dict:
    """Caps for a set of SKUs — what the console flags an over-limit qty against."""
    ids = [p for p in (product_ids or "").split(",") if p.strip()]
    conn = connect()
    _ensure_schema(conn)
    rows = _product_rows(conn, ids)
    deadline = time.monotonic() + LIVE_PROBE_BUDGET_S
    out = {pid: cap_for(conn, pid, rows.get(pid, {}), refresh=refresh,
                        deadline=deadline) for pid in ids}
    conn.close()
    return {"ok": True, "limits": out}


@app.post("/api/ops/cart/plan")
def cart_plan(req: PlanReq) -> dict:
    """Open (or re-open) one cart run per cart in a batch.

    Re-planning an open run keeps whatever was already added, so a browser
    reload mid-run does not cost the operator their progress.
    """
    conn = connect()
    _ensure_schema(conn)
    carts = plan_batch(conn, req.batch_id, refresh_limits=req.refresh_limits)
    if not carts:
        conn.close()
        return {"ok": False, "error": f"no lines for batch {req.batch_id} — "
                                      "build a batch first", "carts": []}
    now = int(time.time())
    out = []
    for cart in carts:
        row = conn.execute(
            "SELECT cart_session_id FROM cart_sessions WHERE batch_id=? AND cart_no=? "
            "AND status='OPEN'", (req.batch_id, cart["cart_no"])).fetchone()
        sid = row["cart_session_id"] if row else "CART-" + uuid.uuid4().hex[:10].upper()
        if not row:
            conn.execute(
                "INSERT INTO cart_sessions (cart_session_id,batch_id,cart_no,"
                "merchant_id,status,created_at) VALUES (?,?,?,?,'OPEN',?)",
                (sid, req.batch_id, cart["cart_no"], cart["merchant_id"], now))
        for item in cart["items"]:
            conn.execute(
                """INSERT INTO cart_session_items
                   (cart_session_id,product_id,seq,name,qty_planned,qty_added,max_qty,
                    limit_source,limit_reason,risk_bucket,unit_price_inr,status,updated_at)
                   VALUES (?,?,?,?,?,0,?,?,?,?,?, 'PENDING', ?)
                   ON CONFLICT(cart_session_id,product_id) DO UPDATE SET
                     seq=excluded.seq, name=excluded.name,
                     qty_planned=excluded.qty_planned, max_qty=excluded.max_qty,
                     limit_source=excluded.limit_source,
                     limit_reason=excluded.limit_reason,
                     risk_bucket=excluded.risk_bucket,
                     unit_price_inr=excluded.unit_price_inr,
                     updated_at=excluded.updated_at""",
                (sid, item["product_id"], item["seq"], item["name"],
                 item["qty_planned"], item["max_qty"], item["limit_source"],
                 item["limit_reason"], item["risk_bucket"],
                 item["unit_price_inr"], now))
        conn.commit()
        items = _session_items(conn, sid)
        out.append({**cart, "cart_session_id": sid, "items": items,
                    "totals": _totals(items),
                    "blinkit_cart_url": BLINKIT_CART_URL,
                    "final_url": f"/cart-run/{sid}"})
    conn.close()
    return {"ok": True, "batch_id": req.batch_id, "carts": out,
            "note": "Add each item in one Blinkit tab, in order; the run ends "
                    "with one link for everything added."}


@app.get("/api/ops/cart/session/{sid}")
def cart_session(sid: str) -> dict:
    conn = connect()
    _ensure_schema(conn)
    row = _session_row(conn, sid)
    items = _session_items(conn, sid)
    nxt = next((i for i in items if i["status"] == "PENDING"), None)
    conn.close()
    return {"ok": True, **row, "items": items, "totals": _totals(items),
            "next_item": nxt, "blinkit_cart_url": BLINKIT_CART_URL,
            "final_url": f"/cart-run/{sid}"}


@app.post("/api/ops/cart/session/{sid}/add")
def cart_add(sid: str, req: AddReq) -> dict:
    """Record one item going into the Blinkit cart — or refuse it, with the cap.

    This is the authority behind the console's live flag: the page checks the
    cap as the quantity is typed, but a quantity is only ever *recorded* here,
    where the cap is re-read from the database first. A stale page cannot talk
    the system into an over-limit line.
    """
    conn = connect()
    _ensure_schema(conn)
    _session_row(conn, sid)
    item = conn.execute(
        "SELECT * FROM cart_session_items WHERE cart_session_id=? AND product_id=?",
        (sid, str(req.product_id))).fetchone()
    if not item:
        conn.close()
        raise HTTPException(status_code=404,
                            detail=f"{req.product_id} is not in cart run {sid}")
    item = dict(item)
    cap = cap_for(conn, str(req.product_id))          # fresh, not the page's copy
    max_qty, qty = int(cap["max_qty"]), int(req.qty)

    if qty < 0:
        conn.close()
        raise HTTPException(status_code=400, detail="qty cannot be negative")

    if qty > max_qty:
        conn.execute(
            "UPDATE cart_session_items SET max_qty=?, limit_source=?, limit_reason=?, "
            "updated_at=? WHERE cart_session_id=? AND product_id=?",
            (max_qty, cap["source"], cap["reason"], int(time.time()),
             sid, str(req.product_id)))
        conn.commit()
        items = _session_items(conn, sid)
        conn.close()
        return {"ok": False, "over_limit": True, "product_id": req.product_id,
                "requested": qty, "max_qty": max_qty, "over_by": qty - max_qty,
                "limit_source": cap["source"], "limit_reason": cap["reason"],
                "limit_detail": cap.get("detail") or {},
                "message": (f"Blinkit takes at most {max_qty} of this item per cart "
                            f"({cap['reason']}). You asked for {qty} — "
                            f"{qty - max_qty} over."
                            if max_qty else
                            f"This item cannot go in the cart ({cap['reason']})."),
                "totals": _totals(items)}

    status = "ADDED" if qty >= (item["qty_planned"] or 0) else "SHORT"
    if req.dry_run:
        # Only the cap is worth persisting from a keystroke check; the quantity
        # itself is not an add until the operator says it is.
        conn.execute(
            "UPDATE cart_session_items SET max_qty=?, limit_source=?, limit_reason=?, "
            "updated_at=? WHERE cart_session_id=? AND product_id=?",
            (max_qty, cap["source"], cap["reason"], int(time.time()),
             sid, str(req.product_id)))
    else:
        conn.execute(
            "UPDATE cart_session_items SET qty_added=?, max_qty=?, limit_source=?, "
            "limit_reason=?, status=?, updated_at=? "
            "WHERE cart_session_id=? AND product_id=?",
            (qty, max_qty, cap["source"], cap["reason"], status, int(time.time()),
             sid, str(req.product_id)))
    conn.commit()
    if not req.dry_run:
        # The buy sheet is live, so every unit that goes into a Blinkit cart has
        # to land on it as it happens. Imported here rather than at module load:
        # buysheet imports this module for the cap and the links.
        try:
            from .buysheet import record_event, BLINKIT_ADD
            record_event(conn, BLINKIT_ADD, product_id=req.product_id, qty=qty,
                         ref=sid, name=item["name"], source="cart-run",
                         actor="operator")
        except Exception:
            pass          # the sheet is a view; never fail an add over it
    items = _session_items(conn, sid)
    nxt = next((i for i in items if i["status"] == "PENDING"), None)
    conn.close()
    return {"ok": True, "over_limit": False, "product_id": req.product_id,
            "qty_added": item["qty_added"] if req.dry_run else qty,
            "max_qty": max_qty, "status": status, "dry_run": req.dry_run,
            "limit_source": cap["source"], "limit_reason": cap["reason"],
            "short_by": max(0, (item["qty_planned"] or 0) - qty),
            "next_item": nxt, "totals": _totals(items)}


@app.post("/api/ops/cart/session/{sid}/finalize")
def cart_finalize(sid: str) -> dict:
    """Close the run and hand back the two links the operator needs.

    `final_url` is this run: every product that went in, with its quantity, on
    one page that survives the browser tab. `blinkit_cart_url` is Blinkit's own
    cart, for the checkout itself.
    """
    conn = connect()
    _ensure_schema(conn)
    _session_row(conn, sid)
    conn.execute("UPDATE cart_sessions SET status='FINALIZED', finalized_at=? "
                 "WHERE cart_session_id=?", (int(time.time()), sid))
    conn.commit()
    items = _session_items(conn, sid)
    totals = _totals(items)
    conn.close()
    return {"ok": True, "cart_session_id": sid, "final_url": f"/cart-run/{sid}",
            "blinkit_cart_url": BLINKIT_CART_URL, "totals": totals,
            "items": [{"product_id": i["product_id"], "name": i["name"],
                       "qty_added": i["qty_added"], "qty_planned": i["qty_planned"],
                       "max_qty": i["max_qty"], "short_by": i["short_by"],
                       "product_url": i["product_url"]} for i in items],
            "short": [i["product_id"] for i in items if i["short_by"] > 0]}


@app.get("/api/ops/cart/session/{sid}/summary")
def cart_summary(sid: str) -> dict:
    """What the final link renders: the run, its items, and both cart links."""
    conn = connect()
    _ensure_schema(conn)
    row = _session_row(conn, sid)
    items = _session_items(conn, sid)
    conn.close()
    return {"ok": True, **row, "items": items, "totals": _totals(items),
            "blinkit_cart_url": BLINKIT_CART_URL}


@app.get("/cart-run/{sid}")
def cart_page(sid: str):
    """The final link itself — shareable, and readable after the run is over."""
    return FileResponse(WEB / "cart.html")
