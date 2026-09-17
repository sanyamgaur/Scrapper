#!/usr/bin/env python3
"""
Step 3, optional: pull the actual image bytes for every product already in the
DB and store them next to it -- on disk, indexed in a `product_images` table.

    python download_images.py --db blinkit.db --out-dir images

Why this is a separate step from crawl.py
------------------------------------------
The listing API already hands back an image URL for free with every product
-- no extra request, no extra time, which is why crawl.py stores it directly
on `products.image` (see blinkit_parse.py). Actually fetching the bytes is a
different cost shape entirely: one HTTP request per *image*, no batching, no
15-90-products-per-call leverage. Doing that inline would turn a crawl that
currently spends ~1 request per 15-70 products into one that spends 1+ request
per product, which is exactly the trade the crawler's whole design (see
README) exists to avoid. So it runs after the crawl, on its own schedule,
against a different host (the CDN, not blinkit.com's API) with its own
concurrency and no shared rate budget with the listing crawl.

Resume-safe like crawl.py: rows already downloaded (status='ok' and the file
still exists) are skipped, so re-running only fetches what's missing or new.
"""
import argparse
import asyncio
import hashlib
import mimetypes
import os
import sqlite3
import sys
import time

import httpx

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_images (
    product_id   TEXT,
    location     TEXT,
    url          TEXT,
    local_path   TEXT,
    content_type TEXT,
    n_bytes      INTEGER,
    status       TEXT,
    error        TEXT,
    fetched_at   INTEGER,
    PRIMARY KEY (product_id, location)
);
"""


def db_open(path):
    con = sqlite3.connect(path, timeout=60)
    con.executescript(SCHEMA)
    con.commit()
    return con


def ext_for(url, content_type):
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if ext in (None, ".jpe"):
        base = url.split("?")[0].rsplit("/", 1)[-1]
        if "." in base:
            ext = "." + base.rsplit(".", 1)[-1].lower()
        else:
            ext = ".jpg"
    return ext


def safe_dir(location):
    return hashlib.sha1(location.encode()).hexdigest()[:10]


class Downloader:
    def __init__(self, con, out_dir, concurrency, timeout, retries):
        self.con = con
        self.out_dir = out_dir
        self.sem = asyncio.Semaphore(concurrency)
        self.timeout = timeout
        self.retries = retries
        self.n_ok = 0
        self.n_skip = 0
        self.n_err = 0
        self.pending = []
        self.t0 = time.time()

    def flush(self):
        if self.pending:
            self.con.executemany(
                "INSERT OR REPLACE INTO product_images VALUES (?,?,?,?,?,?,?,?,?)",
                self.pending)
            self.pending.clear()
            self.con.commit()

    async def fetch_one(self, client, product_id, location, url):
        async with self.sem:
            path_dir = os.path.join(self.out_dir, safe_dir(location))
            delay = 1.0
            for attempt in range(self.retries + 1):
                try:
                    r = await client.get(url, timeout=self.timeout,
                                         follow_redirects=True)
                    if r.status_code == 200 and r.content:
                        ct = r.headers.get("content-type", "")
                        os.makedirs(path_dir, exist_ok=True)
                        fname = "%s%s" % (product_id, ext_for(url, ct))
                        fpath = os.path.join(path_dir, fname)
                        with open(fpath, "wb") as f:
                            f.write(r.content)
                        self.pending.append((
                            product_id, location, url, fpath, ct,
                            len(r.content), "ok", None, int(time.time())))
                        self.n_ok += 1
                        return
                    if r.status_code in (429,) or r.status_code >= 500:
                        await asyncio.sleep(delay); delay *= 2; continue
                    self.pending.append((
                        product_id, location, url, None, None, None,
                        "error", "http %d" % r.status_code, int(time.time())))
                    self.n_err += 1
                    return
                except (httpx.HTTPError, OSError) as e:
                    if attempt == self.retries:
                        self.pending.append((
                            product_id, location, url, None, None, None,
                            "error", repr(e)[:200], int(time.time())))
                        self.n_err += 1
                        return
                    await asyncio.sleep(delay); delay *= 2

    async def run(self, rows):
        limits = httpx.Limits(max_connections=200)
        async with httpx.AsyncClient(limits=limits, http2=True) as client:
            tasks = [self.fetch_one(client, pid, loc, url) for pid, loc, url in rows]
            done = 0
            for fut in asyncio.as_completed(tasks):
                await fut
                done += 1
                if len(self.pending) >= 200:
                    self.flush()
                if done % 25 == 0 or done == len(tasks):
                    el = time.time() - self.t0
                    print("\r[%d/%d] ok=%d err=%d skip=%d | %.0fs | %.1f img/s   "
                          % (done, len(tasks), self.n_ok, self.n_err, self.n_skip,
                             el, self.n_ok / max(el, 1)), end="", file=sys.stderr)
            self.flush()
        print("", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="blinkit.db")
    ap.add_argument("--out-dir", default="images")
    ap.add_argument("--concurrency", type=int, default=24,
                    help="the CDN is a different host with its own limits, "
                         "not blinkit.com's API bucket -- safe to run wider")
    ap.add_argument("--timeout", type=float, default=20)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--limit", type=int, help="cap total images (testing)")
    ap.add_argument("--redo-errors", action="store_true",
                    help="also retry rows previously recorded as errors")
    args = ap.parse_args()

    con = db_open(args.db)
    have = {}
    for pid, loc, status, path in con.execute(
            "SELECT product_id, location, status, local_path FROM product_images"):
        have[(pid, loc)] = (status, path)

    rows = []
    for pid, loc, url in con.execute(
            "SELECT product_id, location, image FROM products "
            "WHERE image IS NOT NULL AND image != ''"):
        prior = have.get((pid, loc))
        if prior:
            status, path = prior
            if status == "ok" and path and os.path.exists(path):
                continue
            if status == "error" and not args.redo_errors:
                continue
        rows.append((pid, loc, url))
    if args.limit:
        rows = rows[:args.limit]

    if not rows:
        print("nothing to download -- all images already fetched (or none in db)",
              file=sys.stderr)
        con.close()
        return

    os.makedirs(args.out_dir, exist_ok=True)
    print("downloading %d images -> %s (concurrency %d)"
          % (len(rows), args.out_dir, args.concurrency), file=sys.stderr)
    dl = Downloader(con, args.out_dir, args.concurrency, args.timeout, args.retries)
    asyncio.run(dl.run(rows))

    total_ok = con.execute(
        "SELECT COUNT(*) FROM product_images WHERE status='ok'").fetchone()[0]
    print("%d ok, %d failed this run | %d images on disk total"
          % (dl.n_ok, dl.n_err, total_ok))
    con.close()


if __name__ == "__main__":
    main()
