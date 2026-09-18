# Cross-border engines: Blinkit catalogue → US storefront

Four engines that turn 31,366 scraped Indian SKUs into a US storefront you can
actually operate, plus the web app that runs them.

```
crawl ──► ingest ──► classify ──► [review queue] ──► storefront
              │          └── LLM tail                    │
              │                                          ├── shipping quote
              │                                          ├── landed cost
              │                                          ├── basket builder
              │                                          └── stock gate ──► ORDER
              │                                                              │
  risk score ─┴──────────────────────────────────────────► daily batch ◄─────┘
                                                                │
                            split by dark store ──► pick sheet ──► operator buys
                                                                │
                                          reconcile ──► pack-out ──► US parcel
```

## Quick start

```bash
pip install -r requirements-web.txt
python -m crossborder.cli ingest inventory_delhi.csv   # 31,366 SKUs
python -m crossborder.cli classify                     # verdicts + review queue
python -m crossborder.cli serve                        # storefront + /ops
```

`http://127.0.0.1:8000/` is the storefront, `/ops` is the review console.

Use `requirements-web.txt`, not the repo-root `requirements.txt` — the root
file also lists `httpx`/`playwright` for the *scraper* (`crawl.py`,
`discover.py`), which pull in `greenlet`. `greenlet` needs a C++ build
toolchain to compile from source on Windows when no prebuilt wheel matches
your Python version, and the website never needs a browser or an HTTP/2
client, so it never needs that toolchain either.

## The four engines

### 1. Listability (`compliance.py` + `rules/compliance.yaml`)

Decides `ALLOWED` / `REVIEW` / `BLOCKED` across eight risk dimensions:
`US_IMPORT`, `HAZMAT`, `TEMPERATURE`, `PERISHABLE`, `FRAGILITY`, `FUNCTIONAL`,
`DUTY`, `IP_RISK`.

Four rule layers, most specific first: **keyword** (product name) → **group**
(Blinkit's 261 shelves) → **category** (28 aisles) → **pack** (physical
thresholds). Worst verdict wins; every fired rule is kept for audit.

Three design commitments:

- **Unknown is not safe.** A SKU matching no rule goes to `REVIEW`, never
  `ALLOWED`. The catalogue grows by deliberate clearance.
- **Every verdict cites an authority.** `9 CFR 94`, `FDA Import Alert 53-19`,
  `IATA DGR UN1950`. When a parcel is held you point at a rule, not a memory.
- **Rules are scoped to their aisle.** "Cream" is chilled dairy in a grocery
  aisle and a moisturiser in a beauty aisle; "tablet" is medicine in pharma and
  a computer in electronics. Without scoping, one rule silently deletes
  thousands of listable SKUs — this happened during development and
  `tests/test_crossborder.py` now locks it out.

Current run: **6,925 listable · 20,826 review · 3,615 blocked.**

### 2. Shipping (`shipping.py` + `rules/shipping.yaml`)

Cart → chargeable weight → one quote per carrier that will accept the parcel.

Weight is the hard part, not the rate table. Carriers bill
`max(actual, volumetric)`; the catalogue only gives free-text pack strings
("2 x 100 g", "100 pulls", "1 pair"). The parser resolves 97% at high
confidence; the rest fall back to per-shelf pack weights. **Unknown weights
always resolve upward** — every under-estimated gram is margin lost on every
order.

A carrier that cannot honour a handling flag (`INSULATED_PACK`) is *excluded*,
not ranked cheaper. A cheap carrier that melts your chocolate is not an option.

Rates are indicative published retail. **Replace `rules/shipping.yaml` with your
contracted rates** — no code changes needed. `LiveRateProvider` is the seam for
a carrier API when you sign one; the rate card then becomes the fallback.

### 3. Landed cost (`pricing.py` + `rules/pricing.yaml`)

Every line between the Blinkit shelf and the customer's card: goods → sourcing
and procurement-failure allowance → freight → duty → MPF → payment processing →
margin → rounded USD price. FX carries a 2.5% buffer over spot so drift between
quote and purchase cannot turn a thin order into a loss.

The **viability check matters more than the price**. It flags SKUs where freight
dwarfs goods value instead of quietly printing a $50 price for $4 of peanuts.

### 4. Live stock (`stock.py`)

Two layers, because Blinkit's ~0.635 req/s limit makes continuous checking of
31k SKUs impossible:

- **Freshness tiers** — cart items re-check within 60s, hot SKUs 15min, listed
  6h, tail 48h. Driven by the existing `availability_engine.py` scheduler.
- **Hard gate at checkout** — every line re-verified synchronously before money
  moves. **Fails closed**: a stale or unknown reading blocks the order exactly
  like a known out-of-stock.

Availability is boolean, not a count — Blinkit exposes an in-stock flag, not
inventory depth, so any "12 left" badge would be fabricated.

### LLM tail (`llm.py`)

The ~28% of SKUs no rule covers. Batched and deduplicated by shelf signature
(~179 API calls for 8,909 SKUs). Returns a verdict *and* a confidence; anything
below 0.80 still goes to a human. Without `ANTHROPIC_API_KEY` it returns nothing
and the tail stays in `REVIEW` — the pipeline degrades, it does not fail.

## Why the review queue is clustered

A per-SKU queue of 20,826 items is one nobody will ever work. Clustering by
(shelf × rule that fired) collapses it to **531 decisions**, and the **top 50
cover 59%** of the queue. Clearing "Jewellery / KW-JEWELLERY-081" moves 829 SKUs
in one click.

## The fulfilment half

### 5. Daily procurement batching (`procurement.py`)

A day's US orders become a handful of Blinkit baskets, then come back apart into
per-customer parcels. Four things make that work:

- **Consolidation with a ledger.** Three customers ordering the same peanuts
  become ONE line of qty 3, while `batch_allocations` records which unit belongs
  to whom. Without that ledger a short has no owner.
- **Split by dark store first.** A Blinkit basket is served by one merchant, and
  the listable catalogue spans merchants 34280 (3,700 SKUs) and 36778 (2,396).
  A day's batch is essentially never one order.
- **Risk-first pick order.** Lines are bought in descending stockout risk,
  weighted by the USD revenue riding on them.
- **Completeness-first shorts.** When 2 of 3 units arrive they go to the orders
  closest to whole. One shippable order plus one refund beats three half-orders,
  none of which can move.

### 6. Stockout risk (`stockout_risk.py`)

**There is no availability history on day one, and this engine says so.** Every
score carries a confidence, and with zero history it reports `low` — a ranking
hint, not a prediction. What it can use immediately: the current in-stock flag,
the shelf's own out-of-stock base rate (18% on Toys & Games, 92% on Baby Toys &
Gifts — a genuinely strong prior), and discount depth. Flip frequency and restock
latency switch on as `stock_checks` accumulates. Weights renormalize over
whatever signals exist, so a missing signal never silently drags a score toward
"safe". Every score is explainable: `CRITICAL (0.91, low confidence): currently
out of stock +0.40, shelf base rate +0.23`.

### 7. Back-in-stock relisting (`restock.py`)

Three gates between "Blinkit has it" and "put it on the site": a **stability
hold** (in stock for ten minutes means nothing), **flap suppression** (a SKU
oscillating several times a day is noise, not news), and a **re-check against
compliance and viability**. Relisting is recommended, never automatic —
`auto_relist` defaults to false. Waitlist notification is capped: telling 200
people an item is back when a handful of units exist manufactures 194 complaints.

### 8. Smart basket builder (`basket.py`)

The highest-leverage engine, because a single cheap SKU is commercially dead
(6.8x freight ratio) while an eight-item basket is healthy (2.7x, 30.8% margin).
Not "customers also bought" — freight arithmetic:

- **Free headroom.** Carriers bill on rounded weight, so a 1.05 kg cart already
  bills at 1.5 kg and the next 450 g ship for nothing. This is the best
  suggestion available: more goods, identical freight.
- **Value density.** Goods value per gram. The catalogue spans 25,000× (saffron
  ₹615/g to erasers ₹0.025/g), so this genuinely discriminates.
- **Procurement-aware.** Only in-stock, low-risk SKUs — a suggestion that fails
  procurement costs more than no suggestion.
- **One dark store.** A cross-merchant add-on silently creates a second Blinkit
  order with a second delivery fee.
- **Relevance guard.** Pure arithmetic recommends a $197 watch beside ₹93 of
  chana. A suggestion may not exceed 1.5× the cart's own goods value.

### 9. Price drift (`pricedrift.py`)

Two thresholds, because they protect different things. **List drift** uses the
greater of $3 or 5% — $3 alone is 12% on a $25 item but 2.5% on a $120 one.
**Quote drift** (what a named customer was promised) is held strictly tighter,
and the engine *enforces* that invariant in code rather than trusting the YAML.
Drops never block an order; only rises drive the ladder:
`IGNORED → REPRICE → DELISTED` (when a rise pushes the SKU past viability).

## Why order placement is not automated

Placing a Blinkit order needs an authenticated consumer account, a cart write
endpoint, a saved address, and a payment authorization. The project holds none
of them — `session_delhi.json` carries only geolocation, Cloudflare and analytics
cookies, and `discover.py` captures three **read** templates. The payment factor
is the hard blocker: Indian card and UPI payments require RBI-mandated 2FA
delivered to a human's device by design.

So `operator.py` builds the job down to a few taps instead: a risk-ordered pick
sheet with a deep link per line (`/prn/<slug>/prid/<id>`, constructible from
stored fields, with a search fallback), and **three API-free confirmations** —
a typed **price attestation** that catches the wrong-pack-size mis-pick before
it is paid for, a **bill reconciliation** that must balance before the run
closes, and a **physical intake** count at the hub. `ProcurementBackend` is the
seam where a genuine partner API attaches without redesigning any of it.

## Files

| File | Role |
|---|---|
| `packparse.py` | free-text pack → net_g / net_ml / pieces |
| `compliance.py` | listability verdicts |
| `shipping.py` | weight + carrier quotes |
| `pricing.py` | landed cost + US list price |
| `stock.py` | freshness tiers + checkout gate |
| `llm.py` | LLM tail-classifier |
| `db.py` / `ingest.py` / `classify_run.py` | persistence and pipeline |
| `api.py` | FastAPI backend |
| `web/storefront.html`, `web/ops.html` | customer and ops UIs |
| `procurement.py` | daily batching, consolidation, reconciliation, pack-out |
| `stockout_risk.py` | risk scoring with honest confidence |
| `restock.py` | back-in-stock detection, relist bucket, waitlist |
| `basket.py` | freight-aware basket builder |
| `pricedrift.py` | deadband drift monitor |
| `operator.py` | pick sheet, deep links, the three confirmations |
| `web/operator.html` | mobile buy sheet for the Delhi operator |
| `rules/*.yaml` | **all policy lives here, not in code** |

## Caveats

- Rule authorities are a documented commercial basis, **not legal advice**. Have
  a licensed customs broker sign off before relying on them in production.
- Duty rates are indicative by category. Real HTS classification is per-SKU.
- The **$800 Section 321 de minimis exemption was suspended in 2025**;
  `de_minimis_usd` is set to 0. Re-verify before each pricing refresh — this
  single number moves landed cost more than any carrier negotiation.
