"""Print what the control tower is actually working with. Read-only.

    python diagnose.py                     # database only
    python diagnose.py --url http://127.0.0.1:8000    # also ask the running app

Run it from the folder that holds crossborder.db, with the server running if
you want the API half. It writes nothing; every question it answers is one that
otherwise needs a SQL prompt:

  * why a pack-out parcel weighs 0 g (no parsed weight on the products)
  * whether placed orders actually reached orders/order_lines
  * whether the buy sheet has seen any cart activity, and from where
  * which HTTP method this build's API wants for reconcile/packout
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request

WEIGHT_COLS = ("weight_g", "pack_weight_g", "net_weight_g", "weight_grams", "weight")


def cols(conn, table):
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def one(conn, sql, *a, default=0):
    try:
        return conn.execute(sql, a).fetchone()[0]
    except sqlite3.Error:
        return default


def section(title):
    print(f"\n{title}\n" + "-" * len(title))


def products(conn):
    section("PRODUCTS")
    pc = cols(conn, "products")
    if not pc:
        print("  no products table — wrong folder?")
        return
    print(f"  rows: {one(conn, 'SELECT COUNT(*) FROM products'):,}")
    wcol = next((c for c in WEIGHT_COLS if c in pc), None)
    if not wcol:
        print(f"  weight column: NONE of {WEIGHT_COLS} exist.")
        print("  -> pack-out weighs 0 g because nothing stores a weight, and the")
        print("     cart limit never binds on parcel weight. Columns present:")
        print("    ", ", ".join(pc))
        return
    total = one(conn, "SELECT COUNT(*) FROM products")
    withw = one(conn, f"SELECT COUNT(*) FROM products WHERE {wcol} IS NOT NULL AND {wcol}>0")
    print(f"  weight column: {wcol} — {withw:,} of {total:,} rows have a weight "
          f"({0 if not total else round(withw / total * 100)}%)")
    if withw == 0:
        print("  -> every weight is NULL or 0, so every parcel weighs 0 g.")
        print("     Re-run the catalogue ingest: that is the step that parses a")
        print("     pack size ('500 g', '1 L') into a weight.")
    ucol = "unit" if "unit" in pc else ("pack_size" if "pack_size" in pc else None)
    sel = f"name, {ucol + ', ' if ucol else ''}{wcol}"
    print(f"  sample ({sel}):")
    for r in conn.execute(f"SELECT {sel} FROM products ORDER BY RANDOM() LIMIT 6"):
        print("    ", " | ".join("—" if v is None else str(v)[:34] for v in r))
    if withw and withw < total:
        print("  items with no weight (these contribute 0 g to a parcel):")
        for r in conn.execute(
                f"SELECT name{', ' + ucol if ucol else ''} FROM products "
                f"WHERE {wcol} IS NULL OR {wcol}=0 LIMIT 5"):
            print("    ", " | ".join(str(v)[:40] for v in r))


def orders(conn):
    section("ORDERS (what 'Place order' wrote)")
    oc = cols(conn, "orders")
    if not oc:
        print("  no orders table.")
        return
    n = one(conn, "SELECT COUNT(*) FROM orders")
    unbatched = one(conn, "SELECT COUNT(*) FROM orders WHERE batch_id IS NULL") \
        if "batch_id" in oc else "n/a"
    lines = one(conn, "SELECT COUNT(*) FROM order_lines")
    print(f"  orders: {n:,}   unbatched: {unbatched}   order_lines: {lines:,}")
    if n and not lines:
        print("  -> orders exist but no order_lines: the buy sheet's 'ordered'")
        print("     column stays empty because that is what it sums.")
    for r in conn.execute("SELECT order_id, status FROM orders ORDER BY rowid DESC LIMIT 5"):
        print("    ", r[0], r[1])


def batches(conn):
    section("BATCHES")
    if not cols(conn, "procurement_batches"):
        print("  no procurement_batches table.")
        return
    for r in conn.execute("SELECT batch_id, state, n_orders, n_lines FROM "
                          "procurement_batches ORDER BY batch_date DESC LIMIT 5"):
        print(f"   {r[0]}  {r[1]}  orders={r[2]}  lines={r[3]}")
    print("  (Build batch refuses a date that already has one — use the existing"
          " batch, or change the date box.)")


def sheet(conn):
    section("BUY SHEET")
    if not cols(conn, "buysheet_events"):
        print("  no buysheet_events table yet — open View buy sheet once.")
        return
    rows = list(conn.execute(
        "SELECT kind, source, COUNT(*) FROM buysheet_events GROUP BY kind, source"))
    if not rows:
        print("  no events recorded at all.")
    for k, src, n in rows:
        print(f"   {k:<14} via {src:<13} {n:,}")
    cart = one(conn, "SELECT COUNT(*) FROM buysheet_events WHERE kind LIKE 'CART%'")
    if not cart:
        print("  -> nothing from a storefront cart. Either the cart lives in the")
        print("     browser until checkout, or it posts to a path the watcher")
        print("     ignores. GET /api/ops/buysheet/watch-log says which.")
    if cols(conn, "cart_session_items"):
        print(f"  cart-run items: "
              f"{one(conn, 'SELECT COUNT(*) FROM cart_session_items'):,}"
              f"   units added: "
              f"{one(conn, 'SELECT COALESCE(SUM(qty_added),0) FROM cart_session_items'):,}")


def api(url):
    section(f"API at {url}")
    try:
        spec = json.load(urllib.request.urlopen(url.rstrip("/") + "/openapi.json", timeout=5))
    except Exception as e:
        print(f"  could not read /openapi.json ({e}). Is the server running?")
        return
    want = ("reconcile", "packout", "picksheet", "batch/build", "cart", "buysheet")
    for path, ops in sorted(spec.get("paths", {}).items()):
        if any(w in path for w in want):
            methods = ",".join(sorted(m.upper() for m in ops if m != "parameters"))
            print(f"   {methods:<12} {path}")
    print("  (The console now retries the other method on a 405, so a mismatch"
          " here is no longer fatal.)")
    try:
        log = json.load(urllib.request.urlopen(
            url.rstrip("/") + "/api/ops/buysheet/watch-log", timeout=5))
        print(f"\n  watcher enabled: {log['watcher_enabled']}   "
              f"hints: {', '.join(log['path_hints'])}")
        if log["seen"]:
            print("  writes seen (newest first):")
            for e in log["seen"][:10]:
                print(f"    {e['method']:<5} {e['path'][:44]:<46} "
                      f"hint={e['matched_hint']} items={e['items_found']}")
        else:
            print("  no non-ops writes seen since start — add something to a cart"
                  " on the storefront, then re-run this.")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="crossborder.db")
    ap.add_argument("--url", help="base URL of the running app, e.g. http://127.0.0.1:8000")
    a = ap.parse_args()
    try:
        conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        sys.exit(f"cannot open {a.db}: {e}")
    print(f"database: {a.db}")
    for step in (products, orders, batches, sheet):
        try:
            step(conn)
        except Exception as e:                    # a diagnostic must not crash
            print(f"  ({step.__name__} failed: {e})")
    conn.close()
    if a.url:
        api(a.url)
    print("\nPaste this whole output back if something above looks wrong.")


if __name__ == "__main__":
    main()
