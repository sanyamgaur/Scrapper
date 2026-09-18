"""Product image serving.

The catalogue stores third-party CDN URLs (cdn.grofers.com). Pointing a US
storefront straight at them works right up until it doesn't:

  - hotlink protection can be switched on at any time and every image dies
  - the asset is served from India to US customers, which is the slow path
  - there is no way to resize, re-encode or cache anything
  - an outage on their side is an outage on the storefront

So the app references `/img/<product_id>` instead. That route serves a locally
cached file when one exists and redirects to the origin CDN when it does not,
which means the storefront works before the cache is warm and gets faster as it
fills. `warm()` populates the cache in the background.

Nothing here fabricates an image. A product with no usable image falls through
to the frontend's monogram placeholder.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .db import connect, DB_PATH

CACHE_DIR = Path("image_cache")
# Only formats a browser renders inline; anything else is not an image we want.
EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
       "image/gif": ".gif", "image/avif": ".avif"}
MAX_BYTES = 3_000_000


def cache_path(url: str) -> Optional[Path]:
    """Deterministic path for a URL, or None if it is not cached yet."""
    if not url:
        return None
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    for ext in (".jpg", ".png", ".webp", ".gif", ".avif"):
        p = CACHE_DIR / key[:2] / (key + ext)
        if p.exists():
            return p
    return None


def _fetch(url: str, timeout: float = 12.0) -> Optional[Path]:
    """Download one image. Returns the cached path, or None on any failure.

    Deliberately forgiving: a failed image is a placeholder, never an error
    page, so every exception resolves to None.
    """
    try:
        import httpx
    except ImportError:
        return None
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return None
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
            ext = EXT.get(ctype)
            if not ext or len(r.content) > MAX_BYTES or not r.content:
                return None
            out = CACHE_DIR / key[:2] / (key + ext)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(r.content)
            return out
    except Exception:
        return None


def warm(limit: Optional[int] = None, workers: int = 8,
         db_path=DB_PATH, listable_only: bool = True) -> dict:
    """Populate the cache. Safe to re-run; already-cached images are skipped."""
    conn = connect(db_path)
    q = """SELECT p.product_id, p.image FROM products p"""
    if listable_only:
        q += " JOIN classifications c USING(product_id) WHERE c.verdict='ALLOWED'"
        q += " AND p.image IS NOT NULL AND p.image != ''"
    else:
        q += " WHERE p.image IS NOT NULL AND p.image != ''"
    q += " ORDER BY p.in_stock DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = [(r["product_id"], r["image"]) for r in conn.execute(q)]
    conn.close()

    todo = [(pid, url) for pid, url in rows if cache_path(url) is None]
    done = failed = 0
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for res in ex.map(lambda t: _fetch(t[1]), todo):
                if res:
                    done += 1
                else:
                    failed += 1
    return {"candidates": len(rows), "already_cached": len(rows) - len(todo),
            "downloaded": done, "failed": failed}


def stats(db_path=DB_PATH) -> dict:
    conn = connect(db_path)
    rows = [r["image"] for r in conn.execute(
        """SELECT p.image FROM products p JOIN classifications c USING(product_id)
           WHERE c.verdict='ALLOWED' AND p.image IS NOT NULL AND p.image != ''""")]
    conn.close()
    cached = sum(1 for u in rows if cache_path(u))
    size = sum(f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file()) \
        if CACHE_DIR.exists() else 0
    return {"listable_with_image": len(rows), "cached": cached,
            "cache_mb": round(size / 1e6, 1)}
