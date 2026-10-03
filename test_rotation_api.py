"""Tests for the API-triggered rotation: atomic ticket, preview, execute.

The ticket reservation runs against a REAL SQLite database with the real
TradingBotState model, because the safeguard being tested is the UNIQUE
constraint on bot_name - a fake would simply assert that the fake behaves.
The endpoints run against a fake grid; no network, no orders.
"""
import asyncio
import os
import tempfile
import unittest

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import rotation_task
import routers.trading_dashboard as td
from models import Base


def fresh_db():
    path = tempfile.mktemp(suffix=".sqlite")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def setup():
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
    asyncio.get_event_loop().run_until_complete(setup()) if False else None
    return engine, maker, path


class ClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        self.factory = lambda: self.maker

    async def asyncTearDown(self):
        await self.engine.dispose()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    async def test_a_fresh_ticket_is_claimed(self):
        ok, state = await rotation_task.claim_once(self.factory, "rotation_api:t1")
        self.assertTrue(ok)
        self.assertEqual(state, "CLAIMED")

    async def test_TWO_SIMULTANEOUS_CLAIMS_EXACTLY_ONE_WINS(self):
        """The safeguard. The database decides, not an application check."""
        results = await asyncio.gather(
            rotation_task.claim_once(self.factory, "rotation_api:same"),
            rotation_task.claim_once(self.factory, "rotation_api:same"),
            rotation_task.claim_once(self.factory, "rotation_api:same"),
            rotation_task.claim_once(self.factory, "rotation_api:same"),
        )
        winners = [r for r in results if r[0]]
        self.assertEqual(len(winners), 1, f"expected exactly one winner, got {results}")
        for ok, state in results:
            if not ok:
                self.assertIn(state, ("IN_FLIGHT", "ALREADY_DONE", "RACED"))

    async def test_a_second_claim_after_the_first_is_refused(self):
        await rotation_task.claim_once(self.factory, "rotation_api:t2")
        ok, state = await rotation_task.claim_once(self.factory, "rotation_api:t2")
        self.assertFalse(ok)
        self.assertEqual(state, "IN_FLIGHT")

    async def test_a_run_that_wrote_rows_spends_the_ticket_forever(self):
        await rotation_task.claim_once(self.factory, "rotation_api:spent")
        self.assertEqual(await rotation_task.finish_claim(self.factory, "rotation_api:spent", 4),
                         "DONE")
        ok, state = await rotation_task.claim_once(self.factory, "rotation_api:spent")
        self.assertFalse(ok)
        self.assertEqual(state, "ALREADY_DONE")

    async def test_a_run_that_wrote_nothing_releases_the_ticket(self):
        await rotation_task.claim_once(self.factory, "rotation_api:noop")
        self.assertEqual(await rotation_task.finish_claim(self.factory, "rotation_api:noop", 0),
                         "RELEASED")
        ok, state = await rotation_task.claim_once(self.factory, "rotation_api:noop")
        self.assertTrue(ok, "a no-op ticket must be reusable")

    async def test_different_tickets_do_not_block_each_other(self):
        self.assertTrue((await rotation_task.claim_once(self.factory, "rotation_api:a"))[0])
        self.assertTrue((await rotation_task.claim_once(self.factory, "rotation_api:b"))[0])

    async def test_the_api_namespace_is_separate_from_the_boot_marker(self):
        """The spent `dip` boot ticket must not block an API ticket named dip."""
        async with self.maker() as db:
            from models import TradingBotState
            db.add(TradingBotState(bot_name=rotation_task.MARKER_PREFIX + "dip",
                                   base_capital=rotation_task.DONE))
            await db.commit()
        ok, _ = await rotation_task.claim_once(self.factory,
                                               rotation_task.API_MARKER_PREFIX + "dip")
        self.assertTrue(ok)


class FakeGrid:
    def __init__(self, plan_ok=True, apply_rows=4, blow_up=False):
        self._plan_ok = plan_ok
        self._apply_rows = apply_rows
        self._blow_up = blow_up
        self.applied = 0
        self.session_factory = None

    def get_session_factory(self):
        return self.session_factory

    async def get_grid_status(self):
        if self._blow_up:
            raise RuntimeError("venue unreachable")
        return {"branches": [
            {"product_id": "JASMY-USD", "bot_name": "g1", "allocated_usd": 193.95,
             "num_levels": 3, "slices": [], "current_price": 0.01},
            {"product_id": "APE-USD", "bot_name": "g2", "allocated_usd": 247.0,
             "num_levels": 3, "slices": [{"id": 1, "qty": 1.0, "entry_price": 1.0}],
             "current_price": 1.0},
        ]}


def fake_apply_factory(grid, rows):
    async def _apply(g, p):
        grid.applied += 1
        return {"status": "APPLIED" if rows else "NOTHING_PLACED",
                "rows_written": rows, "withdrawn_usd": 178.95, "added_usd": 178.95}
    return _apply


def call(fn, grid, **kw):
    old = td.crypto_grid_bot_module
    td.crypto_grid_bot_module = grid
    try:
        return asyncio.run(fn(**kw))
    finally:
        td.crypto_grid_bot_module = old


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        self.grid = FakeGrid()
        self.grid.session_factory = self.maker
        self._real_plan = rotation_task.build_plan
        self._real_apply = rotation_task.apply

        async def _bp(g, ranker=None, release_deployed_idle=None):
            return {"ok": True, "sources": [{"product_id": "JASMY-USD", "release_usd": 178.95}],
                    "targets": [{"product_id": "APE-USD", "add_usd": 178.95}],
                    "skipped": [], "ranking": [], "why": None}
        rotation_task.build_plan = _bp

    async def asyncTearDown(self):
        rotation_task.build_plan = self._real_plan
        rotation_task.apply = self._real_apply
        await self.engine.dispose()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _td(self, grid):
        old = td.crypto_grid_bot_module
        td.crypto_grid_bot_module = grid
        return old

    async def test_preview_moves_nothing_and_claims_nothing(self):
        old = self._td(self.grid)
        try:
            r = await td.rotation_preview_endpoint()
        finally:
            td.crypto_grid_bot_module = old
        self.assertTrue(r["moves_nothing"])
        self.assertTrue(r["ready"])
        self.assertEqual(r["would_withdraw_usd"], 178.95)
        self.assertTrue(r["balances"])
        async with self.maker() as db:
            from sqlalchemy import select
            from models import TradingBotState
            rows = (await db.execute(select(TradingBotState))).scalars().all()
        self.assertEqual(rows, [], "preview must not create a ticket row")

    async def test_execute_without_confirm_moves_nothing_and_claims_nothing(self):
        rotation_task.apply = fake_apply_factory(self.grid, 4)
        old = self._td(self.grid)
        try:
            r = await td.rotation_execute_endpoint(ticket="t", confirm=False)
        finally:
            td.crypto_grid_bot_module = old
        self.assertFalse(r["ran"])
        self.assertEqual(r["reason"], "NOT_CONFIRMED")
        self.assertEqual(self.grid.applied, 0)
        ok, _ = await rotation_task.claim_once(lambda: self.maker, "rotation_api:t")
        self.assertTrue(ok, "an unconfirmed call must not have claimed the ticket")

    async def test_execute_with_confirm_applies_once_then_refuses_the_same_ticket(self):
        rotation_task.apply = fake_apply_factory(self.grid, 4)
        old = self._td(self.grid)
        try:
            r = await td.rotation_execute_endpoint(ticket="go", confirm=True)
            self.assertTrue(r["ran"])
            self.assertEqual(r["rows_written"], 4)
            self.assertEqual(r["ticket_state"], "DONE")
            with self.assertRaises(HTTPException) as cm:
                await td.rotation_execute_endpoint(ticket="go", confirm=True)
            self.assertEqual(cm.exception.status_code, 409)
            self.assertIn("ALREADY_DONE", cm.exception.detail)
        finally:
            td.crypto_grid_bot_module = old
        self.assertEqual(self.grid.applied, 1, "must not apply twice")

    async def test_a_run_that_writes_nothing_frees_the_ticket_for_retry(self):
        rotation_task.apply = fake_apply_factory(self.grid, 0)
        old = self._td(self.grid)
        try:
            r = await td.rotation_execute_endpoint(ticket="again", confirm=True)
            self.assertFalse(r["ran"])
            self.assertEqual(r["ticket_state"], "RELEASED")
            r2 = await td.rotation_execute_endpoint(ticket="again", confirm=True)
            self.assertEqual(r2["ticket_state"], "RELEASED")
        finally:
            td.crypto_grid_bot_module = old
        self.assertEqual(self.grid.applied, 2)

    async def test_a_failure_before_writing_releases_the_ticket(self):
        async def boom(g, p):
            raise RuntimeError("withdraw refused")
        rotation_task.apply = boom
        old = self._td(self.grid)
        try:
            with self.assertRaises(HTTPException) as cm:
                await td.rotation_execute_endpoint(ticket="boom", confirm=True)
            self.assertEqual(cm.exception.status_code, 500)
            self.assertIn("released", cm.exception.detail)
        finally:
            td.crypto_grid_bot_module = old
        ok, _ = await rotation_task.claim_once(lambda: self.maker, "rotation_api:boom")
        self.assertTrue(ok, "a failed run must not burn the ticket")

    async def test_blank_ticket_is_refused(self):
        old = self._td(self.grid)
        try:
            for bad in ("", "   "):
                with self.assertRaises(HTTPException) as cm:
                    await td.rotation_execute_endpoint(ticket=bad, confirm=True)
                self.assertEqual(cm.exception.status_code, 400)
        finally:
            td.crypto_grid_bot_module = old

    async def test_stop_trading_blocks_execution(self):
        os.environ["STOP_TRADING"] = "true"
        old = self._td(self.grid)
        try:
            with self.assertRaises(HTTPException) as cm:
                await td.rotation_execute_endpoint(ticket="x", confirm=True)
            self.assertEqual(cm.exception.status_code, 409)
        finally:
            td.crypto_grid_bot_module = old
            del os.environ["STOP_TRADING"]
        self.assertEqual(self.grid.applied, 0)

    async def test_a_not_ready_plan_writes_nothing_and_frees_the_ticket(self):
        async def _bp(g, ranker=None, release_deployed_idle=None):
            return {"ok": False, "why": "no flat branch has $10.00 to move",
                    "sources": [], "targets": [], "skipped": [], "ranking": []}
        rotation_task.build_plan = _bp
        rotation_task.apply = fake_apply_factory(self.grid, 4)
        old = self._td(self.grid)
        try:
            r = await td.rotation_execute_endpoint(ticket="notready", confirm=True)
        finally:
            td.crypto_grid_bot_module = old
        self.assertFalse(r["ran"])
        self.assertEqual(r["reason"], "NOT_READY")
        self.assertEqual(r["ticket_state"], "RELEASED")
        self.assertEqual(self.grid.applied, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
