"""The read-only harvest preview: writes nothing, and can tell
"watched, nothing earned" apart from "never looked at".

Against a real SQLite database, not mocks - the whole point of the preview
is to prove whether rows exist, so a fake session factory would prove
nothing about the thing being claimed.

Every class here is IsolatedAsyncioTestCase, which gives each test its own
event loop. The first version used asyncio.get_event_loop() and passed
12/12 alone, then failed 9/12 when run alongside the other suites: another
file had closed the shared loop. A test that only passes in isolation is
not a check on anything. It also matters that these are not plain
TestCases - an `async def test_` in a plain TestCase is never awaited and
reports as a pass without executing a line.
"""
import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import profit_harvest as ph


class _Grid:
    """Minimal stand-in: the two attributes plan() actually touches."""

    def __init__(self, session_factory, branches):
        self.get_session_factory = session_factory
        self._branches = branches

    async def get_grid_status(self):
        return {"branches": self._branches}


async def _db():
    """A real file-backed SQLite DB, built in the caller's own loop
    because an aiosqlite engine is bound to the loop that created it."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from models import Base
    path = tempfile.mktemp(suffix=".db")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    return (lambda: maker), path


def _flat(bot, product, alloc):
    return {"bot_name": bot, "product_id": product,
            "allocated_usd": alloc, "slices": []}


def _held(bot, product, alloc, n=2):
    return {"bot_name": bot, "product_id": product, "allocated_usd": alloc,
            "slices": [{"id": i} for i in range(n)]}


async def _count_baselines(sf):
    from sqlalchemy import select
    from models import TradingBotState
    async with sf()() as db:
        rows = (await db.execute(select(TradingBotState).where(
            TradingBotState.bot_name.like(
                ph.BASELINE_PREFIX + "%")))).scalars().all()
        return len(rows)


async def _add_trade(sf, bot, product, pnl):
    from datetime import datetime

    from models import CryptoGridTradeHistory
    async with sf()() as db:
        db.add(CryptoGridTradeHistory(bot_name=bot, product_id=product,
                                      pnl=pnl, closed_at=datetime.utcnow()))
        await db.commit()


class TestHarvestableNoBaseline(unittest.IsolatedAsyncioTestCase):
    """harvestable() with no mark. Pure function; no database needed."""

    async def test_none_baseline_harvests_nothing(self):
        take, why = ph.harvestable(_flat("b", "X-USD", 500.0), 90.0, None)
        self.assertEqual(take, 0.0)
        self.assertIn("no baseline", why)

    async def test_none_baseline_refuses_even_with_huge_realised(self):
        """An unknown is not zero. A branch with $9,000 of lifetime realised
        profit and no baseline must harvest nothing, not $9,000."""
        take, _ = ph.harvestable(_flat("b", "X-USD", 50_000.0), 9000.0, None)
        self.assertEqual(take, 0.0)

    async def test_zero_baseline_is_not_treated_as_none(self):
        """0.0 is a real mark and must behave like one. `if not baseline`
        in place of `is None` would silently refuse every branch whose
        baseline is legitimately zero."""
        take, why = ph.harvestable(_flat("b", "X-USD", 500.0), 90.0, 0.0)
        self.assertEqual(take, 90.0)
        self.assertIsNone(why)


class TestPreviewWritesNothing(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sf, self.path = await _db()

    async def test_create_false_records_no_baseline(self):
        g = _Grid(self.sf, [_flat("crypto_grid_1", "A-USD", 300.0),
                            _held("crypto_grid_2", "B-USD", 300.0)])
        before = await _count_baselines(self.sf)
        p = await ph.plan(g, create=False)
        after = await _count_baselines(self.sf)
        self.assertEqual(before, 0)
        self.assertEqual(after, 0, "the preview wrote a baseline row")
        self.assertEqual(p["total_harvest_usd"], 0.0)

    async def test_create_false_reports_unwatched_and_loop_not_run(self):
        g = _Grid(self.sf, [_flat("crypto_grid_1", "A-USD", 300.0),
                            _flat("crypto_grid_2", "B-USD", 300.0)])
        p = await ph.plan(g, create=False)
        self.assertEqual(p["unwatched_branches"], 2)
        self.assertFalse(p["loop_has_run"])
        for r in p["branches"]:
            self.assertIsNone(r["baseline"])
            self.assertIsNone(r["earned_since_baseline"])

    async def test_create_true_still_writes_baselines(self):
        """The hourly loop's behaviour must be unchanged by this."""
        g = _Grid(self.sf, [_flat("crypto_grid_1", "A-USD", 300.0),
                            _flat("crypto_grid_2", "B-USD", 300.0)])
        await ph.plan(g, create=True)
        self.assertEqual(await _count_baselines(self.sf), 2)

    async def test_default_is_create_true(self):
        """plan(grid) with no argument must keep writing, because that is
        what main.py calls."""
        g = _Grid(self.sf, [_flat("crypto_grid_1", "A-USD", 300.0)])
        await ph.plan(g)
        self.assertEqual(await _count_baselines(self.sf), 1)


class TestPreviewReadsTruthfully(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sf, self.path = await _db()

    async def test_watched_with_nothing_earned_differs_from_unwatched(self):
        """The distinction the endpoint exists for."""
        g = _Grid(self.sf, [_flat("crypto_grid_1", "A-USD", 300.0),
                            _flat("crypto_grid_2", "B-USD", 300.0)])
        await ph._baseline(self.sf, "crypto_grid_1", 0.0)
        p = await ph.plan(g, create=False)
        by = {r["bot_name"]: r for r in p["branches"]}
        self.assertEqual(by["crypto_grid_1"]["baseline"], 0.0)
        self.assertEqual(by["crypto_grid_1"]["earned_since_baseline"], 0.0)
        self.assertIsNone(by["crypto_grid_2"]["baseline"])
        self.assertEqual(p["unwatched_branches"], 1)
        self.assertTrue(p["loop_has_run"])

    async def test_preview_shows_real_earned_profit_after_baseline(self):
        g = _Grid(self.sf, [_flat("crypto_grid_1", "A-USD", 300.0)])
        await ph._baseline(self.sf, "crypto_grid_1", 0.0)
        await _add_trade(self.sf, "crypto_grid_1", "A-USD", 42.0)
        p = await ph.plan(g, create=False)
        r = p["branches"][0]
        self.assertEqual(r["realised_total"], 42.0)
        self.assertEqual(r["earned_since_baseline"], 42.0)
        self.assertEqual(r["harvest_usd"], 42.0)
        self.assertEqual(p["total_harvest_usd"], 42.0)
        self.assertTrue(p["ready"])

    async def test_preview_respects_the_flat_requirement(self):
        g = _Grid(self.sf, [_held("crypto_grid_1", "A-USD", 300.0, n=3)])
        await ph._baseline(self.sf, "crypto_grid_1", 0.0)
        await _add_trade(self.sf, "crypto_grid_1", "A-USD", 42.0)
        p = await ph.plan(g, create=False)
        self.assertEqual(p["branches"][0]["harvest_usd"], 0.0)
        self.assertIn("flat", p["branches"][0]["why_not"])

    async def test_preview_respects_the_keep_alive_floor(self):
        g = _Grid(self.sf, [_flat("crypto_grid_1", "A-USD", 20.0)])
        await ph._baseline(self.sf, "crypto_grid_1", 0.0)
        await _add_trade(self.sf, "crypto_grid_1", "A-USD", 42.0)
        p = await ph.plan(g, create=False)
        self.assertEqual(p["branches"][0]["harvest_usd"], 0.0)
        self.assertIn("keep-alive", p["branches"][0]["why_not"])

    async def test_empty_fleet_does_not_claim_the_loop_ran(self):
        g = _Grid(self.sf, [])
        p = await ph.plan(g, create=False)
        self.assertFalse(p["loop_has_run"])
        self.assertEqual(p["unwatched_branches"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
