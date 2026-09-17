"""Offline test for rebuild_db.py's shelf-name join.

Found on a real run: session_delhi.json has exactly one shelf display name,
"Hair Colour", backed by two different collection_group_ids. A name-keyed dict
join silently drops one, so all 196 products under that name get walked
against a shelf that may not actually list them -- indistinguishable from a
genuinely empty catalogue. This locks in the fix: an ambiguous name gets no
shelf id at all, rather than an arbitrary, possibly wrong one.
"""
import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile


def write_session(path, categories):
    json.dump({"lat": "1", "lon": "1", "categories": categories}, open(path, "w"))


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "location", "name", "brand", "unit", "price",
                    "mrp", "discount_pct", "in_stock", "merchant_id", "image",
                    "source", "query", "location2", "scraped_at",
                    "super_category", "category_name", "group_name"])
        # header order does not matter to DictReader; write matching keys instead
    with open(path, "w", newline="") as f:
        cols = ["product_id", "name", "brand", "unit", "price", "mrp",
                "discount_pct", "in_stock", "merchant_id", "image",
                "category_name", "group_name", "super_category", "source",
                "query", "location", "scraped_at"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def base_row(pid, cat, grp, sup="Super"):
    return {"product_id": pid, "name": "P%s" % pid, "brand": "B", "unit": "1 pc",
            "price": "10", "mrp": "", "discount_pct": "", "in_stock": "1",
            "merchant_id": "1", "image": "http://x/%s.png" % pid,
            "category_name": cat, "group_name": grp, "super_category": sup,
            "source": "category", "query": "", "location": "1,1",
            "scraped_at": "1700000000"}


def run_rebuild(csv_path, session_path, db_path):
    r = subprocess.run(
        [sys.executable, "rebuild_db.py", "--csv", csv_path,
         "--session", session_path, "--db", db_path],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True, text=True, timeout=30)
    return r


def test_ambiguous_shelf_name_gets_no_id():
    d = tempfile.mkdtemp()
    session_path = os.path.join(d, "s.json")
    csv_path = os.path.join(d, "c.csv")
    db_path = os.path.join(d, "b.db")

    # "Hair Colour" appears twice with two different collection_group_ids --
    # exactly the shape found in session_delhi.json.
    write_session(session_path, [
        {"category_name": "Hair", "group_name": "Hair Colour",
         "collection_uuid": "u1", "collection_group_id": "12016"},
        {"category_name": "Hair", "group_name": "Hair Colour",
         "collection_uuid": "u1", "collection_group_id": "135472"},
        {"category_name": "Hair", "group_name": "Shampoo",
         "collection_uuid": "u2", "collection_group_id": "999"},
    ])
    write_csv(csv_path, [
        base_row("h1", "Hair", "Hair Colour"),
        base_row("h2", "Hair", "Hair Colour"),
        base_row("s1", "Hair", "Shampoo"),
    ])

    r = run_rebuild(csv_path, session_path, db_path)
    assert r.returncode == 0, r.stderr
    assert "ambiguously" in r.stderr.lower() or "ambiguous" in r.stdout.lower() \
        or "more than one collection id" in r.stderr, r.stderr

    con = sqlite3.connect(db_path)
    rows = {r[0]: r for r in con.execute(
        "SELECT product_id, collection_uuid, collection_group_id "
        "FROM product_categories")}
    # the ambiguous shelf's products get NO id, not an arbitrary one
    assert rows["h1"][1] == "" and rows["h1"][2] == "", rows["h1"]
    assert rows["h2"][1] == "" and rows["h2"][2] == "", rows["h2"]
    # an unambiguous shelf is unaffected
    assert rows["s1"][1] == "u2" and rows["s1"][2] == "999", rows["s1"]
    con.close()
    print("  ambiguous shelf name -> no id     ok")


def test_unambiguous_join_still_resolves():
    d = tempfile.mkdtemp()
    session_path = os.path.join(d, "s.json")
    csv_path = os.path.join(d, "c.csv")
    db_path = os.path.join(d, "b.db")
    write_session(session_path, [
        {"category_name": "Dairy", "group_name": "Milk",
         "collection_uuid": "u1", "collection_group_id": "1"},
    ])
    write_csv(csv_path, [base_row("m1", "Dairy", "Milk")])
    r = run_rebuild(csv_path, session_path, db_path)
    assert r.returncode == 0, r.stderr
    assert "%d products, %d shelf ids resolved" % (1, 1) in r.stdout or \
        "1 products, 1 shelf ids resolved" in r.stdout, r.stdout
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT collection_uuid, collection_group_id "
                      "FROM product_categories WHERE product_id='m1'").fetchone()
    assert row == ("u1", "1"), row
    con.close()
    print("  unambiguous join resolves         ok")


def test_repeated_identical_pair_is_not_ambiguous():
    """The same (uuid, gid) listed twice for one name is not a collision --
    only genuinely different ids are."""
    d = tempfile.mkdtemp()
    session_path = os.path.join(d, "s.json")
    csv_path = os.path.join(d, "c.csv")
    db_path = os.path.join(d, "b.db")
    write_session(session_path, [
        {"category_name": "Dairy", "group_name": "Milk",
         "collection_uuid": "u1", "collection_group_id": "1"},
        {"category_name": "Dairy", "group_name": "Milk",
         "collection_uuid": "u1", "collection_group_id": "1"},
    ])
    write_csv(csv_path, [base_row("m1", "Dairy", "Milk")])
    r = run_rebuild(csv_path, session_path, db_path)
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT collection_uuid, collection_group_id "
                      "FROM product_categories WHERE product_id='m1'").fetchone()
    assert row == ("u1", "1"), row
    con.close()
    print("  duplicate identical pair is fine  ok")


if __name__ == "__main__":
    print("rebuild_db.py, offline:")
    test_ambiguous_shelf_name_gets_no_id()
    test_unambiguous_join_still_resolves()
    test_repeated_identical_pair_is_not_ambiguous()
    print("\nALL ASSERTIONS PASSED")
