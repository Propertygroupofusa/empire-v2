#!/usr/bin/env python3
"""The owner's switch on the daily idle sweep. Run the way this repo runs tests:

    python3 test_daily_sweep_switch.py

2026-10-11, the owner: "Yeah, turn off daily sweep." The sweep is now OFF
unless GRID_DAILY_IDLE_SWEEP holds an explicit on value. What is pinned here:

  * unset means OFF - deploying the code is the change he asked for
  * OFF reads nothing, writes nothing and burns no day: the grid is never
    asked for its branches and the 24h stamp is never claimed
  * a typo stays OFF, and status() says the value was not recognised
  * turned back on, the sweep behaves exactly as before (the rules
    themselves stay pinned in test_rotation_daily_sweep.py)
  * /grid-status publishes the switch through _armed_flags_block, isolated
    like the two flags already there
  * the profit harvest is NOT governed by this switch

Runs against a REAL SQLite database, because the stamp is a database row.
"""
import ast
import builtins
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import rotation_task
from models import Base, TradingBotState

ENV = rotation_task.DAILY_SWEEP_ENV
DAY = 24 * 60 * 60
HERE = pathlib.Path(__file__).parent


class _Env:
    """Set (or unset, with None) the switch for one block, then restore it."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.prev = os.environ.get(ENV)
        if self.value is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = self.value

    def __exit__(self, *exc):
        if self.prev is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = self.prev


class CountingGrid:
    """Records whether the sweep ever asked for the book."""

    def __init__(self, maker):
        self.session_factory = maker
        self.status_reads = 0

    def get_session_factory(self):
        return self.session_factory

    async def get_grid_status(self):
        self.status_reads += 1
        return {"branches": [{"product_id": "A", "bot_name": "g_A",
                              "allocated_usd": 400.0, "num_levels": 3,
                              "current_price": 1.0, "slices": []}]}


class TestTheSwitch(unittest.TestCase):
    def test_the_variable_name(self):
        self.assertEqual(ENV, "GRID_DAILY_IDLE_SWEEP")

    def test_unset_is_OFF(self):
        with _Env(None):
            self.assertFalse(rotation_task.daily_sweep_enabled())

    def test_off_values_and_typos_are_OFF(self):
        for v in ("", "0", "false", "no", "off", " Off ", "enabled", "tru",
                  "maybe", "disable"):
            with _Env(v):
                self.assertFalse(rotation_task.daily_sweep_enabled(), repr(v))

    def test_only_explicit_on_values_turn_it_on(self):
        for v in ("1", "true", "TRUE", "yes", "on", " ON ", "arm", "armed"):
            with _Env(v):
                self.assertTrue(rotation_task.daily_sweep_enabled(), repr(v))


class TestStatus(unittest.TestCase):
    def test_status_agrees_with_the_switch_for_every_value(self):
        for v in (None, "", "0", "off", "on", "1", "yes", "enabled", "tru"):
            with _Env(v):
                self.assertEqual(rotation_task.daily_sweep_status()["enabled"],
                                 rotation_task.daily_sweep_enabled(), repr(v))

    def test_default_reads_off_and_unset(self):
        with _Env(None):
            s = rotation_task.daily_sweep_status()
        self.assertFalse(s["enabled"])
        self.assertFalse(s["env_is_set"])
        self.assertTrue(s["value_recognised"])
        self.assertEqual(s["default"], "off")
        self.assertEqual(s["env_name"], ENV)

    def test_a_typo_is_flagged_not_silently_accepted(self):
        with _Env("enabled"):
            s = rotation_task.daily_sweep_status()
        self.assertFalse(s["enabled"])
        self.assertTrue(s["env_is_set"])
        self.assertFalse(s["value_recognised"],
                         "a value that matches neither list must say so")

    def test_the_raw_value_is_never_echoed(self):
        secret = "sk-live-pasted-into-the-wrong-field-123"
        with _Env(secret):
            s = rotation_task.daily_sweep_status()
        self.assertNotIn(secret, repr(s))

    def test_it_says_the_harvest_is_not_governed_by_it(self):
        with _Env(None):
            self.assertIn("harvest", rotation_task.daily_sweep_status()["scope"])


class TestOffDoesNothing(unittest.IsolatedAsyncioTestCase):
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
            return {"status": "APPLIED", "rows_written": 4, "added_usd": 385.0}

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

    async def stamp(self):
        async with self.maker() as db:
            r = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == rotation_task.DAILY_SWEEP_KEY))
                 ).scalars().first()
            return None if r is None else float(r.base_capital)

    async def test_OFF_reads_nothing_writes_nothing_moves_nothing(self):
        g = CountingGrid(self.maker)
        with _Env(None):
            for i in range(5):
                out = await rotation_task.run_daily_idle_sweep(
                    g, now_ts=1000.0 + i * DAY)
                self.assertFalse(out["ran"])
                self.assertEqual(out["reason"], "SWEEP_OFF")
                self.assertEqual(out["env_name"], ENV)
        self.assertEqual(g.status_reads, 0, "an off sweep must not read the book")
        self.assertIsNone(await self.stamp(), "an off sweep must not claim the day")
        self.assertEqual(self.applied, [])

    async def test_OFF_with_an_existing_stamp_leaves_it_alone(self):
        """The live database already holds a stamp from 10-11 03:26Z."""
        async with self.maker() as db:
            db.add(TradingBotState(bot_name=rotation_task.DAILY_SWEEP_KEY,
                                   base_capital=1000.0))
            await db.commit()
        with _Env("off"):
            out = await rotation_task.run_daily_idle_sweep(
                CountingGrid(self.maker), now_ts=1000.0 + 3 * DAY)
        self.assertEqual(out["reason"], "SWEEP_OFF")
        self.assertEqual(await self.stamp(), 1000.0)
        self.assertEqual(self.applied, [])

    async def test_STOP_TRADING_still_wins_over_an_on_switch(self):
        os.environ["STOP_TRADING"] = "true"
        try:
            with _Env("on"):
                out = await rotation_task.run_daily_idle_sweep(
                    CountingGrid(self.maker), now_ts=9e9)
        finally:
            del os.environ["STOP_TRADING"]
        self.assertEqual(out["reason"], "STOP_TRADING")

    async def test_turned_back_on_it_runs_exactly_as_before(self):
        g = CountingGrid(self.maker)
        with _Env("on"):
            first = await rotation_task.run_daily_idle_sweep(g, now_ts=1000.0)
            self.assertFalse(first["ran"], "first boot still stamps and waits")
            second = await rotation_task.run_daily_idle_sweep(g, now_ts=1000.0 + DAY)
        self.assertTrue(second["ran"])
        self.assertEqual(len(self.applied), 1)


class TestPublishedOnGridStatus(unittest.TestCase):
    """Source-level, like test_inventory_depth_gate's flags tests, so the
    suite does not import the whole trading engine."""

    src = (HERE / "crypto_grid_bot.py").read_text()

    def _block(self):
        return self.src[self.src.index("def _armed_flags_block"):
                        self.src.index("def _wallet_units_fallback")]

    def test_the_switch_is_in_the_flags_block(self):
        seg = self._block()
        self.assertIn('"daily_idle_sweep"', seg)
        self.assertIn('"rotation_task"', seg)
        self.assertIn('"daily_sweep_status"', seg)

    def test_the_two_existing_flags_are_still_there(self):
        seg = self._block()
        self.assertIn("inventory_depth_gate", seg)
        self.assertIn("sell_deliverability", seg)

    def test_each_module_is_still_isolated(self):
        """Run the block itself with one module failing: only that key fails."""
        tree = ast.parse(self.src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_armed_flags_block")
        class Ok:
            @staticmethod
            def status():
                return {"enabled": True}

        class Boom:
            @staticmethod
            def daily_sweep_status():
                raise RuntimeError("unreadable")

        fake = {"inventory_depth_gate": Ok, "sell_deliverability": Ok,
                "rotation_task": Boom}
        real_import = builtins.__import__

        def _imp(name, *a, **k):
            return fake[name] if name in fake else real_import(name, *a, **k)

        # A module-level name shadows the builtin, so the block's own
        # __import__(modname) call resolves to the fake.
        ns = {"__import__": _imp}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<flags>", "exec"), ns)
        out = ns["_armed_flags_block"]()
        self.assertEqual(out["inventory_depth_gate"], {"enabled": True})
        self.assertEqual(out["sell_deliverability_check"], {"enabled": True})
        self.assertEqual(out["daily_idle_sweep"],
                         {"readable": False, "error": "RuntimeError"})

    def test_it_is_still_spread_into_the_payload(self):
        self.assertIn("**_armed_flags_block(),", self.src)


class TestTheHarvestIsUntouched(unittest.TestCase):
    """He asked about the sweep. The harvest shares the loop and must keep
    running whatever this switch says."""

    def test_the_loop_still_runs_the_harvest_after_the_sweep(self):
        main = (HERE / "main.py").read_text()
        tree = ast.parse(main)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "_daily_idle_sweep_loop")
        body = ast.unparse(fn)
        self.assertIn("run_daily_idle_sweep", body)
        self.assertIn("_ph.run", body)
        self.assertIn("dry_run=False", body)

    def test_the_harvest_does_not_read_the_sweep_switch(self):
        self.assertNotIn(ENV, (HERE / "profit_harvest.py").read_text())
        self.assertNotIn("daily_sweep_enabled", (HERE / "profit_harvest.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
