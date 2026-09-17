#!/usr/bin/env python3
"""
One command: crawl the whole catalogue -- no leaf limit, no product cap -- then
check every product's current availability, and print a plain in-stock /
out-of-stock summary.

    python run_pipeline.py --lat 28.6139 --lon 77.2090

First run (no session yet) also opens the browser step automatically:

    python run_pipeline.py --lat 28.6139 --lon 77.2090

Already have a session and just want to re-check stock, skip the ~45-minute
crawl and jump straight to the ~18-minute availability check:

    python run_pipeline.py --skip-crawl

This does not reimplement crawling or checking -- it is a thin sequencer over
three already-tested scripts, run as real subprocesses so their own progress
output streams through unchanged:

  1. discover.py            -- only if no session file exists, or --refresh-session
  2. crawl.py --with-search -- every leaf (no --limit-cats), the search sweep
                                too, since it runs on its own rate bucket for
                                free (see README)
  3. check_availability.py --all -- every product (no --limit)

Both crawling and checking are bound by Blinkit's measured rate limit
(~0.635 req/s), not by anything in this script: expect roughly 45 minutes for
step 2 and another 15-20 minutes for step 3 on a ~31k-SKU catalogue. That is a
floor, documented in README.md's "How fresh can it be?" section -- there is no
flag here that makes it faster.
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def run(cmd, label):
    print("\n" + "=" * 72, file=sys.stderr)
    print("[%s] %s" % (label, " ".join(cmd)), file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    t0 = time.time()
    # No capture: the underlying scripts already print their own live
    # progress to stderr, and buffering it here would just hide it until the
    # end of a 45-minute crawl.
    r = subprocess.run(cmd, cwd=HERE)
    el = time.time() - t0
    if r.returncode != 0:
        sys.exit("\n[%s] failed (exit %d) after %.0fs -- see the output above"
                 % (label, r.returncode, el))
    print("[%s] done in %.0fs" % (label, el), file=sys.stderr)


def summarize(db_path, location_hint=None):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    run_id = con.execute(
        "SELECT MAX(run_id) FROM availability_runs").fetchone()[0]
    if run_id is None:
        print("\nno availability run recorded -- did check_availability.py run?",
              file=sys.stderr)
        con.close()
        return

    row = con.execute(
        """SELECT COUNT(*) checked, SUM(in_stock=1) in_stock,
                  SUM(seen=1 AND in_stock=0) out_of_stock,
                  SUM(seen=0) not_returned
           FROM availability WHERE run_id=?""", (run_id,)).fetchone()

    print("\n" + "=" * 72)
    print("AVAILABILITY SUMMARY -- run %d" % run_id)
    print("=" * 72)
    print("  checked        %6d" % (row["checked"] or 0))
    print("  in stock       %6d" % (row["in_stock"] or 0))
    print("  out of stock   %6d" % (row["out_of_stock"] or 0))
    print("  no answer      %6d   (unknown, not out of stock -- see README)"
          % (row["not_returned"] or 0))

    events = list(con.execute(
        """SELECT event, COUNT(*) n FROM availability_events
           WHERE run_id=? GROUP BY event ORDER BY n DESC""", (run_id,)))
    if events:
        print("\n  changes since the previous check:")
        for e in events:
            print("    %-15s %d" % (e["event"], e["n"]))
    else:
        prev = con.execute(
            "SELECT COUNT(*) FROM availability_runs WHERE run_id<?",
            (run_id,)).fetchone()[0]
        print("\n  no changes detected" if prev
              else "\n  (first run for this store -- nothing to compare against yet)")
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", default="28.6139")
    ap.add_argument("--lon", default="77.2090")
    ap.add_argument("--session", default="session_delhi.json")
    ap.add_argument("--db", default="blinkit.db")
    ap.add_argument("--csv", default="inventory_delhi.csv",
                    help="crawl.py's CSV export path")
    ap.add_argument("--report", default="stock_availability.csv",
                    help="check_availability.py's per-product report")
    ap.add_argument("--refresh-session", action="store_true",
                    help="re-run discover.py even if a session file exists "
                         "(sessions expire after a few hours)")
    ap.add_argument("--skip-crawl", action="store_true",
                    help="reuse the existing db instead of crawling again")
    ap.add_argument("--skip-check", action="store_true",
                    help="crawl only, do not run the availability check")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="passed through to crawl.py / check_availability.py")
    args = ap.parse_args()

    py = sys.executable

    if args.refresh_session or not os.path.exists(args.session):
        run([py, "discover.py", "--lat", args.lat, "--lon", args.lon,
             "--out", args.session], "1/3 session")
    else:
        print("[1/3 session] reusing %s (pass --refresh-session if it has "
              "expired)" % args.session, file=sys.stderr)

    if not args.skip_crawl:
        # Deliberately no --limit-cats: every one of the ~307 leaves. --with-search
        # spends the search endpoint's separate rate budget, which would
        # otherwise sit idle for the whole crawl (see README).
        run([py, "crawl.py", "--session", args.session, "--db", args.db,
             "--csv", args.csv, "--with-search",
             "--concurrency", str(args.concurrency)], "2/3 crawl")
    else:
        print("[2/3 crawl] skipped (--skip-crawl)", file=sys.stderr)

    if not args.skip_check:
        # Deliberately no --limit: every product currently in the db.
        run([py, "check_availability.py", "--session", args.session,
             "--db", args.db, "--all", "--report", args.report,
             "--concurrency", str(args.concurrency)], "3/3 availability")
        print("\nper-product report -> %s" % args.report, file=sys.stderr)
        summarize(args.db)
    else:
        print("[3/3 availability] skipped (--skip-check)", file=sys.stderr)


if __name__ == "__main__":
    main()
