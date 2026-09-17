"""Offline tests for the availability engine's scheduler and live-state folding.

No network: the walk is faked. What is tested here is the part that decides
where the request budget goes, which is the only part that determines how fresh
the picture is.
"""
import asyncio
import os
import sqlite3
import tempfile
import time

import availability_engine as ae
from availability_engine import Engine, Scheduler, human, pct
from check_availability import BACK_IN_STOCK, OUT_OF_STOCK, PRICE_UP
from test_availability import LOC, SESSION, make_db, page

SHELF_A = ("uuid-a", "1")
SHELF_B = ("uuid-b", "2")
SHELF_C = ("uuid-c", "3")


def args_for(extra=()):
    return ae.build_engine_parser().parse_args(
        ["--session", "x", "--db", "y"] + list(extra))


class FakeEngine(Engine):
    """Engine whose shelf walk returns canned products and costs canned pages."""

    def install(self, shelf_state, pages=1, ok=True):
        self.shelf_state = shelf_state      # shelf -> {pid: (price, in_stock)}
        self.pages = pages
        self.walk_ok = ok
        self.walks = []

    async def walk_shelf(self, client, shelf, wanted):
        # stand in for the latency of a real request, so the loop is paced
        await asyncio.sleep(0.005)
        self.walks.append(shelf)
        self.n_req += self.pages
        state = self.shelf_state.get(shelf, {})
        found = {}
        for pid in wanted:
            if pid in state:
                price, in_stock = state[pid]
                found[pid] = {"product_id": pid, "price": price, "mrp": None,
                              "in_stock": in_stock}
        return found, self.pages, self.walk_ok


def test_scheduler_prefers_value_per_request():
    """A cheap shelf with a hot SKU must beat an expensive shelf of cold ones."""
    sku_shelf = {"hot1": SHELF_A}
    sku_shelf.update({"cold%d" % i: SHELF_B for i in range(50)})
    weights = {"hot1": 50.0}
    weights.update({"cold%d" % i: 1.0 for i in range(50)})
    # SHELF_A is one page; SHELF_B is many
    sizes = {SHELF_A: 10, SHELF_B: 900}
    s = Scheduler(sku_shelf, weights, sizes, 90, 3600.0)
    assert s.cost[SHELF_A] == 1.0
    assert s.cost[SHELF_B] > 9

    now = time.time()
    for pid in sku_shelf:
        s.last_checked[pid] = now - 60      # equally stale
    assert s.pick(now)[0] == SHELF_A, "hot cheap shelf should win"

    # once the hot shelf is fresh, the cold bulk shelf is worth doing
    s.mark_walked(SHELF_A, 1, now)
    assert s.pick(now)[0] == SHELF_B
    print("  value per request           ok")


def test_learned_cost():
    s = Scheduler({"p": SHELF_A}, {"p": 1.0}, {SHELF_A: 900}, 90, 3600.0)
    assert s.cost[SHELF_A] > 9          # estimate assumes a full walk
    for _ in range(20):
        s.mark_walked(SHELF_A, 1, time.time())   # early stop: really 1 page
    assert s.cost[SHELF_A] < 1.5, s.cost[SHELF_A]
    print("  learned shelf cost          ok")


def test_volatility_decay():
    s = Scheduler({"p": SHELF_A}, {"p": 1.0}, {SHELF_A: 10}, 90, half_life=100.0)
    now = time.time()
    assert s.volatility("p", now) == 1.0
    s.mark_flip("p", now)
    # a flip just now doubles the urgency...
    assert s.volatility("p", now) == 2.0
    # ...and is worth half that one half-life later
    assert abs(s.volatility("p", now + 100) - 1.5) < 1e-6
    assert s.volatility("p", now + 10000) < 1.01
    print("  volatility decay            ok")


def test_cold_start():
    """Every SKU equally fresh -- at startup, or just after a sweep -- must
    still yield a pick, not an empty list that stalls the workers."""
    sku_shelf = {"a": SHELF_A, "b": SHELF_B}
    s = Scheduler(sku_shelf, {"a": 1.0, "b": 1.0}, {SHELF_A: 10, SHELF_B: 10},
                  90, 3600.0)
    now = time.time()
    for pid in sku_shelf:
        s.last_checked[pid] = now          # zero staleness everywhere
    assert s.pick(now, 1), "a zero-score board must still pick something"
    # and from a clock at zero, which is what a simulation starts from
    s2 = Scheduler(sku_shelf, {"a": 1.0, "b": 1.0}, {SHELF_A: 10, SHELF_B: 10},
                   90, 3600.0)
    assert s2.pick(0.0, 1)
    print("  cold start picks            ok")


def test_no_starvation():
    """Every SKU must eventually be checked, however cold."""
    sku_shelf = {"hot": SHELF_A, "cold": SHELF_B}
    s = Scheduler(sku_shelf, {"hot": 50.0, "cold": 1.0},
                  {SHELF_A: 10, SHELF_B: 10}, 90, 3600.0)
    now = time.time()
    picked = set()
    for i in range(40):
        t = now + i * 5
        shelf = s.pick(t)[0]
        picked.add(shelf)
        s.mark_walked(shelf, 1, t)
    assert picked == {SHELF_A, SHELF_B}, picked
    print("  no starvation               ok")


def _engine(con, extra=()):
    a = args_for(extra)
    e = FakeEngine(SESSION, con, a)
    return e, a


def test_live_state_and_events():
    d = tempfile.mkdtemp()
    con = make_db(os.path.join(d, "t.db"),
                  [("p1", "Thing", 10.0, 1, "cat")], [("p1", SHELF_A)])
    con.executescript(ae.LIVE_SCHEMA)
    e, a = _engine(con)
    watch = {"p1": {"name": "Thing", "shelves": [SHELF_A]}}
    e.load_live(watch)
    e.open_run(1)
    sched = Scheduler({"p1": SHELF_A}, {"p1": 1.0}, {SHELF_A: 10}, 90, 3600.0)

    now = time.time()
    # snapshot says in stock @10; first reading says out of stock -> an event
    evs = e.apply("p1", {"product_id": "p1", "price": 10.0, "mrp": None,
                         "in_stock": False}, now, sched)
    assert evs == [OUT_OF_STOCK], evs
    assert e.live["p1"]["in_stock"] == 0
    assert e.live["p1"]["checks"] == 1 and e.live["p1"]["flips"] == 1
    assert sched.flips["p1"] == 1

    # unchanged reading -> no event, but the check still counts
    evs = e.apply("p1", {"product_id": "p1", "price": 10.0, "mrp": None,
                         "in_stock": False}, now + 1, sched)
    assert evs == []
    assert e.live["p1"]["checks"] == 2 and e.live["p1"]["flips"] == 1

    # back in stock, dearer -> two events
    evs = e.apply("p1", {"product_id": "p1", "price": 12.0, "mrp": None,
                         "in_stock": True}, now + 2, sched)
    assert set(evs) == {BACK_IN_STOCK, PRICE_UP}, evs

    e.persist_live()
    row = con.execute("SELECT in_stock, price, checks, flips FROM "
                      "availability_live WHERE product_id='p1'").fetchone()
    assert row == (1, 12.0, 3, 2), row
    n = con.execute("SELECT COUNT(*) FROM availability_events "
                    "WHERE run_id=?", (e.run_id,)).fetchone()[0]
    assert n == 3, n
    con.close()
    print("  live state + events         ok")


def test_engine_gives_hot_skus_more_freshness():
    """The whole claim of the engine, measured: hot SKUs end up fresher."""
    d = tempfile.mkdtemp()
    prods = [("hot1", "Hot", 10.0, 1, "cat")]
    prods += [("c%d" % i, "Cold %d" % i, 10.0, 1, "cat") for i in range(30)]
    placements = [("hot1", SHELF_A)]
    placements += [("c%d" % i, SHELF_B if i < 15 else SHELF_C) for i in range(30)]
    con = make_db(os.path.join(d, "t.db"), prods, placements)
    con.executescript(ae.LIVE_SCHEMA)

    e, a = _engine(con, ["--all", "--hot-weight", "50"])
    e.install({SHELF_A: {"hot1": (10.0, True)},
               SHELF_B: {"c%d" % i: (10.0, True) for i in range(15)},
               SHELF_C: {"c%d" % i: (10.0, True) for i in range(15, 30)}})
    sku_shelf = {"hot1": SHELF_A}
    sku_shelf.update({"c%d" % i: (SHELF_B if i < 15 else SHELF_C)
                      for i in range(30)})
    weights = {p: (50.0 if p == "hot1" else 1.0) for p in sku_shelf}
    sched = Scheduler(sku_shelf, weights,
                      {SHELF_A: 10, SHELF_B: 200, SHELF_C: 200}, 90, 3600.0)
    watch = {p: {"name": p, "shelves": [sku_shelf[p]]} for p in sku_shelf}
    e.load_live(watch)
    e.open_run(len(sku_shelf))

    a.workers = 1
    a.run_for = 0.6
    a.status_every = 999
    asyncio.run(e.serve(sched, watch, {"hot1"}))

    hot_walks = sum(1 for w in e.walks if w == SHELF_A)
    b_walks = sum(1 for w in e.walks if w == SHELF_B)
    c_walks = sum(1 for w in e.walks if w == SHELF_C)
    assert len(e.walks) > 10, e.walks
    # the hot shelf is revisited more often than either cold shelf -- comparing
    # against the *sum* of the cold shelves would just count how many there are
    assert hot_walks > b_walks and hot_walks > c_walks, (hot_walks, b_walks, c_walks)

    # the claim that matters: a hot SKU ends up fresher than a cold one
    now = time.time()
    hot_stale = sched.staleness(now, ["hot1"])[0]
    cold_stale = pct(sched.staleness(now, [p for p in sku_shelf if p != "hot1"]), .5)
    assert hot_stale < cold_stale, (hot_stale, cold_stale)
    # and the cold ones are still swept, not starved
    assert b_walks > 0 and c_walks > 0
    con.close()
    print("  hot SKUs get more freshness ok  (%d hot walks vs %d cold)"
          % (hot_walks, b_walks + c_walks))


def test_search_worker_refreshes_stalest_hot():
    """The search leg is what actually makes a scattered hot set fresh, so it
    must pick the stalest hot SKU and fold the result into live state."""
    d = tempfile.mkdtemp()
    con = make_db(os.path.join(d, "t.db"),
                  [("h1", "Hot One", 10.0, 1, "cat"),
                   ("h2", "Hot Two", 20.0, 1, "cat")],
                  [("h1", SHELF_A), ("h2", SHELF_B)])
    con.executescript(ae.LIVE_SCHEMA)
    e, a = _engine(con, ["--all"])
    e.install({})
    watch = {"h1": {"name": "Hot One", "shelves": [SHELF_A]},
             "h2": {"name": "Hot Two", "shelves": [SHELF_B]}}
    e.load_live(watch)
    e.open_run(2)
    sched = Scheduler({"h1": SHELF_A, "h2": SHELF_B},
                      {"h1": 1.0, "h2": 1.0},
                      {SHELF_A: 10, SHELF_B: 10}, 90, 3600.0)
    now = time.time()
    sched.last_checked["h1"] = now          # fresh
    sched.last_checked["h2"] = now - 600    # stale -> must be picked first

    order = []

    async def fake_search(client, url, body, key):
        pid = key.split(":")[-1]
        order.append(pid)
        e.shutdown = len(order) >= 2
        return page([(pid, "x", 9.0, False)])

    e.post_search = fake_search
    asyncio.run(e.search_worker(None, sched, {"h1": "Hot One", "h2": "Hot Two"},
                                {"h1", "h2"}))
    assert order and order[0] == "h2", order
    assert e.live["h2"]["in_stock"] == 0, e.live["h2"]
    assert e.live["h2"]["price"] == 9.0
    # and searching marks it checked, so it stops being the stalest
    assert sched.last_checked["h2"] > now - 600
    con.close()
    print("  search worker (hot set)     ok")


def test_engine_blind_when_network_down():
    """Found by running the engine for real with the network blocked: it
    reported every hot SKU as 'disappeared' and claimed 1s freshness, having
    never reached Blinkit once. A blind engine must emit nothing and must not
    advance last_checked."""
    d = tempfile.mkdtemp()
    con = make_db(os.path.join(d, "t.db"),
                  [("p1", "One", 10.0, 1, "cat"), ("p2", "Two", 20.0, 1, "cat")],
                  [("p1", SHELF_A), ("p2", SHELF_A)])
    con.executescript(ae.LIVE_SCHEMA)
    e, a = _engine(con, ["--all"])

    e.install({}, ok=False)          # every walk learns nothing
    watch = {"p1": {"name": "One", "shelves": [SHELF_A]},
             "p2": {"name": "Two", "shelves": [SHELF_A]}}
    e.load_live(watch)
    e.open_run(2)
    sched = Scheduler({"p1": SHELF_A, "p2": SHELF_A},
                      {"p1": 1.0, "p2": 1.0}, {SHELF_A: 10}, 90, 3600.0)
    before = dict(sched.last_checked)

    a.workers = 1
    a.run_for = 0.4
    a.status_every = 999
    asyncio.run(e.serve(sched, watch, set()))

    assert e.n_events == 0, "a blind engine must not emit events"
    assert e.n_ok == 0 and e.n_fail > 0, (e.n_ok, e.n_fail)
    assert sched.last_checked == before, "freshness must not advance on failure"
    n = con.execute("SELECT COUNT(*) FROM availability_events").fetchone()[0]
    assert n == 0, n
    # and it backed off rather than hot-looping through the whole budget
    assert e.n_fail < 20, "should back off when nothing is answering: %d" % e.n_fail
    con.close()
    print("  blind engine emits nothing  ok  (%d failures, backed off)" % e.n_fail)


def test_human_and_pct():
    assert human(45) == "45s"
    assert human(600) == "10.0m"
    assert human(7200) == "2.0h"
    assert pct([], .5) == 0.0
    assert pct([1, 2, 3, 4], .5) == 3
    print("  formatting helpers          ok")


if __name__ == "__main__":
    print("availability engine, offline:")
    test_scheduler_prefers_value_per_request()
    test_learned_cost()
    test_volatility_decay()
    test_cold_start()
    test_no_starvation()
    test_live_state_and_events()
    test_engine_gives_hot_skus_more_freshness()
    test_search_worker_refreshes_stalest_hot()
    test_engine_blind_when_network_down()
    test_human_and_pct()
    print("\nALL ASSERTIONS PASSED")
