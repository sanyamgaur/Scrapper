#!/usr/bin/env python3
"""
Rebuild blinkit.db from a crawl CSV, for when the DB is lost but the export
survived.

    python rebuild_db.py --csv inventory_delhi.csv --session session_delhi.json \
           --db blinkit.db

The CSV keeps every product field but not the two ids the listing API pages by
(`collection_uuid`, `collection_group_id`) -- it has the human shelf names
instead. Those ids are in the session file, so the pair is recovered by joining
on (category_name, group_name). On the Delhi catalogue that resolves every row;
anything that does not match is still written, just without shelf ids, and
check_availability.py will fall back to searching for it by name.

This is a recovery tool, not a substitute for crawling. What it cannot restore
is the multi-shelf placements: `products` holds one row per product, so the CSV
only carries the last shelf each product was written under. Re-run crawl.py for
the full picture.
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import time

from crawl import SCHEMA


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="inventory_delhi.csv")
    ap.add_argument("--session", default="session_delhi.json")
    ap.add_argument("--db", default="blinkit.db")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a db that already has products in it")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        sys.exit("no such csv: %s" % args.csv)

    shelf_ids = {}
    if os.path.exists(args.session):
        for c in json.load(open(args.session)).get("categories") or []:
            shelf_ids[(c.get("category_name"), c.get("group_name"))] = (
                c.get("collection_uuid"), str(c.get("collection_group_id")))
    else:
        print("no session file -- shelf ids will be blank, and the availability "
              "checker will fall back to search", file=sys.stderr)

    con = sqlite3.connect(args.db, timeout=60)
    con.executescript(SCHEMA)
    existing = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if existing and not args.force:
        sys.exit("%s already holds %d products -- pass --force to overwrite"
                 % (args.db, existing))

    now = int(time.time())
    prod, cats, resolved, unresolved = [], [], 0, 0
    with open(args.csv) as f:
        for r in csv.DictReader(f):
            prod.append((
                r["product_id"], r["location"], r["name"], r["brand"], r["unit"],
                num(r["price"]), num(r["mrp"]), num(r["discount_pct"]),
                int(r["in_stock"]) if r["in_stock"] not in ("", None) else None,
                r["merchant_id"], r["image"], None, None,
                r["category_name"], r["group_name"], r["super_category"],
                r["source"], r["query"] or None, None,
                int(r["scraped_at"]) if r["scraped_at"] else now))
            uuid, gid = shelf_ids.get((r["category_name"], r["group_name"]), ("", ""))
            if uuid:
                resolved += 1
            else:
                unresolved += 1
            cats.append((r["product_id"], r["location"], uuid, gid,
                         r["category_name"], r["group_name"],
                         r["super_category"], r["source"]))

    con.executemany("INSERT OR REPLACE INTO products VALUES (%s)" % ",".join("?" * 20),
                    prod)
    con.executemany("INSERT OR REPLACE INTO product_categories VALUES (%s)"
                    % ",".join("?" * 8), cats)
    con.commit()
    print("%s: %d products, %d shelf ids resolved, %d without"
          % (args.db, len(prod), resolved, unresolved))
    if unresolved:
        print("  rows without shelf ids get checked by search instead of a "
              "shelf walk, which costs one request each", file=sys.stderr)
    con.close()


def num(v):
    if v in (None, "", "None"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


if __name__ == "__main__":
    main()
