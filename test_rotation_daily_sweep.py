"""Tests for the bounded daily idle sweep.

The leash the owner set: at most once per 24h, flat branches only, and only
when at least $50 is actually movable. These pin each of those, and pin that
the clock survives a restart - an in-memory timestamp would let a crash-
looping container rotate on every boot, which is the failure that matters
most for something moving money without being asked.

Runs against a REAL SQLite database with the real model, because the clock
is a database row and a fake would only prove the fake works.
"""
import asyncio
import os
import tempfile
import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import rotation_task
from models import Base, TradingBotState

DAY = 24 * 60 * 60


def branch(pid, alloc, slices=0, levels=3):
    return {"product_id": pid, "bot_name": "g_" + pid, "allocated_usd": alloc,
            "num_levels": levels, "current_price": 1.0,
            "slices": [{"id": i, "qty": 1.0, "entry_price": 1.0}
                       for i in range(slices)]}


class FakeGrid:
    def __init__(self, branches, maker=None):
        self._b = branches
        self.session_factory = maker
        self.applied = 0
        self.apply_rows = 4

    def get_session_factory(self):
        return self.session_factory

    async def get_grid_status(self):
        return {"branches": self._b}


class MovableTests(unittest.TestCase):
    def test_only_completely_flat_branches_count(self):
        bs = [branch("A", 200, slices=0),      # 185 movable
              branch("B", 500, slices=1),      # holds coin -> not movable
              branch("C", 400, slices=3)]      # parked -> not movable
        self.assertAlmostEqual(rotation_task.movable_from_flat(bs), 185.0, places=2)

    def test_the_keep_alive_is_subtracted(self):
        self.assertAlmostEqual(
            rotation_task.movable_from_flat([branch("A", 100, 0)]), 85.0, places=2)

    def test_a_branch_under_the_transfer_minimum_contributes_nothing(self):
        # $20 - $15 keep-alive = $5, under the $10 minimum.
        self.assertEqual(rotation_task.movable_from_flat([branch("A", 20, 0)]), 0.0)

    def test_the_live_book_measures_what_was_quoted(self):
        bs = [branch("JASMY-USD", 193.95, 0), branch("QNT-USD", 160.65, 0),
              branch("TIA-USD", 15.00, 0), branch("ZEC-USD", 2271.29, 6)]
        self.assertAlmostEqual(rotation_task.movable_from_flat(bs), 324.60, places=2)


class SweepTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        self.applied = []
        self._real_apply = rotation_task.apply
        self._real_plan = rotation_task.build_plan

        async def _apply(g, p):
            self.applied.append(p)
            return {"status": "APPLIED", "rows_written": 4, "added_usd": 324.60}

        async def _plan(g, ranker=None, release_deployed_idle=None):
            return {"ok": True, "sources": [], "targets": [], "why": None,
                    "release_deployed_idle": release_deployed_idle}
        rotation_task.apply = _apply
        rotation_task.build_plan = _plan

    async def asyncTearDown(self):
        rotation_task.apply = self._real_apply
        rotation_task.build_plan = self._real_plan
        await self.engine.dispose()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def grid(self, branches=None):
        return FakeGrid(branches if branches is not None
                        else [branch("A", 400, 0)], self.maker)

    async def stamp(self):
        from sqlalchemy import select
        async with self.maker() as db:
            r = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == rotation_task.DAILY_SWEEP_KEY))
                 ).scalars().first()
            return None if r is None else float(r.base_capital)

    async def test_a_fresh_database_stamps_but_does_NOT_run(self):
        g = self.grid()
        out = await rotation_task.run_daily_idle_sweep(g, now_ts=1000.0)
        self.assertFalse(out["ran"])
        self.assertEqual(self.applied, [])
        self.assertEqual(await self.stamp(), 1000.0)

    async def test_it_runs_once_a_day_and_not_twice(self):
        g = self.grid()
        await rotation_task.run_daily_idle_sweep(g, now_ts=1000.0)      # stamps
        out = await rotation_task.run_daily_idle_sweep(g, now_ts=1000.0 + DAY)
        self.assertTrue(out["ran"])
        self.assertEqual(len(self.applied), 1)
        again = await rotation_task.run_daily_idle_sweep(g, now_ts=1000.0 + DAY + 60)
        self.assertFalse(again["ran"])
        self.assertEqual(again["reason"], "NOT_DUE")
        self.assertEqual(len(self.applied), 1, "must not run twice in a day")

    async def test_THE_CLOCK_SURVIVES_A_RESTART(self):
        """An in-memory timestamp would rotate on every boot of a crash loop."""
        await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0)
        await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0 + DAY)
        self.assertEqual(len(self.applied), 1)
        # Ten "restarts" - a brand new grid object each time, same database.
        for i in range(10):
            await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0 + DAY + i)
        self.assertEqual(len(self.applied), 1, "a restart must not re-arm the sweep")

    async def test_below_the_minimum_it_does_not_run_or_burn_the_day(self):
        await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0)
        quiet = self.grid([branch("A", 60, 0)])   # $45 movable, under $50
        out = await rotation_task.run_daily_idle_sweep(quiet, now_ts=1000.0 + DAY)
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "BELOW_MINIMUM")
        self.assertEqual(await self.stamp(), 1000.0, "a quiet day must not burn the slot")
        # ...and later the same day, once enough frees up, it still fires.
        out2 = await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0 + DAY + 5)
        self.assertTrue(out2["ran"])

    async def test_the_clock_is_claimed_BEFORE_the_run(self):
        """A failure costs a day rather than retrying against live money."""
        async def boom(g, p):
            raise RuntimeError("withdraw refused")
        rotation_task.apply = boom
        await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0)
        with self.assertRaises(RuntimeError):
            await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0 + DAY)
        self.assertEqual(await self.stamp(), 1000.0 + DAY,
                         "the slot must already be stamped when the run fails")

    async def test_stop_trading_blocks_it_before_anything_is_read(self):
        os.environ["STOP_TRADING"] = "true"
        try:
            out = await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=9e9)
        finally:
            del os.environ["STOP_TRADING"]
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "STOP_TRADING")
        self.assertIsNone(await self.stamp())
        self.assertEqual(self.applied, [])

    async def test_it_never_releases_deployed_idle(self):
        """Only flat branches. It must not try to pull cash out of held coin."""
        await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0)
        await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0 + DAY)
        self.assertEqual(len(self.applied), 1)
        self.assertIs(self.applied[0]["release_deployed_idle"], False)

    async def test_a_not_ready_plan_writes_nothing(self):
        async def _plan(g, ranker=None, release_deployed_idle=None):
            return {"ok": False, "why": "no target has a free rung"}
        rotation_task.build_plan = _plan
        await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0)
        out = await rotation_task.run_daily_idle_sweep(self.grid(), now_ts=1000.0 + DAY)
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "NOT_READY")
        self.assertEqual(self.applied, [])

    async def test_it_does_not_read_the_auto_rotate_flag(self):
        src = open("rotation_task.py").read()
        tail = src[src.index("THE DAILY IDLE SWEEP"):]
        self.assertNotIn("is_grid_auto_rotate_active", tail)
        self.assertNotIn("GRID_AUTO_ROTATE", tail.replace(
            "It is NOT GRID_AUTO_ROTATE", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
