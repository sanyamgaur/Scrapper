#!/usr/bin/env python3
"""
Gap-filler: sweep Blinkit's search API for SKUs the category tree does not
reach (new launches, unlisted items, search-only results).

    python sweep_search.py --session session_delhi.json --db blinkit.db

Run AFTER crawl.py and it seeds its keyword list from the brands already found,
so it searches terms this store actually stocks instead of guessing blind.

/v1/layout/search is rate-limited *separately* from the listing endpoint --
it keeps answering 200 while listing is handing out 429s. That is why
`crawl.py --with-search` can run this at the same time as the category crawl
for very little extra wall-clock: it is spending a budget that would otherwise
go unused.
"""
import argparse
import asyncio
import itertools
import json
import string
import sys
import time

import httpx

from blinkit_parse import extract_products
from crawl import Bucket, Crawler, build_parser, db_open, export_csv

SEARCH_URL = "https://blinkit.com/v1/layout/search"
SEARCH_PAGE = 12          # search pages 12 at a time, unlike listing's 15

COMMON = """milk bread eggs rice atta dal oil sugar salt tea coffee curd paneer butter
ghee chicken mutton fish onion potato tomato banana apple chips biscuit chocolate
soap shampoo detergent toothpaste diaper sanitizer noodles pasta sauce juice soda
water beer icecream frozen masala snack cereal honey jam cheese yogurt pet baby
makeup perfume razor tissue battery bulb cable charger""".split()


class SearchMixin:
    """The search half of a sweep, usable standalone or bolted onto a Crawler."""

    def init_search(self, session, con):
        tmpl = session["templates"].get("search")
        if not tmpl:
            sys.exit("session has no search template -- re-run discover.py")
        self.search_headers = dict(tmpl["headers"])
        self.search_headers["content-type"] = "application/json"
        self.search_headers["lat"] = str(session["lat"])
        self.search_headers["lon"] = str(session["lon"])
        self.search_bucket = Bucket(self.args.rate, self.args.burst,
                                    max_rate=self.args.max_rate, name="search")
        self.known = set(r[0] for r in con.execute(
            "SELECT product_id FROM products WHERE location=?", (self.location,)))
        self.n_search_new = 0

    async def post_search(self, client, url, body, key):
        # Same retry/backoff path as the listing crawl, but metered against the
        # search endpoint's own bucket and with the search headers. Both are
        # passed in rather than swapped onto self -- when this runs alongside
        # the category crawl, mutating shared state would poison in-flight
        # listing requests.
        return await self.post(client, url, body, key,
                               bucket=self.search_bucket,
                               headers=self.search_headers)

    async def sweep_kw(self, client, kw):
        try:
            return await self._sweep_kw(client, kw)
        except Exception as e:
            self._err("kw:%s" % kw, -2, repr(e))
            return 0

    async def _sweep_kw(self, client, kw):
        key = "kw:%s" % kw
        if key in self.done:
            return 0
        body, offset, found = {}, 0, 0

        for page in range(self.args.search_pages):
            if self.stop:
                break
            if page == 0:
                url = "%s?q=%s&search_type=type_to_search" % (SEARCH_URL, kw)
            else:
                url = ("%s?offset=%d&limit=%d&actual_query=%s"
                       "&search_type=type_to_search"
                       "&last_snippet_type=product_card_snippet_type_2"
                       % (SEARCH_URL, offset, SEARCH_PAGE, kw))
            data = await self.post_search(client, url, body, key)
            if data is None:
                return found

            prods = extract_products(data, {})
            self.note_cats(prods, {"category_name": None, "group_name": None,
                                   "super_category": None}, source="search")
            new = [p for p in prods if p["product_id"] not in self.known]
            for p in new:
                self.known.add(p["product_id"])
            if new:
                self.save(new, {"collection_uuid": None, "collection_group_id": None},
                          source="search", query=kw)
                found += len(new)
                self.n_search_new += len(new)
            if not prods:
                break

            offset += SEARCH_PAGE
            pb = data.get("postback_params") or {}
            body = dict(pb)
            body.update({"offset": str(offset), "limit": str(SEARCH_PAGE),
                         "actual_query": kw, "page_index": str(page + 1)})

        self.con.execute("INSERT OR REPLACE INTO leaves_done VALUES (?,?,?)",
                         (key, found, int(time.time())))
        return found


class Sweeper(SearchMixin, Crawler):
    def __init__(self, session, con, args):
        super().__init__(session, con, args)
        self.init_search(session, con)

    async def run_sweep(self, keywords):
        self.sem = asyncio.Semaphore(self.args.concurrency)
        self.bucket.start()
        self.search_bucket.start()
        limits = httpx.Limits(max_connections=self.args.concurrency * 2,
                              max_keepalive_connections=self.args.concurrency * 2)
        async with httpx.AsyncClient(http2=True, limits=limits, cookies=self.cookies,
                                     follow_redirects=True) as client:
            tasks = [self.sweep_kw(client, k) for k in keywords]
            done = 0
            for fut in asyncio.as_completed(tasks):
                await fut
                done += 1
                self.progress(done, len(keywords), label="kw")
            self.flush(); self.con.commit()
        print("", file=sys.stderr)


def search_side_task(crawler, session, n_keywords):
    """Attach a search sweep to a running category crawl, on its own bucket."""
    crawler.__class__ = type("CrawlerWithSearch", (SearchMixin, crawler.__class__), {})
    crawler.init_search(session, crawler.con)
    kws = build_keywords(crawler.con, 1)[:n_keywords]

    async def task(client):
        crawler.search_bucket.start()
        for kw in kws:
            if crawler.stop:
                break
            await crawler.sweep_kw(client, kw)
    return task


def build_keywords(con, depth):
    kws = list(COMMON)
    for (b,) in con.execute("SELECT DISTINCT brand FROM products "
                            "WHERE brand IS NOT NULL AND brand != ''"):
        kws.append(str(b).lower())
    kws += list(string.ascii_lowercase)
    if depth >= 2:
        kws += ["".join(p) for p in itertools.product(string.ascii_lowercase, repeat=2)]
    if depth >= 3:
        kws += ["".join(p) for p in itertools.product(string.ascii_lowercase, repeat=3)]
    seen, out = set(), []
    for k in kws:
        k = k.strip()
        if k and k not in seen:
            seen.add(k); out.append(k)
    return out


def main():
    # Reuse the crawler's parser rather than restating its flags. The two
    # scripts share Crawler, so a flag that exists in only one of them is a
    # crash waiting to happen the first time shared code reads it.
    ap = build_parser()
    ap.add_argument("--depth", type=int, default=2, choices=[1, 2, 3],
                    help="1=words+a-z  2=+2-letter prefixes  3=+3-letter (17k, slow)")
    ap.add_argument("--limit-kw", type=int)
    ap.set_defaults(verify_deep=False)
    args = ap.parse_args()

    session = json.load(open(args.session))
    con = db_open(args.db)
    s = Sweeper(session, con, args)
    kws = build_keywords(con, args.depth)
    if args.limit_kw:
        kws = kws[:args.limit_kw]
    print("sweeping %d keywords (%d products already known)"
          % (len(kws), len(s.known)), file=sys.stderr)

    asyncio.run(s.run_sweep(kws))

    total = con.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    print("\nsearch sweep added %d products. total now %d (%d rate-limits)"
          % (s.n_new, total, s.n_429))
    if args.csv:
        export_csv(con, args.csv)
        print("csv -> %s" % args.csv)
    con.commit(); con.close()


if __name__ == "__main__":
    main()
