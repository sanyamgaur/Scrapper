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

# 5. Optional: pull the actual image bytes down (see "Images" below)
python download_images.py --db blinkit.db --out-dir images

# 6. Build the browsable catalog page
python make_catalog_page.py --db blinkit.db --session session_delhi.json

# 7. Emit the structured dataset (see "Structured dataset" below)
python export_dataset.py --db blinkit.db --out dataset

# 8. Re-check stock later (see "Availability checks" below)
python check_availability.py --session session_delhi.json --db blinkit.db --all
```

## Images

The listing API hands back an image URL with every product already -- it
rides along in the same payload as the name and price, at zero extra
requests. `blinkit_parse.py` lifts it out (`IMAGE_KEYS`), normalizes
protocol-relative URLs (`//cdn...`) to `https://`, and `crawl.py` writes it
straight into `products.image`. It is in the CSV export and the DB from the
very first crawl -- nothing extra to run.

`download_images.py` is a separate, optional step for when you want the
actual bytes on disk (an offline archive, a training set, whatever) instead
of just the URL. It is deliberately not part of `crawl.py`: fetching an image
is one request per *image*, with none of the 15-90-products-per-call
leverage the listing crawl is built around, and inlining it would turn a
crawl that spends ~1 request per dozens of products into one that spends 1+
request per product. So it runs afterward, against the CDN (a different host
with its own limits, not blinkit.com's API bucket), resume-safe the same way
`crawl.py` is -- rerun it and it only fetches what is missing:

```bash
python download_images.py --db blinkit.db --out-dir images --concurrency 24
```

This adds a `product_images` table (`product_id, location, url, local_path,
content_type, n_bytes, status, error, fetched_at`) so failures are visible
and re-run cleanly (`--redo-errors` to retry the ones that errored).

## Availability checks

`crawl.py` finds products. `check_availability.py` re-checks ones you already
have, records what changed, and is cheap enough to run on a schedule.

```bash
# everything, compared against the last check (or the catalogue snapshot)
python check_availability.py --session session_delhi.json --db blinkit.db --all

# just a watchlist, and write a report
python check_availability.py --session session_delhi.json --db blinkit.db \
       --watch skus.txt --report today.csv

# only things that were out of stock -- catches restocks
python check_availability.py --session session_delhi.json --db blinkit.db --was-out

# what would this cost? plan it without spending a request
python check_availability.py --session session_delhi.json --db blinkit.db \
       --all --dry-run
```

`--watch` takes a bare id-per-line file or any CSV with a `product_id` column,
so `dataset/products.csv` works as-is. `--brand`, `--shelf` and `--category`
filter the catalogue instead.

**It does not have its own endpoint, and does not need one.** Stock state comes
back from `/v1/layout/listing_widgets` along with everything else, ~41 products
per request — so this checks the *shelves* the watched products sit on and
reads their state out of the response. Two things follow, and they are most of
the script:

- **A shelf walk stops as soon as every watched product on it has been seen.**
  Watching one SKU that sits on page 2 of a nine-page shelf costs two requests,
  not nine.
- **A shelf holding one or two watched products is poor value** — a whole walk
  to learn one fact. Those go to `/v1/layout/search` instead, one request each,
  on the endpoint's *separate* rate bucket, so they cost nothing from the
  listing budget and run concurrently with it. `--strategy auto` (the default)
  picks per shelf at `--shelf-threshold`, default 3.

Measured on the 31,366-product Delhi catalogue:

| | requests |
|---|---|
| full re-crawl (`crawl.py`) | 1,649 |
| check every product | ≤694 listing + 23 search |
| check one brand (255 SKUs) | ≤45 listing + 22 search |

Results go to three tables, and `products` is never modified — it stays the
baseline snapshot:

| table | what it holds |
|---|---|
| `availability_runs` | one row per run: counts, strategy, requests, 429s |
| `availability` | one row per product per run: in_stock, price, mrp, whether it was returned at all |
| `availability_events` | one row per transition, with before and after |

Events are `out_of_stock`, `back_in_stock`, `price_up`, `price_down`,
`disappeared` and `reappeared`. `disappeared` is deliberately distinct from
`out_of_stock`: a product the API stops returning entirely is a different fact
from one it returns and marks unavailable.

### How fresh can it be?

This is polling, not push. Blinkit has no webhook and no public stock feed, so
"real time" means "how often can you afford to ask" — and that is set by the
rate limit (~0.635 req/s sustained, measured), which makes it a function of how
many SKUs you watch, not of the code. Costed with `--dry-run` against the
31,366-product Delhi catalogue:

| watchlist | listing reqs | search reqs | one check takes |
|---|---|---|---|
| 5 SKUs | 5 | 2 | ~8s |
| 20 SKUs | 7 | 7 | ~11s |
| one shelf (170 SKUs) | 6 | 0 | ~9s |
| one brand (255 SKUs) | 45 | 22 | ~71s |
| everything last seen out of stock (18,336) | 674 | 40 | ~18 min |
| everything (31,366) | 694 | 23 | ~18 min |

(The two legs run on separate buckets concurrently, so a check costs the slower
leg, not the sum.)

So: **a focused watchlist can be checked every 30 seconds and is effectively
live. The whole catalogue has a floor of ~18 minutes.** If you need
second-by-second truth on 31k SKUs, scraping cannot give it to you at any
cadence — nothing here changes that.

For anything faster than a few minutes use `--interval` rather than cron:

```bash
python check_availability.py --session session_delhi.json --db blinkit.db \
       --watch skus.txt --interval 30
```

One process keeps one rate limiter, so the learned refill rate and the current
token count carry across checks. A fresh process per check — cron every minute —
starts each time assuming a full burst the server may not have, and walks
straight into 429s.

### Running it on a schedule

Availability in quick commerce moves through the day, so a check is worth
running a few times daily rather than once. The catalogue itself changes much
more slowly — re-crawl weekly to pick up genuinely new SKUs.

```cron
# every 3 hours: re-check stock, append a dated report
0 */3 * * * cd /path/to/Scrapper && .venv/bin/python check_availability.py \
    --session session_delhi.json --db blinkit.db --all \
    --report reports/$(date +\%F-\%H).csv >> logs/availability.log 2>&1

# Sunday 04:00: full re-crawl, to find products that did not exist before
0 4 * * 0 cd /path/to/Scrapper && .venv/bin/python crawl.py \
    --session session_delhi.json --db blinkit.db --with-search >> logs/crawl.log 2>&1
```

The session expires. When it does the script stops and says so, and the fix is
to re-run `discover.py` — so keep an eye on the log, or have cron re-run
`discover.py` before the weekly crawl.

To alert rather than just log, query the events table after a run:

```sql
SELECT product_id, event, prev, curr FROM availability_events
WHERE run_id = (SELECT MAX(run_id) FROM availability_runs);
```

## Structured dataset

`crawl.py` writes one wide, flat row per SKU. That is fine for a spreadsheet and
weak as an interface, so `export_dataset.py` reshapes it into a typed, nested
record set under `dataset/`:

| file | what it is |
|---|---|
| `products.jsonl` | one nested JSON object per product — identity, pack, pricing, availability, images, taxonomy, provenance |
| `products.csv` | the flat view, typed, with the derived columns |
| `images.csv` | one row per image asset (`product_id`, url, CDN `asset_id`, local path) |
| `taxonomy.csv` | the shelf tree with product counts |
| `product_taxonomy.csv` | the product-to-shelf many-to-many |
| `brands.csv` | per-brand rollup |
| `manifest.json` | data dictionary, provenance, summary stats |

It reads the DB, or a crawl CSV directly if you no longer have the DB:

```bash
python export_dataset.py --db blinkit.db --out dataset
python export_dataset.py --csv inventory_delhi.csv --out dataset
```

The part that is real work rather than reshaping is `unit`. Blinkit ships pack
size as free text — `500 g`, `2 x 1 ltr`, `100 ml + 1 pc`, `1 pair` — and as a
string you cannot sort, filter or compare by it. It gets parsed into
`(kind, count, size, uom)` and normalized to net grams / net millilitres /
pieces, which is what makes `price_per_kg` comparable across a shelf.
**98.4%** of this catalog parses. Anything that does not is reported as
`kind: null` with `pack.raw` preserved, never guessed at — and measured on the
real data, essentially every null is a book, because Blinkit puts the author or
publisher in the unit field for book SKUs (`Ruskin Bond`, `Maple Press`).

## Catalog page

`make_catalog_page.py` reads the DB into `site/catalog_data.js`, and
`site/catalog.html` is a static, filterable ledger over it (search, department
and shelf filters, brand, sort, in-stock/discounted toggles) -- open it
directly in a browser, no server needed. Each row now carries a thumbnail,
hotlinked straight from Blinkit's own CDN using the URL already sitting in
the DB (`--out-dir` files from `download_images.py` are not needed for this --
the page never re-downloads anything, it just points `<img>` at the CDN and
lazy-loads as you scroll). A product the store didn't give an image for shows
a dash instead of a broken-image icon.

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
