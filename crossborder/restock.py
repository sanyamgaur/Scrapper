"""Back-in-stock detection and the relisting bucket.

A SKU returning to stock is not the same as a SKU being listable again. Three
gates stand between "Blinkit has it" and "put it on the site":

1. STABILITY. In stock for ten minutes means nothing on a q-commerce app -- a
   single restock pallet can sell out in an hour. A SKU must hold availability
   for `stability_hours` before it is recommended.

2. FLAP SUPPRESSION. Some SKUs oscillate several times a day. Left alone they
   would dominate the relist bucket with noise, so a SKU exceeding
   `max_flips_per_day` is suppressed for a cooling period.

3. COMPLIANCE AND VIABILITY. Coming back in stock does not make an item legal to
   export or commercially sensible. The relist candidate is re-checked against
   the compliance verdict and the pricing viability test before it is offered.

Relisting is RECOMMENDED rather than automatic. `auto_relist` exists in policy
and defaults to false: putting a SKU back on the site is a business decision,
and the engine's job is to surface it with the evidence, not to make it.

The waitlist is capped deliberately. Telling 200 people an item is back when a
handful of units exist manufactures 194 complaints and a support queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from .db import connect, DB_PATH

RULES_PATH = Path(__file__).parent / "rules" / "procurement.yaml"


@dataclass
class RelistCandidate:
    product_id: str
    name: str
    status: str
    stable_since: Optional[str]
    flap_count: int
    verdict: Optional[str]
    viable: Optional[bool]
    reason: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class RestockEngine:
    def __init__(self, db_path=DB_PATH, rules_path: Path | str = RULES_PATH):
        cfg = yaml.safe_load(Path(rules_path).read_text())
        self.cfg = cfg["restock"]
        self.db_path = db_path

    def detect(self, actor: str = "system") -> dict:
        """Scan stock history for out->in transitions and update the bucket.

        Reads `stock_checks`, which the availability engine and the checkout gate
        both write, so this works off the same readings that drive everything
        else rather than a parallel source of truth.
        """
        conn = connect(self.db_path)
        rows = conn.execute("""
            SELECT product_id, in_stock, checked_at
            FROM stock_checks ORDER BY product_id, checked_at""").fetchall()

        by_pid: dict[str, list] = {}
        for r in rows:
            by_pid.setdefault(r["product_id"], []).append(r)

        detected = suppressed = promoted = 0
        for pid, readings in by_pid.items():
            states = [(bool(r["in_stock"]), r["checked_at"]) for r in readings]
            if len(states) < 2 or not states[-1][0]:
                continue   # not currently in stock -> not a relist candidate

            flips = sum(1 for a, b in zip(states, states[1:]) if a[0] != b[0])
            # Walk back to where the current in-stock streak began.
            stable_since = states[-1][1]
            for s, at in reversed(states[:-1]):
                if not s:
                    break
                stable_since = at

            came_back = any(not a[0] and b[0] for a, b in zip(states, states[1:]))
            if not came_back:
                continue
            detected += 1

            status = "WATCHING"
            if flips > self.cfg["max_flips_per_day"]:
                status = "WATCHING"      # flapping: held, never promoted
                suppressed += 1
            else:
                age_h = conn.execute(
                    "SELECT (julianday('now') - julianday(?)) * 24.0", (stable_since,)
                ).fetchone()[0] or 0
                if age_h >= self.cfg["stability_hours"]:
                    status = "READY"
                    promoted += 1

            cl = conn.execute("SELECT verdict FROM classifications WHERE product_id=?",
                              (pid,)).fetchone()
            verdict = cl["verdict"] if cl else None
            # A restocked SKU that is BLOCKED never reaches the bucket at all.
            if verdict == "BLOCKED":
                status = "DISMISSED"

            conn.execute("""
                INSERT INTO relist_queue
                    (product_id,detected_at,stable_since,flap_count,status,verdict)
                VALUES (?,datetime('now'),?,?,?,?)
                ON CONFLICT(product_id) DO UPDATE SET
                    stable_since=excluded.stable_since,
                    flap_count=excluded.flap_count,
                    verdict=excluded.verdict,
                    status=CASE WHEN relist_queue.status IN ('RELISTED','DISMISSED')
                                THEN relist_queue.status ELSE excluded.status END
            """, (pid, stable_since, flips, status, verdict))

        conn.commit(); conn.close()
        return {"detected": detected, "ready": promoted,
                "flap_suppressed": suppressed}

    def ready(self, limit: int = 50) -> list[RelistCandidate]:
        """Candidates a human should look at, best evidence first."""
        conn = connect(self.db_path)
        rows = conn.execute("""
            SELECT q.*, p.name FROM relist_queue q
            JOIN products p USING(product_id)
            WHERE q.status='READY' ORDER BY q.stable_since LIMIT ?""",
            (limit,)).fetchall()
        conn.close()
        out = []
        for r in rows:
            reason = (f"back in stock since {r['stable_since']}, "
                      f"{r['flap_count']} flips observed")
            if r["verdict"] != "ALLOWED":
                reason += f" — still {r['verdict']}, needs clearance before listing"
            out.append(RelistCandidate(
                product_id=r["product_id"], name=r["name"], status=r["status"],
                stable_since=r["stable_since"], flap_count=r["flap_count"],
                verdict=r["verdict"], viable=bool(r["viable"]) if r["viable"] is not None else None,
                reason=reason))
        return out

    def decide(self, product_id: str, decision: str, actor: str = "ops",
               note: str = "") -> dict:
        if decision not in ("RELISTED", "DISMISSED"):
            raise ValueError("decision must be RELISTED or DISMISSED")
        conn = connect(self.db_path)
        conn.execute("""UPDATE relist_queue SET status=?, decided_by=?,
                        decided_at=datetime('now'), note=? WHERE product_id=?""",
                     (decision, actor, note, product_id))
        conn.commit(); conn.close()
        return {"product_id": product_id, "status": decision}

    # -- waitlist ------------------------------------------------------------

    def add_waitlist(self, product_id: str, email: str,
                     customer_id: Optional[str] = None) -> dict:
        conn = connect(self.db_path)
        conn.execute("INSERT INTO waitlist (product_id,customer_id,email) VALUES (?,?,?)",
                     (product_id, customer_id, email))
        conn.commit(); conn.close()
        return {"product_id": product_id, "email": email}

    def notify_waitlist(self, product_id: str) -> dict:
        """Who to tell, oldest first, capped.

        Returns the list rather than sending anything: delivery belongs to
        whatever mail provider is wired up, and capping happens here so a
        provider integration cannot accidentally mail everyone.
        """
        conn = connect(self.db_path)
        rows = conn.execute("""SELECT id,email FROM waitlist
            WHERE product_id=? AND notified_at IS NULL
            ORDER BY created_at LIMIT ?""",
            (product_id, self.cfg["waitlist_notify_cap"])).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            conn.executemany("UPDATE waitlist SET notified_at=datetime('now') WHERE id=?",
                             [(i,) for i in ids])
        waiting = conn.execute("SELECT COUNT(*) FROM waitlist WHERE product_id=? "
                               "AND notified_at IS NULL", (product_id,)).fetchone()[0]
        conn.commit(); conn.close()
        return {"product_id": product_id, "notify": [r["email"] for r in rows],
                "still_waiting": waiting,
                "capped_at": self.cfg["waitlist_notify_cap"]}
