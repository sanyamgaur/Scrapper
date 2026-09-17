#!/usr/bin/env python3
"""
Build a single organised .xlsx of the whole catalog, with the product image
rendering in the cell next to each row.

    python make_workbook.py --dataset dataset --out blinkit_catalog.xlsx

Why the image column is a formula and not an embedded picture
-------------------------------------------------------------
An .xlsx can only carry a picture by embedding the bytes, and the crawl stores
image *URLs* (which is the point -- see README: the listing API gives them away
for free, downloading 31k files does not). `=IMAGE(url)` defers the fetch to
whoever opens the workbook, so the file stays ~5 MB instead of ~2 GB and needs
no image download step first.

IMAGE() is Excel 365 / Google Sheets only, so the raw URL is kept in its own
column and the product name links to it. In an older Excel the thumbnail column
shows #NAME? and everything else still works.
"""
import argparse
import csv
import json
import os
import statistics
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

FONT = "Arial"
INK = "1A1A1A"
MUTED = "6C736D"
HEAD_BG = "1F2430"
BAND = "F4F5F2"
ACCENT = "3B45B5"

MONEY = '₹#,##0;-₹#,##0;"—"'
MONEY2 = '₹#,##0.00;-₹#,##0.00;"—"'
PCT = '0.0%;-0.0%;"—"'
NUM = '#,##0;-#,##0;"—"'
NUM2 = '#,##0.0;-#,##0.0;"—"'

COLUMNS = [
    ("Image", 12),          ("Product", 46),     ("Brand", 20),
    ("Pack", 14),           ("Pack type", 11),   ("Net g", 9),
    ("Net ml", 9),          ("Pieces", 8),       ("Price", 11),
    ("MRP", 11),            ("You save", 11),    ("% off", 9),
    ("₹ / kg", 11),         ("₹ / litre", 11),   ("₹ / piece", 11),
    ("In stock", 9),        ("Department", 22),  ("Category", 22),
    ("Shelf", 26),          ("Product ID", 12),  ("Image URL", 62),
]

THIN = Side(style="thin", color="DDDDDD")


def fnum(v):
    if v in (None, "", "None"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(dataset):
    path = os.path.join(dataset, "products.csv")
    if not os.path.exists(path):
        sys.exit("no %s -- run export_dataset.py first" % path)
    with open(path) as f:
        rows = list(csv.DictReader(f))
    manifest = {}
    mpath = os.path.join(dataset, "manifest.json")
    if os.path.exists(mpath):
        manifest = json.load(open(mpath))
    # Organised means sorted the way a person browses: aisle, then shelf, then
    # name -- not by the id the crawler happened to see first.
    rows.sort(key=lambda r: ((r.get("super_category") or "~").lower(),
                             (r.get("category") or "~").lower(),
                             (r.get("shelf") or "~").lower(),
                             (r.get("name") or "").lower()))
    return rows, manifest


def style_header(ws, ncols, row=1):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = Font(name=FONT, bold=True, size=10, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=HEAD_BG)
        cell.alignment = Alignment(vertical="center", horizontal="left",
                                   wrap_text=True)
    ws.row_dimensions[row].height = 30


def build_products(wb, rows, with_image):
    ws = wb.create_sheet("Products")
    ws.append([c[0] for c in COLUMNS])
    style_header(ws, len(COLUMNS))
    for i, (name, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width

    body = Font(name=FONT, size=10, color=INK)
    faint = Font(name=FONT, size=10, color=MUTED)
    link = Font(name=FONT, size=10, color=ACCENT, underline="single")

    for n, r in enumerate(rows, start=2):
        price = fnum(r["price"])
        mrp = fnum(r["mrp"])
        pct = fnum(r["discount_pct"])
        url = r["image_url"] or ""

        ws.cell(row=n, column=1).value = (
            '=IMAGE("%s")' % url.replace('"', "%22") if (with_image and url) else None)
        c = ws.cell(row=n, column=2, value=r["name"])
        if url:
            c.hyperlink = url
            c.font = link
        else:
            c.font = body
        ws.cell(row=n, column=3, value=r["brand"] or None).font = body
        ws.cell(row=n, column=4, value=r["pack_raw"] or None).font = faint
        ws.cell(row=n, column=5, value=r["pack_kind"] or None).font = faint
        for col, key in ((6, "net_g"), (7, "net_ml"), (8, "pieces")):
            cell = ws.cell(row=n, column=col, value=fnum(r[key]))
            cell.font = body
            cell.number_format = NUM2
        for col, val, fmt in ((9, price, MONEY), (10, mrp, MONEY),
                              (11, fnum(r["savings"]), MONEY)):
            cell = ws.cell(row=n, column=col, value=val)
            cell.font = body
            cell.number_format = fmt
        cell = ws.cell(row=n, column=12,
                       value=(pct / 100.0) if pct is not None else None)
        cell.font = body
        cell.number_format = PCT
        for col, key in ((13, "price_per_kg"), (14, "price_per_l"),
                         (15, "price_per_piece")):
            cell = ws.cell(row=n, column=col, value=fnum(r[key]))
            cell.font = body
            cell.number_format = MONEY2
        ws.cell(row=n, column=16,
                value={"1": "Yes", "0": "No"}.get(r["in_stock"], "")).font = body
        ws.cell(row=n, column=17, value=r["super_category"] or None).font = body
        ws.cell(row=n, column=18, value=r["category"] or None).font = body
        ws.cell(row=n, column=19, value=r["shelf"] or None).font = body
        ws.cell(row=n, column=20, value=r["product_id"]).font = faint
        ws.cell(row=n, column=21, value=url or None).font = faint

        for col in range(1, len(COLUMNS) + 1):
            cell = ws.cell(row=n, column=col)
            cell.border = Border(bottom=THIN)
            if cell.alignment.vertical != "center":
                cell.alignment = Alignment(vertical="center",
                                           wrap_text=(col == 2))
        if with_image:
            ws.row_dimensions[n].height = 56

    last = len(rows) + 1
    ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(COLUMNS)), last)
    ws.freeze_panes = "C2"
    return ws, last


def build_rollup(wb, title, label_cols, groups, note=None):
    """A rollup of already-aggregated groups.

    These are stored values rather than COUNTIFS/AVERAGEIFS on purpose. This
    workbook is a snapshot of a finished crawl -- nothing downstream edits the
    Products sheet, so there is no live total to keep live, and ~2,000
    criteria-formulas each scanning 31k rows would make the file chug on every
    open for no gain. The numbers are aggregated in Python and cross-checked
    against dataset/taxonomy.csv and dataset/brands.csv, which are built by a
    separate pass over the same data."""
    ws = wb.create_sheet(title)
    headers = label_cols + ["Products", "In stock", "Median price", "Avg price"]
    ws.append(headers)
    style_header(ws, len(headers))
    for i, w in enumerate([26] * len(label_cols) + [12, 11, 14, 13], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    body = Font(name=FONT, size=10, color=INK)
    for n, (key, agg) in enumerate(groups, start=2):
        for i, val in enumerate(key, start=1):
            ws.cell(row=n, column=i, value=val).font = body
        base = len(label_cols)
        for off, (val, fmt) in enumerate((
                (agg["n"], NUM), (agg["in_stock"], NUM),
                (agg["median"], MONEY), (agg["avg"], MONEY)), start=1):
            c = ws.cell(row=n, column=base + off, value=val)
            c.font = body
            c.number_format = fmt
        for col in range(1, len(headers) + 1):
            ws.cell(row=n, column=col).border = Border(bottom=THIN)

    ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(headers)), len(groups) + 1)
    ws.freeze_panes = "A2"
    if note:
        r = len(groups) + 3
        ws.cell(row=r, column=1, value=note).font = Font(
            name=FONT, size=9, italic=True, color=MUTED)
    return ws


def aggregate(rows, keyfn):
    """-> [(key_tuple, {n, in_stock, median, avg}), ...] sorted by key."""
    buckets = {}
    for r in rows:
        k = keyfn(r)
        if k is None:
            continue
        b = buckets.setdefault(k, {"n": 0, "in_stock": 0, "prices": []})
        b["n"] += 1
        if r["in_stock"] == "1":
            b["in_stock"] += 1
        p = fnum(r["price"])
        if p:
            b["prices"].append(p)
    out = []
    for k in sorted(buckets):
        b = buckets[k]
        pz = sorted(b["prices"])
        out.append((k, {
            "n": b["n"], "in_stock": b["in_stock"],
            "median": round(statistics.median(pz), 2) if pz else None,
            "avg": round(sum(pz) / len(pz), 2) if pz else None,
        }))
    return out


def build_about(wb, manifest, rows, with_image):
    ws = wb.create_sheet("About", 0)
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 96
    st = manifest.get("stats", {})
    store = manifest.get("store", {})

    ws["A1"] = "Blinkit darkstore catalogue"
    ws["A1"].font = Font(name=FONT, bold=True, size=16, color=INK)
    ws["A2"] = "One row per SKU, with the product image beside it."
    ws["A2"].font = Font(name=FONT, size=10, color=MUTED)

    facts = [
        ("Store address", store.get("address")),
        ("Pinned at", "%s, %s" % (store.get("lat"), store.get("lon"))),
        ("Delivery estimate", store.get("eta")),
        ("Scraped", " to ".join(st.get("scraped_at_range") or []) or "—"),
        ("Exported", manifest.get("generated_at")),
        (None, None),
        ("Products", len(rows)),
        ("With an image", st.get("with_image")),
        ("Brands", len({(r["brand"] or "").strip() for r in rows if (r["brand"] or "").strip()})),
        ("Shelves", st.get("shelves")),
        ("In stock at scrape time", st.get("in_stock")),
        ("Discounted", st.get("discounted")),
    ]
    r = 4
    for k, v in facts:
        if k is None:
            r += 1
            continue
        ws.cell(row=r, column=1, value=k).font = Font(name=FONT, bold=True, size=10, color=INK)
        c = ws.cell(row=r, column=2, value=v)
        c.font = Font(name=FONT, size=10, color=INK)
        if isinstance(v, int):
            c.number_format = NUM
        r += 1

    notes = [
        "",
        "How to read this workbook",
        "Products — every SKU, sorted by department, then category, then shelf, then name. "
        "Use the filter arrows in row 1. The product name links to its image.",
        "By shelf / By brand — counts, in-stock tallies, median and average price per group. "
        "These are stored values, not formulas: this is a snapshot of a finished crawl, so "
        "there is nothing for a live total to track, and they were cross-checked against a "
        "separate aggregation of the same data.",
        "",
        "About the Image column",
        ("Column A uses =IMAGE(url), which fetches the picture from Blinkit's CDN when you "
         "open the file. It needs Excel 365 or Google Sheets and an internet connection; in "
         "an older Excel or LibreOffice that column shows #NAME? and nothing else is affected "
         "— the raw URL is always in the last column."
         if with_image else
         "This copy has no =IMAGE() formulas; the raw image URL is in the last column."),
        "The images are hotlinked, not embedded, which is why this file is a few MB rather than "
        "several GB. Run download_images.py if you need the actual image files on disk.",
        "Be patient on first open: there are 31,366 thumbnails to fetch, and Excel pulls them "
        "as it calculates. Filter to a shelf or a brand first and it only renders what you "
        "are looking at.",
        "",
        "Caveats worth knowing",
        "Inventory is per-darkstore. These prices and this assortment belong to the one store "
        "pinned above; another location returns a different catalogue.",
        "Pack type is parsed from Blinkit's free-text pack field. Where it is blank the text "
        "did not parse — on this catalogue those are all books, because Blinkit puts the "
        "author's name in the pack field for book SKUs.",
        "₹/kg, ₹/litre and ₹/piece are derived from the parsed pack size, so a shelf can be "
        "compared like for like.",
    ]
    r += 1
    for line in notes:
        c = ws.cell(row=r, column=1 if line and not line.startswith(("Products —", "By shelf", "Column A", "This copy", "The images", "Inventory", "Pack type", "₹/kg")) else 2,
                    value=line or None)
        if line and c.column == 1:
            c.font = Font(name=FONT, bold=True, size=11, color=INK)
        else:
            c.font = Font(name=FONT, size=10, color=MUTED)
            c.alignment = Alignment(wrap_text=True, vertical="top")
            ws.row_dimensions[r].height = 30
        r += 1
    return ws


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="dataset")
    ap.add_argument("--out", default="blinkit_catalog.xlsx")
    ap.add_argument("--no-image-formula", action="store_true",
                    help="write the URL only, no =IMAGE() (for recalc checks)")
    ap.add_argument("--limit", type=int, help="first N products only (testing)")
    args = ap.parse_args()

    rows, manifest = load(args.dataset)
    if args.limit:
        rows = rows[:args.limit]
    with_image = not args.no_image_formula

    wb = Workbook()
    wb.remove(wb.active)
    build_products(wb, rows, with_image)

    shelves = aggregate(rows, lambda r: (r["super_category"] or "",
                                         r["category"] or "", r["shelf"] or ""))
    build_rollup(wb, "By shelf", ["Department", "Category", "Shelf"], shelves)

    brands = aggregate(rows, lambda r: ((r["brand"] or "").strip(),)
                       if (r["brand"] or "").strip() else None)
    brands.sort(key=lambda kv: (-kv[1]["n"], kv[0][0]))
    build_rollup(wb, "By brand", ["Brand"], brands,
                 note="All %d brands in the catalogue, most products first."
                      % len(brands))

    build_about(wb, manifest, rows, with_image)
    wb.active = 0
    wb.save(args.out)
    print("%s  %.1f MB  (%d products, %d shelves, %d brands)"
          % (args.out, os.path.getsize(args.out) / 1e6, len(rows),
             len(shelves), len(brands)))


if __name__ == "__main__":
    main()
