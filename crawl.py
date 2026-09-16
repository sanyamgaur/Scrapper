#!/usr/bin/env python3
"""
Step 2 of 2: replay Blinkit's listing API across every category leaf until each
one is exhausted.

    python crawl.py --session session_delhi.json --db blinkit.db --csv out.csv

Resume-safe: re-running skips category leaves already completed.

Why this is shaped the way it is
--------------------------------
Blinkit's limiter is a token bucket, measured not guessed: ~17 requests of
burst, then it refills at roughly 0.6/s. Pushing past that does not just waste
the extra calls, it *lowers* good throughput (measured 0.62 good req/s when
pacing at 1/s, 0.50 when pacing at 3/s). So requests -- not connections, not
concurrency -- are the scarce resource, and the only way to go faster is to
carry more products home per request:

  * `limit` IS honoured, on every page except the first. 15/page is what the
    website asks for, not a cap. At limit=90 a leaf costs ~1/6 the requests.
  * A page shorter than `limit` is the last page, so there is no need to spend
    a request discovering an empty one. Saves one request per leaf.
  * Very deep leaves (>1 page of drift) lose ~2-4% of SKUs at any single page
    size, and the loss is systematic, not random -- repeating the same walk
    returns the same set. A second pass at a *different* page size lands on
    different boundaries and recovers them, so deep leaves get one.
  * /v1/layout/search has its own separate bucket: it still answers 200 while
    listing is returning 429. --with-search spends that second budget instead
    of letting it idle.
"""
import argparse
import asyncio
import csv
import json
import random
import sqlite3
import sys
import time
import zlib

import httpx

from blinkit_parse import extract_products

FIRST_PAGE_SIZE = 15    # page 0 always returns 15; `limit` only bites after it
LISTING_URL = "https://blinkit.com/v1/layout/listing_widgets"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id     TEXT,
    location       TEXT,
    name           TEXT,
    brand          TEXT,
    unit           TEXT,
    price          REAL,
    mrp            REAL,
    discount_pct   REAL,
    in_stock       INTEGER,
    merchant_id    TEXT,
    image          TEXT,
    collection_uuid     TEXT,
    collection_group_id TEXT,
    category_name  TEXT,
    group_name     TEXT,
    super_category TEXT,
    source         TEXT,
    query          TEXT,
    raw            TEXT,
    scraped_at     INTEGER,
    PRIMARY KEY (product_id, location)
);
-- A product routinely appears under more than one leaf (a 2-in-1 shows up in
-- both Shampoo and Conditioner). products has one row per product, so it can
-- only remember the last leaf that wrote it; this table keeps every placement,
-- which is what "list everything in Ice Creams > Tubs" actually needs.
CREATE TABLE IF NOT EXISTS product_categories (
    product_id     TEXT,
    location       TEXT,
    collection_uuid     TEXT,
    collection_group_id TEXT,
    category_name  TEXT,
    group_name     TEXT,
    super_category TEXT,
    source         TEXT,
    PRIMARY KEY (product_id, location, collection_uuid, collection_group_id)
);
CREATE TABLE IF NOT EXISTS leaves_done (
    key TEXT PRIMARY KEY, n_products INTEGER, done_at INTEGER
);
CREATE TABLE IF NOT EXISTS errors (
    key TEXT, status INTEGER, detail TEXT, at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cat  ON products(category_name, group_name);
CREATE INDEX IF NOT EXISTS idx_name ON products(name);
CREATE INDEX IF NOT EXISTS idx_pc_cat ON product_categories(group_name, category_name);
CREATE INDEX IF NOT EXISTS idx_pc_pid ON product_categories(product_id);
"""


def db_open(path):
    con = sqlite3.connect(path, timeout=60)
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.commit()
    return con


def unpack_raw(v):
    """products.raw is zlib-compressed JSON by default; plain JSON with --raw-plain."""
    if v is None:
        return None
    if isinstance(v, bytes):
        return json.loads(zlib.decompress(v).decode())
    return json.loads(v)


class Bucket:
    """Token bucket that mirrors the server's, so we stop before it says stop.

    Refill rate is the thing we cannot know exactly and that drifts by time of
    day, so it is learned: a clean streak nudges it up, a 429 means the estimate
    was too high and cuts it down. Converges on the real ceiling from below,
    which is the only side worth being wrong on -- overshooting costs more
    throughput than it buys.
    """

    def __init__(self, rate, capacity, min_rate=0.15, max_rate=3.0, name=""):
        self.rate = rate
        self.capacity = capacity
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.name = name
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self.lock = None          # built in start(), inside the live loop
        self.ok_streak = 0
        self.n_429 = 0
        self.n_req = 0

    def start(self):
        self.lock = asyncio.Lock()

    async def take(self):
        while True:
            async with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity,
                                  self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    self.n_req += 1
                    return
                wait = (1.0 - self.tokens) / self.rate
            await asyncio.sleep(min(wait, 5.0) + random.uniform(0, 0.05))

    def on_ok(self):
        self.ok_streak += 1
        # Only probe upward once the current rate has clearly proven itself.
        if self.ok_streak >= 25:
            self.ok_streak = 0
            self.rate = min(self.max_rate, self.rate * 1.08)

    def on_429(self):
        self.n_429 += 1
        self.ok_streak = 0
        self.rate = max(self.min_rate, self.rate * 0.80)
        self.tokens = 0.0         # the server's bucket is empty; match it
        self.updated = time.monotonic()


class Crawler:
    def __init__(self, session, con, args):
        self.s = session
        self.con = con
        self.args = args
        tmpl = session["templates"].get("listing") or session["templates"].get("tag_collections")
        if not tmpl:
            sys.exit("session has no usable template -- re-run discover.py")
        self.headers = dict(tmpl["headers"])
        self.headers["content-type"] = "application/json"
        self.headers["lat"] = str(session["lat"])
        self.headers["lon"] = str(session["lon"])
        self.cookies = session.get("cookies") or {}
        self.location = "%s,%s" % (session["lat"], session["lon"])
        self.sem = None
        self.bucket = Bucket(args.rate, args.burst, max_rate=args.max_rate, name="listing")
        self.done = set(r[0] for r in con.execute("SELECT key FROM leaves_done"))
        self.seen = set()          # product ids already written this run
        self.n_new = 0
        self.n_req = 0
        self.n_429 = 0
        self.n_pages = 0
        self.n_prod_seen = 0
        self.pending = []          # buffered rows, flushed per leaf
        self.pending_cats = []     # buffered (product, leaf) placements
        self.t0 = time.time()
        self.stop = False

    # -- compatibility shims for sweep_search.py -----------------------------
    @property
    def penalty(self):
        return 1.0 / max(self.bucket.rate, 0.01)

    # -- transport ----------------------------------------------------------
    async def post(self, client, url, body, key, bucket=None, headers=None):
        bucket = bucket or self.bucket
        headers = headers or self.headers
        delay = 1.0
        for attempt in range(self.args.retries + 1):
            await bucket.take()
            async with self.sem:
                try:
                    r = await client.post(url, headers=headers,
                                          content=json.dumps(body).encode(),
                                          timeout=self.args.timeout)
                    self.n_req += 1
                except (httpx.HTTPError, OSError) as e:
                    if attempt == self.args.retries:
                        self._err(key, -1, repr(e)); return None
                    await asyncio.sleep(delay); delay *= 2; continue

            if r.status_code == 200:
                bucket.on_ok()
                try:
                    return r.json()
                except ValueError:
                    self._err(key, 200, "non-json"); return None
            if r.status_code in (401, 403):
                self._err(key, r.status_code, "auth expired")
                self.stop = True
                return None
            if r.status_code == 429 or r.status_code >= 500:
                if r.status_code == 429:
                    self.n_429 += 1
                    bucket.on_429()
                if attempt == self.args.retries:
                    self._err(key, r.status_code, "gave up"); return None
                wait = float(r.headers.get("retry-after") or delay)
                await asyncio.sleep(wait + random.uniform(0, 1))
                delay = max(delay * 2, wait * 2); continue
            self._err(key, r.status_code, r.text[:200]); return None
        return None

    def _err(self, key, status, detail):
        self.con.execute("INSERT INTO errors VALUES (?,?,?,?)",
                         (key, status, detail, int(time.time())))

    # -- storage ------------------------------------------------------------
    def _raw_blob(self, raw):
        if self.args.no_raw:
            return None
        if self.args.raw_plain:
            return json.dumps(raw, ensure_ascii=False)
        # ~10x smaller than the JSON text, and the write is what costs time
        return zlib.compress(json.dumps(raw, ensure_ascii=False).encode(), 6)

    def save(self, products, cat, source="category", query=None):
        now = int(time.time())
        for p in products:
            self.pending.append((
                p["product_id"], self.location, p["name"], p["brand"], p["unit"],
                p["price"], p["mrp"], p["discount_pct"],
                None if p["in_stock"] is None else int(p["in_stock"]),
                str(p["merchant_id"]) if p["merchant_id"] is not None else None,
                p["image"], cat.get("collection_uuid"), cat.get("collection_group_id"),
                cat.get("category_name"), cat.get("group_name"),
                cat.get("super_category"), source, query,
                self._raw_blob(p["raw"]), now))
            if p["product_id"] not in self.seen:
                self.seen.add(p["product_id"])
                self.n_new += 1
        if len(self.pending) >= 400 or len(self.pending_cats) >= 800:
            self.flush()

    def note_cats(self, products, cat, source="category"):
        """Record every (product, leaf) placement, new product or not.

        save() only receives products not yet written, so placements cannot ride
        along with it: a 2-in-1 first seen under Shampoo would never be recorded
        under Conditioner."""
        for p in products:
            self.pending_cats.append((
                p["product_id"], self.location, cat.get("collection_uuid") or "",
                cat.get("collection_group_id") or "", cat.get("category_name"),
                cat.get("group_name"), cat.get("super_category"), source))

    def flush(self):
        if self.pending:
            self.con.executemany(
                "INSERT OR REPLACE INTO products VALUES (%s)" % ",".join("?" * 20),
                self.pending)
            self.pending.clear()
        if self.pending_cats:
            self.con.executemany(
                "INSERT OR REPLACE INTO product_categories VALUES (%s)" % ",".join("?" * 8),
                self.pending_cats)
            self.pending_cats.clear()

    # -- crawling -----------------------------------------------------------
    async def crawl_leaf(self, client, cat):
        try:
            return await self._crawl_leaf(client, cat)
        except Exception as e:
            self._err("leaf:%s:%s" % (cat.get("collection_uuid"),
                                      cat.get("collection_group_id")), -2, repr(e))
            self.con.commit()
            return 0

    async def _walk(self, client, cat, key, limit, known):
        """One pass over a leaf at a given page size. Returns the ids it saw.

        Blinkit pages by echoing the previous response's postback_params back
        with the offset advanced by however many items actually arrived."""
        uuid = cat["collection_uuid"]
        gid = str(cat["collection_group_id"])
        body = {"collection_group_id": gid, "collection_uuid": uuid}
        offset, seen = 0, set()

        for page in range(self.args.max_pages):
            if self.stop:
                break
            url = LISTING_URL if page == 0 else (
                "%s?offset=%d&limit=%d" % (LISTING_URL, offset, limit))
            data = await self.post(client, url, body, key)
            if data is None:
                return seen
            self.n_pages += 1

            prods = extract_products(data, {})
            self.n_prod_seen += len(prods)
            # `seen` decides whether this pass is still making progress;
            # `known` only decides what is worth writing. Conflating them makes
            # the verify pass stop on its first page, since by then everything
            # is already known.
            page_new = [p for p in prods if p["product_id"] not in seen]
            for p in prods:
                seen.add(p["product_id"])
            self.note_cats(prods, cat)
            fresh = [p for p in page_new if p["product_id"] not in known]
            if fresh:
                self.save(fresh, cat)
            if not prods:
                break
            # A short page is the last page -- do not spend a request proving it.
            if page > 0 and len(prods) < limit:
                break
            if page > 0 and not page_new:
                break

            offset += len(prods)
            pb = data.get("postback_params") or {}
            body = dict(pb)
            body.update({"collection_group_id": gid, "collection_uuid": uuid,
                         "offset": str(offset), "limit": str(limit),
                         "page_index": str(page + 1)})
        return seen

    async def _crawl_leaf(self, client, cat):
        uuid = cat["collection_uuid"]
        gid = str(cat["collection_group_id"])
        key = "leaf:%s:%s" % (uuid, gid)
        if key in self.done:
            return 0

        ids = await self._walk(client, cat, key, self.args.page_limit, set())

        # Deep leaves shed a few percent at any one page size, always the same
        # ones. A second pass at a different size straddles different page
        # boundaries and picks them up.
        if (self.args.verify_deep and not self.stop
                and len(ids) >= self.args.deep_threshold):
            extra = await self._walk(client, cat, key, self.args.verify_limit, ids)
            ids |= extra

        self.flush()
        self.con.execute("INSERT OR REPLACE INTO leaves_done VALUES (?,?,?)",
                         (key, len(ids), int(time.time())))
        self.con.commit()
        return len(ids)

    def progress(self, done, total, label="leaves"):
        el = time.time() - self.t0
        print("\r[%d/%d %s] %d products | %d req (%d pages) | %.0fs | "
              "%.2f prod/s | %.1f prod/req | %d x429 | rate %.2f/s     "
              % (done, total, label, self.n_new, self.n_req, self.n_pages, el,
                 self.n_new / max(el, 1), self.n_prod_seen / max(self.n_pages, 1),
                 self.n_429, self.bucket.rate), end="", file=sys.stderr)

    async def run(self, cats, extra_tasks=()):
        self.sem = asyncio.Semaphore(self.args.concurrency)
        self.bucket.start()
        limits = httpx.Limits(max_connections=self.args.concurrency * 2,
                              max_keepalive_connections=self.args.concurrency * 2)
        async with httpx.AsyncClient(http2=True, limits=limits,
                                     cookies=self.cookies, follow_redirects=True) as client:
            side = [asyncio.ensure_future(t(client)) for t in extra_tasks]
            tasks = [self.crawl_leaf(client, c) for c in cats]
            done = 0
            for fut in asyncio.as_completed(tasks):
                await fut
                done += 1
                self.progress(done, len(cats))
            for f in side:
                try:
                    await f
                except Exception as e:
                    self._err("side", -3, repr(e))
            self.flush(); self.con.commit()
        print("", file=sys.stderr)


COLS = ["product_id", "name", "brand", "unit", "price", "mrp", "discount_pct",
        "in_stock", "super_category", "category_name", "group_name",
        "merchant_id", "image", "source", "query", "location", "scraped_at"]


def export_csv(con, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        for row in con.execute(
                "SELECT %s FROM products ORDER BY super_category,category_name,group_name,name"
                % ",".join(COLS)):
            w.writerow(row)


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="session.json")
    ap.add_argument("--db", default="blinkit.db")
    ap.add_argument("--csv")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--page-limit", type=int, default=90,
                    help="products per page. Page 0 always returns 15; this "
                         "applies from page 1 on. 15 is the site's own value "
                         "and is ~6x more requests for the same catalog.")
    ap.add_argument("--verify-deep", action="store_true", default=True,
                    help="second pass at a different page size on deep leaves")
    ap.add_argument("--no-verify-deep", dest="verify_deep", action="store_false")
    ap.add_argument("--deep-threshold", type=int, default=100,
                    help="leaf size above which the second pass runs. Below this a\n                         leaf fits in one or two pages and has no drift.")
    ap.add_argument("--verify-limit", type=int, default=47,
                    help="page size for the second pass; must differ from "
                         "--page-limit or it lands on the same boundaries")
    ap.add_argument("--with-search", action="store_true",
                    help="also sweep the search endpoint concurrently -- it has "
                         "its own rate-limit bucket, so this is close to free")
    ap.add_argument("--search-keywords", type=int, default=120)
    ap.add_argument("--search-pages", type=int, default=12,
                    help="max pages per search keyword (used by --with-search)")
    ap.add_argument("--max-pages", type=int, default=400)
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--rate", type=float, default=0.6,
                    help="starting token refill rate, req/s. Learned from here.")
    ap.add_argument("--max-rate", type=float, default=2.5)
    ap.add_argument("--burst", type=int, default=15,
                    help="token bucket capacity; the server's measures ~17")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--limit-cats", type=int)
    ap.add_argument("--no-raw", action="store_true",
                    help="drop the raw payload column entirely")
    ap.add_argument("--raw-plain", action="store_true",
                    help="store raw as plain JSON instead of zlib-compressed")
    return ap


def main():
    args = build_parser().parse_args()

    session = json.load(open(args.session))
    cats = session["categories"]
    if args.limit_cats:
        cats = cats[:args.limit_cats]
    if not cats:
        sys.exit("session has no categories -- re-run discover.py")

    con = db_open(args.db)
    c = Crawler(session, con, args)

    extra = ()
    if args.with_search:
        from sweep_search import search_side_task
        extra = (search_side_task(c, session, args.search_keywords),)

    print("crawling %d leaves | page-limit %d | concurrency %d%s"
          % (len(cats), args.page_limit, args.concurrency,
             " | +search sweep" if args.with_search else ""), file=sys.stderr)
    asyncio.run(c.run(cats, extra))

    total = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    errs = con.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
    el = time.time() - c.t0
    print("\n%d distinct products in %s   (%d errors, %d rate-limits)"
          % (total, args.db, errs, c.n_429))
    print("%.0fs | %d requests | %.1f products per request | %.2f products/s"
          % (el, c.n_req, c.n_prod_seen / max(c.n_pages, 1), c.n_new / max(el, 1)))
    done_keys = set(r[0] for r in con.execute("SELECT key FROM leaves_done"))
    pending = sum(1 for c_ in cats
                  if "leaf:%s:%s" % (c_["collection_uuid"],
                                     c_["collection_group_id"]) not in done_keys)
    if pending > 0:
        print("%d leaves not finished -- re-run the same command to resume them."
              % pending)
    if args.csv:
        export_csv(con, args.csv)
        print("csv -> %s" % args.csv)
    if c.stop:
        print("STOPPED: auth expired. Re-run discover.py then crawl.py "
              "(it resumes).", file=sys.stderr)
    con.commit(); con.close()


if __name__ == "__main__":
    main()
