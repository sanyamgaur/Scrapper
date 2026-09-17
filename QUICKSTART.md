# Quickstart

Everything here runs on **your** machine. It cannot run in a cloud session:
the first step opens a real browser window, because Cloudflare blocks headless
Chromium outright.

## What you need

- **A desktop machine with a screen** (macOS, Windows, or Linux with a GUI).
  A headless server will fail at step 2.
- **Python 3.9 or newer.**
- **An internet connection that can reach blinkit.com.**

## 0. Get the code

```bash
git clone https://github.com/sanyamgaur/Scrapper.git
cd Scrapper
git checkout claude/confident-hopper-qdqqed
```

## 1. Install

```bash
./setup.sh
source .venv/bin/activate
```

On Windows use `.venv\Scripts\activate` instead. This installs httpx and
Playwright, then downloads Chromium (~150 MB, once).

## 2. Get a session

```bash
python discover.py --lat 28.6139 --lon 77.2090 --out session_delhi.json
```

A browser window opens and loads Blinkit. **Do not close it** — it closes
itself after about 20 seconds. Nothing to click.

Change `--lat`/`--lon` to pin a different darkstore. Inventory is per-store, so
the coordinates decide which catalogue you get. The script prints the address
Blinkit reports back, so check it is the store you meant.

It should end with:

```
categories : 306 leaves across 4 super-categories
templates  : listing, search, tag_collections
```

If it says `no categories` you were blocked — wait a few minutes and retry.

**This session expires after a few hours.** Every step below needs a fresh one;
when something says "auth expired", re-run this step.

## 3. Get a catalogue

Either crawl a fresh one (~45 min, gives current stock):

```bash
python crawl.py --session session_delhi.json --db blinkit.db \
       --csv inventory_delhi.csv --with-search
```

Or, to skip the crawl, rebuild from the CSV already in the repo (~10 seconds,
but its stock flags are from 16 Sept 2026):

```bash
python rebuild_db.py --csv inventory_delhi.csv --session session_delhi.json \
       --db blinkit.db
```

Smoke-test the crawl first if you like: add `--limit-cats 5` to the crawl
command and it does five shelves instead of 306.

## 4. Check availability

Always plan first. This spends no requests:

```bash
python check_availability.py --session session_delhi.json --db blinkit.db \
       --all --dry-run
```

Then start small — one brand, about a minute:

```bash
python check_availability.py --session session_delhi.json --db blinkit.db \
       --brand Amul --report amul.csv
```

Then the whole catalogue, about 18 minutes:

```bash
python check_availability.py --session session_delhi.json --db blinkit.db \
       --all --report today.csv
```

### Reading the output

A healthy run looks like this — note **requests is not zero**:

```
[14:02:11] run 3 | 255 watched | 255 checked | 168 in stock | 47 requests (0 429) | 78s
compared against run 2
  out_of_stock    4
  back_in_stock   2
    out_of_stock   481234    in stock @ 28.0 -> out of stock @ 28.0
```

A broken run says so explicitly. If you see this, the network or the session is
the problem, **not** your products:

```
  WARNING: 8 of 8 watched products could not be checked -- no answer from the
  API. Their state below is unknown, not unchanged.
  NOTHING was checked this run. Check the network, and re-run discover.py if
  the session has expired.
```

### Other ways to choose what to check

```bash
--watch skus.txt         # your own list: ids one per line, or any CSV with a product_id column
--was-out                # only what was last seen out of stock, i.e. catch restocks
--brand Amul             # by brand
--shelf Shampoo          # by shelf
--category "Baby Care"   # by category
--limit 20               # cap the list, useful for a first run
```

## 5. Watch continuously (optional)

```bash
echo "481234
99881" > hot.txt

python availability_engine.py --session session_delhi.json --db blinkit.db \
       --all --hot hot.txt --events-out events.jsonl --run-for 300
```

`--run-for 300` stops after five minutes so you can check it is working before
leaving it running; drop it to run until Ctrl-C.

The status line every 30s is the thing to watch:

```
[engine] 5.0m up | 31366 SKUs | hot p50 38s max 71s | freshness p50 6.9m p90 17.2m | 12 events | 190 ok / 0 failed | 190 req (0.63/s, 0 x429)
```

`190 ok / 0 failed` is what tells you it is really working. If it prints
`NO SUCCESSFUL CHECKS YET`, stop and fix the session or the network.

Changes stream to stdout and to `events.jsonl` as they are detected:

```json
{"ts": 1789662863, "product_id": "481234", "event": "out_of_stock",
 "prev": {"in_stock": 1, "price": 28.0}, "curr": {"in_stock": 0, "price": 28.0}}
```

## 6. Everything else

```bash
python export_dataset.py --db blinkit.db --out dataset      # JSONL + normalised CSVs
python make_workbook.py --dataset dataset                   # xlsx with product images
python make_catalog_page.py --db blinkit.db --session session_delhi.json
python download_images.py --db blinkit.db --out-dir images  # actual image files
```

## Querying the results

```sql
-- what changed in the last run
SELECT product_id, event, prev, curr FROM availability_events
WHERE run_id = (SELECT MAX(run_id) FROM availability_runs);

-- current state of everything (engine only)
SELECT product_id, in_stock, price, last_checked FROM availability_live;

-- how often a SKU flips
SELECT product_id, checks, flips FROM availability_live ORDER BY flips DESC LIMIT 20;
```

## When something goes wrong

| symptom | cause | fix |
|---|---|---|
| `no categories -- tag_collections did not load` | Cloudflare blocked the browser | wait a few minutes, retry step 2 |
| `STOPPED: auth expired` | session is a few hours old | re-run step 2 |
| `NOTHING was checked this run` | no network, or expired session | check connectivity, then re-run step 2 |
| `nothing matched` | the DB has no catalogue for those coordinates | run step 3 |
| lots of `x429` in the status line | being rate limited | lower `--rate`, it will re-learn upward |
| browser never opens | headless machine | run it somewhere with a screen |

## The one limit worth knowing

Blinkit allows about **0.635 requests per second**. Everything else follows
from that:

- Checking all 31,366 SKUs costs ~694 requests, so ~18 minutes. That is a
  floor, not a tuning problem.
- A small hot set can be much fresher: 5 SKUs stay within ~3s, 20 within ~14s,
  50 within ~39s.
- Second-by-second across the whole catalogue would need ~694 requests per
  second. It is not achievable by any arrangement of this code.
