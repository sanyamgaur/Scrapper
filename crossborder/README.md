# Cross-border engines: Blinkit catalogue → US storefront

Four engines that turn 31,366 scraped Indian SKUs into a US storefront you can
actually operate, plus the web app that runs them.

```
crawl (existing) ──► ingest ──► classify ──► [review queue] ──► storefront
                                   │                               │
                                   └── LLM tail                    ├── shipping quote
                                                                   ├── landed cost
                                                                   └── stock gate ──► order
```

## Quick start

```bash
pip install -r requirements.txt
python -m crossborder.cli ingest inventory_delhi.csv   # 31,366 SKUs
python -m crossborder.cli classify                     # verdicts + review queue
python -m crossborder.cli serve                        # storefront + /ops
```

`http://127.0.0.1:8000/` is the storefront, `/ops` is the review console.

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
| `rules/*.yaml` | **all policy lives here, not in code** |

## Caveats

- Rule authorities are a documented commercial basis, **not legal advice**. Have
  a licensed customs broker sign off before relying on them in production.
- Duty rates are indicative by category. Real HTS classification is per-SKU.
- The **$800 Section 321 de minimis exemption was suspended in 2025**;
  `de_minimis_usd` is set to 0. Re-verify before each pricing refresh — this
  single number moves landed cost more than any carrier negotiation.
