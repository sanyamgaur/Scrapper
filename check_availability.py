#!/usr/bin/env python3
"""
Step 5: re-check whether products are still in stock, and record what changed.

    python check_availability.py --session session_delhi.json --db blinkit.db --all
    python check_availability.py --session session_delhi.json --watch skus.txt
    python check_availability.py --session session_delhi.json --was-out   # restocks

Writes a row per product per run into `availability`, a summary into
`availability_runs`, and one row per transition into `availability_events`
(went out of stock, came back, price moved, vanished from the catalogue).
The `products` table is never modified -- it stays the baseline snapshot, and
availability history is kept alongside it.

How it keeps the request count down
-----------------------------------
Stock state does not need its own endpoint: `/v1/layout/listing_widgets`
already reports it for ~41 products per request. So this does not check
products, it checks the *shelves* they sit on, and reads the watched products
out of the result. Two consequences shape the whole script:

  * Walking a shelf stops the moment every watched product on it has been
    seen. Watching one SKU that happens to sit on page 2 of a nine-page shelf
    costs two requests, not nine.
  * A shelf holding one watched product is poor value -- you pay for a whole
    walk to learn one fact. For those, `/v1/layout/search` answers in a single
    request, and it is metered on a *separate* bucket from listing (see
    README), so those checks cost nothing from the listing budget and run
    concurrently with it.

`--strategy auto` (default) picks per shelf: shelf-walk when at least
`--shelf-threshold` watched products sit on it, search otherwise. `--dry-run`
prints the plan and the estimated request count without spending any.
"""
import asyncio
import csv
import json
import sys
import time
from urllib.parse import quote_plus

import httpx

from blinkit_parse import extract_products
from crawl import FIRST_PAGE_SIZE, LISTING_URL, Crawler, build_parser, db_open
from sweep_search import SEARCH_URL, SearchMixin

SCHEMA = """
CREATE TABLE IF NOT EXISTS availability_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    location    TEXT,
    started_at  INTEGER,
    finished_at INTEGER,
    strategy    TEXT,
    n_watched   INTEGER,
    n_seen      INTEGER,
    n_missing   INTEGER,
    n_in_stock  INTEGER,
    n_requests  INTEGER,
    n_429       INTEGER
);
CREATE TABLE IF NOT EXISTS availability (
    run_id     INTEGER,
    product_id TEXT,
    location   TEXT,
    in_stock   INTEGER,
    price      REAL,
    mrp        REAL,
    seen       INTEGER,
    via        TEXT,
    checked_at INTEGER,
    PRIMARY KEY (run_id, product_id, location)
);
CREATE TABLE IF NOT EXISTS availability_events (
    run_id     INTEGER,
    product_id TEXT,
    location   TEXT,
    event      TEXT,
    prev       TEXT,
    curr       TEXT,
    at         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_av_pid   ON availability(product_id, location);
CREATE INDEX IF NOT EXISTS idx_av_run   ON availability(run_id);
CREATE INDEX IF NOT EXISTS idx_ave_run  ON availability_events(run_id);
CREATE INDEX IF NOT EXISTS idx_ave_pid  ON availability_events(product_id);
"""

OUT_OF_STOCK = "out_of_stock"
BACK_IN_STOCK = "back_in_stock"
DISAPPEARED = "disappeared"
REAPPEARED = "reappeared"
PRICE_UP = "price_up"
PRICE_DOWN = "price_down"


def baseline_state(con, location):
    """Last known state per product: the previous run if there is one, else the
    catalogue snapshot in `products`."""
    row = con.execute("SELECT MAX(run_id) FROM availability_runs WHERE location=?",
                      (location,)).fetchone()
    prev_run = row[0] if row else None
    if prev_run:
        return prev_run, {
            r[0]: {"in_stock": r[1], "price": r[2], "seen": r[3]}
            for r in con.execute(
                "SELECT product_id, in_stock, price, seen FROM availability "
                "WHERE run_id=? AND location=?", (prev_run, location))}
    return None, {
        r[0]: {"in_stock": r[1], "price": r[2], "seen": 1}
        for r in con.execute(
            "SELECT product_id, in_stock, price FROM products WHERE location=?",
            (location,))}


def diff_events(prev, curr):
    """One product's before/after -> list of event names.

    `seen` and `in_stock` are different facts and both matter: a product the
    API stops returning entirely (delisted, or moved off every shelf we walk)
    is not the same as one the API returns and marks out of stock."""
    events = []
    if prev is None:
        return events
    was_seen = bool(prev.get("seen", 1))
    now_seen = bool(curr["seen"])
    if was_seen and not now_seen:
        return [DISAPPEARED]
    if not was_seen and now_seen:
        events.append(REAPPEARED)

    pi, ci = prev.get("in_stock"), curr["in_stock"]
    if pi is not None and ci is not None and bool(pi) != bool(ci):
        events.append(BACK_IN_STOCK if ci else OUT_OF_STOCK)

    pp, cp = prev.get("price"), curr["price"]
    if pp and cp and abs(pp - cp) >= 0.01:
        events.append(PRICE_UP if cp > pp else PRICE_DOWN)
    return events


class AvailabilityChecker(SearchMixin, Crawler):
    def __init__(self, session, con, args):
        super().__init__(session, con, args)
        con.executescript(SCHEMA)
        con.commit()
        # A session captured without a search template is still perfectly good
        # for shelf walks, so this must not be fatal until search is needed.
        self.can_search = bool((session.get("templates") or {}).get("search"))
        if self.can_search:
            self.init_search(session, con)
        self.state = {}          # product_id -> current reading
        self.n_pages_av = 0
        self.n_unchecked = 0     # watched but never successfully answered for

    # -- what to check ------------------------------------------------------
    def load_watchlist(self):
        """-> {product_id: {"name":..., "shelves":[(uuid, gid), ...]}}"""
        a = self.args
        where, params = ["location = ?"], [self.location]
        if a.brand:
            where.append("LOWER(brand) LIKE ?")
            params.append("%%%s%%" % a.brand.lower())
        if a.shelf:
            where.append("LOWER(group_name) LIKE ?")
            params.append("%%%s%%" % a.shelf.lower())
        if a.category:
            where.append("LOWER(category_name) LIKE ?")
            params.append("%%%s%%" % a.category.lower())
        if a.was_out:
            where.append("(in_stock = 0 OR in_stock IS NULL)")

        ids = None
        if a.watch:
            ids = read_watchfile(a.watch)
            if not ids:
                sys.exit("watchlist %s has no product ids" % a.watch)

        sql = ("SELECT product_id, name FROM products WHERE %s ORDER BY name"
               % " AND ".join(where))
        watch = {}
        for pid, name in self.con.execute(sql, params):
            if ids is not None and pid not in ids:
                continue
            watch[pid] = {"name": name, "shelves": []}

        if ids:
            missing = ids - set(watch)
            if missing:
                print("%d watched id(s) are not in the catalogue for this store "
                      "and will be reported as unknown: %s"
                      % (len(missing), ", ".join(sorted(missing)[:5])), file=sys.stderr)
                for pid in missing:
                    watch[pid] = {"name": None, "shelves": []}

        for pid, uuid, gid in self.con.execute(
                "SELECT product_id, collection_uuid, collection_group_id "
                "FROM product_categories WHERE location=?", (self.location,)):
            if pid in watch and uuid:
                watch[pid]["shelves"].append((uuid, str(gid)))

        if a.limit:
            watch = dict(list(watch.items())[:a.limit])
        return watch

    def plan(self, watch):
        """Split the watchlist into shelf-walks and per-product searches."""
        by_shelf = {}
        no_shelf = []
        for pid, meta in watch.items():
            if meta["shelves"]:
                # One shelf is enough to learn a product's stock; pick the
                # first so a product on five shelves is not walked five times.
                by_shelf.setdefault(meta["shelves"][0], set()).add(pid)
            else:
                no_shelf.append(pid)

        strategy = self.args.strategy
        if strategy != "shelf" and not self.can_search:
            print("session has no search template -- falling back to shelf walks. "
                  "Re-run discover.py if you want the search strategy.",
                  file=sys.stderr)
            strategy = "shelf"
        shelf_jobs, search_ids = {}, set(no_shelf)
        for shelf, pids in by_shelf.items():
            if strategy == "search":
                search_ids |= pids
            elif strategy == "shelf":
                shelf_jobs[shelf] = pids
            elif len(pids) >= self.args.shelf_threshold:
                shelf_jobs[shelf] = pids
            else:
                search_ids |= pids
        # Nothing to check it with: no shelf to walk, or no name to search by.
        unknown = {p for p in search_ids
                   if not watch[p]["name"] or not self.can_search}
        return shelf_jobs, search_ids - unknown, unknown

    # -- checking -----------------------------------------------------------
    def _record(self, pid, prods_by_id, via):
        p = prods_by_id.get(pid)
        if p is None:
            self.state[pid] = {"in_stock": None, "price": None, "mrp": None,
                               "seen": 0, "via": via}
        else:
            self.state[pid] = {
                "in_stock": None if p["in_stock"] is None else int(p["in_stock"]),
                "price": p["price"], "mrp": p["mrp"], "seen": 1, "via": via}

    async def walk_shelf(self, client, shelf, wanted):
        """Page a shelf until every watched product on it has been seen.

        Returns (found_by_id, pages_spent, ok). Split out from check_shelf so
        the continuous engine can reuse the walk and its early stop without
        inheriting check_shelf's write into self.state.

        `ok` is the difference between "the shelf does not list this product"
        and "we never got an answer". A failed request ends the walk with an
        empty `found`, which is indistinguishable from a genuine absence unless
        the caller is told. Reporting a product missing because the network was
        down -- or the session expired -- is worse than reporting nothing at
        all, so callers must check this before concluding anything."""
        uuid, gid = shelf
        key = "avail:%s:%s" % (uuid, gid)
        body = {"collection_group_id": gid, "collection_uuid": uuid}
        limit = self.args.page_limit
        offset = 0
        found = {}
        outstanding = set(wanted)
        pages = 0
        ok = True

        for page in range(self.args.max_pages):
            if self.stop:
                ok = False
                break
            url = listing_url(page, offset, limit)
            data = await self.post(client, url, body, key)
            if data is None:
                # No answer: whatever is still outstanding is unknown, not absent.
                ok = not outstanding
                break
            self.n_pages_av += 1
            pages += 1
            prods = extract_products(data, {})
            for p in prods:
                if p["product_id"] in outstanding:
                    found[p["product_id"]] = p
                    outstanding.discard(p["product_id"])
            # Every watched product on this shelf is accounted for -- the rest
            # of the shelf is someone else's inventory.
            if not outstanding:
                break
            if not prods:
                break
            if page > 0 and len(prods) < limit:
                break
            offset += len(prods)
            pb = data.get("postback_params") or {}
            body = dict(pb)
            body.update({"collection_group_id": gid, "collection_uuid": uuid,
                         "offset": str(offset), "limit": str(limit),
                         "page_index": str(page + 1)})

        return found, pages, ok

    async def check_shelf(self, client, shelf, wanted):
        found, _pages, ok = await self.walk_shelf(client, shelf, wanted)
        if not ok:
            # Record nothing. An unchecked product must not be reported as
            # missing; persist() counts these separately.
            self.n_unchecked += len(wanted)
            return 0
        for pid in wanted:
            self._record(pid, found, "shelf")
        return len(found)

    async def check_search(self, client, pid, name):
        key = "avail-search:%s" % pid
        url = "%s?q=%s&search_type=type_to_search" % (SEARCH_URL, quote_plus(name or ""))
        data = await self.post_search(client, url, {}, key)
        if data is None:
            self.n_unchecked += 1
            return 0
        found = {}
        for p in extract_products(data, {}):
            if p["product_id"] == pid:
                found[pid] = p
                break
        self._record(pid, found, "search")
        return 1 if found else 0

    async def run_check(self, shelf_jobs, search_jobs, unknown):
        for pid in unknown:
            self.state[pid] = {"in_stock": None, "price": None, "mrp": None,
                               "seen": 0, "via": "unknown"}
        self.sem = asyncio.Semaphore(self.args.concurrency)
        self.bucket.start()
        self.search_bucket.start()
        limits = httpx.Limits(max_connections=self.args.concurrency * 2,
                              max_keepalive_connections=self.args.concurrency * 2)
        total = len(shelf_jobs) + len(search_jobs)
        async with httpx.AsyncClient(http2=True, limits=limits,
                                     cookies=self.cookies,
                                     follow_redirects=True) as client:
            tasks = [self.check_shelf(client, s, p) for s, p in shelf_jobs.items()]
            tasks += [self.check_search(client, pid, name)
                      for pid, name in search_jobs]
            done = 0
            for fut in asyncio.as_completed(tasks):
                try:
                    await fut
                except Exception as e:
                    self._err("avail", -2, repr(e))
                done += 1
                if done % 5 == 0 or done == total:
                    self.progress(done, total, label="checks")
        print("", file=sys.stderr)

    # -- storage ------------------------------------------------------------
    def persist(self, watch, prev_state, started):
        now = int(time.time())
        cur = self.con.execute(
            "INSERT INTO availability_runs (location, started_at, finished_at, "
            "strategy, n_watched, n_seen, n_missing, n_in_stock, n_requests, n_429) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.location, started, now, self.args.strategy, len(watch),
             sum(1 for s in self.state.values() if s["seen"]),
             sum(1 for s in self.state.values() if not s["seen"]),
             sum(1 for s in self.state.values() if s["in_stock"]),
             self.n_req, self.n_429))
        run_id = cur.lastrowid

        self.con.executemany(
            "INSERT OR REPLACE INTO availability VALUES (?,?,?,?,?,?,?,?,?)",
            [(run_id, pid, self.location, s["in_stock"], s["price"], s["mrp"],
              s["seen"], s["via"], now) for pid, s in self.state.items()])

        events = []
        for pid, s in self.state.items():
            for ev in diff_events(prev_state.get(pid), s):
                prev = prev_state.get(pid) or {}
                events.append((run_id, pid, self.location, ev,
                               fmt_state(prev), fmt_state(s), now))
        if events:
            self.con.executemany(
                "INSERT INTO availability_events VALUES (?,?,?,?,?,?,?)", events)
        self.con.commit()
        return run_id, events


def listing_url(page, offset, limit):
    return LISTING_URL if page == 0 else (
        "%s?offset=%d&limit=%d" % (LISTING_URL, offset, limit))


def fmt_state(s):
    if not s:
        return ""
    if not s.get("seen", 1):
        return "not returned"
    stock = {1: "in stock", 0: "out of stock"}.get(
        None if s.get("in_stock") is None else int(s["in_stock"]), "unknown")
    return "%s @ %s" % (stock, "" if s.get("price") is None else s["price"])


HEADER_WORDS = {"product_id", "productid", "id", "sku", "sku_id"}


def read_watchfile(path):
    """Accepts a bare id-per-line list or any CSV with a product_id column.

    The header is detected by name rather than by the presence of a comma, so a
    single-column export with a `product_id` header does not turn its own
    header into a watched id."""
    ids = set()
    with open(path) as f:
        head = f.readline()
        f.seek(0)
        first = head.split(",")[0].strip().lower()
        if first in HEADER_WORDS:
            for r in csv.DictReader(f):
                val = next((r[k] for k in r
                            if k and k.strip().lower() in HEADER_WORDS), None)
                if val:
                    ids.add(str(val).strip())
        else:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    ids.add(line.split(",")[0].strip())
    return ids


def write_report(con, run_id, path, as_json=False):
    rows = list(con.execute(
        """SELECT a.product_id, p.name, p.brand, p.group_name,
                  a.in_stock, a.price, a.mrp, a.seen, a.via
           FROM availability a LEFT JOIN products p
             ON p.product_id = a.product_id AND p.location = a.location
           WHERE a.run_id = ? ORDER BY p.group_name, p.name""", (run_id,)))
    events = {}
    for pid, ev in con.execute(
            "SELECT product_id, event FROM availability_events WHERE run_id=?",
            (run_id,)):
        events.setdefault(pid, []).append(ev)

    cols = ["product_id", "name", "brand", "shelf", "in_stock", "price", "mrp",
            "seen", "checked_via", "events"]
    if as_json:
        out = [dict(zip(cols, list(r) + ["|".join(events.get(r[0], []))]))
               for r in rows]
        with open(path, "w") as f:
            json.dump({"run_id": run_id, "products": out}, f, indent=2,
                      ensure_ascii=False)
    else:
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow(list(r) + ["|".join(events.get(r[0], []))])
    return len(rows)


def estimate(shelf_jobs, search_jobs, con, location, page_limit):
    """Worst-case request cost of a plan, for --dry-run.

    Page 0 always returns FIRST_PAGE_SIZE no matter what `limit` asks for, so a
    shelf costs 1 + ceil((n - 15) / limit), not ceil(n / limit). Worst case
    because a walk stops as soon as every watched product on the shelf has been
    seen, which on a partial watchlist is usually well before the end."""
    sizes = {}
    for uuid, gid, n in con.execute(
            "SELECT collection_uuid, collection_group_id, COUNT(*) "
            "FROM product_categories WHERE location=? GROUP BY 1,2", (location,)):
        sizes[(uuid, str(gid))] = n
    worst = 0
    for shelf in shelf_jobs:
        n = sizes.get(shelf, page_limit)
        worst += 1 + max(0, -(-(n - FIRST_PAGE_SIZE) // page_limit))
    return worst, len(search_jobs)


def build_av_parser():
    """The crawler's flags plus this script's. Shared with the tests so they
    exercise the same defaults the CLI does."""
    ap = build_parser()
    ap.add_argument("--all", action="store_true", help="check every product")
    ap.add_argument("--watch", help="file of product ids (one per line, or a CSV "
                                    "with a product_id column)")
    ap.add_argument("--brand", help="only products whose brand matches")
    ap.add_argument("--shelf", help="only products on shelves matching")
    ap.add_argument("--category", help="only products in categories matching")
    ap.add_argument("--was-out", action="store_true",
                    help="only products last seen out of stock -- catches restocks")
    ap.add_argument("--limit", type=int, help="cap the watchlist size (testing)")
    ap.add_argument("--strategy", choices=["auto", "shelf", "search"], default="auto")
    ap.add_argument("--shelf-threshold", type=int, default=3,
                    help="under --strategy auto, walk a shelf when it holds at "
                         "least this many watched products; search otherwise")
    ap.add_argument("--report", help="write a per-product CSV report here")
    ap.add_argument("--report-json", action="store_true",
                    help="write --report as JSON instead of CSV")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and estimated requests, check nothing")
    ap.add_argument("--interval", type=float, metavar="SECONDS",
                    help="keep running, starting a new check every SECONDS. "
                         "Preferred over cron for anything faster than a few "
                         "minutes: one process keeps one rate limiter, so the "
                         "learned refill rate and token count carry over "
                         "instead of resetting on every check.")
    ap.add_argument("--max-runs", type=int,
                    help="with --interval, stop after this many checks")
    ap.set_defaults(verify_deep=False)
    return ap


def main():
    args = build_av_parser().parse_args()

    if not (args.all or args.watch or args.brand or args.shelf or args.category
            or args.was_out):
        sys.exit("pick what to check: --all, --watch FILE, --brand/--shelf/"
                 "--category, or --was-out")

    session = json.load(open(args.session))
    con = db_open(args.db)
    con.executescript(SCHEMA)
    c = AvailabilityChecker(session, con, args)

    watch = c.load_watchlist()
    if not watch:
        sys.exit("nothing matched -- is the catalogue crawled for this location?")
    shelf_jobs, search_ids, unknown = c.plan(watch)
    search_jobs = [(pid, watch[pid]["name"]) for pid in sorted(search_ids)]

    shelf_reqs, search_reqs = estimate(shelf_jobs, search_jobs, con,
                                       c.location, args.page_limit)
    print("watching %d products | %d shelf walks (<=%d listing requests) | "
          "%d searches (separate bucket)%s"
          % (len(watch), len(shelf_jobs), shelf_reqs, len(search_jobs),
             " | %d unknown" % len(unknown) if unknown else ""), file=sys.stderr)
    if args.dry_run:
        for shelf, pids in list(shelf_jobs.items())[:10]:
            print("  shelf %s/%s -> %d watched" % (shelf[0][:8], shelf[1], len(pids)),
                  file=sys.stderr)
        print("dry run: nothing checked", file=sys.stderr)
        con.close()
        return

    cycle = 0
    while True:
        cycle += 1
        run_one(c, con, args, watch, shelf_jobs, search_jobs, unknown)
        if c.stop:
            print("STOPPED: auth expired. Re-run discover.py, then this again.",
                  file=sys.stderr)
            break
        if not args.interval:
            break
        if args.max_runs and cycle >= args.max_runs:
            break
        wait = max(0.0, args.interval - (time.time() - c.cycle_started))
        if wait:
            time.sleep(wait)
    con.close()


def run_one(c, con, args, watch, shelf_jobs, search_jobs, unknown):
    """One check cycle. Safe to call repeatedly on the same checker -- and
    should be, under --interval: the token bucket lives on the checker, so
    reusing it carries the learned refill rate and the current token count
    across cycles. A fresh process per check (cron every minute, say) instead
    assumes a full burst every time the server may not actually have, and
    walks straight into 429s."""
    c.state = {}
    c.cycle_started = time.time()
    req0, r429_0 = c.n_req, c.n_429

    prev_run, prev_state = baseline_state(con, c.location)
    asyncio.run(c.run_check(shelf_jobs, search_jobs, unknown))
    run_id, events = c.persist(watch, prev_state, int(c.cycle_started))

    seen = sum(1 for s in c.state.values() if s["seen"])
    instock = sum(1 for s in c.state.values() if s["in_stock"])
    by_ev = {}
    for e in events:
        by_ev[e[3]] = by_ev.get(e[3], 0) + 1

    print("\n[%s] run %d | %d watched | %d found | %d in stock | "
          "%d requests (%d 429) | %.0fs"
          % (time.strftime("%H:%M:%S"), run_id, len(watch), seen, instock,
             c.n_req - req0, c.n_429 - r429_0, time.time() - c.cycle_started))
    print("compared against %s"
          % ("run %d" % prev_run if prev_run else "the catalogue snapshot"))
    if by_ev:
        for ev in (OUT_OF_STOCK, BACK_IN_STOCK, PRICE_UP, PRICE_DOWN,
                   DISAPPEARED, REAPPEARED):
            if by_ev.get(ev):
                print("  %-15s %d" % (ev, by_ev[ev]))
        for e in events[:15]:
            print("    %-14s %-9s %s -> %s" % (e[3], e[1], e[4], e[5]))
        if len(events) > 15:
            print("    ... %d more (see availability_events)" % (len(events) - 15))
    else:
        print("  no changes since the last check")

    if args.report:
        path = args.report
        if args.interval:
            base, _, ext = path.rpartition(".")
            path = "%s-%d.%s" % (base or path, run_id, ext or "csv")
        n = write_report(con, run_id, path, args.report_json)
        print("report -> %s (%d rows)" % (path, n))
    return run_id, events


if __name__ == "__main__":
    main()
