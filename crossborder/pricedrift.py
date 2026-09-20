"""Price drift monitor with a deadband.

Blinkit prices move constantly. Most of that movement is noise that must not
generate work, and some of it quietly destroys margin. The deadband separates
the two.

TWO THRESHOLDS, because they protect different things:

  LIST DRIFT -- the published USD price versus what the goods cost today.
    Per instruction, movement inside roughly +/- USD 3-5 is ignored. A flat USD 3
    is used as the floor rather than the whole rule, because $3 on a $25 item is
    12% while $3 on a $120 item is 2.5%. The band is therefore "the greater of
    USD 3 or 5%": quiet on small absolute moves, still awake to a 20% jump on an
    expensive SKU.

  QUOTE DRIFT -- what a specific customer was quoted versus the price at
    checkout. Held tighter, because that is a promise to a person rather than a
    shelf price. This supersedes the 12% constant previously hardcoded in
    stock.py, so there is one policy instead of two competing ones.

ASYMMETRY. A price DROP is margin in our favour. It is never allowed to block an
order or raise an alert; it is queued for a quiet reprice. Only rises erode
margin, so only rises drive the action ladder:

    within deadband        -> IGNORED
    rise beyond deadband   -> REPRICE
    rise past viability    -> DELISTED   (freight now dwarfs the goods value)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .db import connect, DB_PATH
from .pricing import PricingEngine

RULES_PATH = Path(__file__).parent / "rules" / "procurement.yaml"


@dataclass
class DriftVerdict:
    product_id: str
    listed_usd: float
    current_usd: float
    delta_usd: float
    delta_pct: float
    action: str                # IGNORED | REPRICE_SILENT | REPRICE | DELISTED
    within_deadband: bool
    reason: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class PriceDriftEngine:
    def __init__(self, db_path=DB_PATH, rules_path: Path | str = RULES_PATH,
                 pricing: Optional[PricingEngine] = None):
        cfg = yaml.safe_load(Path(rules_path).read_text())
        self.cfg = cfg["price_drift"]
        self.db_path = db_path
        self.pricing = pricing or PricingEngine()

    # -- the deadband --------------------------------------------------------

    def deadband_usd(self, reference_usd: float) -> float:
        """The greater of the absolute floor and the percentage band."""
        return max(self.cfg["list_deadband_usd"],
                   reference_usd * self.cfg["list_deadband_pct"] / 100.0)

    def quote_deadband_usd(self, reference_usd: float) -> float:
        """Tighter band for a price already quoted to a named customer.

        The invariant -- a quote is honoured more tightly than a shelf price --
        is enforced here rather than trusted to the YAML. Setting the quote
        percentage above the list percentage would otherwise silently invert the
        policy at some price points and nowhere else, which is the kind of bug
        that only shows up in a customer complaint.
        """
        band = max(self.cfg["quote_tolerance_usd"],
                   reference_usd * self.cfg["quote_tolerance_pct"] / 100.0)
        return min(band, self.deadband_usd(reference_usd))

    def check(self, listed_usd: float, current_usd: float,
              product_id: str = "", viable: bool = True,
              is_quote: bool = False) -> DriftVerdict:
        delta = current_usd - listed_usd
        pct = (delta / listed_usd * 100.0) if listed_usd else 0.0
        band = (self.quote_deadband_usd(listed_usd) if is_quote
                else self.deadband_usd(listed_usd))
        within = abs(delta) <= band

        if within:
            action = "IGNORED"
            reason = (f"moved ${delta:+.2f} ({pct:+.1f}%), inside the "
                      f"${band:.2f} deadband")
        elif delta < 0 and self.cfg["asymmetric"]:
            # Cheaper than we listed: good news, never an alert.
            action = self.cfg["drop_action"]
            reason = (f"dropped ${abs(delta):.2f} ({pct:+.1f}%) — margin improved, "
                      f"queued for a quiet reprice")
        elif not viable and self.cfg["delist_when_unviable"]:
            action = "DELISTED"
            reason = (f"rose ${delta:.2f} ({pct:+.1f}%) and the SKU no longer "
                      f"clears its viability test")
        else:
            action = "REPRICE"
            reason = f"rose ${delta:.2f} ({pct:+.1f}%), beyond the ${band:.2f} deadband"

        return DriftVerdict(product_id=product_id, listed_usd=round(listed_usd, 2),
                            current_usd=round(current_usd, 2),
                            delta_usd=round(delta, 2), delta_pct=round(pct, 1),
                            action=action, within_deadband=within, reason=reason)

    # -- batch sweep ---------------------------------------------------------

    def sweep(self, limit: Optional[int] = None) -> dict:
        """Re-price every listed SKU against today's INR and record the drift.

        The comparison is USD list price against USD list price: INR moves that
        the FX buffer and rounding absorb never reach a human.
        """
        conn = connect(self.db_path)
        q = """SELECT p.*, d.current_usd AS last_usd FROM products p
               JOIN classifications c USING(product_id)
               LEFT JOIN (SELECT product_id, current_usd,
                                 ROW_NUMBER() OVER (PARTITION BY product_id
                                     ORDER BY detected_at DESC) rn
                          FROM price_drift) d
                 ON d.product_id = p.product_id AND d.rn = 1
               WHERE c.verdict='ALLOWED' AND p.price_inr > 0"""
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = [dict(r) for r in conn.execute(q)]

        counts: dict[str, int] = {}
        records = []
        for r in rows:
            p = dict(r); p["price"] = r["price_inr"]
            try:
                lc, _ = self.pricing.price_single(p, 1)
            except Exception:
                continue
            current = lc.list_price_usd
            listed = r["last_usd"] if r["last_usd"] else current
            v = self.check(listed, current, product_id=r["product_id"],
                           viable=lc.viable)
            counts[v.action] = counts.get(v.action, 0) + 1
            # Only record movement that actually means something; logging every
            # no-op sweep would bury the real drift in noise.
            if v.action != "IGNORED" or r["last_usd"] is None:
                records.append((r["product_id"], None, r["price_inr"],
                                listed, current, v.delta_usd, v.delta_pct, v.action))

        if records:
            conn.executemany("""INSERT INTO price_drift
                (product_id,listed_inr,current_inr,listed_usd,current_usd,
                 delta_usd,delta_pct,action) VALUES (?,?,?,?,?,?,?,?)""", records)
        conn.commit(); conn.close()
        counts["scanned"] = len(rows)
        counts["recorded"] = len(records)
        return counts
