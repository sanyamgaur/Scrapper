"""Offline tests for the availability checker.

The network layer is faked, so everything here runs with no session and no
calls to Blinkit: the shelf-walk early stop, the plan split, change detection,
persistence and the report. What is NOT covered is whether Blinkit's live
responses still look the way the fakes do -- only a real run proves that.
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

from crawl import SCHEMA as CRAWL_SCHEMA, build_parser
import check_availability as ca
from check_availability import (BACK_IN_STOCK, DISAPPEARED, OUT_OF_STOCK,
                                PRICE_DOWN, PRICE_UP, REAPPEARED,
                                AvailabilityChecker, diff_events, read_watchfile)

LOC = "28.6139,77.2090"
SHELF_A = ("uuid-a", "1")
SHELF_B = ("uuid-b", "2")

SESSION = {
    "lat": "28.6139", "lon": "77.2090", "cookies": {},
    "templates": {"listing": {"headers": {"auth_key": "x"}},
                  "search": {"headers": {"auth_key": "x"}}},
}


def snippet(pid, name, price, in_stock):
    d = {"identity": {"id": pid}, "name": {"text": name},
         "normal_price": {"text": "₹%s" % price}}
    d["inventory"] = 5 if in_stock else 0
    d["out_of_stock"] = not in_stock
    return {"widget_type": "product_card_snippet_type_2", "data": d}


def page(products):
    return {"response": {"snippets": [snippet(*p) for p in products]},
            "postback_params": {"c": "x"}}


def make_db(path, products, placements):
    con = sqlite3.connect(path)
    con.executescript(CRAWL_SCHEMA)
    con.executescript(ca.SCHEMA)
    for pid, name, price, in_stock, cat in products:
        con.execute(
            "INSERT OR REPLACE INTO products VALUES (%s)" % ",".join("?" * 20),
            (pid, LOC, name, "BrandX", "1 pc", price, None, None,
             in_stock, "36778", "http://img/%s.png" % pid, None, None,
             cat, cat, "Dept", "category", None, None, 1700000000))
    for pid, shelf in placements:
        con.execute("INSERT OR REPLACE INTO product_categories VALUES (?,?,?,?,?,?,?,?)",
                    (pid, LOC, shelf[0], shelf[1], "cat", "shelf", "Dept", "category"))
    con.commit()
    return con


def make_args(extra=()):
    return ca.build_av_parser().parse_args(
        ["--session", "x", "--db", "y"] + list(extra))


class FakeChecker(AvailabilityChecker):
    """AvailabilityChecker with the two network calls replaced by canned pages."""

    def install(self, shelf_pages, search_hits):
        self.shelf_pages = shelf_pages      # (uuid,gid) -> [page, page, ...]
        self.search_hits = search_hits      # name -> payload or None
        self.calls = []
        self._page_for = {}

    async def post(self, client, url, body, key, bucket=None, headers=None):
        shelf = (body.get("collection_uuid"), str(body.get("collection_group_id")))
        n = self._page_for.get(shelf, 0)
        self._page_for[shelf] = n + 1
        self.calls.append(("listing", shelf, n))
        self.n_req += 1
        pages = self.shelf_pages.get(shelf, [])
        return pages[n] if n < len(pages) else None

    async def post_search(self, client, url, body, key):
        self.calls.append(("search", url, None))
        self.n_req += 1
        for name, payload in self.search_hits.items():
            if ca.quote_plus(name) in url:
                return payload
        return None

    def progress(self, *a, **k):
        pass


def run(checker, shelf_jobs, search_jobs, unknown=()):
    asyncio.run(checker.run_check(shelf_jobs, search_jobs, set(unknown)))


# ---------------------------------------------------------------- diff_events
def test_diff_events():
    assert diff_events(None, {"seen": 1, "in_stock": 1, "price": 10}) == []
    assert diff_events({"seen": 1, "in_stock": 1, "price": 10},
                       {"seen": 1, "in_stock": 0, "price": 10}) == [OUT_OF_STOCK]
    assert diff_events({"seen": 1, "in_stock": 0, "price": 10},
                       {"seen": 1, "in_stock": 1, "price": 10}) == [BACK_IN_STOCK]
    assert diff_events({"seen": 1, "in_stock": 1, "price": 10},
                       {"seen": 0, "in_stock": None, "price": None}) == [DISAPPEARED]
    assert REAPPEARED in diff_events({"seen": 0, "in_stock": None, "price": None},
                                     {"seen": 1, "in_stock": 1, "price": 10})
    assert diff_events({"seen": 1, "in_stock": 1, "price": 10},
                       {"seen": 1, "in_stock": 1, "price": 12}) == [PRICE_UP]
    assert diff_events({"seen": 1, "in_stock": 1, "price": 12},
                       {"seen": 1, "in_stock": 1, "price": 10}) == [PRICE_DOWN]
    # unchanged, and unknown stock on either side, are not events
    assert diff_events({"seen": 1, "in_stock": 1, "price": 10},
                       {"seen": 1, "in_stock": 1, "price": 10}) == []
    assert diff_events({"seen": 1, "in_stock": None, "price": 10},
                       {"seen": 1, "in_stock": 1, "price": 10}) == []
    # a price that rounds to the same paisa is not a price move
    assert diff_events({"seen": 1, "in_stock": 1, "price": 10.0},
                       {"seen": 1, "in_stock": 1, "price": 10.001}) == []
    print("  diff_events                 ok")


# ------------------------------------------------------------- read_watchfile
def test_watchfile():
    d = tempfile.mkdtemp()
    plain = os.path.join(d, "ids.txt")
    open(plain, "w").write("# a comment\n136945\n41995\n\n")
    assert read_watchfile(plain) == {"136945", "41995"}

    as_csv = os.path.join(d, "ids.csv")
    open(as_csv, "w").write("product_id,name\n136945,Milk\n99,Other\n")
    assert read_watchfile(as_csv) == {"136945", "99"}

    # a one-column export still has a header, and it is not a product id
    one_col = os.path.join(d, "one.csv")
    open(one_col, "w").write("product_id\n136945\n41995\n")
    assert read_watchfile(one_col) == {"136945", "41995"}

    # ids that merely start with digits-and-letters are untouched
    mixed = os.path.join(d, "mixed.csv")
    open(mixed, "w").write("sku,name\nabc123,Thing\n")
    assert read_watchfile(mixed) == {"abc123"}
    print("  read_watchfile              ok")


# ------------------------------------------------------- shelf walk early stop
def test_early_stop():
    d = tempfile.mkdtemp()
    dbp = os.path.join(d, "t.db")
    prods = [("p%d" % i, "Product %d" % i, 10.0, 1, "cat") for i in range(400)]
    con = make_db(dbp, prods, [("p%d" % i, SHELF_A) for i in range(400)])

    # five pages; the watched product sits on page 1 (the second request)
    pages = [page([(p[0], p[1], p[2], True) for p in prods[i * 90:(i + 1) * 90]])
             for i in range(5)]
    args = make_args(["--all"])
    c = FakeChecker(SESSION, con, args)
    c.install({SHELF_A: pages}, {})
    run(c, {SHELF_A: {"p100"}}, [])

    listing_calls = [x for x in c.calls if x[0] == "listing"]
    assert len(listing_calls) == 2, listing_calls
    assert c.state["p100"]["seen"] == 1 and c.state["p100"]["in_stock"] == 1

    # a product on the LAST page costs the whole walk -- no early exit possible
    c2 = FakeChecker(SESSION, con, args)
    c2.install({SHELF_A: pages}, {})
    run(c2, {SHELF_A: {"p395"}}, [])
    assert len([x for x in c2.calls if x[0] == "listing"]) == 5
    assert c2.state["p395"]["seen"] == 1

    # many watched products on one shelf: still one walk, all resolved
    c3 = FakeChecker(SESSION, con, args)
    c3.install({SHELF_A: pages}, {})
    want = {"p5", "p100", "p200"}
    run(c3, {SHELF_A: want}, [])
    assert len([x for x in c3.calls if x[0] == "listing"]) == 3
    assert all(c3.state[p]["seen"] == 1 for p in want)
    con.close()
    print("  shelf walk early stop       ok")


# ------------------------------------------------------------- not-found path
def test_missing_product():
    d = tempfile.mkdtemp()
    con = make_db(os.path.join(d, "t.db"),
                  [("gone", "Gone Product", 10.0, 1, "cat")], [("gone", SHELF_A)])
    args = make_args(["--all"])
    c = FakeChecker(SESSION, con, args)
    # the shelf no longer lists it
    c.install({SHELF_A: [page([("other", "Other", 5.0, True)])]}, {})
    run(c, {SHELF_A: {"gone"}}, [])
    assert c.state["gone"]["seen"] == 0
    assert c.state["gone"]["in_stock"] is None

    _, prev = ca.baseline_state(con, LOC)
    assert diff_events(prev["gone"], c.state["gone"]) == [DISAPPEARED]
    con.close()
    print("  missing product             ok")


# ------------------------------------------------------------------ search leg
def test_search_leg():
    d = tempfile.mkdtemp()
    con = make_db(os.path.join(d, "t.db"),
                  [("s1", "Amul Milk", 28.0, 1, "cat")], [])
    args = make_args(["--all"])
    c = FakeChecker(SESSION, con, args)
    c.install({}, {"Amul Milk": page([("s1", "Amul Milk", 31.0, False)])})
    run(c, {}, [("s1", "Amul Milk")])
    assert c.state["s1"]["seen"] == 1
    assert c.state["s1"]["in_stock"] == 0
    assert c.state["s1"]["price"] == 31.0
    assert c.state["s1"]["via"] == "search"
    con.close()
    print("  search leg                  ok")


# ----------------------------------------------------------------- plan split
def test_plan():
    d = tempfile.mkdtemp()
    prods = ([("a%d" % i, "A%d" % i, 1.0, 1, "cat") for i in range(5)]
             + [("b1", "B1", 1.0, 1, "cat")])
    placements = [("a%d" % i, SHELF_A) for i in range(5)] + [("b1", SHELF_B)]
    con = make_db(os.path.join(d, "t.db"), prods, placements)

    c = FakeChecker(SESSION, con, make_args(["--all", "--shelf-threshold", "3"]))
    watch = c.load_watchlist()
    assert len(watch) == 6
    shelf_jobs, search_ids, unknown = c.plan(watch)
    # SHELF_A has 5 watched -> walk it; SHELF_B has 1 -> search instead
    assert set(shelf_jobs) == {SHELF_A}, shelf_jobs
    assert search_ids == {"b1"}, search_ids
    assert not unknown

    # --strategy shelf forces everything onto listing
    c2 = FakeChecker(SESSION, con, make_args(["--all", "--strategy", "shelf"]))
    sj, si, _ = c2.plan(c2.load_watchlist())
    assert set(sj) == {SHELF_A, SHELF_B} and not si

    # with no search template, auto must degrade to shelf walks, not crash
    nosearch = {**SESSION, "templates": {"listing": SESSION["templates"]["listing"]}}
    c3 = FakeChecker(nosearch, con, make_args(["--all"]))
    assert c3.can_search is False
    sj3, si3, _ = c3.plan(c3.load_watchlist())
    assert set(sj3) == {SHELF_A, SHELF_B} and not si3
    con.close()
    print("  plan split                  ok")


# ------------------------------------------------------- filters + persistence
def test_persist_and_report():
    d = tempfile.mkdtemp()
    dbp = os.path.join(d, "t.db")
    prods = [("x1", "In then out", 10.0, 1, "cat"),
             ("x2", "Out then in", 20.0, 0, "cat"),
             ("x3", "Price mover", 30.0, 1, "cat")]
    con = make_db(dbp, prods, [(p[0], SHELF_A) for p in prods])

    args = make_args(["--all"])
    c = FakeChecker(SESSION, con, args)
    c.install({SHELF_A: [page([("x1", "In then out", 10.0, False),
                               ("x2", "Out then in", 20.0, True),
                               ("x3", "Price mover", 33.0, True)])]}, {})
    watch = c.load_watchlist()
    prev_run, prev_state = ca.baseline_state(con, LOC)
    assert prev_run is None          # first run compares to the snapshot
    run(c, {SHELF_A: set(watch)}, [])
    run_id, events = c.persist(watch, prev_state, 1700000000)

    kinds = {(e[1], e[3]) for e in events}
    assert ("x1", OUT_OF_STOCK) in kinds
    assert ("x2", BACK_IN_STOCK) in kinds
    assert ("x3", PRICE_UP) in kinds
    assert len(events) == 3, events

    stored = dict(con.execute(
        "SELECT product_id, in_stock FROM availability WHERE run_id=?", (run_id,)))
    assert stored == {"x1": 0, "x2": 1, "x3": 1}

    # a second run now compares against run 1, not the snapshot
    prev_run2, prev2 = ca.baseline_state(con, LOC)
    assert prev_run2 == run_id
    assert prev2["x1"]["in_stock"] == 0

    c2 = FakeChecker(SESSION, con, args)
    c2.install({SHELF_A: [page([("x1", "In then out", 10.0, True),
                                ("x2", "Out then in", 20.0, True),
                                ("x3", "Price mover", 33.0, True)])]}, {})
    run(c2, {SHELF_A: set(watch)}, [])
    _, events2 = c2.persist(watch, prev2, 1700000100)
    assert {(e[1], e[3]) for e in events2} == {("x1", BACK_IN_STOCK)}, events2

    rpt = os.path.join(d, "r.csv")
    n = ca.write_report(con, run_id, rpt)
    assert n == 3
    body = open(rpt).read()
    assert "product_id,name,brand,shelf" in body and "x1" in body
    assert OUT_OF_STOCK in body

    rptj = os.path.join(d, "r.json")
    ca.write_report(con, run_id, rptj, as_json=True)
    import json as _json
    doc = _json.load(open(rptj))
    assert len(doc["products"]) == 3 and doc["run_id"] == run_id
    con.close()
    print("  persist + events + report   ok")


def test_was_out_filter():
    d = tempfile.mkdtemp()
    con = make_db(os.path.join(d, "t.db"),
                  [("in1", "Has stock", 10.0, 1, "cat"),
                   ("out1", "No stock", 10.0, 0, "cat")],
                  [("in1", SHELF_A), ("out1", SHELF_A)])
    c = FakeChecker(SESSION, con, make_args(["--was-out"]))
    assert set(c.load_watchlist()) == {"out1"}

    c2 = FakeChecker(SESSION, con, make_args(["--brand", "brandx"]))
    assert len(c2.load_watchlist()) == 2
    c3 = FakeChecker(SESSION, con, make_args(["--brand", "nosuch"]))
    assert c3.load_watchlist() == {}
    con.close()
    print("  filters                     ok")


if __name__ == "__main__":
    print("availability checker, offline:")
    test_diff_events()
    test_watchfile()
    test_early_stop()
    test_missing_product()
    test_search_leg()
    test_plan()
    test_persist_and_report()
    test_was_out_filter()
    print("\nALL ASSERTIONS PASSED")
