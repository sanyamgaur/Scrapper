# Blinkit inventory scraper

Two stages: **discover once in a browser, then replay the API at scale.**

## How Blinkit actually works (learned by tracing it, not guessing)

- **Inventory is per-darkstore.** The catalog you get depends entirely on the
  `lat`/`lon` you pin. There is no global product list. One run = one store's
  inventory. `discover.py` prints the address the site reports back, so you can
  confirm which store you are actually reading.
- **`l0_cat` / `l1_cat` is a dead legacy route.** Those path params are still
  accepted but return `is_success: false` and zero products for nearly every
  category. Do not build on them.
- **The live route is `collection_uuid` + `collection_group_id`**, POSTed to
  `/v1/layout/listing_widgets`. Both come from Blinkit's own
  `/v1/layout/tag_collections` call, which the homepage fires on load —
  28 collections, 307 crawlable leaves.
- **Pagination echoes `postback_params`**, with `offset` advanced by however
  many items actually arrived. Sending `offset` alone just replays page 1.
- **`limit` is honoured on every page except the first.** Page 0 always returns
  15 no matter what you ask for; from page 1 on, `limit=90` returns 90. The
  site asking for 15 at a time is a UI choice, not a cap — see below, this is
  the single biggest lever in the whole crawler.
- **Search pages 12 at a time**, not 15, and needs `actual_query` +
  `last_snippet_type` once you are past the first page.
- **Search is rate-limited separately from listing.** `/v1/layout/search` keeps
  answering 200 while `/v1/layout/listing_widgets` is returning 429, so the two
  can run at once.
- **Headless Chromium is blocked.** Cloudflare fingerprints it and returns
  "sorry, you have been blocked!" immediately. The browser stage runs headed.

## Setup

```bash
./setup.sh
source .venv/bin/activate
```

## Run

```bash
# 1. Session + category tree. A browser opens; no clicking needed.
python discover.py --lat 28.6139 --lon 77.2090 --out session_delhi.json

# 2. Smoke test
python crawl.py --session session_delhi.json --db blinkit.db --limit-cats 5

# 3. Full crawl (307 leaves), plus the search sweep on its own rate budget
python crawl.py --session session_delhi.json --db blinkit.db \
       --csv inventory_delhi.csv --with-search

# 4. Deeper search sweep for SKUs outside the category tree
python sweep_search.py --session session_delhi.json --db blinkit.db --depth 2
```

Other cities: rerun `discover.py` with new coordinates into a new session file,
then crawl into the **same** DB — the `location` column keeps stores apart.

## Rate limiting, and why the crawler is shaped this way

Blinkit's limiter is a token bucket. Measured, not guessed: **~17 requests of
burst, refilling at roughly 0.6/s.** Once the burst is spent, that refill rate
is the whole budget, and pushing harder actively costs you — at a sustained
1 req/s, 0.62 req/s come back 200; at 3 req/s, only 0.50 do, because the
rejections keep the bucket pinned at empty.

So **requests are the scarce resource, not connections or concurrency.** Adding
workers cannot help. The only way to finish sooner is to carry more products
home per request, which is what the defaults do:

| | requests for one 765-product leaf | products/request |
|---|---|---|
| `--page-limit 15` (the site's own) | 59 | 12.8 |
| `--page-limit 90` | 11 | 67 |
| `--page-limit 90` + verify pass | 29 | 44 |

Four things follow from that:

- **`--page-limit 90`** (default). Roughly 6x fewer requests per leaf.
- **A short page is the last page.** No request is spent discovering an empty
  one — that alone saves one request per leaf, 307 per run.
- **Deep leaves get a second pass at a different page size** (`--verify-limit`,
  default 47). Leaves past a few hundred SKUs drop 2-4% at *any* single page
  size, and the loss is systematic, not random — repeating the identical walk
  returns the identical set, so retrying is useless. A different page size
  lands on different page boundaries and recovers them. On the deepest leaf
  tested this is the difference between 742 and 765 products. Disable with
  `--no-verify-deep` if you want the fast-and-slightly-lossy run.
- **`--with-search`** spends the search endpoint's separate bucket at the same
  time, which would otherwise sit idle for the whole crawl.

The token bucket is mirrored client-side and its refill rate is *learned*: a
clean streak nudges it up, a 429 cuts it 20% and zeroes the local tokens to
match the server. It converges on the real ceiling from below, which is the
only side worth being wrong on. Watch `x429` and `rate` in the progress line —
a healthy run sits at zero 429s.

If it stops with "auth expired", re-run `discover.py`, then re-run `crawl.py` —
it resumes from `leaves_done`.

## Output

SQLite `blinkit.db`:

- `products` — one row per `(product_id, location)`: name, brand, unit, price,
  mrp, discount_pct, in_stock, merchant_id, category path, and the full original
  payload in `raw` so nothing is lost if a field mapping was wrong. `raw` is
  zlib-compressed (about 5x smaller on disk); read it with
  `crawl.unpack_raw(row)`, or store it as plain JSON with `--raw-plain`, or
  skip it entirely with `--no-raw`.
- `product_categories` — every `(product, leaf)` placement. A product routinely
  sits under more than one leaf (a 2-in-1 appears under both Shampoo and
  Conditioner), and `products` can only remember the last leaf that wrote it,
  so **this is the table to use for "what is in this category".**
- `leaves_done` — resume markers. A leaf that errors is *not* marked done, so
  re-running retries exactly what failed.
- `errors` — status and detail per failed request.

```bash
# everything in one category, which is what product_categories is for
sqlite3 blinkit.db "
  SELECT p.name, p.brand, p.unit, p.price, p.mrp, p.in_stock
  FROM product_categories c JOIN products p USING (product_id, location)
  WHERE c.group_name = 'Tubs' AND c.category_name = 'Ice Creams & More'
  ORDER BY p.price;"

sqlite3 blinkit.db "SELECT super_category, COUNT(*) FROM products GROUP BY 1 ORDER BY 2 DESC;"
sqlite3 blinkit.db "SELECT name, price, mrp, discount_pct FROM products
                    WHERE mrp IS NOT NULL ORDER BY discount_pct DESC LIMIT 20;"
```

## Before you run this

Scraping Blinkit is against their Terms of Service, and the catalog may be
protected as a database right. Fine for personal price tracking or research; not
fine to redistribute the dataset or run it commercially without permission. The
defaults stay inside the rate limit the server advertises by its own 429s —
keep them that way.
