"""Tests for POST /grid-status/close-slices (partial close of named slices).

Drives the endpoint function directly against a fake grid module, so no
network, no database, and no real order can be placed by a test run.

The ZEC case these were written for: 6 slices against 3 levels. Selling 3
leaves 3, which is STILL parked - the loss is realised and nothing is
unlocked. Selling 4 leaves 2 and the branch can buy again. Those two cases
are pinned below because they are the whole reason this endpoint exists.
"""
import asyncio
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

import crypto_grid_bot as real_grid
import routers.trading_dashboard as td


# ZEC-USD as it stood on 2026-10-03: 6 slices, 3 levels, price 1298.46.
ZEC_SLICES = [
    {"id": 53, "entry_price": 1659.17, "qty": 0.073518066164, "adopted": True,
     "entry_fee_rate": None},
    {"id": 54, "entry_price": 1659.17, "qty": 0.080360000000, "adopted": True,
     "entry_fee_rate": None},
    {"id": 55, "entry_price": 1650.61, "qty": 0.378170000000, "adopted": True,
     "entry_fee_rate": None},
    {"id": 56, "entry_price": 1650.61, "qty": 0.378170000000, "adopted": True,
     "entry_fee_rate": None},
    {"id": 57, "entry_price": 1650.61, "qty": 0.378170000000, "adopted": True,
     "entry_fee_rate": None},
    # The one real buy in the book - it paid an entry commission, so it is
    # priced on BOTH legs while the adopted five are priced on the exit only.
    {"id": 58, "entry_price": 1586.44, "qty": 0.043390000000, "adopted": False,
     "entry_fee_rate": 0.0075},
]


class FakeGrid:
    def __init__(self, slices=None, price=1298.46, num_levels=3, round_trip=0.014,
                 maker_active=True):
        self._maker_active = maker_active
        self._slices = [dict(s) for s in (ZEC_SLICES if slices is None else slices)]
        self._price = price
        self._num_levels = num_levels
        self._round_trip = round_trip
        self.closed_with = None

    async def get_grid_status(self):
        return {"branches": [{
            "product_id": "ZEC-USD", "bot_name": "crypto_grid_9",
            "current_price": self._price, "num_levels": self._num_levels,
            "allocated_usd": 2271.29, "slices": self._slices,
        }]}

    async def get_effective_round_trip_fee_rate(self):
        return self._round_trip

    def __init_subclass__(cls, **kw):
        raise AssertionError("FakeGrid is not meant to be subclassed")

    async def is_maker_orders_active(self):
        return self._maker_active

    # The preview MUST price slices with the engine's own functions, not a
    # second expression. Delegating here means a test failure can only mean
    # the endpoint stopped using them.
    _slice_rate = staticmethod(real_grid._slice_rate)
    _grid_slice_net_pnl = staticmethod(real_grid._grid_slice_net_pnl)

    async def close_all_grid_slices(self, only_product_id=None, only_slice_ids=None,
                                    exit_reason=None, **kw):
        self.closed_with = {"only_product_id": only_product_id,
                            "only_slice_ids": list(only_slice_ids or []),
                            "exit_reason": exit_reason}
        return {"slices_closed": len(only_slice_ids or []), "total_realized_pnl": -207.81}


def call(grid, **kw):
    kw.setdefault("product_id", "ZEC-USD")
    old = td.crypto_grid_bot_module
    td.crypto_grid_bot_module = grid
    try:
        return asyncio.run(td.close_some_grid_slices_endpoint(**kw))
    finally:
        td.crypto_grid_bot_module = old


class MenuTests(unittest.TestCase):
    def test_dry_run_without_ids_lists_every_slice_with_its_id(self):
        r = call(FakeGrid())
        self.assertTrue(r["menu"])
        self.assertEqual(r["open_slices"], 6)
        self.assertEqual([s["slice_id"] for s in r["slices"]], [53, 54, 55, 56, 57, 58])
        self.assertTrue(r["parked_now"])
        for s in r["slices"]:
            self.assertIn("net_pnl_usd", s)
        # internals must not leak
        self.assertNotIn("_net", r["slices"][0])

    def test_execute_without_ids_is_refused_never_guessed(self):
        g = FakeGrid()
        with self.assertRaises(HTTPException) as cm:
            call(g, dry_run=False, accept_loss=True)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("slice_ids is required", cm.exception.detail)
        self.assertIsNone(g.closed_with)


class SelectionTests(unittest.TestCase):
    def test_unknown_id_refuses_and_sells_nothing(self):
        g = FakeGrid()
        with self.assertRaises(HTTPException) as cm:
            call(g, slice_ids="53,999", dry_run=False, accept_loss=True)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("999", cm.exception.detail)
        self.assertIsNone(g.closed_with)

    def test_duplicate_ids_count_once(self):
        r = call(FakeGrid(), slice_ids="53,53,53")
        self.assertEqual(r["selling_count"], 1)
        self.assertEqual(r["slices_remaining"], 5)

    def test_whitespace_and_empties_are_tolerated(self):
        r = call(FakeGrid(), slice_ids=" 53 , ,54 ,")
        self.assertEqual(r["selling_count"], 2)


class ParkedArithmeticTests(unittest.TestCase):
    """The reason this endpoint exists."""

    def test_selling_three_of_six_leaves_it_PARKED(self):
        r = call(FakeGrid(), slice_ids="53,54,58")
        self.assertEqual(r["slices_remaining"], 3)
        self.assertEqual(r["num_levels"], 3)
        self.assertTrue(r["parked_after"])
        self.assertIn("STILL PARKED", r["branch_after"])

    def test_selling_four_of_six_unparks_it(self):
        r = call(FakeGrid(), slice_ids="53,54,58,55")
        self.assertEqual(r["slices_remaining"], 2)
        self.assertFalse(r["parked_after"])
        self.assertIn("can buy again", r["branch_after"])

    def test_selling_all_six_reports_flat_and_withdrawable(self):
        r = call(FakeGrid(), slice_ids="53,54,55,56,57,58")
        self.assertEqual(r["slices_remaining"], 0)
        self.assertFalse(r["parked_after"])
        self.assertIn("FLAT", r["branch_after"])
        self.assertIn("withdrawable", r["branch_after"])


class MathTests(unittest.TestCase):
    def _engine_net(self, sl, price=1298.46, rt=0.014, exit_leg=0.007):
        shim = SimpleNamespace(adopted=sl.get("adopted"),
                               entry_fee_rate=sl.get("entry_fee_rate"))
        rate = real_grid._slice_rate(shim, rt, exit_leg)
        return real_grid._grid_slice_net_pnl(sl["qty"], sl["entry_price"], price, rate)

    def test_every_slice_is_priced_by_the_engine_formula(self):
        """The preview must equal what close_all_grid_slices will book."""
        r = call(FakeGrid(), slice_ids="53,54,55,56,57,58")
        want = sum(self._engine_net(s) for s in ZEC_SLICES)
        self.assertAlmostEqual(r["realized_pnl_usd"], round(want, 2), places=2)
        for row, sl in zip(r["selling"], ZEC_SLICES):
            self.assertAlmostEqual(row["net_pnl_usd"],
                                   round(self._engine_net(sl), 2), places=2)

    def test_an_adopted_slice_pays_the_exit_leg_only(self):
        r = call(FakeGrid(), slice_ids="53")
        self.assertTrue(r["selling"][0]["adopted"])
        self.assertAlmostEqual(r["selling"][0]["round_trip_fee_rate"], 0.007, places=6)

    def test_a_bought_slice_pays_both_legs(self):
        r = call(FakeGrid(), slice_ids="58")
        self.assertFalse(r["selling"][0]["adopted"])
        self.assertAlmostEqual(r["selling"][0]["round_trip_fee_rate"],
                               0.0075 + 0.007, places=6)

    def test_the_exit_only_shortcut_would_have_been_wrong(self):
        """Pins the $2.11 gap the old close-branch expression carries."""
        r = call(FakeGrid(), slice_ids="53,54,55,56,57,58")
        exit_only = sum(s["qty"] * 1298.46
                        - s["qty"] * s["entry_price"]
                        - s["qty"] * 1298.46 * 0.007 for s in ZEC_SLICES)
        self.assertGreater(exit_only, r["realized_pnl_usd"])
        self.assertAlmostEqual(exit_only - r["realized_pnl_usd"], 2.11, places=1)

    def test_totals_are_the_sum_of_the_per_slice_numbers(self):
        r = call(FakeGrid(), slice_ids="53,54,58")
        self.assertAlmostEqual(r["realized_pnl_usd"],
                               round(sum(x["net_pnl_usd"] for x in r["selling"]), 2),
                               places=2)

    def test_the_four_slice_cut_is_cheaper_than_half_the_position(self):
        """3 small + 1 big realises less than 3 of the big ones."""
        cheap = call(FakeGrid(), slice_ids="53,54,58,55")["realized_pnl_usd"]
        dear = call(FakeGrid(), slice_ids="55,56,57")["realized_pnl_usd"]
        self.assertGreater(cheap, dear)
        self.assertLess(cheap, 0)

    def test_subset_totals_never_exceed_the_whole_position(self):
        part = call(FakeGrid(), slice_ids="53,54")["realized_pnl_usd"]
        whole = call(FakeGrid(), slice_ids="53,54,55,56,57,58")["realized_pnl_usd"]
        self.assertGreater(part, whole)


class GateTests(unittest.TestCase):
    def test_dry_run_is_the_default_and_sells_nothing(self):
        g = FakeGrid()
        r = call(g, slice_ids="53,54")
        self.assertTrue(r["dry_run"])
        self.assertIn("PREVIEW ONLY", r["detail"])
        self.assertIsNone(g.closed_with)

    def test_a_loss_needs_accept_loss(self):
        g = FakeGrid()
        with self.assertRaises(HTTPException) as cm:
            call(g, slice_ids="53,54", dry_run=False)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("LOSS", cm.exception.detail)
        self.assertIsNone(g.closed_with)

    def test_a_profit_does_not_need_accept_loss(self):
        g = FakeGrid(price=2000.0)
        r = call(g, slice_ids="53", dry_run=False)
        self.assertIsNotNone(g.closed_with)
        self.assertEqual(g.closed_with["only_slice_ids"], [53])

    def test_execute_passes_exactly_the_chosen_ids_and_scopes_the_product(self):
        g = FakeGrid()
        call(g, slice_ids="53,54,58,55", dry_run=False, accept_loss=True)
        self.assertEqual(g.closed_with["only_product_id"], "ZEC-USD")
        self.assertEqual(sorted(g.closed_with["only_slice_ids"]), [53, 54, 55, 58])
        self.assertEqual(g.closed_with["exit_reason"], "partial_close")


class RefusalTests(unittest.TestCase):
    def test_unknown_product_is_404(self):
        with self.assertRaises(HTTPException) as cm:
            call(FakeGrid(), product_id="NOPE-USD")
        self.assertEqual(cm.exception.status_code, 404)

    def test_branch_with_no_slices_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            call(FakeGrid(slices=[]))
        self.assertEqual(cm.exception.status_code, 400)

    def test_unpriceable_branch_refuses_rather_than_closing_blind(self):
        g = FakeGrid(price=None)
        with self.assertRaises(HTTPException) as cm:
            call(g, slice_ids="53", dry_run=False, accept_loss=True)
        self.assertIn("A gap is not a zero", cm.exception.detail)
        self.assertIsNone(g.closed_with)


if __name__ == "__main__":
    unittest.main(verbosity=2)
