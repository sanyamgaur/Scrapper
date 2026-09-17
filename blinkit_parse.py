#!/usr/bin/env python3
"""Shape-agnostic product extraction.

Blinkit ships its listing payloads as deeply nested "snippet" trees whose exact
shape changes between releases. Rather than pinning to one path we walk the
whole JSON and lift out any object that looks like a product, so a layout
change degrades into missing optional fields instead of zero rows.
"""

ID_KEYS = ("product_id", "productId", "merchant_id", "item_id", "sku_id",
           "variant_id", "product_variant_id")
NAME_KEYS = ("name", "display_name", "product_name", "title", "product_title")
PRICE_KEYS = ("price", "normal_price", "offer_price", "discounted_price",
              "selling_price", "unit_price", "final_price")
MRP_KEYS = ("mrp", "original_price", "strike_price", "crossed_price", "compare_price")
UNIT_KEYS = ("unit", "weight", "quantity", "variant", "pack_size", "unit_text")
BRAND_KEYS = ("brand", "brand_name", "manufacturer")
IMAGE_KEYS = ("image", "image_url", "images", "product_image", "thumbnail")
STOCK_KEYS = ("inventory", "stock", "available_quantity", "in_stock",
              "is_available", "out_of_stock", "max_quantity")


def text(v):
    """Unwrap Blinkit's {"text": "..."} / {"value": ...} scalar wrappers."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        for k in ("text", "value", "title", "name", "url", "label"):
            if k in v:
                return text(v[k])
        return None
    if isinstance(v, list) and v:
        return text(v[0])
    return None


def first(d, keys):
    for k in keys:
        if k in d:
            val = text(d[k])
            if val not in (None, "", []):
                return val
    return None


def image_url(v):
    """Normalize whatever the IMAGE_KEYS lookup returned into an absolute URL.

    Blinkit serves images protocol-relative ("//cdn...") from some snippet
    types and absolute from others; the API itself is never hit for this, it
    is already sitting in the same listing payload as the price and name.
    """
    if not v or not isinstance(v, str):
        return v
    if v.startswith("//"):
        return "https:" + v
    if v.startswith("/"):
        return "https://cdn.grofers.com" + v
    return v


def money(v):
    """'₹1,299.00' / '1299' / 1299 -> 1299.0"""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = "".join(c for c in str(v) if c.isdigit() or c == ".")
    if not s or s == ".":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def looks_like_product(d):
    has_id = any(k in d for k in ID_KEYS)
    has_name = any(k in d for k in NAME_KEYS)
    has_price = any(k in d for k in PRICE_KEYS + MRP_KEYS)
    # id+name is a product; name+price is a product; id alone is not (too many
    # widget/banner objects carry a bare id).
    return (has_id and has_name) or (has_name and has_price)


def extract_products(obj, ctx=None, out=None, depth=0):
    """Walk any JSON and return a list of normalized product dicts."""
    ctx = ctx or {}
    if out is None:
        out = []
    if depth > 40:
        return out

    if isinstance(obj, dict):
        # A snippet usually wraps the real payload in "data"; merge so keys on
        # either level are visible to the heuristic.
        merged = dict(obj)
        inner = obj.get("data")
        if isinstance(inner, dict):
            merged = dict(inner)
            for k, v in obj.items():
                merged.setdefault(k, v)
        ident = merged.get("identity")
        if isinstance(ident, dict) and "id" in ident:
            merged.setdefault("product_id", ident["id"])

        if looks_like_product(merged):
            pid = first(merged, ID_KEYS)
            name = first(merged, NAME_KEYS)
            if pid is not None and name:
                price = money(first(merged, PRICE_KEYS))
                mrp = money(first(merged, MRP_KEYS))
                stock_raw = first(merged, STOCK_KEYS)
                out.append({
                    "product_id": str(pid),
                    "name": str(name).strip(),
                    "brand": first(merged, BRAND_KEYS),
                    "unit": first(merged, UNIT_KEYS),
                    "price": price,
                    "mrp": mrp,
                    "discount_pct": (
                        round((mrp - price) / mrp * 100, 2)
                        if mrp and price and mrp > price else None
                    ),
                    "image": image_url(first(merged, IMAGE_KEYS)),
                    "in_stock": _stock_bool(merged, stock_raw),
                    "stock_raw": stock_raw,
                    "merchant_id": merged.get("merchant_id"),
                    "l0_cat": ctx.get("l0_cat"),
                    "l1_cat": ctx.get("l1_cat"),
                    "category_name": ctx.get("category_name"),
                    "source": ctx.get("source"),
                    "query": ctx.get("query"),
                    "raw": merged,
                })
                # Do not descend into a matched product -- its children are
                # atoms (price tag, badge), not nested products.
                return out

        for v in merged.values():
            extract_products(v, ctx, out, depth + 1)

    elif isinstance(obj, list):
        for v in obj:
            extract_products(v, ctx, out, depth + 1)

    return out


def _stock_bool(d, stock_raw):
    if "out_of_stock" in d:
        return not bool(text(d["out_of_stock"]))
    for k in ("in_stock", "is_available"):
        if k in d:
            return bool(text(d[k]))
    if isinstance(stock_raw, (int, float)):
        return stock_raw > 0
    return None


def find_pagination(obj, depth=0):
    """Locate a next-page cursor anywhere in the response."""
    if depth > 30:
        return None
    if isinstance(obj, dict):
        for k in ("postback_params", "next_url", "next_page_url", "cursor",
                  "next_cursor", "pagination_token"):
            if obj.get(k):
                return {k: obj[k]}
        if obj.get("has_more") is True or obj.get("hasMore") is True:
            for k in ("offset", "next_offset", "page", "page_index", "page_no"):
                if k in obj:
                    return {k: obj[k]}
            return {"has_more": True}
        for v in obj.values():
            r = find_pagination(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_pagination(v, depth + 1)
            if r:
                return r
    return None
