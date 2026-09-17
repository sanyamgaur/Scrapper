"""Load the Blinkit crawl into the cross-border catalogue.

Idempotent: re-running upserts by product_id, so a fresh crawl updates prices
and stock without losing classifications, overrides or order history.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from .db import connect, DB_PATH
from .packparse import parse_pack
from .shipping import ShippingEngine


def ingest_csv(csv_path: Path | str, db_path: Path | str = DB_PATH) -> int:
    conn = connect(db_path)
    ship = ShippingEngine()
    rows = list(csv.DictReader(open(csv_path, newline="", encoding="utf-8")))

    payload = []
    for r in rows:
        pack = parse_pack(r.get("unit"))
        # Precompute the shipping weight now so the storefront never has to.
        est_g, _ = ship.item_weight_g(r)
        payload.append((
            r.get("product_id"), r.get("name"), r.get("brand"), r.get("unit"),
            _f(r.get("price")), _f(r.get("mrp")), _f(r.get("discount_pct")),
            _i(r.get("in_stock")), r.get("super_category"), r.get("category_name"),
            r.get("group_name"), r.get("merchant_id"), r.get("image"),
            pack.net_g, pack.net_ml, pack.pieces, pack.confidence,
            round(est_g, 1), _i(r.get("scraped_at")),
        ))

    conn.executemany("""
        INSERT INTO products (product_id,name,brand,unit,price_inr,mrp_inr,
            discount_pct,in_stock,super_category,category_name,group_name,
            merchant_id,image,net_g,net_ml,pieces,pack_confidence,est_weight_g,scraped_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(product_id) DO UPDATE SET
            name=excluded.name, brand=excluded.brand, unit=excluded.unit,
            price_inr=excluded.price_inr, mrp_inr=excluded.mrp_inr,
            discount_pct=excluded.discount_pct, in_stock=excluded.in_stock,
            super_category=excluded.super_category, category_name=excluded.category_name,
            group_name=excluded.group_name, image=excluded.image,
            net_g=excluded.net_g, net_ml=excluded.net_ml, pieces=excluded.pieces,
            pack_confidence=excluded.pack_confidence, est_weight_g=excluded.est_weight_g,
            scraped_at=excluded.scraped_at
    """, payload)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    conn.close()
    return n


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "inventory_delhi.csv"
    print(f"ingested {ingest_csv(src)} products")
