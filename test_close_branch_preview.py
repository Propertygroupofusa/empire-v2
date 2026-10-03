"""Tests for POST /grid-status/close-branch's PREVIEW arithmetic.

The preview used to charge the exit leg only while close_all_grid_slices
charges both legs through _grid_slice_net_pnl (an ADOPTED slice excepted -
its basis never paid a commission). It was therefore always optimistic:
measured 2026-10-03 at $9.21 across the live fleet, and on BTC it read
+$0.05 on a close the engine books at -$0.08. Green on a red, irreversible
close. The BTC book below is that exact live position, kept as a
regression fixture.

Drives the endpoint directly against a fake grid that delegates the fee
functions to the real module, so a failure can only mean the preview
stopped agreeing with the engine. No network, no database, no orders.
"""
import asyncio
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

import crypto_grid_bot as real_grid
import routers.trading_dashboard as td

# BTC-USD exactly as it stood at 2026-10-03T20:00Z. Both slices are real
# buys, so both pay an entry leg - which is why the shortcut flipped the
# sign on this one.
BTC_PRICE = 84786.99
BTC_SLICES = [
    {"id": 133, "entry_price": 83297.48000000001, "qty": 0.00022389,
     "adopted": False, "entry_fee_rate": 0.0035},
    {"id": 140, "entry_price": 84866.70999999999, "qty": 0.00022058,
     "adopted": False, "entry_fee_rate": 0.0035},
]

# ZEC-USD: five adopted slices and one real buy, the mixed case.
ZEC_PRICE = 1298.46
ZEC_SLICES = [
    {"id": 53, "entry_price": 1659.17, "qty": 0.073518066164, "adopted": True,
     "entry_fee_rate": None},
    {"id": 68, "entry_price": 1586.44, "qty": 0.043386, "adopted": False,
     "entry_fee_rate": 0.0075},
]


class FakeGrid:
    def __init__(self, slices=BTC_SLICES, price=BTC_PRICE, product_id="BTC-USD",
                 round_trip=0.014, maker_active=True):
        self._slices = [dict(s) for s in slices]
        self._price = price
        self._pid = product_id
        self._round_trip = round_trip
        self._maker_active = maker_active
        self.closed_with = None

    async def get_grid_status(self):
        return {"branches": [{
            "product_id": self._pid, "bot_name": "crypto_grid_x",
            "current_price": self._price, "num_levels": 3,
            "allocated_usd": 56.0, "slices": self._slices,
        }]}

    async def get_effective_round_trip_fee_rate(self):
        return self._round_trip

    async def is_maker_orders_active(self):
        return self._maker_active

    async def close_all_grid_slices(self, only_product_id=None, **kw):
        self.closed_with = {"only_product_id": only_product_id}
        return {"slices_closed": len(self._slices), "total_realized_pnl": -0.08}

    _slice_rate = staticmethod(real_grid._slice_rate)
    _grid_slice_net_pnl = staticmethod(real_grid._grid_slice_net_pnl)


def call(grid, **kw):
    kw.setdefault("product_id", grid._pid)
    old = td.crypto_grid_bot_module
    td.crypto_grid_bot_module = grid
    try:
        return asyncio.run(td.close_one_grid_branch_endpoint(**kw))
    finally:
        td.crypto_grid_bot_module = old


def engine_net(sl, price, rt=0.014, exit_leg=0.007):
    shim = SimpleNamespace(adopted=sl.get("adopted"),
                           entry_fee_rate=sl.get("entry_fee_rate"))
    return real_grid._grid_slice_net_pnl(sl["qty"], sl["entry_price"], price,
                                         real_grid._slice_rate(shim, rt, exit_leg))


def exit_only(sl, price, rate=0.007):
    """The shortcut this fix removed."""
    value = sl["qty"] * price
    return value - sl["qty"] * sl["entry_price"] - value * rate


class ParityTests(unittest.TestCase):
    def test_preview_equals_what_the_engine_will_book(self):
        for slices, price, pid in ((BTC_SLICES, BTC_PRICE, "BTC-USD"),
                                   (ZEC_SLICES, ZEC_PRICE, "ZEC-USD")):
            r = call(FakeGrid(slices, price, pid))
            want = sum(engine_net(s, price) for s in slices)
            self.assertAlmostEqual(r["realized_pnl_usd"], round(want, 2), places=2, msg=pid)

    def test_per_slice_rows_match_too(self):
        r = call(FakeGrid())
        for row, sl in zip(r["slices"], BTC_SLICES):
            self.assertAlmostEqual(row["net_pnl_usd"],
                                   round(engine_net(sl, BTC_PRICE), 2), places=2)

    def test_totals_are_the_sum_of_the_rows(self):
        r = call(FakeGrid(ZEC_SLICES, ZEC_PRICE, "ZEC-USD"))
        self.assertAlmostEqual(r["realized_pnl_usd"],
                               round(sum(x["net_pnl_usd"] for x in r["slices"]), 2),
                               places=2)


class RegressionTests(unittest.TestCase):
    """The shapes that made this worth fixing."""

    def test_btc_no_longer_reads_green_on_a_red_close(self):
        r = call(FakeGrid())
        old = sum(exit_only(s, BTC_PRICE) for s in BTC_SLICES)
        self.assertGreater(old, 0, "fixture should reproduce the optimistic +$0.05")
        self.assertLess(r["realized_pnl_usd"], 0, "engine books this close at a loss")

    def test_the_shortcut_was_always_optimistic(self):
        for slices, price, pid in ((BTC_SLICES, BTC_PRICE, "BTC-USD"),
                                   (ZEC_SLICES, ZEC_PRICE, "ZEC-USD")):
            r = call(FakeGrid(slices, price, pid))
            old = sum(exit_only(s, price) for s in slices)
            self.assertGreater(old, r["realized_pnl_usd"], pid)

    def test_an_adopted_slice_still_pays_the_exit_leg_only(self):
        r = call(FakeGrid(ZEC_SLICES, ZEC_PRICE, "ZEC-USD"))
        adopted = next(x for x in r["slices"] if x["adopted"])
        bought = next(x for x in r["slices"] if not x["adopted"])
        self.assertAlmostEqual(adopted["round_trip_fee_rate"], 0.007, places=6)
        self.assertAlmostEqual(bought["round_trip_fee_rate"], 0.0075 + 0.007, places=6)


class GateTests(unittest.TestCase):
    def test_dry_run_is_the_default_and_sells_nothing(self):
        g = FakeGrid()
        r = call(g)
        self.assertTrue(r["dry_run"])
        self.assertIsNone(g.closed_with)

    def test_a_loss_needs_accept_loss(self):
        g = FakeGrid()
        with self.assertRaises(HTTPException) as cm:
            call(g, dry_run=False)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("LOSS", cm.exception.detail)
        self.assertIsNone(g.closed_with)

    def test_the_accept_loss_gate_now_fires_on_the_true_figure(self):
        """Under the shortcut BTC previewed +$0.05 and would have skipped
        the gate entirely. On the engine's figure it is a loss and does not."""
        g = FakeGrid()
        self.assertGreater(sum(exit_only(s, BTC_PRICE) for s in BTC_SLICES), 0)
        with self.assertRaises(HTTPException):
            call(g, dry_run=False)

    def test_a_profit_executes_without_accept_loss(self):
        g = FakeGrid(price=120000.0)
        call(g, dry_run=False)
        self.assertEqual(g.closed_with["only_product_id"], "BTC-USD")


class RefusalTests(unittest.TestCase):
    def test_unknown_product_is_404(self):
        with self.assertRaises(HTTPException) as cm:
            call(FakeGrid(), product_id="NOPE-USD")
        self.assertEqual(cm.exception.status_code, 404)

    def test_no_open_slices_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            call(FakeGrid(slices=[]))
        self.assertEqual(cm.exception.status_code, 400)

    def test_unpriceable_branch_refuses_rather_than_closing_blind(self):
        g = FakeGrid(price=None)
        with self.assertRaises(HTTPException) as cm:
            call(g, dry_run=False, accept_loss=True)
        self.assertIn("A gap is not a zero", cm.exception.detail)
        self.assertIsNone(g.closed_with)


if __name__ == "__main__":
    unittest.main(verbosity=2)
