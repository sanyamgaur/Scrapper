#!/usr/bin/env python3
"""Emit site/catalog_data.js from the crawl DB, for the browsable catalog page.

One row per product, not per placement: a product that sits on several shelves
is one item that filters under all of them, rather than a duplicate per shelf.
"""
import argparse
import json
import sqlite3
import statistics
import time


def build(db, out, session_path=None):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    placements = {}          # (super, cat, group) -> index
    plist = []
    by_product = {}
    for r in con.execute("""SELECT product_id, super_category, category_name, group_name
                            FROM product_categories
                            WHERE group_name IS NOT NULL"""):
        key = (r["super_category"] or "", r["category_name"] or "", r["group_name"] or "")
        idx = placements.get(key)
        if idx is None:
            idx = placements[key] = len(plist)
            plist.append(list(key))
        by_product.setdefault(r["product_id"], []).append(idx)

    # SKUs the search sweep found that no category leaf lists. They are real
    # products with no shelf, so give them one rather than dropping them.
    orphan = placements.setdefault(("Search only", "Outside the category tree",
                                    "Search results"), len(plist))
    if orphan == len(plist):
        plist.append(["Search only", "Outside the category tree", "Search results"])

    brands, blist = {}, []
    rows = []
    prices = []
    for r in con.execute("""SELECT product_id, name, brand, unit, price, mrp,
                                   discount_pct, in_stock
                            FROM products ORDER BY name"""):
        pl = by_product.get(r["product_id"]) or [orphan]
        b = (r["brand"] or "").strip()
        bi = brands.get(b)
        if bi is None:
            bi = brands[b] = len(blist)
            blist.append(b)
        if r["price"]:
            prices.append(r["price"])
        rows.append([
            r["product_id"], r["name"] or "", bi, r["unit"] or "",
            r["price"], r["mrp"], 1 if r["in_stock"] else 0, sorted(set(pl)),
        ])

    leaves = con.execute(
        "SELECT COUNT(*) FROM leaves_done WHERE key LIKE 'leaf:%'").fetchone()[0]
    scraped = con.execute("SELECT MAX(scraped_at) FROM products").fetchone()[0]

    store = {}
    if session_path:
        try:
            s = json.load(open(session_path))
            hdr = (s.get("delivery_header") or "").split("|")
            store = {"address": (hdr[1].strip() if len(hdr) > 1 else ""),
                     "eta": (hdr[0].strip() if hdr else ""),
                     "lat": s.get("lat"), "lon": s.get("lon"),
                     "leaves_total": len(s.get("categories") or [])}
        except Exception:
            pass

    discounted = sum(1 for r in rows if r[5] and r[4] and r[5] > r[4])
    data = {
        "store": store,
        "scraped_at": scraped,
        "generated_at": int(time.time()),
        "leaves_done": leaves,
        "leaves_total": store.get("leaves_total") or 307,
        "placements": plist,
        "brands": blist,
        "rows": rows,
        "stats": {
            "products": len(rows),
            "brands": len(blist),
            "shelves": len(plist),
            "median_price": round(statistics.median(prices), 0) if prices else 0,
            "discounted": discounted,
            "in_stock": sum(1 for r in rows if r[6]),
        },
    }
    with open(out, "w") as f:
        f.write("window.CATALOG=")
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
        f.write(";")
    return data["stats"], leaves, data["leaves_total"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="blinkit.db")
    ap.add_argument("--session", default="session_delhi.json")
    ap.add_argument("--out", default="site/catalog_data.js")
    a = ap.parse_args()
    stats, done, total = build(a.db, a.out, a.session)
    import os
    print("%s  %.1f MB" % (a.out, os.path.getsize(a.out) / 1e6))
    print("  %d products, %d brands, %d shelves, %d/%d leaves"
          % (stats["products"], stats["brands"], stats["shelves"], done, total))
