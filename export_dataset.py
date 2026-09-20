#!/usr/bin/env python3
"""
Step 4: turn the crawl into a structured dataset other systems can consume.

    python export_dataset.py --db blinkit.db --out dataset
    python export_dataset.py --csv inventory_delhi.csv --out dataset   # no DB needed

What the crawl produces is one wide, flat row per SKU -- fine for a spreadsheet,
weak as an interface. This emits the same catalog as a typed, nested record set:

  products.jsonl        one JSON object per product: identity, pack, pricing,
                        availability, images, taxonomy, provenance
  products.csv          the flat view, but typed and with the derived columns
  images.csv            one row per image asset (product_id, url, asset id)
  taxonomy.csv          the shelf tree, with product counts
  product_taxonomy.csv  the product-to-shelf many-to-many
  brands.csv            brand rollup
  manifest.json         data dictionary, provenance and summary stats

The part that is actual work, rather than reshaping, is `unit`. Blinkit ships
pack size as free text -- '500 g', '2 x 100 ml', '100 ml + 1 pc', '1 pair', and
for books the publisher's name in the same field. Left as a string you cannot
sort, filter or compare by it. Parsed into (kind, count, size, uom) it
normalizes to net grams / net millilitres / pieces, which is what makes
price-per-kg comparable across a shelf. Anything that does not parse is
reported as kind=null rather than guessed at -- see manifest.json for the
share, and `pack.raw` always keeps the original string.
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import statistics
import sys
import time

CURRENCY = "INR"

# Base units. Everything else in the `unit` field is a counting word or noise.
WEIGHT = {"g": 1.0, "gm": 1.0, "gms": 1.0, "gram": 1.0, "grams": 1.0,
          "kg": 1000.0, "kgs": 1000.0, "kilogram": 1000.0}
VOLUME = {"ml": 1.0, "mls": 1.0, "l": 1000.0, "lt": 1000.0, "ltr": 1000.0,
          "ltrs": 1000.0, "litre": 1000.0, "liter": 1000.0, "litres": 1000.0}
COUNT = {"pc", "pcs", "piece", "pieces", "unit", "units", "set", "sets",
         "pair", "pairs", "pack", "packs", "packet", "packets", "roll", "rolls",
         "sheet", "sheets", "wipe", "wipes", "tab", "tabs", "tablet", "tablets",
         "capsule", "capsules", "strip", "strips", "pull", "pulls", "book",
         "books", "sachet", "sachets", "bottle", "bottles", "can", "cans",
         "bar", "bars", "box", "boxes", "n", "no", "nos", "combo", "dozen"}

TERM = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?\s*$")
MULTI = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*[x×*]\s*(.+?)\s*$", re.I)


def _term(s):
    """'500 g' -> (500.0, 'weight', 500.0) ; '2 pcs' -> (2.0, 'count', 2.0)"""
    m = TERM.match(s or "")
    if not m:
        return None
    n = float(m.group(1))
    word = (m.group(2) or "").lower()
    if word in WEIGHT:
        return (n, "weight", n * WEIGHT[word])
    if word in VOLUME:
        return (n, "volume", n * VOLUME[word])
    if word in COUNT or word == "":
        return (n, "count", n * (12 if word == "dozen" else 1))
    return None


def parse_pack(raw):
    """Free-text pack size -> normalized dict. kind is None when unparseable."""
    out = {"raw": raw, "kind": None, "count": None, "size": None, "uom": None,
           "net_g": None, "net_ml": None, "pieces": None, "components": None}
    if not raw or not raw.strip():
        return out

    # '100 ml + 1 pc' / '6 ml + 6 ml + 6 ml' -- a combo pack, sum per kind.
    parts = [p for p in re.split(r"\s*\+\s*", raw.strip()) if p]
    comps = []
    for p in parts:
        mm = MULTI.match(p)
        if mm:
            inner = _term(mm.group(2))
            if inner:
                comps.append((float(mm.group(1)), inner))
                continue
            return out
        t = _term(p)
        if not t:
            return out
        comps.append((1.0, t))

    totals = {"weight": 0.0, "volume": 0.0, "count": 0.0}
    for mult, (_size, kind, base) in comps:
        totals[kind] += mult * base
    kinds = [k for k, v in totals.items() if v > 0]

    if len(comps) == 1:
        # size is the normalized amount in ONE multipack unit, so '2 x 1 ltr'
        # is count=2, size=1000 ml -- not size=1 in units of ml.
        mult, (_size, kind, base) = comps[0]
        out["count"] = mult
        out["size"] = base
        out["uom"] = {"weight": "g", "volume": "ml", "count": "piece"}[kind]
    else:
        out["components"] = [
            {"count": m, "size": s,
             "uom": {"weight": "g", "volume": "ml", "count": "piece"}[k]}
            for m, (s, k, _b) in comps]

    out["kind"] = kinds[0] if len(kinds) == 1 else "mixed"
    if totals["weight"]:
        out["net_g"] = round(totals["weight"], 3)
    if totals["volume"]:
        out["net_ml"] = round(totals["volume"], 3)
    if totals["count"]:
        out["pieces"] = round(totals["count"], 3)
    return out


def unit_price(price, pack):
    """Per-kg / per-litre / per-piece, so a shelf becomes comparable."""
    up = {"per_kg": None, "per_l": None, "per_piece": None}
    if not price:
        return up
    if pack["net_g"]:
        up["per_kg"] = round(price / (pack["net_g"] / 1000.0), 2)
    if pack["net_ml"]:
        up["per_l"] = round(price / (pack["net_ml"] / 1000.0), 2)
    if pack["pieces"] and not pack["net_g"] and not pack["net_ml"]:
        up["per_piece"] = round(price / pack["pieces"], 2)
    return up


ASSET = re.compile(r"/([^/?#]+)\.(png|jpe?g|webp|gif|avif)(?:[?#]|$)", re.I)


def image_record(url, local_path=None):
    if not url:
        return None
    m = ASSET.search(url)
    return {"url": url,
            "asset_id": m.group(1) if m else None,
            "format": m.group(2).lower() if m else None,
            "role": "primary",
            "local_path": local_path or None}


def iso(ts):
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def fnum(v):
    if v in (None, "", "None"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_rows(db, csv_path):
    """-> (rows, taxonomy_by_product, local_images). Same shape either way.

    The DB carries every shelf a product sits on; the CSV export only keeps the
    last one written, so a CSV-sourced dataset has thinner taxonomy. Everything
    else is identical, which is what lets this run with no DB at all."""
    if csv_path:
        rows, tax = [], {}
        with open(csv_path) as f:
            for r in csv.DictReader(f):
                rows.append(r)
                tax.setdefault(r["product_id"], []).append(
                    (r.get("super_category"), r.get("category_name"),
                     r.get("group_name")))
        return rows, tax, {}

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    tax = {}
    for r in con.execute("""SELECT product_id, super_category, category_name, group_name
                            FROM product_categories WHERE group_name IS NOT NULL"""):
        tax.setdefault(r["product_id"], []).append(
            (r["super_category"], r["category_name"], r["group_name"]))
    local = {}
    if con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                   "AND name='product_images'").fetchone():
        for r in con.execute("SELECT product_id, local_path FROM product_images "
                             "WHERE status='ok'"):
            local[r["product_id"]] = r["local_path"]
    rows = [dict(r) for r in con.execute(
        """SELECT product_id, name, brand, unit, price, mrp, discount_pct,
                  in_stock, merchant_id, image, super_category, category_name,
                  group_name, source, query, location, scraped_at
           FROM products ORDER BY name""")]
    con.close()
    return rows, tax, local


def build(rows, tax, local, out_dir, session_path=None):
    os.makedirs(out_dir, exist_ok=True)

    store = {}
    if session_path and os.path.exists(session_path):
        s = json.load(open(session_path))
        hdr = (s.get("delivery_header") or "").split("|")
        store = {"address": hdr[1].strip() if len(hdr) > 1 else None,
                 "eta": hdr[0].strip() if hdr else None,
                 "lat": fnum(s.get("lat")), "lon": fnum(s.get("lon"))}

    products = []
    for r in rows:
        pid = str(r["product_id"])
        price = fnum(r["price"])
        mrp = fnum(r["mrp"])
        pack = parse_pack(r["unit"])
        img = image_record(r["image"], local.get(pid))
        shelves = sorted(set(tax.get(pid, [])))
        in_stock = r["in_stock"]
        in_stock = None if in_stock in (None, "", "None") else bool(int(in_stock))
        loc = r.get("location") or ""
        lat, _, lon = loc.partition(",")

        products.append({
            "product_id": pid,
            "name": (r["name"] or "").strip(),
            "brand": (r["brand"] or "").strip() or None,
            "pack": pack,
            "pricing": {
                "currency": CURRENCY,
                "price": price,
                "mrp": mrp,
                "savings": round(mrp - price, 2) if mrp and price and mrp > price else None,
                "discount_pct": fnum(r["discount_pct"]),
                "unit_price": unit_price(price, pack),
            },
            "availability": {
                "in_stock": in_stock,
                "merchant_id": str(r["merchant_id"]) if r.get("merchant_id") else None,
            },
            "images": [img] if img else [],
            "taxonomy": [{"super_category": a, "category": b, "shelf": c}
                         for a, b, c in shelves],
            "provenance": {
                "source": r.get("source"),
                "query": r.get("query") or None,
                "store": {"location": loc or None,
                          "lat": fnum(lat), "lon": fnum(lon)},
                "scraped_at": iso(r.get("scraped_at")),
            },
        })

    _write_jsonl(products, os.path.join(out_dir, "products.jsonl"))
    _write_products_csv(products, os.path.join(out_dir, "products.csv"))
    _write_images_csv(products, os.path.join(out_dir, "images.csv"))
    shelf_counts = _write_taxonomy(products, out_dir)
    _write_brands(products, os.path.join(out_dir, "brands.csv"))
    stats = _manifest(products, shelf_counts, store, out_dir)
    return stats


def _write_jsonl(products, path):
    with open(path, "w") as f:
        for p in products:
            f.write(json.dumps(p, ensure_ascii=False, separators=(",", ":")) + "\n")


FLAT = ["product_id", "name", "brand", "pack_raw", "pack_kind", "pack_count",
        "pack_size", "pack_uom", "net_g", "net_ml", "pieces", "currency",
        "price", "mrp", "savings", "discount_pct", "price_per_kg", "price_per_l",
        "price_per_piece", "in_stock", "merchant_id", "image_url",
        "image_asset_id", "image_format", "image_local_path", "super_category",
        "category", "shelf", "n_shelves", "source", "query", "location",
        "lat", "lon", "scraped_at"]


def _write_products_csv(products, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FLAT)
        for p in products:
            pk, pr, av = p["pack"], p["pricing"], p["availability"]
            up = pr["unit_price"]
            img = p["images"][0] if p["images"] else {}
            t = p["taxonomy"][0] if p["taxonomy"] else {}
            st = p["provenance"]["store"]
            w.writerow([
                p["product_id"], p["name"], p["brand"], pk["raw"], pk["kind"],
                pk["count"], pk["size"], pk["uom"], pk["net_g"], pk["net_ml"],
                pk["pieces"], pr["currency"], pr["price"], pr["mrp"],
                pr["savings"], pr["discount_pct"], up["per_kg"], up["per_l"],
                up["per_piece"],
                "" if av["in_stock"] is None else int(av["in_stock"]),
                av["merchant_id"], img.get("url"), img.get("asset_id"),
                img.get("format"), img.get("local_path"),
                t.get("super_category"), t.get("category"), t.get("shelf"),
                len(p["taxonomy"]), p["provenance"]["source"],
                p["provenance"]["query"], st["location"], st["lat"], st["lon"],
                p["provenance"]["scraped_at"],
            ])


def _write_images_csv(products, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "role", "url", "asset_id", "format", "local_path"])
        for p in products:
            for img in p["images"]:
                w.writerow([p["product_id"], img["role"], img["url"],
                            img["asset_id"], img["format"], img["local_path"]])


def _write_taxonomy(products, out_dir):
    counts = {}
    with open(os.path.join(out_dir, "product_taxonomy.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "super_category", "category", "shelf"])
        for p in products:
            for t in p["taxonomy"]:
                key = (t["super_category"], t["category"], t["shelf"])
                counts[key] = counts.get(key, 0) + 1
                w.writerow([p["product_id"], key[0], key[1], key[2]])
    with open(os.path.join(out_dir, "taxonomy.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["super_category", "category", "shelf", "n_products"])
        for key in sorted(counts, key=lambda k: tuple(x or "" for x in k)):
            w.writerow([key[0], key[1], key[2], counts[key]])
    return counts


def _write_brands(products, path):
    agg = {}
    for p in products:
        b = p["brand"] or ""
        d = agg.setdefault(b, {"n": 0, "prices": [], "stock": 0})
        d["n"] += 1
        if p["pricing"]["price"]:
            d["prices"].append(p["pricing"]["price"])
        if p["availability"]["in_stock"]:
            d["stock"] += 1
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["brand", "n_products", "n_in_stock", "median_price",
                    "min_price", "max_price"])
        for b in sorted(agg, key=lambda k: (-agg[k]["n"], k)):
            d = agg[b]
            pz = d["prices"]
            w.writerow([b, d["n"], d["stock"],
                        round(statistics.median(pz), 2) if pz else "",
                        min(pz) if pz else "", max(pz) if pz else ""])


SCHEMA_DOC = {
    "product_id": "Blinkit's own SKU id. Unique per store, stable across runs.",
    "pack": "Parsed from the free-text `unit` field. kind is weight|volume|count|"
            "mixed, or null when the text did not parse (raw is always kept). "
            "Measured on this catalog, effectively every null is a book SKU: "
            "Blinkit puts the author or publisher in the unit field for books "
            "('Ruskin Bond', 'Maple Press'), so a null kind on a Books shelf is "
            "a byline, not a missing pack size.",
    "pack.net_g": "Total net grams in the pack, multipacks and combos summed.",
    "pack.net_ml": "Total net millilitres in the pack.",
    "pack.pieces": "Total countable pieces (pc, set, pair, sheet, ...).",
    "pricing.price": "What this darkstore charges, INR.",
    "pricing.mrp": "Printed maximum retail price, INR. Null when not advertised.",
    "pricing.unit_price": "price normalized per kg / per litre / per piece.",
    "availability.in_stock": "As the store served it at scrape time.",
    "images": "Image assets from Blinkit's CDN. asset_id is the CDN filename, "
              "usable as a dedup key. local_path is set only after "
              "download_images.py has run.",
    "taxonomy": "Every shelf the product sits on. A product can sit on several.",
    "provenance": "Which endpoint the row came from, which store, and when. "
                  "Inventory is per-darkstore: another pin is a different catalog.",
}


def _manifest(products, shelf_counts, store, out_dir):
    prices = [p["pricing"]["price"] for p in products if p["pricing"]["price"]]
    prices.sort()
    kinds = {}
    for p in products:
        k = p["pack"]["kind"] or "unparsed"
        kinds[k] = kinds.get(k, 0) + 1
    supers = {}
    for key, n in shelf_counts.items():
        supers[key[0] or ""] = supers.get(key[0] or "", 0) + n
    scraped = [p["provenance"]["scraped_at"] for p in products
               if p["provenance"]["scraped_at"]]

    def pct(x):
        return prices[min(len(prices) - 1, int(len(prices) * x))] if prices else None

    stats = {
        "products": len(products),
        "with_image": sum(1 for p in products if p["images"]),
        "with_local_image": sum(1 for p in products
                                if p["images"] and p["images"][0]["local_path"]),
        "with_price": len(prices),
        "with_mrp": sum(1 for p in products if p["pricing"]["mrp"]),
        "discounted": sum(1 for p in products if p["pricing"]["savings"]),
        "in_stock": sum(1 for p in products if p["availability"]["in_stock"]),
        "brands": len(set(p["brand"] or "" for p in products)),
        "shelves": len(shelf_counts),
        "pack_kinds": kinds,
        "pack_parsed_pct": round(
            100.0 * (len(products) - kinds.get("unparsed", 0)) / max(len(products), 1), 2),
        "price_percentiles": {"p10": pct(.10), "p50": pct(.50), "p90": pct(.90),
                              "min": prices[0] if prices else None,
                              "max": prices[-1] if prices else None},
        "products_per_super_category": dict(sorted(supers.items(),
                                                   key=lambda kv: -kv[1])),
        "scraped_at_range": [min(scraped), max(scraped)] if scraped else None,
    }
    manifest = {
        "dataset": "blinkit-darkstore-catalog",
        "generated_at": iso(int(time.time())),
        "store": store,
        "currency": CURRENCY,
        "files": {
            "products.jsonl": "One nested JSON object per product.",
            "products.csv": "Flat typed view with derived columns.",
            "images.csv": "One row per image asset.",
            "taxonomy.csv": "Shelf tree with product counts.",
            "product_taxonomy.csv": "Product-to-shelf many-to-many.",
            "brands.csv": "Per-brand rollup.",
        },
        "schema": SCHEMA_DOC,
        "stats": stats,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="blinkit.db")
    ap.add_argument("--csv", help="read from a crawl CSV instead of the DB")
    ap.add_argument("--session", default="session_delhi.json")
    ap.add_argument("--out", default="dataset")
    args = ap.parse_args()

    if not args.csv and not os.path.exists(args.db):
        sys.exit("no such db: %s (pass --csv to build from a CSV export)" % args.db)

    rows, tax, local = load_rows(args.db, args.csv)
    if not rows:
        sys.exit("no products found -- run crawl.py first")
    stats = build(rows, tax, local, args.out, args.session)

    print("%s/" % args.out)
    for name in sorted(os.listdir(args.out)):
        p = os.path.join(args.out, name)
        print("  %-22s %7.1f MB" % (name, os.path.getsize(p) / 1e6))
    print("\n%d products | %d brands | %d shelves | %d%% with image | "
          "%.1f%% pack parsed"
          % (stats["products"], stats["brands"], stats["shelves"],
             round(100 * stats["with_image"] / max(stats["products"], 1)),
             stats["pack_parsed_pct"]))


if __name__ == "__main__":
    main()
