"""Run the compliance engine across the catalogue and persist the results.

Two outputs:
  1. A verdict per SKU, with every fired rule kept for audit.
  2. A CLUSTERED review queue. This is the operationally important one: the
     naive per-SKU queue is ~21k items, which nobody will ever work through.
     Clustering by (shelf x rule that fired) collapses it to ~530 decisions,
     of which the top 50 cover ~60% of the catalogue. One decision on
     "Lifestyle Accessories / DEFAULT-REVIEW" clears 1,279 SKUs at once.
"""

from __future__ import annotations

from pathlib import Path

from .compliance import ComplianceEngine
from .db import connect, DB_PATH


def load_overrides(conn) -> dict[str, dict]:
    return {r["product_id"]: dict(r) for r in conn.execute("SELECT * FROM overrides")}


def run(db_path: Path | str = DB_PATH, apply_cluster_decisions: bool = True) -> dict:
    conn = connect(db_path)
    engine = ComplianceEngine(overrides=load_overrides(conn))

    products = [dict(r) for r in conn.execute("SELECT * FROM products")]
    # The engine reads `price`/`product_id`; the DB column is price_inr.
    for p in products:
        p["price"] = p.get("price_inr")

    verdicts = [engine.classify(p) for p in products]

    conn.execute("DELETE FROM classifications")
    conn.execute("DELETE FROM fired_rules")
    conn.executemany("""
        INSERT INTO classifications (product_id,verdict,dimensions,handling,
            primary_rule,reason,authority,source,confidence)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, [(
        v.product_id, v.verdict, ",".join(v.dimensions), ",".join(v.handling),
        v.primary.rule_id if v.primary else None,
        v.primary.reason if v.primary else "",
        v.primary.authority if v.primary else "",
        "override" if any(f.layer == "override" for f in v.fired) else "rules",
        1.0,
    ) for v in verdicts])

    conn.executemany("""
        INSERT OR IGNORE INTO fired_rules (product_id,rule_id,layer,verdict,dimension,matched_on)
        VALUES (?,?,?,?,?,?)
    """, [(v.product_id, f.rule_id, f.layer, f.verdict, f.dimension, f.matched_on)
          for v in verdicts for f in v.fired])

    # --- build the clustered queue ----------------------------------------
    clusters: dict[str, dict] = {}
    for p, v in zip(products, verdicts):
        if v.verdict != "REVIEW":
            continue
        rid = v.primary.rule_id if v.primary else "DEFAULT-REVIEW"
        key = f"{p.get('group_name')}|{rid}"
        c = clusters.setdefault(key, {
            "cluster_id": key, "group_name": p.get("group_name"), "rule_id": rid,
            "dimension": v.primary.dimension if v.primary else None,
            "reason": v.primary.reason if v.primary else "",
            "authority": v.primary.authority if v.primary else "",
            "sku_count": 0, "samples": [],
        })
        c["sku_count"] += 1
        if len(c["samples"]) < 5:
            c["samples"].append(p["name"])

    # Preserve decisions already made against a cluster across re-runs.
    prior = {r["cluster_id"]: dict(r) for r in conn.execute("SELECT * FROM review_queue")}
    conn.execute("DELETE FROM review_queue")
    conn.executemany("""
        INSERT INTO review_queue (cluster_id,group_name,rule_id,dimension,reason,
            authority,sku_count,sample_names,status,decided_by,decided_at,note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, [(
        c["cluster_id"], c["group_name"], c["rule_id"], c["dimension"],
        c["reason"], c["authority"], c["sku_count"], " | ".join(c["samples"]),
        prior.get(c["cluster_id"], {}).get("status", "PENDING"),
        prior.get(c["cluster_id"], {}).get("decided_by"),
        prior.get(c["cluster_id"], {}).get("decided_at"),
        prior.get(c["cluster_id"], {}).get("note"),
    ) for c in clusters.values()])
    conn.commit()

    if apply_cluster_decisions:
        _apply_cluster_decisions(conn)

    stats = {r["verdict"]: r["n"] for r in conn.execute(
        "SELECT verdict, COUNT(*) n FROM classifications GROUP BY verdict")}
    stats["clusters"] = len(clusters)
    conn.close()
    return stats


def _apply_cluster_decisions(conn) -> None:
    """Propagate a cleared/killed cluster down onto its member SKUs.

    A cluster decision is a policy, not an override: it moves every SKU that
    currently sits in that cluster. Per-SKU overrides still win over it, which
    is why they are re-applied after.
    """
    for row in conn.execute(
            "SELECT * FROM review_queue WHERE status IN ('CLEARED','KILLED')"):
        new = "ALLOWED" if row["status"] == "CLEARED" else "BLOCKED"
        conn.execute("""
            UPDATE classifications SET verdict=?, source='cluster',
                reason=COALESCE(?, reason)
            WHERE verdict='REVIEW' AND primary_rule=? AND product_id IN (
                SELECT product_id FROM products WHERE group_name=?)
        """, (new, row["note"], row["rule_id"], row["group_name"]))
    for row in conn.execute("SELECT * FROM overrides"):
        conn.execute("UPDATE classifications SET verdict=?, source='override' "
                     "WHERE product_id=?", (row["verdict"], row["product_id"]))
    conn.commit()


if __name__ == "__main__":
    print(run())
