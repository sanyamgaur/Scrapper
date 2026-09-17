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

import re
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

-- ---------------------------------------------------------------- customers
-- orders previously carried only customer_zip, which cannot produce a shipping
-- label or a pack-out bin. Without this table the fulfilment half of the funnel
-- cannot exist.
CREATE TABLE IF NOT EXISTS customers (
    customer_id   TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    name          TEXT,
    phone         TEXT,
    ship_name     TEXT,
    line1         TEXT,
    line2         TEXT,
    city          TEXT,
    state         TEXT,
    zip5          TEXT,
    country       TEXT DEFAULT 'US',
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cust_email ON customers(email);

-- Order lines normalized out of orders.lines_json. A physical unit cannot be
-- allocated to a customer against a JSON blob, and shorts need a per-line owner.
CREATE TABLE IF NOT EXISTS order_lines (
    order_line_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id           TEXT NOT NULL REFERENCES orders(order_id),
    product_id         TEXT NOT NULL,
    qty                INTEGER NOT NULL,
    unit_price_inr     REAL,
    state              TEXT DEFAULT 'PENDING',
    -- PENDING -> ALLOCATED -> RECEIVED | SHORT | REFUNDED
    UNIQUE(order_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_ol_order ON order_lines(order_id);
CREATE INDEX IF NOT EXISTS idx_ol_pid   ON order_lines(product_id);

-- ------------------------------------------------------- procurement batching
-- One batch = one day's US demand. It splits into one run per dark store,
-- because a Blinkit basket is served by a single merchant.
CREATE TABLE IF NOT EXISTS procurement_batches (
    batch_id       TEXT PRIMARY KEY,          -- PB-YYYY-MM-DD
    batch_date     TEXT,
    cutoff_at      TEXT,
    sealed_at      TEXT,
    state          TEXT DEFAULT 'OPEN',
    -- OPEN -> SEALED -> PREFLIGHT -> PICKING -> RECONCILE -> CLOSED
    n_orders       INTEGER DEFAULT 0,
    n_lines        INTEGER DEFAULT 0,
    planned_inr    REAL DEFAULT 0,
    actual_inr     REAL DEFAULT 0,
    operator       TEXT,
    note           TEXT
);

-- One row per SKU per batch: this IS the operator's shopping list.
CREATE TABLE IF NOT EXISTS batch_lines (
    batch_id        TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    merchant_id     TEXT,
    qty_required    INTEGER NOT NULL,
    qty_bought      INTEGER DEFAULT 0,
    qty_received    INTEGER DEFAULT 0,
    expected_inr    REAL,
    actual_inr      REAL,
    risk_score      REAL,
    risk_bucket     TEXT,
    risk_confidence TEXT,
    value_at_risk   REAL,          -- USD revenue depending on this line
    pick_rank       INTEGER,
    cart_no         INTEGER,
    preflight_stock INTEGER,
    state           TEXT DEFAULT 'PENDING',
    -- PENDING -> IN_CART -> BOUGHT | SHORT | SUBSTITUTED | SKIPPED -> RECEIVED
    substitute_for  TEXT,
    note            TEXT,
    PRIMARY KEY (batch_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_bl_batch ON batch_lines(batch_id, pick_rank);
CREATE INDEX IF NOT EXISTS idx_bl_state ON batch_lines(batch_id, state);

-- Which customer each physical unit belongs to. Without this a short has no
-- owner and pack-out is guesswork.
CREATE TABLE IF NOT EXISTS batch_allocations (
    batch_id    TEXT NOT NULL,
    product_id  TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    qty_filled  INTEGER DEFAULT 0,
    PRIMARY KEY (batch_id, product_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_ba_order ON batch_allocations(order_id);

-- Append-only operator action log. Current state lives on the row; how it got
-- there lives here, mirroring the availability/availability_events split.
CREATE TABLE IF NOT EXISTS procurement_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   TEXT,
    product_id TEXT,
    event      TEXT,
    prev       TEXT,
    curr       TEXT,
    actor      TEXT,
    at         TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pe_batch ON procurement_events(batch_id, at);

-- ------------------------------------------------------------ stockout risk
CREATE TABLE IF NOT EXISTS stockout_risk (
    product_id  TEXT PRIMARY KEY,
    score       REAL,
    bucket      TEXT,          -- CRITICAL | HIGH | NORMAL
    confidence  TEXT,          -- low | medium | high
    signals     TEXT,          -- JSON: each contributing signal and its weight
    scored_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_risk_bucket ON stockout_risk(bucket, score DESC);

-- ------------------------------------------------------------ relist queue
CREATE TABLE IF NOT EXISTS relist_queue (
    product_id     TEXT PRIMARY KEY,
    detected_at    TEXT,
    stable_since   TEXT,          -- when the current in-stock streak began
    flap_count     INTEGER DEFAULT 0,
    status         TEXT DEFAULT 'WATCHING',
    -- WATCHING -> READY -> RELISTED | DISMISSED
    verdict        TEXT,          -- compliance verdict at detection time
    viable         INTEGER,       -- passes the pricing viability check
    note           TEXT,
    decided_at     TEXT,
    decided_by     TEXT
);
CREATE INDEX IF NOT EXISTS idx_relist_status ON relist_queue(status, stable_since);

-- Customers waiting on an out-of-stock SKU.
CREATE TABLE IF NOT EXISTS waitlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  TEXT NOT NULL,
    customer_id TEXT,
    email       TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wait_pid ON waitlist(product_id, created_at);

-- ------------------------------------------------------------- price drift
CREATE TABLE IF NOT EXISTS price_drift (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    TEXT NOT NULL,
    listed_inr    REAL,
    current_inr   REAL,
    listed_usd    REAL,
    current_usd   REAL,
    delta_usd     REAL,
    delta_pct     REAL,
    action        TEXT,      -- IGNORED | REPRICED | FLAGGED | DELISTED
    detected_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_drift_pid ON price_drift(product_id, detected_at DESC);

CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    created_at   TEXT DEFAULT (datetime('now')),
    customer_zip TEXT,
    status       TEXT DEFAULT 'PLACED',
    -- PLACED -> STOCK_CONFIRMED -> PROCURED -> PACKED -> SHIPPED -> DELIVERED
    -- failure branches: STOCK_FAILED, PROCUREMENT_FAILED, REFUNDED
    customer_id  TEXT REFERENCES customers(customer_id),
    batch_id     TEXT,
    lines_json   TEXT,          -- immutable quote snapshot
    quote_json   TEXT,
    total_usd    REAL,
    carrier      TEXT,
    tracking     TEXT
);
"""


# SQLite's `CREATE TABLE IF NOT EXISTS` is a no-op on a table that already
# exists, so adding a column to SCHEMA silently fails to reach existing
# databases. Worse, a later `CREATE INDEX` on that new column then throws, so an
# older database cannot even be opened. Columns are therefore reconciled against
# SCHEMA before it runs.
_COLDEF_SKIP = ("primary key", "unique", "foreign key", "check", "constraint")


def _expected_columns(schema: str) -> dict[str, list[tuple[str, str]]]:
    """Parse SCHEMA into {table: [(column, type_and_default), ...]}.

    Derived from SCHEMA itself rather than a hand-kept migration list, so a
    column added above is migrated automatically and this bug cannot recur.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for block in re.finditer(
            r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);",
            schema, re.S | re.I):
        table, body = block.group(1), block.group(2)
        # Strip `--` comments BEFORE splitting on commas: a comment containing a
        # comma would otherwise split mid-sentence and its tail would be read as
        # a column name.
        body = re.sub(r"--[^\n]*", "", body)
        cols: list[tuple[str, str]] = []
        depth = 0
        cur = ""
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                cols.append(cur); cur = ""
            else:
                cur += ch
        cols.append(cur)
        parsed = []
        for raw in cols:
            line = " ".join(raw.split())
            if not line or line.lower().startswith(_COLDEF_SKIP):
                continue
            parts = line.split(None, 1)
            if not parts:
                continue
            parsed.append((parts[0], parts[1] if len(parts) > 1 else ""))
        if parsed:
            out[table] = parsed
    return out


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """Add columns missing from tables that already exist. Returns what changed."""
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    applied: list[str] = []
    for table, cols in _expected_columns(SCHEMA).items():
        if table not in existing:
            continue   # CREATE TABLE will handle it
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name in have:
                continue
            # SQLite refuses ADD COLUMN with a non-constant default such as
            # datetime('now'), so add those as plain nullable columns.
            safe = re.sub(r"DEFAULT\s*\([^)]*\)", "", decl, flags=re.I)
            safe = re.sub(r"REFERENCES\s+\w+\s*\([^)]*\)", "", safe, flags=re.I)
            safe = safe.replace("PRIMARY KEY", "").replace("AUTOINCREMENT", "").strip()
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {safe}".strip())
            applied.append(f"{table}.{name}")
    if applied:
        conn.commit()
    return applied


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn
