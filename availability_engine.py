#!/usr/bin/env python3
"""
A continuous inventory availability engine: keeps a live picture of stock for
every watched SKU, emits changes as they are detected, and reports how fresh
that picture actually is.

    python availability_engine.py --session session_delhi.json --db blinkit.db \
           --all --hot hot_skus.txt --events-out events.jsonl

What this can and cannot do
---------------------------
Blinkit publishes no stock feed and no webhook, so this polls, and polling is
bounded by their rate limiter: ~0.635 requests/second, measured. Checking all
31,366 SKUs costs ~694 requests, so a full sweep takes ~18 minutes and there is
no arrangement of code that makes it one second. Second-by-second truth across
a whole catalogue is not available at any price short of running a thousand
parallel identities against their limiter.

What *is* available is choosing where the freshness goes. One request returns a
whole page of a shelf -- up to 90 products, each with its stock state -- so the
scarce resource buys wildly different amounts of information depending on which
shelf it is spent on. This engine therefore schedules *shelves*, not SKUs:

    value(shelf) = sum over watched SKUs on it of
                   weight x seconds-since-checked x volatility
    cost(shelf)  = pages it actually took last time (learned, not assumed)
    pick         = highest value / cost, continuously

The consequences are the point:

  * A hot SKU on a small shelf gets re-checked every few seconds, because it
    scores high and costs one request.
  * A SKU that flips in and out of stock often earns a rising volatility
    score, so the engine spends more on it -- attention follows churn rather
    than a fixed schedule.
  * The long tail is swept with whatever budget is left, so nothing goes
    unchecked indefinitely.

And because the honest number is the one that matters, the status line reports
measured staleness percentiles -- the real answer to "how live is this?" -- for
the hot set and for everything, rather than a cadence you configured and hoped
for.
"""
import argparse
import asyncio
import heapq
import json
import math
import os
import signal
import sys
import time

import httpx

from blinkit_parse import extract_products
from check_availability import (BACK_IN_STOCK, DISAPPEARED, OUT_OF_STOCK,
                                PRICE_DOWN, PRICE_UP, REAPPEARED,
                                AvailabilityChecker, build_av_parser,
                                diff_events, quote_plus, read_watchfile)
from crawl import FIRST_PAGE_SIZE, db_open
from sweep_search import SEARCH_URL

LIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS availability_live (
    product_id   TEXT,
    location     TEXT,
    in_stock     INTEGER,
    price        REAL,
    mrp          REAL,
    seen         INTEGER,
    last_checked INTEGER,
    last_changed INTEGER,
    checks       INTEGER,
    flips        INTEGER,
    PRIMARY KEY (product_id, location)
);
CREATE INDEX IF NOT EXISTS idx_live_checked ON availability_live(last_checked);
"""


class Scheduler:
    """Decides which shelf to spend the next requests on.

    Holds one entry per watched SKU and one aggregate per shelf. Everything is
    recomputed from timestamps rather than kept on a timer, so a shelf that has
    just been walked naturally falls to the bottom and climbs back as its SKUs
    go stale."""

    def __init__(self, sku_shelf, weights, sizes, page_limit, half_life):
        self.sku_shelf = sku_shelf          # pid -> shelf
        self.weights = weights              # pid -> float
        self.shelf_skus = {}
        for pid, shelf in sku_shelf.items():
            self.shelf_skus.setdefault(shelf, set()).add(pid)
        self.last_checked = {pid: 0.0 for pid in sku_shelf}
        self.flips = {pid: 0 for pid in sku_shelf}
        self.last_flip = {pid: 0.0 for pid in sku_shelf}
        self.half_life = half_life
        # Start from the paper estimate, then replace it with what walks
        # actually cost -- early stop means the estimate is usually pessimistic.
        self.cost = {}
        for shelf, pids in self.shelf_skus.items():
            n = sizes.get(shelf, page_limit)
            self.cost[shelf] = 1.0 + max(0, math.ceil((n - FIRST_PAGE_SIZE) / page_limit))
        self.inflight = set()

    def volatility(self, pid, now):
        """Recent churn, decayed. A SKU that flipped an hour ago is more
        interesting than one that flipped last week, and one that has never
        flipped still gets a floor of 1 so it is not starved forever."""
        f = self.flips.get(pid, 0)
        if not f:
            return 1.0
        age = now - self.last_flip.get(pid, 0.0)
        return 1.0 + f * 0.5 ** (age / self.half_life)

    def shelf_value(self, shelf, now):
        v = 0.0
        for pid in self.shelf_skus[shelf]:
            staleness = now - self.last_checked.get(pid, 0.0)
            v += self.weights.get(pid, 1.0) * staleness * self.volatility(pid, now)
        return v

    def pick(self, now, n=1):
        """The n best shelves by value per request, skipping in-flight ones.

        A zero score is still returned. Every shelf is worth re-checking
        eventually, and when all of them are equally fresh -- at startup, or
        the instant after a full sweep -- a "nothing scores above zero" guard
        would stall the engine outright instead of simply picking one."""
        scored = []
        for shelf in self.shelf_skus:
            if shelf in self.inflight:
                continue
            c = max(0.5, self.cost.get(shelf, 1.0))
            scored.append((self.shelf_value(shelf, now) / c, shelf))
        if not scored:
            return []
        return [shelf for _score, shelf in heapq.nlargest(n, scored)]

    def mark_walked(self, shelf, pages, now):
        for pid in self.shelf_skus[shelf]:
            self.last_checked[pid] = now
        if pages:
            # EMA: converge on the real cost without letting one odd walk
            # dominate.
            self.cost[shelf] = 0.7 * self.cost.get(shelf, pages) + 0.3 * pages

    def mark_flip(self, pid, now):
        self.flips[pid] = self.flips.get(pid, 0) + 1
        self.last_flip[pid] = now

    def staleness(self, now, pids=None):
        pids = pids if pids is not None else self.last_checked.keys()
        return sorted(now - self.last_checked.get(p, 0.0) for p in pids)


def pct(sorted_vals, q):
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(len(sorted_vals) * q))
    return sorted_vals[i]


def human(s):
    if s < 90:
        return "%.0fs" % s
    if s < 5400:
        return "%.1fm" % (s / 60)
    return "%.1fh" % (s / 3600)


class Engine(AvailabilityChecker):
    def __init__(self, session, con, args):
        super().__init__(session, con, args)
        con.executescript(LIVE_SCHEMA)
        con.commit()
        self.live = {}
        self.run_id = None
        self.events_file = None
        self.n_events = 0
        self.started = time.time()
        self.shutdown = False

    # -- state --------------------------------------------------------------
    def load_live(self, watch):
        for r in self.con.execute(
                "SELECT product_id, in_stock, price, mrp, seen, last_checked, "
                "last_changed, checks, flips FROM availability_live WHERE location=?",
                (self.location,)):
            if r[0] in watch:
                self.live[r[0]] = {"in_stock": r[1], "price": r[2], "mrp": r[3],
                                   "seen": r[4], "last_checked": r[5],
                                   "last_changed": r[6], "checks": r[7] or 0,
                                   "flips": r[8] or 0}
        # Anything not yet in the live table starts from the catalogue snapshot,
        # so the first check of a SKU reports a real transition rather than a
        # spurious one.
        for pid, in_stock, price, mrp in self.con.execute(
                "SELECT product_id, in_stock, price, mrp FROM products WHERE location=?",
                (self.location,)):
            if pid in watch and pid not in self.live:
                self.live[pid] = {"in_stock": in_stock, "price": price, "mrp": mrp,
                                  "seen": 1, "last_checked": 0, "last_changed": 0,
                                  "checks": 0, "flips": 0}

    def open_run(self, n_watched):
        cur = self.con.execute(
            "INSERT INTO availability_runs (location, started_at, finished_at, "
            "strategy, n_watched, n_seen, n_missing, n_in_stock, n_requests, n_429) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.location, int(self.started), None, "engine", n_watched,
             0, 0, 0, 0, 0))
        self.run_id = cur.lastrowid
        self.con.commit()

    def close_run(self):
        self.con.execute(
            "UPDATE availability_runs SET finished_at=?, n_requests=?, n_429=?, "
            "n_seen=?, n_in_stock=? WHERE run_id=?",
            (int(time.time()), self.n_req, self.n_429,
             sum(1 for v in self.live.values() if v["seen"]),
             sum(1 for v in self.live.values() if v["in_stock"]), self.run_id))
        self.con.commit()

    def apply(self, pid, product, now, sched):
        """Fold one reading into live state, emitting any transition."""
        prev = self.live.get(pid) or {}
        if product is None:
            curr = {"in_stock": None, "price": None, "mrp": None, "seen": 0}
        else:
            curr = {"in_stock": None if product["in_stock"] is None
                              else int(product["in_stock"]),
                    "price": product["price"], "mrp": product["mrp"], "seen": 1}

        events = diff_events(prev, curr) if prev.get("checks") or prev.get("seen") is not None else []
        entry = {**curr,
                 "last_checked": int(now),
                 "last_changed": int(now) if events else prev.get("last_changed", 0),
                 "checks": prev.get("checks", 0) + 1,
                 "flips": prev.get("flips", 0) + (1 if events else 0)}
        self.live[pid] = entry

        for ev in events:
            if ev in (OUT_OF_STOCK, BACK_IN_STOCK, DISAPPEARED, REAPPEARED):
                sched.mark_flip(pid, now)
            self.emit(pid, ev, prev, curr, now)
        return events

    def emit(self, pid, event, prev, curr, now):
        self.n_events += 1
        self.con.execute(
            "INSERT INTO availability_events VALUES (?,?,?,?,?,?,?)",
            (self.run_id, pid, self.location, event,
             json.dumps(_slim(prev)), json.dumps(_slim(curr)), int(now)))
        rec = {"ts": int(now), "product_id": pid, "event": event,
               "prev": _slim(prev), "curr": _slim(curr)}
        line = json.dumps(rec, ensure_ascii=False)
        if self.events_file:
            self.events_file.write(line + "\n")
            self.events_file.flush()
        if self.args.print_events:
            print(line, flush=True)

    def persist_live(self):
        self.con.executemany(
            "INSERT OR REPLACE INTO availability_live VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(pid, self.location, v["in_stock"], v["price"], v["mrp"], v["seen"],
              v["last_checked"], v["last_changed"], v["checks"], v["flips"])
             for pid, v in self.live.items()])
        self.con.commit()

    # -- the loop -----------------------------------------------------------
    async def worker(self, client, sched, watch):
        while not self.shutdown and not self.stop:
            # Always yield. A walk that returns without awaiting -- everything
            # cached, or a stubbed transport -- would otherwise spin this loop
            # without ever letting the status and shutdown tasks run.
            await asyncio.sleep(0)
            now = time.time()
            picks = sched.pick(now, 1)
            if not picks:
                await asyncio.sleep(0.2)
                continue
            shelf = picks[0]
            sched.inflight.add(shelf)
            try:
                wanted = set(sched.shelf_skus[shelf])
                found, pages = await self.walk_shelf(client, shelf, wanted)
                now = time.time()
                for pid in wanted:
                    self.apply(pid, found.get(pid), now, sched)
                sched.mark_walked(shelf, pages, now)
            except Exception as e:
                self._err("engine:%s" % (shelf,), -2, repr(e))
                sched.mark_walked(shelf, None, time.time())
            finally:
                sched.inflight.discard(shelf)

    async def search_worker(self, client, sched, names, hot):
        """Refresh hot SKUs one at a time over the search endpoint.

        This exists because measuring the shelf scheduler showed weighting
        alone barely helps a *scattered* hot set: one hot SKU on an 800-product
        shelf is outvoted by its cold neighbours, so its shelf never wins on
        value-per-request. Search costs one request per SKU regardless of which
        shelf it lives on, and runs on its own rate bucket, so the hot set gets
        refreshed without spending any of the listing budget that is sweeping
        the catalogue."""
        pool = sorted(hot)
        while not self.shutdown and not self.stop and pool:
            await asyncio.sleep(0)
            now = time.time()
            pid = max(pool, key=lambda p: now - sched.last_checked.get(p, 0.0))
            name = names.get(pid)
            if not name:
                pool.remove(pid)
                continue
            try:
                data = await self.post_search(
                    client, "%s?q=%s&search_type=type_to_search"
                    % (SEARCH_URL, quote_plus(name)), {}, "engine-search:%s" % pid)
                product = None
                if data is not None:
                    for p in extract_products(data, {}):
                        if p["product_id"] == pid:
                            product = p
                            break
                now = time.time()
                self.apply(pid, product, now, sched)
                sched.last_checked[pid] = now
            except Exception as e:
                self._err("engine-search:%s" % pid, -2, repr(e))

    async def status_loop(self, sched, hot):
        while not self.shutdown and not self.stop:
            await asyncio.sleep(self.args.status_every)
            self.status(sched, hot)
            self.persist_live()

    def status(self, sched, hot):
        now = time.time()
        all_s = sched.staleness(now)
        hot_s = sched.staleness(now, hot) if hot else []
        el = now - self.started
        parts = [
            "%s up" % human(el),
            "%d SKUs" % len(all_s),
            "freshness p50 %s p90 %s max %s" % (human(pct(all_s, .5)),
                                                human(pct(all_s, .9)),
                                                human(all_s[-1] if all_s else 0)),
        ]
        if hot_s:
            parts.insert(2, "hot p50 %s max %s" % (human(pct(hot_s, .5)),
                                                   human(hot_s[-1])))
        parts += ["%d events" % self.n_events,
                  "%d req (%.2f/s, %d x429)" % (self.n_req,
                                                self.n_req / max(el, 1), self.n_429),
                  "rate %.2f/s" % self.bucket.rate]
        print("[engine] " + " | ".join(parts), file=sys.stderr, flush=True)

    async def serve(self, sched, watch, hot):
        names = {pid: meta["name"] for pid, meta in watch.items()}
        self.sem = asyncio.Semaphore(self.args.concurrency)
        self.bucket.start()
        if self.can_search:
            self.search_bucket.start()
        limits = httpx.Limits(max_connections=self.args.concurrency * 2,
                              max_keepalive_connections=self.args.concurrency * 2)
        async with httpx.AsyncClient(http2=True, limits=limits,
                                     cookies=self.cookies,
                                     follow_redirects=True) as client:
            tasks = [asyncio.ensure_future(self.worker(client, sched, watch))
                     for _ in range(self.args.workers)]
            if hot and self.can_search and not self.args.no_hot_search:
                tasks.append(asyncio.ensure_future(
                    self.search_worker(client, sched, names, hot)))
            tasks.append(asyncio.ensure_future(self.status_loop(sched, hot)))
            if self.args.run_for:
                await asyncio.sleep(self.args.run_for)
                self.shutdown = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def _slim(s):
    return {"in_stock": s.get("in_stock"), "price": s.get("price"),
            "seen": s.get("seen")}


def build_engine_parser():
    ap = build_av_parser()
    ap.add_argument("--hot", help="file of product ids to prioritise "
                                  "(same formats as --watch)")
    ap.add_argument("--hot-weight", type=float, default=50.0,
                    help="how much more urgent a hot SKU is than a normal one")
    ap.add_argument("--workers", type=int, default=3,
                    help="concurrent shelf walks. The rate limiter serialises "
                         "the requests anyway; more workers just keep it fed.")
    ap.add_argument("--half-life", type=float, default=3600.0,
                    help="seconds over which a stock flip stops counting "
                         "towards a SKU's volatility score")
    ap.add_argument("--no-hot-search", action="store_true",
                    help="do not refresh hot SKUs over the search endpoint. "
                         "Measured, the shelf scheduler alone barely helps a "
                         "scattered hot set, so this mostly costs freshness.")
    ap.add_argument("--status-every", type=float, default=30.0)
    ap.add_argument("--events-out", help="append events here as JSON lines")
    ap.add_argument("--print-events", action="store_true", default=True)
    ap.add_argument("--quiet-events", dest="print_events", action="store_false")
    ap.add_argument("--run-for", type=float, metavar="SECONDS",
                    help="stop after this long (default: run until interrupted)")
    return ap


def main():
    args = build_engine_parser().parse_args()
    if not (args.all or args.watch or args.brand or args.shelf or args.category
            or args.was_out):
        sys.exit("pick what to watch: --all, --watch FILE, --brand/--shelf/"
                 "--category, or --was-out")

    session = json.load(open(args.session))
    con = db_open(args.db)
    e = Engine(session, con, args)

    watch = e.load_watchlist()
    if not watch:
        sys.exit("nothing matched -- is the catalogue crawled for this location?")

    sku_shelf, no_shelf = {}, []
    for pid, meta in watch.items():
        if meta["shelves"]:
            sku_shelf[pid] = meta["shelves"][0]
        else:
            no_shelf.append(pid)

    hot = set()
    if args.hot:
        hot = {p for p in read_watchfile(args.hot) if p in sku_shelf}
    weights = {pid: (args.hot_weight if pid in hot else 1.0) for pid in sku_shelf}

    sizes = {}
    for uuid, gid, n in con.execute(
            "SELECT collection_uuid, collection_group_id, COUNT(*) "
            "FROM product_categories WHERE location=? GROUP BY 1,2", (e.location,)):
        sizes[(uuid, str(gid))] = n

    sched = Scheduler(sku_shelf, weights, sizes, args.page_limit, args.half_life)
    e.load_live(watch)
    e.open_run(len(sku_shelf))
    if args.events_out:
        e.events_file = open(args.events_out, "a")

    n_shelves = len(sched.shelf_skus)
    budget = sum(sched.cost.values())
    print("engine watching %d SKUs across %d shelves | %d hot | "
          "a full sweep is ~%d requests, ~%s at the measured rate"
          % (len(sku_shelf), n_shelves, len(hot), budget,
             human(budget / 0.635)), file=sys.stderr)
    if no_shelf:
        print("%d SKUs have no shelf recorded and are not watched -- re-run "
              "crawl.py to place them" % len(no_shelf), file=sys.stderr)
    print("hot SKUs refresh fastest; everything else is swept with the "
          "leftover budget. Ctrl-C to stop.", file=sys.stderr)

    def on_signal(signum, frame):
        e.shutdown = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)

    try:
        asyncio.run(e.serve(sched, watch, hot))
    except KeyboardInterrupt:
        e.shutdown = True

    e.persist_live()
    e.close_run()
    e.status(sched, hot)
    if e.events_file:
        e.events_file.close()
    print("engine stopped: run %d, %d events, %d requests"
          % (e.run_id, e.n_events, e.n_req), file=sys.stderr)
    if e.stop:
        print("auth expired -- re-run discover.py", file=sys.stderr)
    con.close()


if __name__ == "__main__":
    main()
