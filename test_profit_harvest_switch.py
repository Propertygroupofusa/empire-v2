#!/usr/bin/env python3
"""The owner's switch on the profit harvest. Run the way this repo runs tests:

    python3 test_profit_harvest_switch.py

2026-10-11, the owner: "turn off the harvest too, push it." The live harvest
is now OFF unless GRID_PROFIT_HARVEST holds an explicit on value. Pinned here:

  * unset means OFF - deploying the code is the change he asked for
  * OFF withdraws nothing, marks no baseline and advances none: plan() is
    never reached, so the database is untouched
  * the read-only preview (dry_run=True, plan(create=False)) still reports
    what a harvest would take, and now says whether the harvest is armed
  * a typo stays OFF, and status() says the value was not recognised
  * STOP_TRADING still wins; turned back on, it harvests exactly as before
  * /grid-status publishes the switch through _armed_flags_block
  * the daily sweep switch is a separate variable and is not touched

Runs against a REAL SQLite database, because the baseline is a database row
and the realised total is the trade ledger.
"""
import ast
import builtins
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import profit_harvest as H
from models import Base, CryptoGridTradeHistory, TradingBotState

ENV = H.HARVEST_ENV
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
        self.status_reads = 0

    def get_session_factory(self):
        return self.maker

    async def get_grid_status(self):
        self.status_reads += 1
        return {"branches": self._b}

    async def withdraw_from_grid_branch(self, bot, usd):
        self.withdrawn.append((bot, usd))

    async def _log_activity_safe(self, *a, **k):
        pass


class TestTheSwitch(unittest.TestCase):
    def test_the_variable_name(self):
        self.assertEqual(ENV, "GRID_PROFIT_HARVEST")

    def test_it_is_not_the_sweep_variable(self):
        import rotation_task
        self.assertNotEqual(ENV, rotation_task.DAILY_SWEEP_ENV)

    def test_unset_is_OFF(self):
        with _Env(None):
            self.assertFalse(H.harvest_enabled())

    def test_off_values_and_typos_are_OFF(self):
        for v in ("", "0", "false", "no", "off", " Off ", "enabled", "tru",
                  "maybe", "disable"):
            with _Env(v):
                self.assertFalse(H.harvest_enabled(), repr(v))

    def test_only_explicit_on_values_turn_it_on(self):
        for v in ("1", "true", "TRUE", "yes", "on", " ON ", "arm", "armed"):
            with _Env(v):
                self.assertTrue(H.harvest_enabled(), repr(v))


class TestStatus(unittest.TestCase):
    def test_status_agrees_with_the_switch_for_every_value(self):
        for v in (None, "", "0", "off", "on", "1", "yes", "enabled", "tru"):
            with _Env(v):
                self.assertEqual(H.harvest_status()["enabled"],
                                 H.harvest_enabled(), repr(v))

    def test_default_reads_off_and_unset(self):
        with _Env(None):
            s = H.harvest_status()
        self.assertFalse(s["enabled"])
        self.assertFalse(s["env_is_set"])
        self.assertTrue(s["value_recognised"])
        self.assertEqual(s["default"], "off")
        self.assertEqual(s["env_name"], ENV)

    def test_a_typo_is_flagged_not_silently_accepted(self):
        with _Env("enabled"):
            s = H.harvest_status()
        self.assertFalse(s["enabled"])
        self.assertTrue(s["env_is_set"])
        self.assertFalse(s["value_recognised"])

    def test_the_raw_value_is_never_echoed(self):
        secret = "sk-live-pasted-into-the-wrong-field-123"
        with _Env(secret):
            s = H.harvest_status()
        self.assertNotIn(secret, repr(s))

    def test_it_states_what_turning_it_back_on_does(self):
        with _Env(None):
            self.assertIn("baseline", H.harvest_status()["if_turned_back_on"])


class TestOffDoesNothing(unittest.IsolatedAsyncioTestCase):
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
                db.add(CryptoGridTradeHistory(
                    bot_name=bot, product_id="X-USD", entry_price=1.0,
                    exit_price=1.0, qty=1.0, pnl=p,
                    opened_at=datetime.utcnow(), closed_at=datetime.utcnow()))
            await db.commit()

    async def base(self, bot):
        async with self.maker() as db:
            r = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == H.BASELINE_PREFIX + bot))
                 ).scalars().first()
            return None if r is None else float(r.base_capital)

    async def test_OFF_withdraws_nothing_and_writes_no_baseline(self):
        g = FakeGrid([br("b1", "X-USD", 200.0)], self.maker)
        with _Env(None):
            out = await H.run(g, dry_run=False)
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "HARVEST_OFF")
        self.assertEqual(out["env_name"], ENV)
        self.assertEqual(out["harvested_usd"], 0.0)
        self.assertEqual(g.withdrawn, [])
        self.assertEqual(g.status_reads, 0, "an off harvest must not read the book")
        self.assertIsNone(await self.base("b1"), "an off harvest must not mark")

    async def test_OFF_leaves_earned_profit_in_the_branch(self):
        """The case he asked for: a flat branch with profit keeps it."""
        g = FakeGrid([br("b1", "X-USD", 200.0)], self.maker)
        with _Env("on"):
            await H.run(g, dry_run=False)          # marks the baseline at 0
        await self.ledger("b1", 3.0, 2.0)           # $5 earned since
        with _Env("off"):
            for _ in range(5):
                out = await H.run(g, dry_run=False)
                self.assertEqual(out["reason"], "HARVEST_OFF")
        self.assertEqual(g.withdrawn, [])
        self.assertEqual(await self.base("b1"), 0.0, "the mark must not move")

    async def test_the_preview_still_reports_what_it_would_take(self):
        g = FakeGrid([br("b1", "X-USD", 200.0)], self.maker)
        with _Env("on"):
            await H.run(g, dry_run=False)
        await self.ledger("b1", 3.0, 2.0)
        with _Env(None):
            prev = await H.run(g)                   # dry_run is the default
            ro = await H.plan(g, create=False)
        self.assertTrue(prev["dry_run"])
        self.assertEqual(prev["total_harvest_usd"], 5.0)
        self.assertFalse(prev["harvest_active"])
        self.assertFalse(ro["harvest_active"])
        self.assertEqual(g.withdrawn, [])

    async def test_STOP_TRADING_still_wins_over_an_on_switch(self):
        os.environ["STOP_TRADING"] = "true"
        try:
            with _Env("on"):
                out = await H.run(FakeGrid([], self.maker), dry_run=False)
        finally:
            del os.environ["STOP_TRADING"]
        self.assertEqual(out["reason"], "STOP_TRADING")

    async def test_turned_back_on_it_harvests_exactly_as_before(self):
        g = FakeGrid([br("b1", "X-USD", 200.0)], self.maker)
        with _Env("on"):
            first = await H.run(g, dry_run=False)
            self.assertFalse(first["ran"], "first sight still marks and takes nothing")
            await self.ledger("b1", 3.0, 2.0)
            second = await H.run(g, dry_run=False)
        self.assertTrue(second["ran"])
        self.assertEqual(g.withdrawn, [("b1", 5.0)])
        self.assertEqual(await self.base("b1"), 5.0)


class TestPublishedOnGridStatus(unittest.TestCase):
    src = (HERE / "crypto_grid_bot.py").read_text()

    def _block(self):
        return self.src[self.src.index("def _armed_flags_block"):
                        self.src.index("def _wallet_units_fallback")]

    def test_the_switch_is_in_the_flags_block(self):
        seg = self._block()
        self.assertIn('("profit_harvest", "profit_harvest", "harvest_status")', seg)

    def test_the_other_three_flags_are_still_there(self):
        seg = self._block()
        for key in ("inventory_depth_gate", "sell_deliverability_check",
                    "daily_idle_sweep"):
            self.assertIn(key, seg)

    def test_each_module_is_still_isolated(self):
        tree = ast.parse(self.src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_armed_flags_block")

        class Ok:
            @staticmethod
            def status():
                return {"enabled": True}

            @staticmethod
            def daily_sweep_status():
                return {"enabled": False}

        class Boom:
            @staticmethod
            def harvest_status():
                raise RuntimeError("unreadable")

        fake = {"inventory_depth_gate": Ok, "sell_deliverability": Ok,
                "rotation_task": Ok, "profit_harvest": Boom}
        real_import = builtins.__import__

        def _imp(name, *a, **k):
            return fake[name] if name in fake else real_import(name, *a, **k)

        ns = {"__import__": _imp}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<flags>", "exec"), ns)
        out = ns["_armed_flags_block"]()
        self.assertEqual(out["daily_idle_sweep"], {"enabled": False})
        self.assertEqual(out["profit_harvest"],
                         {"readable": False, "error": "RuntimeError"})


class TestTheLoopIsUnchanged(unittest.TestCase):
    def test_main_still_calls_the_live_harvest_hourly(self):
        main = (HERE / "main.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(main))
                  if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "_daily_idle_sweep_loop")
        body = ast.unparse(fn)
        self.assertIn("_ph.run", body)
        self.assertIn("dry_run=False", body)

    def test_the_switch_is_checked_before_plan(self):
        src = (HERE / "profit_harvest.py").read_text()
        run = src[src.index("async def run("):]
        self.assertLess(run.index("harvest_enabled()"), run.index("await plan(grid)"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
