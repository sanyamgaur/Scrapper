# Sourced — How The System Works

2026-09-20

## The live system

| What | URL |
| --- | --- |
| Storefront | https://baba-it.onrender.com/ |
| Control tower | https://baba-it.onrender.com/console |
| Compliance / ops queue | https://baba-it.onrender.com/ops |
| Operator view | https://baba-it.onrender.com/operator |
| API docs (all 47 endpoints) | https://baba-it.onrender.com/docs |

One application, one address. First load after idle takes 30–50 seconds — the host sleeps the service and the catalogue is a 28 MB database. Source: [github.com/sanyamgaur/Scrapper](https://github.com/sanyamgaur/Scrapper)

## 1. What it does

Sourced takes **31,366 grocery products** scraped from an Indian quick-commerce app and decides which can legally, physically and profitably be sold to a US customer — then carries the order through to someone buying it off a shelf in Delhi.

Four questions block every product: **may we ship it** (customs, FDA, airline dangerous goods), **can we ship it** (carriers bill on weight the catalogue never states), **should we ship it** (a $4 bag of peanuts with $27 freight is not a product), and **can we get it** (the price and stock flag were true only at crawl time).

Nine engines answer them. Every verdict traces to a specific rule, number and reason.

| Verdict | SKUs | Meaning |
| --- | --- | --- |
| `ALLOWED` | 6,173 | Listed for sale |
| `REVIEW` | 21,713 | Not rejected — not yet cleared by a human |
| `BLOCKED` | 3,480 | A rule says no, with a citation |

**Unknown is not safe.** A product matching no rule goes to review, never to the storefront. That is why review outnumbers listable 3:1 — the catalogue grows by deliberate clearance.

## 2. The pipeline

1. **Crawl** — Blinkit's catalogue at ~0.635 req/s → `inventory_delhi.csv`
2. **Parse pack** — `2 x 100 g` → 200 g. 97% resolve at high confidence; failures are held, never guessed
3. **Classify** — the rule pack writes a verdict plus every rule that fired
4. **Cluster** — 21,713 review SKUs collapse to 531 human decisions
5. **List** — price and freight computed live per request
6. **Gate** — stock re-verified synchronously at checkout, fails closed
7. **Batch** — a day's orders become Blinkit carts, split by dark store, bought in risk order

Everything lives in one SQLite file, `crossborder.db`: 18 tables covering catalogue, verdicts, the human queue, stock, orders and fulfilment. Every rule that fired is kept — 38,546 rows against 31,366 products — so any block can be explained, not just asserted.

## 3. Compliance — the rules

Rule pack `2026.09.1`. Four layers, evaluated most-specific first. Worst verdict wins; every match is recorded.

| Layer | Rules | Matched against |
| --- | --- | --- |
| Keyword | 39 | Product name + brand (regex) |
| Group | 71 | Blinkit's shelves — 29 allow, 24 block, 18 review |
| Category | 9 | Blinkit's aisles |
| Pack | 2 | Physical thresholds |

**Eight dimensions** say *why* a rule fired, so ops can triage by cause: `US_IMPORT` (border prohibition), `HAZMAT` (airlines refuse it), `TEMPERATURE` (needs cold chain), `PERISHABLE` (shelf life < transit), `FRAGILITY`, `FUNCTIONAL` (230 V won't work in the US), `DUTY` (antidumping exposure), `IP_RISK`.

### The keyword layer, by family

| Family | Rules | Verdict | Example authority |
| --- | --- | --- | --- |
| Meat, seafood, eggs, dairy | 6 | 5 blocked, ghee → review | 9 CFR 94; 21 CFR 123 |
| Produce, seeds, poppy | 3 | All blocked | 7 CFR 319.56; 21 U.S.C. 802 |
| Tobacco, betel, alcohol, pharma | 6 | 4 blocked, 2 review | FDA Import Alert 21-19; 21 U.S.C. 387 |
| Dangerous goods | 7 | 4 blocked, 3 review | IATA DGR UN1950; 49 CFR 173.306 |
| Cold chain, shelf life | 5 | 3 blocked, 2 review | FDA Import Alert 34-02 |
| Cosmetics, drugs, devices | 5 | Kajal blocked, 4 review | FDA Import Alert 53-19; MoCRA 2022 |
| Consumer goods, trade | 7 | IP risk blocked, 6 review | CPSIA; Antidumping A-533-903 |

Notable calls: **ghee** is review not blocked — shelf-stable, still a milk product. **Kajal** is blocked on a named lead-content import alert. **Honey** is admissible but carries a live antidumping order on Indian raw honey. **Toys inside confectionery** are blocked outright, a category that is unremarkable in India.

**Category layer:** meat, produce, dairy and ice cream blocked; pharma, electronics, stationery and cleaners to review. Feminine Hygiene is the only aisle cleared wholesale.

**Pack layer:** over 20 kg billable → review (freight exceeds goods value, may need formal customs entry). Unparseable pack → review (weight unknown, so freight would be under-charged).

**Scoping.** The same word means different things on different shelves — **cream** is dairy in grocery and moisturiser in beauty; **tablet** is medicine in pharma and a computer in electronics. Rules carry scope qualifiers. Without them one broad rule silently deletes thousands of listable SKUs; this happened in development and a test now locks it out.

The rule pack states its own limit: the citations are *"our documented basis for a commercial decision, not legal advice"* — have a licensed customs broker sign off.

## 4. The ops sheet

A per-SKU queue of 21,713 items is one nobody works. Clustering by **shelf × rule that fired** collapses it to **531 decisions**, and the top 50 cover **59%** of the backlog. Clearing `Jewellery / KW-JEWELLERY-081` moves 829 SKUs in one click.

| Column | Holds |
| --- | --- |
| `cluster_id` | Shelf + rule — the unique key |
| `group_name` · `rule_id` · `dimension` | What fired, and why |
| `reason` · `authority` | Plain-language explanation + the citation |
| `sku_count` | How many SKUs this one decision moves |
| `sample_names` | Real product names, so the reviewer sees what they're judging |
| `status` · `decided_by` · `decided_at` · `note` | The decision and its trail |

The last four columns are empty across all 531 rows — no clusters worked yet.

| Biggest waiting decisions | Rule | SKUs |
| --- | --- | --- |
| Party & Festive Needs | `DEFAULT-REVIEW` | 834 |
| Jewellery | `KW-JEWELLERY-081` | 829 |
| Jewellery | `GROUP:Jewellery` | 775 |
| Home Needs | `DEFAULT-REVIEW` | 693 |
| Pooja Needs | `GROUP:Pooja Needs` (HAZMAT) | 617 |
| Home Appliances | `GROUP:Home Appliances` | 575 |

`DEFAULT-REVIEW` means **no rule matched at all** — unexplored catalogue, and the cheapest wins available. Pooja Needs is HAZMAT because camphor and incense are flammable solids.

Cleared clusters write to `overrides`, a separate table. Rebuilding the rule pack rewrites every verdict, then re-applies overrides — so a rule tweak never discards an operator's work.

## 5. Money — pricing and freight

Eight lines turn an INR shelf price into a USD price. Every number lives in YAML, never in code.

| Line | Value |
| --- | --- |
| Goods | INR ÷ FX at 88.0, held with a **2.5% buffer** over spot |
| Sourcing | ₹35 delivery + 2% handling + ₹45 pick/pack + **4% procurement-failure allowance** |
| Freight | `max(actual, volumetric)` — see below |
| Duty | 3.0–8.5% by category, **6.0% default** |
| MPF | 0.3464% of dutiable value, floor $2.62, cap $634.62 |
| Payments | 2.9% + $0.30, plus 1% FX spread, 0.8% chargeback reserve |
| Margin | Target **28%**, floor 15%. Beauty 38%, staples **18%** — freight already dominates |
| Rounding | To `.99`, minimum $2.99 |

**De minimis is set to $0.** The $800 duty-free allowance is treated as unavailable (Section 321 suspended 2025), so every parcel is priced as dutiable. If it returns, one number changes and every price updates.

**Freight.** Weight is the hard part, not the rate table: ×1.18 dimensional factor, +260 g box, 500 g billable floor, and unknown weights always **round upward** — an under-estimated gram costs margin on every order forever. Five carriers quote (DHL, FedEx, Aramex, India Post EMS, a consolidator) on declining per-kg slabs. A carrier that cannot honour a handling flag is **excluded, not ranked cheaper** — a cheap carrier that melts your chocolate is not an option.

**Viability matters more than price.** Freight above 4× goods value is not viable; above 2× is flagged. A single cheap SKU runs 6.8× and is commercially dead; an eight-item basket runs 2.7× at 30.8% margin. That gap is why the basket builder exists.

## 6. The other engines

| Engine | What it decides | Key rule |
| --- | --- | --- |
| **Stock** | Whether a SKU is buyable now | Freshness tiers (cart 60 s → tail 48 h) + a synchronous checkout gate that **fails closed**. Availability is boolean — Blinkit gives no depth, so no fabricated "12 left" |
| **Stockout risk** | What's about to vanish | Weighted score; **27,886 SKUs** scored. Reports `low` confidence with no history rather than faking certainty. Shelf base rate discriminates hard: 18% on Toys & Games vs 92% on Baby Toys |
| **Procurement** | A day's orders → Blinkit carts | Consolidate with an allocation ledger; split by dark store first; buy in risk order; give shorts to the orders **closest to whole** |
| **Price drift** | When a shelf price moved | List drift: greater of $3 or 5%. Quote drift held tighter, enforced in code. **Drops never block an order** — only rises escalate to `REPRICE → DELISTED` |
| **Basket** | What to suggest adding | Freight arithmetic, not "customers also bought". **Free headroom**: a 1.05 kg cart already bills at 1.5 kg, so the next 450 g ship free. Capped at 1.5× cart value so it doesn't suggest a $197 watch beside ₹93 of chana |
| **Restock** | When to relist | 6-hour stability hold, flap suppression, re-check against compliance. **Recommends, never auto-relists**; waitlist notifications capped at 5 |

**Order placement is not automated, deliberately.** It needs an authenticated account, a cart-write endpoint, a saved address and payment authorisation — the project holds none. The blocker is payment: Indian card and UPI require RBI-mandated 2FA on a human's device by design. So the operator gets a risk-ordered pick sheet with deep links and three confirmations, including a typed price attestation that catches wrong-pack mis-picks.

## 7. Real vs snapshot

**Real:** all nine engines compute live on every order. Change a YAML rule and the next quote reflects it.

**Snapshot:** the catalogue. 31,366 SKUs crawled 16 September 2026. Refreshing needs the scraper, which requires a real machine in India with a visible browser — it will not run on a server.

Three consequences worth stating in a demo:

- **No live availability feed.** Checkout sells against the snapshot and blocks only items already flagged out of stock. Setting `stock.require_live_confirmation: true` makes it strict, but needs a live session.
- **Risk confidence is `low` everywhere** — `stock_checks` is empty, so flip frequency and restock latency have nothing to work with.
- **No orders placed yet.** Fulfilment is built and tested, not yet run on real traffic. The database is also on ephemeral disk: anything written at runtime is wiped on the next deploy.

In short: a complete decision system running on a frozen catalogue. The reasoning is real and inspectable; the products are a photograph.

## 8. Where things live

| Rule pack | Covers |
| --- | --- |
| [`rules/compliance.yaml`](https://github.com/sanyamgaur/Scrapper/blob/main/crossborder/rules/compliance.yaml) | Sections 3–4 |
| [`rules/pricing.yaml`](https://github.com/sanyamgaur/Scrapper/blob/main/crossborder/rules/pricing.yaml) | Section 5 |
| [`rules/shipping.yaml`](https://github.com/sanyamgaur/Scrapper/blob/main/crossborder/rules/shipping.yaml) | Section 5, carriers + customs |
| [`rules/procurement.yaml`](https://github.com/sanyamgaur/Scrapper/blob/main/crossborder/rules/procurement.yaml) | Section 6 |

Engines are in [`crossborder/`](https://github.com/sanyamgaur/Scrapper/tree/main/crossborder): `compliance.py`, `pricing.py`, `shipping.py`, `stock.py`, `stockout_risk.py`, `procurement.py`, `pricedrift.py`, `basket.py`, `restock.py`, `packparse.py`. The API and control tower are `api.py` and `console.py`. Getting started: [`START-HERE.txt`](https://github.com/sanyamgaur/Scrapper/blob/main/START-HERE.txt).

The legal citations above are quoted from the rule pack and **not independently verified** — check them at ecfr.gov or fda.gov before relying on them.
