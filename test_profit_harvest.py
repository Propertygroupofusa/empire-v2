"""Tests for the profit harvest.

The rule it has to get right is the one that separates profit from capital.
CryptoGridBranch has a single money column and no record of what any branch
was funded with, so the harvest is bounded by REALISED P&L EARNED SINCE A
BASELINE - every dollar it takes must appear in the trade ledger as a
closed win recorded after it started watching. These pin that it can never
take more, that a branch seen for the first time harvests nothing, and that
the baseline only advances on a withdrawal that actually succeeded.

Real SQLite, real models: the baseline IS a database row and the realised
total IS the ledger, so a fake would only prove the fake works.
"""
import os
import tempfile
import unittest
from datetime import datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import profit_harvest as H
from models import Base, CryptoGridTradeHistory, TradingBotState


def br(bot, pid, alloc, slices=0):
    return {"bot_name": bot, "product_id": pid, "allocated_usd": alloc,
            "num_levels": 3,
            "slices": [{"id": i, "qty": 1.0, "entry_price": 1.0}
                       for i in range(slices)]}


class FakeGrid:
    def __init__(self, branches, maker):
        self._b = branches
        self.maker = maker
        self.withdrawn = []
        self.refuse = set()

    def get_session_factory(self):
        return self.maker

    async def get_grid_status(self):
        return {"branches": self._b}

    async def withdraw_from_grid_branch(self, bot_name, amount):
        if bot_name in self.refuse:
            raise ValueError(f"{bot_name} has real open slices")
        self.withdrawn.append((bot_name, round(amount, 2)))

    async def _log_activity_safe(self, *a, **k):
        pass


class PureTests(unittest.TestCase):
    def test_a_branch_holding_coin_can_never_be_harvested(self):
        take, why = H.harvestable(br("b", "X", 500, slices=1), 100.0, 0.0)
        self.assertEqual(take, 0.0)
        self.assertIn("flat", why)

    def test_it_is_capped_by_profit_earned_since_the_baseline(self):
        # $900 of allocation, but only $40 earned since we started watching.
        take, why = H.harvestable(br("b", "X", 900), 140.0, 100.0)
        self.assertEqual(take, 40.0)
        self.assertIsNone(why)

    def test_the_keep_alive_floor_caps_the_take_and_is_never_breached(self):
        # $60 earned but only $20 - $15 = $5 of room, so $5 comes out and
        # the branch is left at exactly the $15 floor. Under the old $10
        # minimum this refused outright; $5 of real profit stayed on the
        # table for no reason. The floor itself is what must hold, and it
        # does - this asserts the remainder, not just the take.
        take, why = H.harvestable(br("b", "X", 20), 60.0, 0.0)
        self.assertEqual(take, 5.0)
        self.assertIsNone(why)
        self.assertEqual(20 - take, H.KEEP_BRANCH_ALIVE_USD)

    def test_room_under_the_minimum_is_still_refused(self):
        # $15.20 allocated leaves $0.20 of room, under the $0.50 minimum.
        # Churning allocated_usd for 20 cents is not worth a row.
        take, why = H.harvestable(br("b", "X", 15.20), 60.0, 0.0)
        self.assertEqual(take, 0.0)
        self.assertIn("keep-alive", why)

    def test_a_branch_at_the_floor_is_refused_not_emptied(self):
        # Exactly $15.00: zero room. A drained row gets DELETED by
        # withdraw, taking its coin out of the fleet, so this must refuse.
        take, why = H.harvestable(br("b", "X", 15.0), 60.0, 0.0)
        self.assertEqual(take, 0.0)
        self.assertIn("keep-alive", why)

    def test_the_floor_wins_when_it_is_the_tighter_cap(self):
        take, _ = H.harvestable(br("b", "X", 50), 1000.0, 0.0)
        self.assertEqual(take, 35.0)   # 50 - 15, not 1000

    def test_profit_under_the_measured_minimum_is_left_alone(self):
        take, why = H.harvestable(br("b", "X", 500), 0.49, 0.0)
        self.assertEqual(take, 0.0)
        self.assertIn("minimum", why)

    def test_a_typical_winning_trade_now_clears_the_minimum(self):
        # The point of the change. $0.68 is the MEASURED median winning
        # trade over 2026-09-27..10-03 (109 winners). Under the old $10.00
        # floor, zero of those 109 trades cleared it.
        take, why = H.harvestable(br("b", "X", 500), 0.68, 0.0)
        self.assertEqual(take, 0.68)
        self.assertIsNone(why)

    def test_the_minimum_is_the_measured_value(self):
        # A guard on the constant itself: a future edit that walks it back
        # toward $10 would silently stop the harvest firing at all.
        self.assertEqual(H.MIN_HARVEST_USD, 0.50)
        self.assertEqual(H.KEEP_BRANCH_ALIVE_USD, 15.0)

    def test_a_losing_branch_is_never_harvested(self):
        take, why = H.harvestable(br("b", "X", 500), -40.0, 0.0)
        self.assertEqual(take, 0.0)


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    async def ledger(self, bot, *pnls):
        async with self.maker() as db:
            for p in pnls:
                db.add(CryptoGridTradeHistory(bot_name=bot, product_id="X-USD",
                                              pnl=p, closed_at=datetime.utcnow()))
            await db.commit()

    async def baseline(self, bot):
        from sqlalchemy import select
        async with self.maker() as db:
            r = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == H.BASELINE_PREFIX + bot))).scalars().first()
            return None if r is None else float(r.base_capital)

    async def test_realised_comes_from_the_ledger_not_the_allocation(self):
        await self.ledger("b1", 10.0, 5.5, -2.0)
        await self.ledger("b2", 3.0)
        got = await H.realised_by_branch(lambda: self.maker)
        self.assertAlmostEqual(got["b1"], 13.5, places=2)
        self.assertAlmostEqual(got["b2"], 3.0, places=2)

    async def test_FIRST_SIGHT_HARVESTS_NOTHING(self):
        """Five weeks of history is not freshly earned profit."""
        await self.ledger("b1", 500.0)
        g = FakeGrid([br("b1", "X-USD", 900)], self.maker)
        p = await H.plan(g)
        self.assertEqual(p["total_harvest_usd"], 0.0)
        self.assertEqual(await self.baseline("b1"), 500.0)
        self.assertFalse(p["ready"])

    async def test_only_profit_earned_AFTER_the_baseline_is_taken(self):
        await self.ledger("b1", 500.0)
        g = FakeGrid([br("b1", "X-USD", 900)], self.maker)
        await H.plan(g)                      # marks the baseline at 500
        await self.ledger("b1", 42.0)        # a new win
        out = await H.run(g, dry_run=False)
        self.assertTrue(out["ran"])
        self.assertEqual(g.withdrawn, [("b1", 42.0)])
        self.assertEqual(out["harvested_usd"], 42.0)

    async def test_the_baseline_advances_so_profit_is_not_taken_twice(self):
        await self.ledger("b1", 100.0)
        g = FakeGrid([br("b1", "X-USD", 900)], self.maker)
        await H.plan(g)
        await self.ledger("b1", 30.0)
        await H.run(g, dry_run=False)
        self.assertEqual(await self.baseline("b1"), 130.0)
        g.withdrawn.clear()
        out = await H.run(g, dry_run=False)
        self.assertFalse(out["ran"])
        self.assertEqual(g.withdrawn, [], "the same profit must not be taken twice")

    async def test_a_REFUSED_withdrawal_does_not_advance_the_baseline(self):
        """Otherwise the profit is forgotten and never harvested."""
        await self.ledger("b1", 100.0)
        g = FakeGrid([br("b1", "X-USD", 900)], self.maker)
        await H.plan(g)
        await self.ledger("b1", 30.0)
        g.refuse.add("b1")
        out = await H.run(g, dry_run=False)
        self.assertFalse(out["ran"])
        self.assertEqual(await self.baseline("b1"), 100.0)
        self.assertTrue(out["failed"])
        # ...and it is still offered on the next pass.
        g.refuse.clear()
        out2 = await H.run(g, dry_run=False)
        self.assertEqual(g.withdrawn, [("b1", 30.0)])

    async def test_dry_run_is_the_default_and_withdraws_nothing(self):
        await self.ledger("b1", 100.0)
        g = FakeGrid([br("b1", "X-USD", 900)], self.maker)
        await H.plan(g)
        await self.ledger("b1", 50.0)
        out = await H.run(g)
        self.assertFalse(out["ran"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(g.withdrawn, [])
        self.assertEqual(out["total_harvest_usd"], 50.0)

    async def test_a_branch_holding_coin_is_skipped_even_with_profit(self):
        await self.ledger("b1", 100.0)
        g = FakeGrid([br("b1", "X-USD", 900, slices=2)], self.maker)
        await H.plan(g)
        await self.ledger("b1", 80.0)
        out = await H.run(g, dry_run=False)
        self.assertEqual(g.withdrawn, [])
        self.assertFalse(out["ran"])

    async def test_stop_trading_blocks_everything(self):
        await self.ledger("b1", 100.0)
        g = FakeGrid([br("b1", "X-USD", 900)], self.maker)
        await H.plan(g)
        await self.ledger("b1", 50.0)
        os.environ["STOP_TRADING"] = "true"
        try:
            out = await H.run(g, dry_run=False)
        finally:
            del os.environ["STOP_TRADING"]
        self.assertEqual(out["reason"], "STOP_TRADING")
        self.assertEqual(g.withdrawn, [])

    async def test_it_never_leaves_a_branch_below_the_deletion_floor(self):
        await self.ledger("b1", 0.0)
        g = FakeGrid([br("b1", "X-USD", 40)], self.maker)
        await H.plan(g)
        await self.ledger("b1", 500.0)      # huge profit, tiny branch
        await H.run(g, dry_run=False)
        self.assertEqual(g.withdrawn, [("b1", 25.0)])   # 40 - 15
        self.assertGreaterEqual(40 - g.withdrawn[0][1], H.KEEP_BRANCH_ALIVE_USD)

    async def test_it_places_no_order_and_sells_no_coin(self):
        src = open("profit_harvest.py").read()
        for forbidden in ("place_market_sell", "place_market_buy", "_submit_order",
                          "close_all_grid_slices", "add_cash_to_grid_branch"):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
