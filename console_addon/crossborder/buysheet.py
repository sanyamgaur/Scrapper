"""A buy sheet that moves — every cart add and every placed order, as it happens.

The buy sheet used to exist only once a batch was sealed: until someone pressed
"Build batch" it was empty, and after that it was a snapshot that never moved
again. Everything that actually happens to demand -- a customer putting an item
in their cart, a customer pressing Place order, the operator dropping a unit
into the Blinkit cart -- happened off-sheet, and the only way to see any of it
was to rebuild the batch.

This module makes the sheet live. One row per SKU, four quantities on it:

    in cart    someone is holding it, not yet ordered
    ordered    Place order was pressed, not yet batched
    batched    sealed into the buying batch
    in Blinkit the operator has actually added it during a cart run

and a feed of the events those numbers came from, newest first.

Where the events come from, in order of reliability:

  * `orders` / `order_lines` -- a placed order is a database row whoever wrote
    it, so `_backfill_orders()` reads the table on every poll and raises an
    ORDER_PLACED event for any order the sheet has not seen. Place order can
    never go unnoticed, with no change to the storefront at all.
  * the cart run (cart.py) records every add it takes.
  * POST /api/ops/buysheet/track -- what a storefront calls when a customer
    adds to or removes from their cart. One fetch in the cart handler.
  * a fail-safe request watcher over the existing app, for when nobody wants to
    touch the storefront: it notices cart/checkout POSTs going past and reads
    the product/quantity pairs out of them. Opt out with SOURCED_CART_WATCH=0.

Additive like the rest of the add-on: one new table, api.py untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Iterable, Optional

from pydantic import BaseModel

from .api import app
from .db import connect
from .cart import (_cols, _pick, _product_rows, _ensure_schema as _ensure_cart_schema,
                   cap_for, product_url, search_url)

# Kinds of thing that can happen to a line before it is bought.
CART_ADD = "CART_ADD"            # a quantity for one product in someone's cart
CART_SNAPSHOT = "CART_SNAPSHOT"  # the whole cart at once; replaces what we knew
CART_CLEAR = "CART_CLEAR"        # cart emptied or abandoned
ORDER_PLACED = "ORDER_PLACED"    # Place order pressed
BLINKIT_ADD = "BLINKIT_ADD"      # the operator added it in a cart run

EVENT_WINDOW_S = 7 * 24 * 3600   # how far back the sheet aggregates carts
MAX_WATCH_BODY = 256 * 1024      # don't buffer a large upload to peek at it
WATCH_PATH_HINTS = ("cart", "basket", "checkout", "order", "buy")

SCHEMA = """
CREATE TABLE IF NOT EXISTS buysheet_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at         INTEGER,
    kind       TEXT,       -- CART_ADD | CART_SNAPSHOT | CART_CLEAR | ORDER_PLACED | BLINKIT_ADD
    ref        TEXT,       -- cart id, order id, or cart-run id
    product_id TEXT,
    name       TEXT,
    qty        INTEGER,
    mode       TEXT,       -- absolute (qty IS the line now) | delta (qty is a change)
    actor      TEXT,
    source     TEXT,       -- track | watch | cart-run | orders-table
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_bs_at   ON buysheet_events(at DESC);
CREATE INDEX IF NOT EXISTS idx_bs_ref  ON buysheet_events(ref, product_id, at);
CREATE INDEX IF NOT EXISTS idx_bs_kind ON buysheet_events(kind, ref);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ------------------------------------------------------------- recording -----

def record_event(conn: sqlite3.Connection, kind: str, *, product_id: str = None,
                 qty: int = 0, ref: str = None, name: str = None,
                 mode: str = "absolute", actor: str = None,
                 source: str = "track", detail: Any = None) -> int:
    _ensure_schema(conn)
    cur = conn.execute(
        "INSERT INTO buysheet_events (at,kind,ref,product_id,name,qty,mode,actor,"
        "source,detail_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), kind, ref, (str(product_id) if product_id else None),
         name, int(qty or 0), mode, actor, source,
         json.dumps(detail) if detail is not None else None))
    conn.commit()
    return cur.lastrowid


def record_events(conn: sqlite3.Connection, kind: str,
                  items: Iterable[dict], **kw) -> int:
    n = 0
    for it in items:
        pid = it.get("product_id") or it.get("productId") or it.get("sku")
        if pid is None:
            continue
        record_event(conn, kind, product_id=pid, qty=it.get("qty", it.get("quantity", 1)),
                     name=it.get("name"), **kw)
        n += 1
    return n


def _backfill_orders(conn: sqlite3.Connection) -> int:
    """Raise ORDER_PLACED for any order the sheet has not recorded yet.

    The storefront's own checkout writes orders and order_lines; this turns
    those rows into sheet events without the storefront knowing the sheet
    exists, so pressing Place order always shows up even if nothing calls
    /track and the request watcher is off.
    """
    oc = _cols(conn, "orders")
    if not oc:
        return 0
    seen = {r[0] for r in conn.execute(
        "SELECT DISTINCT ref FROM buysheet_events WHERE kind=? AND ref IS NOT NULL",
        (ORDER_PLACED,))}
    order_ids = [r[0] for r in conn.execute("SELECT order_id FROM orders")]
    fresh = [o for o in order_ids if o not in seen]
    if not fresh:
        return 0

    lc = _cols(conn, "order_lines")
    qty_c = _pick(lc, "qty", "quantity") or "qty"
    n = 0
    for oid in fresh:
        lines = []
        if lc:
            lines = [dict(r) for r in conn.execute(
                f"SELECT product_id, {qty_c} AS qty FROM order_lines WHERE order_id=?",
                (oid,))]
        if not lines:
            # Older orders keep their lines as JSON on the order itself.
            row = conn.execute("SELECT lines_json FROM orders WHERE order_id=?",
                               (oid,)).fetchone() if "lines_json" in oc else None
            try:
                lines = json.loads(row["lines_json"]) if row and row["lines_json"] else []
            except (ValueError, TypeError):
                lines = []
        for l in lines:
            record_event(conn, ORDER_PLACED, product_id=l.get("product_id"),
                         qty=l.get("qty", 1), ref=oid, source="orders-table",
                         actor="storefront")
            n += 1
        if not lines:        # an order with no readable lines still happened
            record_event(conn, ORDER_PLACED, ref=oid, source="orders-table",
                         actor="storefront")
    return n


# ------------------------------------------------------------ aggregation ----

def _carts_in_progress(conn: sqlite3.Connection) -> dict[str, int]:
    """Net quantity per SKU across carts nobody has ordered yet.

    Events replay in time order per cart: a snapshot replaces what we knew
    about that cart, an absolute line sets it, a delta moves it, a clear empties
    it. A cart whose ref later shows up as a placed order stops counting here --
    it is ordered now, not held.
    """
    since = int(time.time()) - EVENT_WINDOW_S
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM buysheet_events WHERE at>=? AND kind IN (?,?,?) "
        "ORDER BY at, event_id", (since, CART_ADD, CART_SNAPSHOT, CART_CLEAR))]
    ordered_refs = {r[0] for r in conn.execute(
        "SELECT DISTINCT ref FROM buysheet_events WHERE kind=?", (ORDER_PLACED,))}

    carts: dict[str, dict[str, int]] = {}
    for e in rows:
        ref = e["ref"] or "anon"
        cart = carts.setdefault(ref, {})
        if e["kind"] == CART_CLEAR:
            cart.clear()
            continue
        if e["kind"] == CART_SNAPSHOT:
            try:
                snap = json.loads(e["detail_json"] or "[]")
            except ValueError:
                snap = []
            cart.clear()
            for l in snap:
                pid = l.get("product_id")
                if pid is not None:
                    cart[str(pid)] = int(l.get("qty") or 0)
            continue
        pid = e["product_id"]
        if pid is None:
            continue
        cart[pid] = int(e["qty"] or 0) if e["mode"] == "absolute" \
            else cart.get(pid, 0) + int(e["qty"] or 0)

    out: dict[str, int] = {}
    for ref, cart in carts.items():
        if ref in ordered_refs:
            continue
        for pid, qty in cart.items():
            if qty > 0:
                out[pid] = out.get(pid, 0) + qty
    return out


def _ordered_unbatched(conn: sqlite3.Connection) -> dict[str, int]:
    """Placed but not yet sealed into a batch — the queue the buyer inherits."""
    oc, lc = _cols(conn, "orders"), _cols(conn, "order_lines")
    if not oc or not lc:
        return {}
    qty_c = _pick(lc, "qty", "quantity") or "qty"
    where = "WHERE o.batch_id IS NULL" if "batch_id" in oc else ""
    try:
        rows = conn.execute(
            f"""SELECT l.product_id AS pid, SUM(l.{qty_c}) AS qty
                FROM order_lines l JOIN orders o USING(order_id) {where}
                GROUP BY l.product_id""").fetchall()
    except sqlite3.Error:
        return {}
    return {str(r["pid"]): int(r["qty"] or 0) for r in rows}


def _batched(conn: sqlite3.Connection, batch_id: Optional[str]) -> dict[str, int]:
    bc = _cols(conn, "batch_lines")
    if not bc or not batch_id:
        return {}
    qty_c = _pick(bc, "qty_required", "qty") or "qty_required"
    rows = conn.execute(
        f"SELECT product_id AS pid, SUM({qty_c}) AS qty FROM batch_lines "
        "WHERE batch_id=? GROUP BY product_id", (batch_id,)).fetchall()
    return {str(r["pid"]): int(r["qty"] or 0) for r in rows}


def _in_blinkit_cart(conn: sqlite3.Connection,
                     batch_id: Optional[str]) -> dict[str, int]:
    """What the operator has actually put in a Blinkit cart for this batch."""
    if not _cols(conn, "cart_session_items"):
        return {}
    q = ("SELECT i.product_id AS pid, SUM(i.qty_added) AS qty "
         "FROM cart_session_items i JOIN cart_sessions s USING(cart_session_id) ")
    args: tuple = ()
    if batch_id:
        q += "WHERE s.batch_id=? "
        args = (batch_id,)
    rows = conn.execute(q + "GROUP BY i.product_id", args).fetchall()
    return {str(r["pid"]): int(r["qty"] or 0) for r in rows}


def live_sheet(conn: sqlite3.Connection, batch_id: Optional[str] = None,
               n_events: int = 30) -> dict:
    """The sheet as it stands right now, plus the events that moved it."""
    _ensure_schema(conn)
    _ensure_cart_schema(conn)
    _backfill_orders(conn)

    in_cart = _carts_in_progress(conn)
    ordered = _ordered_unbatched(conn)
    batched = _batched(conn, batch_id)
    added = _in_blinkit_cart(conn, batch_id)

    pids = set(in_cart) | set(ordered) | set(batched) | set(added)
    rows = _product_rows(conn, pids)
    last_seen = {str(r["product_id"]): r["at"] for r in conn.execute(
        "SELECT product_id, MAX(at) AS at FROM buysheet_events "
        "WHERE product_id IS NOT NULL GROUP BY product_id")}
    rank = {"CRITICAL": 0, "HIGH": 1, "NORMAL": 2}

    lines = []
    for pid in pids:
        row = rows.get(pid, {})
        cap = cap_for(conn, pid, row)
        name = row.get("name") or pid
        qty_cart, qty_ord = in_cart.get(pid, 0), ordered.get(pid, 0)
        qty_bat, qty_add = batched.get(pid, 0), added.get(pid, 0)
        # The furthest a unit of this SKU has got is what the row is "at".
        state = ("ADDED" if qty_add else "BATCHED" if qty_bat
                 else "ORDERED" if qty_ord else "IN_CART")
        demand = qty_bat or qty_ord or qty_cart
        lines.append({
            "product_id": pid, "name": name,
            "risk_bucket": (row.get("risk_bucket") or "NORMAL").upper(),
            "unit_price_inr": row.get("unit_price_inr") or 0,
            "merchant_id": row.get("merchant_id"),
            "qty_in_cart": qty_cart, "qty_ordered": qty_ord,
            "qty_batched": qty_bat, "qty_in_blinkit_cart": qty_add,
            "qty_outstanding": max(0, demand - qty_add),
            "state": state,
            "max_qty": cap["max_qty"], "limit_source": cap["source"],
            "limit_reason": cap["reason"], "limit_detail": cap.get("detail") or {},
            "over_limit": qty_add > cap["max_qty"],
            "needs_passes": (demand + cap["max_qty"] - 1) // cap["max_qty"]
                            if cap["max_qty"] else None,
            "last_event_at": last_seen.get(pid),
            "product_url": product_url(pid, name),
            "search_url": search_url(name),
        })
    lines.sort(key=lambda l: (rank.get(l["risk_bucket"], 3),
                              -(l["unit_price_inr"] or 0) * (l["qty_outstanding"] or 0),
                              l["name"]))

    events = [dict(r) for r in conn.execute(
        "SELECT event_id, at, kind, ref, product_id, name, qty, actor, source "
        "FROM buysheet_events ORDER BY at DESC, event_id DESC LIMIT ?",
        (max(1, min(n_events, 200)),))]
    for e in events:
        if not e["name"] and e["product_id"]:
            e["name"] = (rows.get(str(e["product_id"])) or {}).get("name")

    return {
        "ok": True, "batch_id": batch_id, "as_of": int(time.time()),
        "lines": lines, "events": events,
        "totals": {
            "skus": len(lines),
            "qty_in_cart": sum(l["qty_in_cart"] for l in lines),
            "qty_ordered": sum(l["qty_ordered"] for l in lines),
            "qty_batched": sum(l["qty_batched"] for l in lines),
            "qty_in_blinkit_cart": sum(l["qty_in_blinkit_cart"] for l in lines),
            "qty_outstanding": sum(l["qty_outstanding"] for l in lines),
            "n_over_limit": sum(1 for l in lines if l["over_limit"]),
            "n_events": conn.execute(
                "SELECT COUNT(*) FROM buysheet_events").fetchone()[0],
        },
    }


# --------------------------------------------------------------- endpoints ---

class TrackItem(BaseModel):
    product_id: str
    qty: int = 1
    name: Optional[str] = None


class TrackReq(BaseModel):
    """What a storefront posts when a cart changes or an order is placed."""
    kind: str = CART_ADD               # CART_ADD | CART_SNAPSHOT | CART_CLEAR | ORDER_PLACED
    ref: Optional[str] = None          # cart id / session id / order id
    items: list[TrackItem] = []
    mode: str = "absolute"             # absolute: qty IS the line; delta: qty is a change
    actor: Optional[str] = None


@app.post("/api/ops/buysheet/track")
def buysheet_track(req: TrackReq) -> dict:
    """Record one cart change (or a placed order) on the live buy sheet.

    A storefront wires this up with a single call in its cart handler:

        fetch('/api/ops/buysheet/track', {method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({kind:'CART_SNAPSHOT', ref: cartId, items: cart})});
    """
    kind = (req.kind or CART_ADD).upper()
    if kind not in (CART_ADD, CART_SNAPSHOT, CART_CLEAR, ORDER_PLACED, BLINKIT_ADD):
        return {"ok": False, "error": f"unknown kind: {kind}"}
    conn = connect()
    _ensure_schema(conn)
    items = [i.model_dump() if hasattr(i, "model_dump") else i.dict() for i in req.items]
    if kind == CART_SNAPSHOT:
        n = 1
        units = sum(int(i.get("qty") or 0) for i in items)
        record_event(conn, CART_SNAPSHOT, ref=req.ref, actor=req.actor,
                     source="track", detail=items, qty=units,
                     name=f"{len(items)} item{'' if len(items) == 1 else 's'}")
    elif kind == CART_CLEAR:
        n = 1
        record_event(conn, CART_CLEAR, ref=req.ref, actor=req.actor, source="track")
    else:
        n = record_events(conn, kind, items, ref=req.ref, mode=req.mode,
                          actor=req.actor, source="track")
    conn.close()
    return {"ok": True, "kind": kind, "recorded": n, "ref": req.ref}


@app.get("/api/ops/buysheet/live")
def buysheet_live(batch_id: Optional[str] = None, n_events: int = 30) -> dict:
    """The live buy sheet: one row per SKU, plus the feed that moved it."""
    conn = connect()
    try:
        return live_sheet(conn, batch_id, n_events)
    finally:
        conn.close()


@app.get("/api/ops/buysheet/events")
def buysheet_events(limit: int = 50, since: Optional[int] = None) -> dict:
    """Just the feed — what a poller asks for between full sheet refreshes."""
    conn = connect()
    _ensure_schema(conn)
    _backfill_orders(conn)
    q = ("SELECT event_id, at, kind, ref, product_id, name, qty, actor, source "
         "FROM buysheet_events ")
    args: tuple = ()
    if since:
        q += "WHERE event_id > ? "
        args = (int(since),)
    q += "ORDER BY event_id DESC LIMIT ?"
    rows = [dict(r) for r in conn.execute(q, args + (max(1, min(limit, 500)),))]
    conn.close()
    return {"ok": True, "events": rows,
            "last_event_id": rows[0]["event_id"] if rows else (since or 0)}


# ----------------------------------------------------- the request watcher ---

def _walk_lines(obj: Any, out: list, depth: int = 0) -> list:
    """Lift {product_id, qty} pairs out of a payload of unknown shape.

    Same tactic the scraper's parser uses on Blinkit's snippets: storefronts
    post carts in whatever shape they like, so look for the pair rather than
    pin a schema.
    """
    if depth > 12 or len(out) > 200:
        return out
    if isinstance(obj, dict):
        pid = obj.get("product_id", obj.get("productId", obj.get("sku")))
        if pid is not None and not isinstance(pid, (dict, list)):
            qty = obj.get("qty", obj.get("quantity", obj.get("count", 1)))
            try:
                qty = int(qty)
            except (TypeError, ValueError):
                qty = 1
            out.append({"product_id": str(pid), "qty": qty,
                        "name": obj.get("name") if isinstance(obj.get("name"), str)
                        else None})
        for v in obj.values():
            _walk_lines(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk_lines(v, out, depth + 1)
    return out


def install_cart_watch(fastapi_app) -> bool:
    """Watch cart/checkout POSTs going past, so the sheet sees them untouched.

    A storefront that never calls /track still moves the sheet: this reads the
    product/quantity pairs out of requests already flowing through the app. It
    is strictly an observer -- the request is replayed to the real handler
    byte-for-byte, anything that goes wrong is swallowed, and nothing about the
    response changes. Set SOURCED_CART_WATCH=0 to leave it out entirely.
    """
    if os.environ.get("SOURCED_CART_WATCH", "1") == "0":
        return False

    @fastapi_app.middleware("http")
    async def cart_watch(request, call_next):
        body = b""
        path = request.url.path
        watchable = (
            request.method in ("POST", "PUT", "PATCH")
            and not path.startswith("/api/ops/")          # never watch ourselves
            and any(h in path.lower() for h in WATCH_PATH_HINTS)
        )
        if watchable:
            try:
                body = await request.body()
                if len(body) > MAX_WATCH_BODY:
                    body, watchable = b"", False

                async def _replay():                      # hand the handler its
                    return {"type": "http.request",       # body back, unchanged
                            "body": body, "more_body": False}
                request._receive = _replay
            except Exception:
                watchable = False

        response = await call_next(request)

        if watchable and body and response.status_code < 400:
            try:
                _record_watched(path, body)
            except Exception:
                pass          # an observer must never break a checkout
        return response

    return True


def _record_watched(path: str, body: bytes) -> None:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return
    items = _walk_lines(payload, [])
    if not items:
        return
    low = path.lower()
    placed = any(h in low for h in ("checkout", "order", "place"))
    ref = None
    if isinstance(payload, dict):
        for k in ("cart_id", "cartId", "order_id", "session_id", "customer_id",
                  "email"):
            if payload.get(k):
                ref = str(payload[k])
                break
    if not ref:
        # No id in the payload: key the cart by its own contents so repeated
        # posts of the same cart update one row instead of stacking up.
        ref = "watch-" + hashlib.sha1(
            ("|".join(sorted(i["product_id"] for i in items))).encode()).hexdigest()[:12]

    units = sum(i["qty"] for i in items)
    conn = connect()
    try:
        _ensure_schema(conn)
        if placed:
            # The cart just converted, so it stops counting as held. The order
            # itself is raised from the orders table by _backfill_orders(),
            # which knows the real order id -- recording it here too would put
            # the same Place order on the feed twice. If this system keeps no
            # orders table, there is no backfill to wait for, so record it.
            record_event(conn, CART_CLEAR, ref=ref, source="watch",
                         actor="storefront", name="cart checked out")
            if not _cols(conn, "orders"):
                record_events(conn, ORDER_PLACED, items, ref=ref, source="watch",
                              actor="storefront")
        else:
            # A cart POST carries the whole cart, so it replaces what we knew.
            record_event(conn, CART_SNAPSHOT, ref=ref, source="watch",
                         actor="storefront", detail=items, qty=units,
                         name=f"{len(items)} item{'' if len(items) == 1 else 's'}")
    finally:
        conn.close()


install_cart_watch(app)
