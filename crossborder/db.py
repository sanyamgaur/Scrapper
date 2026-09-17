"""SQLite schema and helpers for the cross-border catalogue.

One database holds the whole funnel so that any question -- "why is this SKU
not listed", "what did we quote that customer", "when did this go out of stock"
-- is answerable with a join rather than a guess.

Tables:
  products          normalized SKUs from the Blinkit crawl
  classifications   current listability verdict per SKU (rebuilt on rule change)
  fired_rules       every rule that matched, kept for audit
  review_queue      clustered human work: one decision covers many SKUs
  overrides         durable human decisions; survive rule-pack rebuilds
  stock_checks      live availability readings
  orders            US-side orders and their India-side procurement state
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path("crossborder.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS products (
    product_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    brand           TEXT,
    unit            TEXT,
    price_inr       REAL,
    mrp_inr         REAL,
    discount_pct    REAL,
    in_stock        INTEGER,
    super_category  TEXT,
    category_name   TEXT,
    group_name      TEXT,
    merchant_id     TEXT,
    image           TEXT,
    -- parsed pack, denormalized so pricing never re-parses at query time
    net_g           REAL,
    net_ml          REAL,
    pieces          INTEGER,
    pack_confidence TEXT,
    est_weight_g    REAL,
    scraped_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_products_group ON products(group_name);
CREATE INDEX IF NOT EXISTS idx_products_cat   ON products(category_name);
CREATE INDEX IF NOT EXISTS idx_products_stock ON products(in_stock);

CREATE TABLE IF NOT EXISTS classifications (
    product_id    TEXT PRIMARY KEY REFERENCES products(product_id),
    verdict       TEXT NOT NULL,            -- ALLOWED | REVIEW | BLOCKED
    dimensions    TEXT,                     -- comma separated
    handling      TEXT,
    primary_rule  TEXT,
    reason        TEXT,
    authority     TEXT,
    source        TEXT,                     -- rules | llm | override
    confidence    REAL,
    classified_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_class_verdict ON classifications(verdict);

CREATE TABLE IF NOT EXISTS fired_rules (
    product_id TEXT NOT NULL,
    rule_id    TEXT NOT NULL,
    layer      TEXT,
    verdict    TEXT,
    dimension  TEXT,
    matched_on TEXT,
    PRIMARY KEY (product_id, rule_id)
);
CREATE INDEX IF NOT EXISTS idx_fired_rule ON fired_rules(rule_id);

-- The queue is clustered, not per-SKU. 21k SKUs collapse into ~530 decisions.
CREATE TABLE IF NOT EXISTS review_queue (
    cluster_id   TEXT PRIMARY KEY,          -- group_name || '|' || rule_id
    group_name   TEXT,
    rule_id      TEXT,
    dimension    TEXT,
    reason       TEXT,
    authority    TEXT,
    sku_count    INTEGER,
    sample_names TEXT,
    status       TEXT DEFAULT 'PENDING',    -- PENDING | CLEARED | KILLED
    decided_by   TEXT,
    decided_at   TEXT,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON review_queue(status, sku_count DESC);

CREATE TABLE IF NOT EXISTS overrides (
    product_id TEXT PRIMARY KEY,
    verdict    TEXT NOT NULL,
    reviewer   TEXT,
    note       TEXT,
    decided_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stock_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  TEXT NOT NULL,
    in_stock    INTEGER,
    price_inr   REAL,
    checked_at  TEXT DEFAULT (datetime('now')),
    source      TEXT
);
CREATE INDEX IF NOT EXISTS idx_stock_pid ON stock_checks(product_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    created_at   TEXT DEFAULT (datetime('now')),
    customer_zip TEXT,
    status       TEXT DEFAULT 'PLACED',
    -- PLACED -> STOCK_CONFIRMED -> PROCURED -> PACKED -> SHIPPED -> DELIVERED
    -- failure branches: STOCK_FAILED, PROCUREMENT_FAILED, REFUNDED
    lines_json   TEXT,
    quote_json   TEXT,
    total_usd    REAL,
    carrier      TEXT,
    tracking     TEXT
);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
