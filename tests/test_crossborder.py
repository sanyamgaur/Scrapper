"""Offline tests for the cross-border engines. No network, no Blinkit calls.

These lock in the bugs found while building, so they cannot silently return:
  - "cream" blocking cosmetics and cream biscuits (aisle scoping)
  - "Cheese Balls" blocked because a trailing \b rejected the plural
  - 100 tissue pulls weighed as 25 kg (per-piece vs per-pack weights)
  - a cart quoted on a carrier that will not accept its handling flags
  - checkout accepting an order on a stale stock reading
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crossborder.packparse import parse_pack
from crossborder.compliance import ComplianceEngine
from crossborder.shipping import ShippingEngine
from crossborder.pricing import PricingEngine
from crossborder.stock import StockEngine, StockState
from crossborder import db as dbmod


def P(name, unit="1 pc", price=100, group=None, cat=None, sup=None, pid="T1"):
    return dict(product_id=pid, name=name, brand="", unit=unit, price=price,
                group_name=group, category_name=cat, super_category=sup)


class TestPackParse(unittest.TestCase):
    def test_mass_and_volume(self):
        self.assertEqual(parse_pack("500 g").net_g, 500)
        self.assertEqual(parse_pack("1 kg").net_g, 1000)
        self.assertEqual(parse_pack("1 ltr").net_ml, 1000)

    def test_multipliers(self):
        self.assertEqual(parse_pack("2 x 100 g").net_g, 200)
        self.assertEqual(parse_pack("200 g (Pack of 2)").net_g, 400)
        self.assertEqual(parse_pack("250 ml x 2").net_ml, 500)

    def test_pieces_and_pairs(self):
        self.assertEqual(parse_pack("6 pcs").pieces, 6)
        self.assertEqual(parse_pack("1 pair").pieces, 2)   # a pair is two objects
        self.assertEqual(parse_pack("100 pulls").pieces, 100)

    def test_unparseable_is_explicit_not_guessed(self):
        # A book with the publisher in the unit field must not yield a fake weight.
        p = parse_pack("Wonder House Books Editorial Team")
        self.assertEqual(p.confidence, "none")
        self.assertIsNone(p.billable_g)

    def test_never_raises(self):
        for junk in [None, "", "   ", "???", "x" * 400, "0 g", "1.5.2 kg"]:
            parse_pack(junk)


class TestCompliance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.e = ComplianceEngine()

    def v(self, *a, **k):
        return self.e.classify(P(*a, **k)).verdict

    def test_blocks_hard_prohibitions(self):
        self.assertEqual(self.v("Fresh Chicken Curry Cut", cat="Chicken, Meat & Fish"), "BLOCKED")
        self.assertEqual(self.v("Amul Ice Cream Vanilla", group="Ice Cream Tubs"), "BLOCKED")
        self.assertEqual(self.v("Nivea Deodorant Spray", sup="Beauty & Personal Care"), "BLOCKED")
        self.assertEqual(self.v("Good Knight Mosquito Repellent Refill"), "BLOCKED")
        self.assertEqual(self.v("Kinder Joy Surprise Egg"), "BLOCKED")
        self.assertEqual(self.v("Lakme Kajal Deep Black", sup="Beauty & Personal Care"), "BLOCKED")

    def test_aisle_scoping_stops_false_blocks(self):
        # REGRESSION: a bare \bcream\b once blocked every moisturiser and biscuit.
        self.assertEqual(self.v("Ponds Cold Cream", group="Face Cleaning",
                                sup="Beauty & Personal Care"), "REVIEW")
        self.assertEqual(self.v("Britannia Bourbon Cream Biscuits", group="Cookies",
                                sup="Grocery & Kitchen"), "ALLOWED")
        self.assertEqual(self.v("Pintola Peanut Butter", group="Spreads",
                                sup="Snacks & Drinks"), "ALLOWED")
        # ...while real chilled dairy still blocks.
        self.assertEqual(self.v("Amul Salted Butter", group="Butter & More",
                                sup="Grocery & Kitchen"), "BLOCKED")

    def test_plural_exception_regression(self):
        # REGRESSION: a trailing \b made the except_pattern miss "Cheese Balls".
        self.assertEqual(self.v("Kurkure Cheese Balls", group="Chips & Wafers",
                                sup="Snacks & Drinks"), "ALLOWED")

    def test_tablet_is_a_computer_outside_pharma(self):
        self.assertEqual(self.v("Samsung Galaxy Tablet", group="Mobile & Computer",
                                sup="Household Essentials"), "REVIEW")

    def test_unknown_defaults_to_review_never_allowed(self):
        v = self.e.classify(P("Completely Novel Object", group="No Such Shelf"))
        self.assertEqual(v.verdict, "REVIEW")
        self.assertTrue(v.needs_llm)

    def test_worst_verdict_wins_but_all_rules_kept(self):
        v = self.e.classify(P("Amul Ice Cream Vanilla", group="Ice Cream Tubs"))
        self.assertEqual(v.verdict, "BLOCKED")
        self.assertGreater(len(v.fired), 1)

    def test_every_verdict_is_explainable(self):
        for nm in ["Haldiram Bhujia", "Fresh Chicken", "Unknown Thing"]:
            v = self.e.classify(P(nm))
            self.assertTrue(v.primary.reason and v.primary.authority,
                            f"{nm} has no citation")

    def test_human_override_beats_rules(self):
        e = ComplianceEngine(overrides={"T1": {"verdict": "ALLOWED",
                                               "reviewer": "ops", "note": "cleared"}})
        self.assertEqual(e.classify(P("Fresh Chicken Curry Cut")).verdict, "ALLOWED")


class TestShipping(unittest.TestCase):
    def setUp(self):
        self.e = ShippingEngine()

    def test_consumable_piece_weight_regression(self):
        # REGRESSION: 100 pulls x a 300 g shelf default once produced 25 kg.
        g, _ = self.e.item_weight_g(P("Tissue", unit="100 pulls",
                                      group="Tissues & Disposables"))
        self.assertLess(g, 500)

    def test_shelf_default_is_a_pack_not_a_piece(self):
        # REGRESSION: "10 pcs" party cups were weighed as ten separate packs.
        g, _ = self.e.item_weight_g(P("Party Cups", unit="10 pcs", group="Home Needs"))
        self.assertLess(g, 1000)

    def test_parsed_mass_wins_over_defaults(self):
        g, est = self.e.item_weight_g(P("Dal", unit="1 kg", group="Dal"))
        self.assertEqual(g, 1000)
        self.assertFalse(est)

    def test_chargeable_is_max_of_actual_and_volumetric(self):
        w = self.e.cart_weight([(P("X", unit="500 g"), 2)])
        self.assertGreaterEqual(w.chargeable_g, w.actual_g)
        self.assertGreaterEqual(w.chargeable_g, 1000)   # goods alone

    def test_carrier_refusing_handling_is_excluded_not_ranked(self):
        q = self.e.quote_cart([(P("Choc", unit="100 g"), 1)],
                              handling=["INSULATED_PACK"])
        codes = [o.carrier_code for o in q.options]
        self.assertNotIn("INDIA_POST_EMS", codes)   # does not accept insulation
        self.assertTrue(any(e["carrier"] == "INDIA_POST_EMS" for e in q.excluded))

    def test_overweight_cart_has_no_options(self):
        q = self.e.quote_cart([(P("Heavy", unit="10 kg"), 8)])   # 80 kg
        self.assertEqual(q.options, [])

    def test_range_spans_cheapest_to_fastest(self):
        q = self.e.quote_cart([(P("X", unit="500 g"), 1)])
        lo, hi = q.range_usd
        self.assertLess(lo, hi)
        self.assertEqual(lo, q.cheapest.total_usd)


class TestPricing(unittest.TestCase):
    def setUp(self):
        self.e = PricingEngine()

    def test_cheap_heavy_single_sku_is_flagged_unviable(self):
        lc, _ = self.e.price_single(P("Peanuts", unit="500 g", price=54,
                                      cat="Dry Fruits & Cereals"), 1)
        self.assertFalse(lc.viable)
        self.assertGreater(lc.freight_ratio, 4)

    def test_list_price_always_covers_cost(self):
        for price, unit in [(54, "500 g"), (500, "1 kg"), (2000, "1 unit")]:
            lc, _ = self.e.price_single(P("X", unit=unit, price=price), 2)
            if lc.list_price_usd:
                self.assertGreaterEqual(lc.list_price_usd, lc.total_cost_usd,
                                        f"{price}/{unit} sells below cost")

    def test_fx_buffer_is_worse_than_spot(self):
        self.assertLess(self.e.usd_inr, 88.0)

    def test_baskets_beat_single_skus(self):
        one, _ = self.e.price_single(P("X", unit="200 g", price=100), 1)
        many, _ = self.e.price_cart([(P(f"X{i}", unit="200 g", price=100, pid=f"P{i}"), 2)
                                     for i in range(8)])
        self.assertLess(many.freight_ratio, one.freight_ratio)


class TestSchemaMigration(unittest.TestCase):
    """REGRESSION: `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table,
    so columns added to SCHEMA never reached databases already in the field --
    and a later CREATE INDEX on such a column made the database impossible to
    even OPEN. Columns are now reconciled against SCHEMA before it runs."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = self.tmp.name
        c = sqlite3.connect(self.path)
        # Exactly the shape shipped before customers/order_lines existed.
        c.executescript("""
            CREATE TABLE orders (order_id TEXT PRIMARY KEY, created_at TEXT,
                customer_zip TEXT, status TEXT, lines_json TEXT, quote_json TEXT,
                total_usd REAL, carrier TEXT, tracking TEXT);
            CREATE TABLE products (product_id TEXT PRIMARY KEY, name TEXT);
            INSERT INTO orders (order_id,status) VALUES ('OLD-1','PLACED');
            INSERT INTO products VALUES ('P9','Legacy');""")
        c.commit(); c.close()

    def tearDown(self):
        os.unlink(self.path)

    def test_old_database_still_opens(self):
        dbmod.connect(self.path).close()      # threw before the fix

    def test_missing_columns_are_added(self):
        conn = dbmod.connect(self.path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
        self.assertIn("customer_id", cols)
        self.assertIn("batch_id", cols)
        conn.close()

    def test_existing_rows_survive(self):
        conn = dbmod.connect(self.path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)
        conn.close()

    def test_migration_is_idempotent(self):
        dbmod.connect(self.path).close()
        conn = dbmod.connect(self.path)
        self.assertEqual(dbmod._migrate(conn), [])
        conn.close()

    def test_parser_never_yields_a_malformed_column(self):
        # A comment containing a comma once split mid-sentence and its tail was
        # read as a column name, producing invalid ALTER TABLE statements.
        for table, cols in dbmod._expected_columns(dbmod.SCHEMA).items():
            for name, _ in cols:
                self.assertTrue(name.replace("_", "").isalnum(),
                                f"{table}.{name} is not a valid column name")


class TestStockGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = self.tmp.name
        conn = dbmod.connect(self.path)
        conn.execute("INSERT INTO products (product_id,name,in_stock,price_inr,scraped_at) "
                     "VALUES ('A','Item A',1,100,0)")
        conn.execute("INSERT INTO products (product_id,name,in_stock,price_inr,scraped_at) "
                     "VALUES ('B','Item B',1,100,0)")
        conn.commit(); conn.close()
        self.e = StockEngine(db_path=self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_gate_blocks_out_of_stock(self):
        self.e.record("A", True, 100); self.e.record("B", False, 100)
        g = self.e.gate_order([("A", 1), ("B", 1)])
        self.assertFalse(g.accepted)
        self.assertEqual(g.blocked[0]["reason"], "out_of_stock")

    def test_gate_fails_closed_on_stale_data(self):
        # No fresh reading exists; the 1970 crawl timestamp is far too old.
        g = self.e.gate_order([("A", 1)])
        self.assertFalse(g.accepted)
        self.assertEqual(g.blocked[0]["reason"], "unverified")

    def test_gate_accepts_fresh_in_stock(self):
        self.e.record("A", True, 100)
        self.assertTrue(self.e.gate_order([("A", 2)]).accepted)

    def test_price_drift_is_surfaced(self):
        self.e.record("A", True, 130)
        g = self.e.gate_order([("A", 1)], quoted_prices={"A": 100})
        self.assertTrue(g.accepted)              # still sellable
        self.assertEqual(len(g.price_changes), 1)   # but flagged
        self.assertAlmostEqual(g.price_changes[0]["delta_pct"], 30.0)

    def test_unknown_product_never_sells(self):
        self.assertFalse(self.e.gate_order([("NOPE", 1)]).accepted)


if __name__ == "__main__":
    unittest.main(verbosity=2)
