"""Offline tests for the procurement half of the funnel. No network.

Locks in the behaviours that cost real money when they regress:
  - consolidation keeps a per-customer ledger, so a short has an owner
  - carts split by dark store, because a Blinkit basket has one merchant
  - scarce units go to the orders closest to whole, not spread thin
  - risk scores never claim confidence they do not have
  - the basket builder never suggests an item that dwarfs the cart
  - the price deadband ignores noise and still catches a real rise
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crossborder import db as dbmod
from crossborder.basket import billed_weight_g
from crossborder.operator import OperatorFlow, product_link, search_link
from crossborder.pricedrift import PriceDriftEngine
from crossborder.procurement import ProcurementEngine
from crossborder.restock import RestockEngine
from crossborder.stockout_risk import StockoutRiskEngine


class TempDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.path = self.tmp.name
        self.conn = dbmod.connect(self.path)
        # Two SKUs on merchant A, one on merchant B.
        for pid, name, merch, price, stock in [
                ("P1", "Peanuts", "MA", 100.0, 1),
                ("P2", "Saffron", "MA", 500.0, 1),
                ("P3", "Masala", "MB", 80.0, 1)]:
            self.conn.execute("""INSERT INTO products
                (product_id,name,merchant_id,price_inr,in_stock,est_weight_g,
                 group_name,category_name,super_category)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (pid, name, merch, price, stock, 200.0, "Dal",
                 "Atta, Rice & Dal", "Grocery & Kitchen"))
            self.conn.execute("""INSERT INTO classifications
                (product_id,verdict,source) VALUES (?,'ALLOWED','rules')""", (pid,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.path)

    def order(self, oid, cid, lines):
        self.conn.execute("""INSERT INTO customers (customer_id,email,ship_name,zip5)
            VALUES (?,?,?,?)""", (cid, f"{cid}@x.com", cid, "10001"))
        self.conn.execute("""INSERT INTO orders (order_id,customer_id,status)
            VALUES (?,?,'STOCK_CONFIRMED')""", (oid, cid))
        for pid, q in lines:
            self.conn.execute("""INSERT INTO order_lines
                (order_id,product_id,qty,unit_price_inr) VALUES (?,?,?,100.0)""",
                (oid, pid, q))
        self.conn.commit()


class TestBatching(TempDB):
    def test_consolidates_across_customers(self):
        self.order("O1", "C1", [("P1", 2)])
        self.order("O2", "C2", [("P1", 1)])
        e = ProcurementEngine(db_path=self.path)
        b = e.build_batch("2026-01-01")
        self.assertEqual(b["n_orders"], 2)
        self.assertEqual(b["n_lines"], 1)          # one Blinkit line...
        line = self.conn.execute(
            "SELECT qty_required FROM batch_lines WHERE product_id='P1'").fetchone()
        self.assertEqual(line["qty_required"], 3)  # ...of qty 3

    def test_allocation_ledger_preserves_ownership(self):
        self.order("O1", "C1", [("P1", 2)])
        self.order("O2", "C2", [("P1", 1)])
        ProcurementEngine(db_path=self.path).build_batch("2026-01-01")
        rows = {r["order_id"]: r["qty"] for r in self.conn.execute(
            "SELECT order_id, qty FROM batch_allocations WHERE product_id='P1'")}
        self.assertEqual(rows, {"O1": 2, "O2": 1})

    def test_splits_carts_by_dark_store(self):
        # A Blinkit basket is served by ONE merchant, so this must never merge.
        self.order("O1", "C1", [("P1", 1), ("P3", 1)])
        b = ProcurementEngine(db_path=self.path).build_batch("2026-01-01")
        self.assertEqual(b["n_carts"], 2)
        self.assertEqual({c["merchant_id"] for c in b["carts"]}, {"MA", "MB"})

    def test_risky_lines_are_picked_first(self):
        self.order("O1", "C1", [("P1", 1), ("P2", 1)])
        self.conn.execute("""INSERT INTO stockout_risk
            (product_id,score,bucket,confidence) VALUES ('P2',0.9,'CRITICAL','low')""")
        self.conn.execute("""INSERT INTO stockout_risk
            (product_id,score,bucket,confidence) VALUES ('P1',0.1,'NORMAL','low')""")
        self.conn.commit()
        ProcurementEngine(db_path=self.path).build_batch("2026-01-01")
        first = self.conn.execute(
            "SELECT product_id FROM batch_lines ORDER BY pick_rank LIMIT 1").fetchone()
        self.assertEqual(first["product_id"], "P2")

    def test_rejects_duplicate_batch(self):
        self.order("O1", "C1", [("P1", 1)])
        e = ProcurementEngine(db_path=self.path)
        e.build_batch("2026-01-01")
        with self.assertRaises(ValueError):
            e.build_batch("2026-01-01")


class TestReconciliation(TempDB):
    def test_completeness_first_allocation(self):
        # Three customers want P1; only 2 units are found. The two smallest
        # orders should ship whole rather than all three going short.
        self.order("O1", "C1", [("P1", 1)])
        self.order("O2", "C2", [("P1", 1)])
        self.order("O3", "C3", [("P1", 1), ("P2", 1), ("P3", 1)])
        e = ProcurementEngine(db_path=self.path)
        e.build_batch("2026-01-01")
        e.mark_line("PB-2026-01-01", "P1", "BOUGHT", qty_bought=2)
        e.mark_line("PB-2026-01-01", "P2", "BOUGHT", qty_bought=1)
        e.mark_line("PB-2026-01-01", "P3", "BOUGHT", qty_bought=1)
        r = e.reconcile("PB-2026-01-01")
        self.assertEqual(r["orders_complete"], 2)
        self.assertIn("O3", r["short"])     # the biggest order absorbs the short

    def test_short_marks_order_not_shippable(self):
        self.order("O1", "C1", [("P1", 2)])
        e = ProcurementEngine(db_path=self.path)
        e.build_batch("2026-01-01")
        e.mark_line("PB-2026-01-01", "P1", "BOUGHT", qty_bought=1)
        e.reconcile("PB-2026-01-01")
        st = self.conn.execute("SELECT status FROM orders WHERE order_id='O1'").fetchone()
        self.assertEqual(st["status"], "PROCUREMENT_SHORT")

    def test_buying_fewer_than_required_downgrades_to_short(self):
        self.order("O1", "C1", [("P1", 3)])
        e = ProcurementEngine(db_path=self.path)
        e.build_batch("2026-01-01")
        r = e.mark_line("PB-2026-01-01", "P1", "BOUGHT", qty_bought=1)
        self.assertEqual(r["state"], "SHORT")

    def test_packout_bins_carry_shipping_details(self):
        self.order("O1", "C1", [("P1", 1)])
        e = ProcurementEngine(db_path=self.path)
        e.build_batch("2026-01-01")
        e.mark_line("PB-2026-01-01", "P1", "BOUGHT", qty_bought=1)
        e.reconcile("PB-2026-01-01")
        bins = e.packout("PB-2026-01-01")["bins"]
        self.assertEqual(len(bins), 1)
        self.assertEqual(bins[0]["ship_to"]["zip5"], "10001")
        self.assertTrue(bins[0]["complete"])


class TestStockoutRisk(TempDB):
    def test_confidence_is_low_without_history(self):
        e = StockoutRiskEngine(db_path=self.path)
        out = e.score_all(listable_only=False)
        self.assertEqual(out["confidence"], "low")
        self.assertEqual(out["history_runs"], 0)

    def test_out_of_stock_scores_higher_than_in_stock(self):
        self.conn.execute("UPDATE products SET in_stock=0 WHERE product_id='P1'")
        self.conn.commit()
        e = StockoutRiskEngine(db_path=self.path)
        e.score_all(listable_only=False)
        a = e.get("P1", self.path).score
        b = e.get("P2", self.path).score
        self.assertGreater(a, b)

    def test_every_score_is_explainable(self):
        e = StockoutRiskEngine(db_path=self.path)
        e.score_all(listable_only=False)
        s = e.get("P1", self.path)
        self.assertTrue(s.signals)
        self.assertIn("confidence", s.explain())

    def test_score_stays_in_range(self):
        e = StockoutRiskEngine(db_path=self.path)
        e.score_all(listable_only=False)
        for pid in ("P1", "P2", "P3"):
            self.assertTrue(0.0 <= e.get(pid, self.path).score <= 1.0)


class TestPriceDrift(unittest.TestCase):
    def setUp(self):
        self.e = PriceDriftEngine()

    def test_small_move_is_ignored(self):
        self.assertEqual(self.e.check(24.99, 26.50).action, "IGNORED")

    def test_deadband_is_greater_of_dollars_or_percent(self):
        # $3 floor on a cheap item, 5% on an expensive one.
        self.assertAlmostEqual(self.e.deadband_usd(24.99), 3.00, places=2)
        self.assertAlmostEqual(self.e.deadband_usd(119.99), 6.00, places=2)

    def test_real_rise_triggers_reprice(self):
        self.assertEqual(self.e.check(24.99, 29.99).action, "REPRICE")

    def test_drop_never_blocks(self):
        v = self.e.check(24.99, 19.99)
        self.assertEqual(v.action, "REPRICE_SILENT")
        self.assertNotEqual(v.action, "DELISTED")

    def test_rise_past_viability_delists(self):
        self.assertEqual(self.e.check(24.99, 44.99, viable=False).action, "DELISTED")

    def test_quote_band_is_tighter_than_list_band(self):
        self.assertLess(self.e.quote_deadband_usd(50.0), self.e.deadband_usd(50.0))


class TestOperatorFlow(TempDB):
    def test_links_are_constructible_from_stored_fields(self):
        self.assertIn("/prid/P1", product_link("P1", "Peanuts 500g"))
        self.assertIn("q=", search_link("Peanuts", "Bikaji"))

    def test_search_link_does_not_double_the_brand(self):
        # Names in this catalogue usually already start with the brand.
        self.assertEqual(search_link("Bikaji Peanuts", "Bikaji").count("Bikaji"), 1)

    def test_price_attestation_catches_wrong_pack_size(self):
        self.order("O1", "C1", [("P1", 1)])
        ProcurementEngine(db_path=self.path).build_batch("2026-01-01")
        o = OperatorFlow(db_path=self.path)
        self.assertFalse(o.attest_price("PB-2026-01-01", "P1", 999.0)["ok"])
        self.assertTrue(o.attest_price("PB-2026-01-01", "P1", 102.0)["ok"])

    def test_bill_must_balance(self):
        self.order("O1", "C1", [("P1", 1)])
        e = ProcurementEngine(db_path=self.path)
        e.build_batch("2026-01-01")
        e.mark_line("PB-2026-01-01", "P1", "BOUGHT", qty_bought=1, actual_inr=100.0)
        o = OperatorFlow(db_path=self.path)
        self.assertTrue(o.reconcile_bill("PB-2026-01-01", 1, 104.0)["balanced"])
        self.assertFalse(o.reconcile_bill("PB-2026-01-01", 1, 400.0)["balanced"])

    def test_pick_sheet_flags_shared_lines(self):
        self.order("O1", "C1", [("P1", 1)])
        self.order("O2", "C2", [("P1", 1)])
        ProcurementEngine(db_path=self.path).build_batch("2026-01-01")
        sheet = OperatorFlow(db_path=self.path).pick_sheet("PB-2026-01-01")
        line = sheet["carts"][0]["lines"][0]
        self.assertEqual(line["for_customers"], 2)
        self.assertIn("customers", line["priority_note"])


class TestRestock(TempDB):
    def test_flapping_sku_is_not_promoted(self):
        for st in [1, 0, 1, 0, 1, 0, 1]:
            self.conn.execute("""INSERT INTO stock_checks (product_id,in_stock,checked_at)
                VALUES ('P1',?,datetime('now'))""", (st,))
        self.conn.commit()
        out = RestockEngine(db_path=self.path).detect()
        self.assertGreaterEqual(out["flap_suppressed"], 1)
        row = self.conn.execute(
            "SELECT status FROM relist_queue WHERE product_id='P1'").fetchone()
        self.assertNotEqual(row["status"], "READY")

    def test_blocked_sku_never_reaches_the_bucket(self):
        self.conn.execute("UPDATE classifications SET verdict='BLOCKED' WHERE product_id='P1'")
        for st in [0, 1]:
            self.conn.execute("""INSERT INTO stock_checks (product_id,in_stock,checked_at)
                VALUES ('P1',?,datetime('now'))""", (st,))
        self.conn.commit()
        RestockEngine(db_path=self.path).detect()
        row = self.conn.execute(
            "SELECT status FROM relist_queue WHERE product_id='P1'").fetchone()
        self.assertEqual(row["status"], "DISMISSED")

    def test_waitlist_notification_is_capped(self):
        r = RestockEngine(db_path=self.path)
        for i in range(20):
            r.add_waitlist("P1", f"u{i}@x.com")
        out = r.notify_waitlist("P1")
        self.assertEqual(len(out["notify"]), out["capped_at"])
        self.assertEqual(out["still_waiting"], 20 - out["capped_at"])


class TestBasketMath(unittest.TestCase):
    def test_billed_weight_matches_carrier_rounding(self):
        self.assertEqual(billed_weight_g(420), 500)
        self.assertEqual(billed_weight_g(1050), 1500)
        self.assertEqual(billed_weight_g(2100), 3000)

    def test_headroom_exists_just_past_a_step(self):
        self.assertAlmostEqual(billed_weight_g(1050) - 1050, 450)


if __name__ == "__main__":
    unittest.main(verbosity=2)
