"""Command line for the cross-border pipeline.

    python -m crossborder.cli ingest [csv]      load a Blinkit crawl
    python -m crossborder.cli classify          run rules -> verdicts + queue
    python -m crossborder.cli llm [--limit N]   classify the no-rule tail
    python -m crossborder.cli queue [--top N]   show the clustered review queue
    python -m crossborder.cli decide <id> <CLEARED|KILLED>
    python -m crossborder.cli explain <pid>     why one SKU got its verdict
    python -m crossborder.cli quote <pid:qty>.. price a cart from the CLI
    python -m crossborder.cli serve [--port]    run the storefront + ops app
    python -m crossborder.cli stats             funnel summary
"""

from __future__ import annotations

import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crossborder", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest"); p.add_argument("csv", nargs="?", default="inventory_delhi.csv")
    sub.add_parser("classify")
    p = sub.add_parser("llm"); p.add_argument("--limit", type=int, default=None)
    p = sub.add_parser("queue"); p.add_argument("--top", type=int, default=25)
    p.add_argument("--status", default="PENDING")
    p = sub.add_parser("decide"); p.add_argument("cluster_id"); p.add_argument("decision")
    p.add_argument("--note", default="")
    p = sub.add_parser("explain"); p.add_argument("product_id")
    p = sub.add_parser("quote"); p.add_argument("lines", nargs="+", metavar="PID:QTY")
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    sub.add_parser("stats")

    a = ap.parse_args(argv)

    if a.cmd == "ingest":
        from .ingest import ingest_csv
        print(f"ingested {ingest_csv(a.csv):,} products")

    elif a.cmd == "classify":
        from .classify_run import run
        s = run()
        print(f"ALLOWED {s.get('ALLOWED',0):,}   REVIEW {s.get('REVIEW',0):,}   "
              f"BLOCKED {s.get('BLOCKED',0):,}   ({s['clusters']} review clusters)")

    elif a.cmd == "llm":
        from .llm import apply_to_db
        r = apply_to_db(limit=a.limit)
        if not r.get("classified"):
            print(f"tail={r['tail']:,}. No verdicts written "
                  f"(set ANTHROPIC_API_KEY and `pip install anthropic`).")
        else:
            print(f"tail={r['tail']:,}  classified={r['classified']:,}  "
                  f"resolved out of review={r['resolved']:,}")

    elif a.cmd == "queue":
        from .db import connect
        c = connect()
        rows = c.execute("SELECT * FROM review_queue WHERE status=? "
                         "ORDER BY sku_count DESC LIMIT ?", (a.status, a.top)).fetchall()
        tot = c.execute("SELECT COALESCE(SUM(sku_count),0) FROM review_queue "
                        "WHERE status=?", (a.status,)).fetchone()[0]
        shown = sum(r["sku_count"] for r in rows)
        print(f"{tot:,} SKUs in {a.status}; top {len(rows)} clusters cover "
              f"{shown:,} ({shown/tot*100:.0f}%)\n" if tot else "queue empty\n")
        for r in rows:
            print(f"  {r['sku_count']:6,}  {(r['group_name'] or '?')[:28]:30} "
                  f"{r['rule_id'][:24]:26} {r['dimension'] or '-'}")
            print(f"          {r['cluster_id']}")
        c.close()

    elif a.cmd == "decide":
        from .db import connect
        from .classify_run import _apply_cluster_decisions
        c = connect()
        cur = c.execute("UPDATE review_queue SET status=?, decided_by='cli', "
                        "decided_at=datetime('now'), note=? WHERE cluster_id=?",
                        (a.decision, a.note, a.cluster_id))
        if not cur.rowcount:
            print("no such cluster", file=sys.stderr); return 1
        c.commit(); _apply_cluster_decisions(c)
        n = c.execute("SELECT sku_count FROM review_queue WHERE cluster_id=?",
                      (a.cluster_id,)).fetchone()["sku_count"]
        print(f"{a.decision}: {n:,} SKUs"); c.close()

    elif a.cmd == "explain":
        from .db import connect
        c = connect()
        p = c.execute("SELECT * FROM products WHERE product_id=?", (a.product_id,)).fetchone()
        if not p:
            print("unknown product", file=sys.stderr); return 1
        cl = c.execute("SELECT * FROM classifications WHERE product_id=?", (a.product_id,)).fetchone()
        print(f"{p['name']}  ({p['unit']}, INR {p['price_inr']})")
        print(f"  shelf   : {p['super_category']} > {p['category_name']} > {p['group_name']}")
        if cl:
            print(f"  verdict : {cl['verdict']}  via {cl['primary_rule']} [{cl['source']}]")
            print(f"  reason  : {cl['reason']}")
            print(f"  basis   : {cl['authority']}")
        print("  all rules that fired:")
        for r in c.execute("SELECT * FROM fired_rules WHERE product_id=?", (a.product_id,)):
            print(f"    {r['rule_id']:26} {r['verdict']:8} {r['layer']:9} "
                  f"matched={r['matched_on']!r}")
        c.close()

    elif a.cmd == "quote":
        from .db import connect
        from .pricing import PricingEngine
        c = connect(); eng = PricingEngine()
        lines = []
        for spec in a.lines:
            pid, _, qty = spec.partition(":")
            row = c.execute("SELECT * FROM products WHERE product_id=?", (pid,)).fetchone()
            if not row:
                print(f"unknown product {pid}", file=sys.stderr); return 1
            d = dict(row); d["price"] = d["price_inr"]
            lines.append((d, int(qty or 1)))
        lc, q = eng.price_cart(lines)
        print(f"chargeable {q.weight.chargeable_g/1000:.2f} kg  "
              f"({q.weight.confidence})\n")
        for o in q.options:
            print(f"  {o.carrier_name[:40]:42} ${o.total_usd:8.2f}  "
                  f"{o.transit_days[0]}-{o.transit_days[1]}d  {o.tracking}")
        print(f"\n  goods ${lc.goods_usd}  freight ${lc.freight_usd}  "
              f"duty ${lc.duty_usd}  fees ${lc.mpf_usd}")
        print(f"  LIST ${lc.list_price_usd}   margin ${lc.margin_usd} "
              f"({lc.margin_pct}%)   viable={lc.viable}")
        for w in lc.warnings:
            print(f"  ! {w}")
        c.close()

    elif a.cmd == "serve":
        import uvicorn
        print(f"storefront  http://{a.host}:{a.port}/")
        print(f"ops         http://{a.host}:{a.port}/ops")
        uvicorn.run("crossborder.api:app", host=a.host, port=a.port)

    elif a.cmd == "stats":
        from .db import connect
        c = connect()
        tot = c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        print(f"catalogue: {tot:,} SKUs")
        for r in c.execute("SELECT verdict, COUNT(*) n FROM classifications "
                           "GROUP BY 1 ORDER BY n DESC"):
            print(f"  {r['verdict']:8} {r['n']:7,}  {r['n']/tot*100:5.1f}%")
        print("\nheld by dimension:")
        for r in c.execute("SELECT dimension, COUNT(*) n FROM fired_rules "
                           "WHERE dimension NOT IN ('','NULL') AND dimension IS NOT NULL "
                           "GROUP BY 1 ORDER BY n DESC"):
            print(f"  {r['dimension']:14} {r['n']:7,}")
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
