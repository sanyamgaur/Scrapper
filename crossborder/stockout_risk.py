"""Stockout risk engine: which SKUs are most likely to vanish before we buy them.

The honest framing matters here. On day one there is NO availability history --
`availability_events` is empty -- so this engine cannot predict in any
meaningful sense. What it can do on day one is rank by evidence that genuinely
exists in a single crawl snapshot:

  - the SKU is out of stock RIGHT NOW (by far the strongest signal)
  - the shelf's own out-of-stock base rate, which ranges from 18% (Toys & Games)
    to 92% (Baby Toys & Gifts) and is therefore a real prior
  - deep discounting, which often precedes clearance

As availability runs accumulate, history signals switch on: how often a SKU
flips, and how long it stays gone once it does. The score is a plain weighted
sum over whatever signals are available, renormalized so a missing signal does
not silently drag the score toward zero.

Every score carries a CONFIDENCE. A score computed with no history is labelled
`low`, and callers are expected to treat it as a ranking hint rather than a
prediction. Nothing in this module pretends to know more than it does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .db import connect, DB_PATH

RULES_PATH = Path(__file__).parent / "rules" / "procurement.yaml"


@dataclass
class RiskScore:
    product_id: str
    score: float
    bucket: str                     # CRITICAL | HIGH | NORMAL
    confidence: str                 # low | medium | high
    signals: dict[str, float] = field(default_factory=dict)

    def explain(self) -> str:
        """Operator-facing. A number nobody can interrogate gets ignored."""
        if not self.signals:
            return f"{self.bucket} ({self.score:.2f}) - no signals"
        top = sorted(self.signals.items(), key=lambda kv: -abs(kv[1]))[:3]
        why = ", ".join(f"{k.replace('_',' ')} {v:+.2f}" for k, v in top)
        return f"{self.bucket} ({self.score:.2f}, {self.confidence} confidence): {why}"

    def as_dict(self) -> dict:
        return {"product_id": self.product_id, "score": round(self.score, 4),
                "bucket": self.bucket, "confidence": self.confidence,
                "signals": self.signals, "explain": self.explain()}


class StockoutRiskEngine:
    def __init__(self, db_path=DB_PATH, rules_path: Path | str = RULES_PATH):
        cfg = yaml.safe_load(Path(rules_path).read_text())
        self.cfg = cfg["risk"]
        self.w = self.cfg["weights"]
        self.buckets = self.cfg["buckets"]
        self.db_path = db_path
        self._shelf_rate: dict[str, float] = {}
        self._runs = 0

    # -- priors --------------------------------------------------------------

    def load_priors(self, conn) -> None:
        """Shelf out-of-stock base rates from the current snapshot.

        Shelves with too few SKUs get the catalogue-wide rate instead of a rate
        computed from a handful of items, which would be noise dressed as signal.
        """
        total = conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN in_stock=0 THEN 1 ELSE 0 END) o "
            "FROM products").fetchone()
        self._global_rate = (total["o"] / total["n"]) if total["n"] else 0.5

        self._shelf_rate = {}
        for r in conn.execute("""
                SELECT group_name, COUNT(*) n,
                       SUM(CASE WHEN in_stock=0 THEN 1 ELSE 0 END) o
                FROM products WHERE group_name IS NOT NULL GROUP BY 1"""):
            self._shelf_rate[r["group_name"]] = (
                r["o"] / r["n"] if r["n"] >= 25 else self._global_rate)

        # How much history exists decides which signals are even usable.
        try:
            self._runs = conn.execute(
                "SELECT COUNT(DISTINCT checked_at) FROM stock_checks").fetchone()[0] or 0
        except Exception:
            self._runs = 0

    def _confidence(self) -> str:
        c = self.cfg["confidence"]
        if self._runs >= c["high_min_runs"]:
            return "high"
        if self._runs >= c["medium_min_runs"]:
            return "medium"
        return "low"

    # -- history signals -----------------------------------------------------

    def _history(self, conn, product_id: str) -> dict[str, Optional[float]]:
        """Flip frequency and restock latency, when readings exist.

        Returns None values when there is nothing to compute, so the caller
        renormalizes rather than treating absence as a zero-risk reading.
        """
        rows = conn.execute(
            "SELECT in_stock, checked_at FROM stock_checks WHERE product_id=? "
            "ORDER BY checked_at", (product_id,)).fetchall()
        if len(rows) < 3:
            return {"flip_frequency": None, "restock_latency": None,
                    "never_seen_in_stock": None}

        states = [bool(r["in_stock"]) for r in rows]
        flips = sum(1 for a, b in zip(states, states[1:]) if a != b)
        flip_freq = min(1.0, flips / max(1, len(states) - 1) * 2.0)

        # Fraction of readings spent out of stock stands in for restock latency:
        # a SKU that is usually gone takes longer to come back.
        out_fraction = sum(1 for s in states if not s) / len(states)
        never_in = 1.0 if not any(states) else 0.0
        return {"flip_frequency": flip_freq, "restock_latency": out_fraction,
                "never_seen_in_stock": never_in}

    # -- scoring -------------------------------------------------------------

    def score_one(self, product: dict[str, Any],
                  history: Optional[dict] = None) -> RiskScore:
        history = history or {}
        raw: dict[str, float] = {}

        in_stock = product.get("in_stock")
        raw["currently_out_of_stock"] = 0.0 if in_stock else 1.0

        shelf = product.get("group_name")
        raw["shelf_base_rate"] = self._shelf_rate.get(
            shelf, getattr(self, "_global_rate", 0.5))

        # A steep markdown is often clearance rather than a bargain.
        disc = product.get("discount_pct")
        if disc is not None:
            try:
                raw["deep_discount"] = min(1.0, max(0.0, float(disc) / 60.0))
            except (TypeError, ValueError):
                pass

        for k in ("flip_frequency", "restock_latency", "never_seen_in_stock"):
            v = history.get(k)
            if v is not None:
                raw[k] = float(v)

        # Renormalize over the signals actually present. Without this, missing
        # history would pull every score down and make everything look safe.
        avail = {k: self.w[k] for k in raw if k in self.w}
        wsum = sum(avail.values()) or 1.0
        score = sum(raw[k] * self.w[k] for k in avail) / wsum
        score = max(0.0, min(1.0, score))

        bucket = ("CRITICAL" if score >= self.buckets["critical"]
                  else "HIGH" if score >= self.buckets["high"] else "NORMAL")

        contributions = {k: round(raw[k] * self.w[k] / wsum, 4) for k in avail}
        return RiskScore(product_id=str(product.get("product_id")), score=score,
                         bucket=bucket, confidence=self._confidence(),
                         signals=contributions)

    def score_all(self, db_path=None, listable_only: bool = True) -> dict:
        """Score the catalogue and persist. Returns bucket counts."""
        conn = connect(db_path or self.db_path)
        self.load_priors(conn)

        q = "SELECT p.* FROM products p"
        if listable_only:
            q += (" JOIN classifications c USING(product_id) "
                  "WHERE c.verdict IN ('ALLOWED','REVIEW')")
        products = [dict(r) for r in conn.execute(q)]

        # One pass over stock_checks beats a per-SKU query across 31k rows.
        have_history = {r[0] for r in conn.execute(
            "SELECT DISTINCT product_id FROM stock_checks")} if self._runs else set()

        rows = []
        counts: dict[str, int] = {}
        for p in products:
            hist = self._history(conn, p["product_id"]) if p["product_id"] in have_history else {}
            rs = self.score_one(p, hist)
            counts[rs.bucket] = counts.get(rs.bucket, 0) + 1
            rows.append((rs.product_id, rs.score, rs.bucket, rs.confidence,
                         json.dumps(rs.signals)))

        conn.executemany("""
            INSERT INTO stockout_risk (product_id,score,bucket,confidence,signals)
            VALUES (?,?,?,?,?)
            ON CONFLICT(product_id) DO UPDATE SET
                score=excluded.score, bucket=excluded.bucket,
                confidence=excluded.confidence, signals=excluded.signals,
                scored_at=datetime('now')""", rows)
        conn.commit(); conn.close()
        counts["confidence"] = self._confidence()
        counts["history_runs"] = self._runs
        return counts

    def get(self, product_id: str, db_path=None) -> Optional[RiskScore]:
        conn = connect(db_path or self.db_path)
        r = conn.execute("SELECT * FROM stockout_risk WHERE product_id=?",
                         (product_id,)).fetchone()
        conn.close()
        if not r:
            return None
        return RiskScore(product_id=r["product_id"], score=r["score"],
                         bucket=r["bucket"], confidence=r["confidence"],
                         signals=json.loads(r["signals"] or "{}"))
