#!/usr/bin/env python3
"""
Step 1 of 2: boot a browser once, pin a location, and record what the crawler
needs -- API auth headers, cookies, and the full category tree.

    python discover.py --lat 28.6139 --lon 77.2090 --out session_delhi.json

Fully automatic. Blinkit accepts a location set via the gr_1_lat / gr_1_lon
cookies, so no clicking is required and this runs headless by default.

The category tree comes from Blinkit's own /v1/layout/tag_collections call,
which the homepage fires on load. Each leaf is a
(collection_uuid, collection_group_id) pair -- those, not the legacy
l0_cat/l1_cat path params, are what the live listing API takes.
"""
import argparse
import asyncio
import json
import sys
import time

from playwright.async_api import async_playwright

BASE = "https://blinkit.com"
TAG_COLLECTIONS = "/v1/layout/tag_collections"

# Everything except hop-by-hop and HTTP/2 pseudo headers. Blinkit validates
# auth_key + device_id + app_client together, so keep the set intact.
SKIP_HEADERS = {"content-length", "host", "connection", "accept-encoding", "cookie"}


def build_categories(tag_collections_json):
    """tag_collections -> flat list of crawlable leaves."""
    cats = []
    cmap = (tag_collections_json or {}).get("CategoryCollectionsMap") or {}
    for group_key, group in cmap.items():
        for col in (group.get("Collection") or []):
            uuid = col.get("uuid")
            if not uuid:
                continue
            for g in (col.get("groupings") or []):
                if g.get("id") is None:
                    continue
                cats.append({
                    "collection_uuid": uuid,
                    "collection_group_id": str(g["id"]),
                    "category_name": col.get("display_name") or col.get("name"),
                    "group_name": g.get("name"),
                    "super_category": group.get("DisplayName") or group_key,
                    "collection_id": col.get("id"),
                })
    return cats


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", required=True)
    ap.add_argument("--lon", required=True)
    ap.add_argument("--out", default="session.json")
    ap.add_argument("--headless", action="store_true",
                    help="hide the browser. Cloudflare fingerprints headless "
                         "Chromium and will block you -- leave this off.")
    ap.add_argument("--probe", default="milk",
                    help="search term used to capture the search API template")
    args = ap.parse_args()

    templates = {}
    tag_json = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless)
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            geolocation={"latitude": float(args.lat), "longitude": float(args.lon)},
            permissions=["geolocation"],
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/129.0.0.0 Safari/537.36",
        )
        await ctx.add_cookies([
            {"name": n, "value": v, "domain": ".blinkit.com", "path": "/"}
            for n, v in [("gr_1_lat", str(args.lat)), ("gr_1_lon", str(args.lon)),
                         ("gr_1_locality", ""), ("gr_1_city", "")]
        ])
        page = await ctx.new_page()

        async def on_response(resp):
            url = resp.url
            if "blinkit.com/v" not in url or url.endswith(".js"):
                return
            req = resp.request
            hdrs = {k: v for k, v in req.headers.items()
                    if not k.startswith(":") and k.lower() not in SKIP_HEADERS}
            if TAG_COLLECTIONS in url:
                templates["tag_collections"] = {
                    "url": url, "method": req.method, "headers": hdrs,
                    "body": req.post_data}
                try:
                    tag_json.update(await resp.json())
                except Exception:
                    pass
            elif "/v1/layout/listing_widgets" in url:
                templates.setdefault("listing", {
                    "url": BASE + "/v1/layout/listing_widgets",
                    "method": "POST", "headers": hdrs, "body": None})
            elif "/v1/layout/search" in url:
                templates.setdefault("search", {
                    "url": BASE + "/v1/layout/search",
                    "method": "POST", "headers": hdrs, "body": None})

        page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        print("loading blinkit (location %s, %s)..." % (args.lat, args.lon), file=sys.stderr)
        await page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)

        addr = None
        try:
            addr = await page.evaluate(
                "()=>document.body.innerText.split('\\n').slice(0,6).join(' | ')")
        except Exception:
            pass

        # Visit one real category page and run one search so the listing and
        # search templates get captured with valid per-page headers.
        cats_preview = build_categories(tag_json)
        if cats_preview:
            c = cats_preview[0]
            url = "%s/dc/x/y/?collection_uuid=%s&collection_group_id=%s" % (
                BASE, c["collection_uuid"], c["collection_group_id"])
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(5000)
            except Exception:
                pass
        try:
            await page.goto("%s/s/?q=%s" % (BASE, args.probe),
                            wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(5000)
        except Exception:
            pass

        cookies = await ctx.cookies()
        await browser.close()

    cats = build_categories(tag_json)
    out = {
        "captured_at": int(time.time()),
        "lat": args.lat, "lon": args.lon,
        "delivery_header": addr,
        "cookies": {c["name"]: c["value"] for c in cookies
                    if "blinkit" in c.get("domain", "")},
        "templates": templates,
        "categories": cats,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    with open(args.out.replace(".json", "") + ".tagcollections.json", "w") as f:
        json.dump(tag_json, f, indent=2)

    print("\nlocation reported by site: %s" % (addr or "?"), file=sys.stderr)
    print("wrote %s" % args.out, file=sys.stderr)
    print("  categories : %d leaves across %d super-categories"
          % (len(cats), len(set(c["super_category"] for c in cats))), file=sys.stderr)
    print("  templates  : %s" % ", ".join(sorted(templates)), file=sys.stderr)
    if not cats:
        print("ERROR: no categories -- tag_collections did not load. Re-run with "
              "Blocked by Cloudflare? Wait a few minutes and retry.", file=sys.stderr)
        sys.exit(1)
    if "listing" not in templates:
        print("ERROR: no listing template captured.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
