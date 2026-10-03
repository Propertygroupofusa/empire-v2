"""Alpaca symbol exclusion counts DAYS of evidence, not restarts.

The once-a-day backtest's clock lived in memory, so every deploy re-ran
it: ~700 runs where a daily schedule makes ~40. "Excluded after 3
negative runs" then meant "after 3 deploys". These pin the fix.
"""
import asyncio
import inspect
import os
import tempfile
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import prop_bot


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    return engine, async_sessionmaker(engine, expire_on_commit=False), path


async def _seed(factory, rows):
    async with factory() as db:
        for pid, roi, when in rows:
            db.add(models.AlpacaBacktestRun(product_id=pid, num_trades=5, win_rate=40.0,
                                            roi_pct_of_spend=roi, run_at=when))
        await db.commit()


def _run(rows, coro_fn):
    engine, factory, path = _db()

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        await _seed(factory, rows)
        with mock.patch.object(prop_bot, "AsyncSessionLocal", factory), \
             mock.patch.object(prop_bot, "TOP_N_ELIGIBLE_SYMBOLS", 99):
            return await coro_fn()

    try:
        return asyncio.run(go())
    finally:
        os.unlink(path)


NOW = datetime.utcnow()


def test_three_reruns_on_one_day_count_once():
    rows = [("SPY", -2.0, NOW - timedelta(minutes=m)) for m in (1, 20, 40)]
    assert "SPY" not in _run(rows, prop_bot.get_effective_excluded_symbols)


def test_three_negative_days_still_exclude():
    rows = [("SPY", -2.0, NOW - timedelta(days=d, minutes=5)) for d in (0, 1, 2)]
    assert "SPY" in _run(rows, prop_bot.get_effective_excluded_symbols)
    reason = _run(rows, lambda: prop_bot.describe_symbol_exclusion_reason("SPY"))
    assert "scored days" in reason


def test_one_good_day_breaks_the_streak_even_after_bad_reruns():
    rows = ([("QQQ", -1.0, NOW - timedelta(minutes=m)) for m in (1, 2, 3)]
            + [("QQQ", 4.0, NOW - timedelta(days=1))]
            + [("QQQ", -1.0, NOW - timedelta(days=2))])
    assert "QQQ" not in _run(rows, prop_bot.get_effective_excluded_symbols)


def test_latest_run_of_the_day_is_the_one_that_counts():
    rows = ([("GLD", 3.0, NOW - timedelta(minutes=1)),      # latest today: positive
             ("GLD", -3.0, NOW - timedelta(minutes=30))]
            + [("GLD", -3.0, NOW - timedelta(days=d)) for d in (1, 2)])
    assert "GLD" not in _run(rows, prop_bot.get_effective_excluded_symbols)


def test_last_run_age_is_read_from_the_table():
    age = _run([("SPY", 1.0, NOW - timedelta(hours=2))], prop_bot._seconds_since_last_backtest)
    assert 2 * 3600 - 60 < age < 2 * 3600 + 60
    assert _run([], prop_bot._seconds_since_last_backtest) is None


def test_the_schedule_consults_the_table_before_rerunning():
    src = inspect.getsource(prop_bot.run_prop_cycle)
    i = src.index("_seconds_since_last_backtest()")
    assert i < src.index("_run_scheduled_backtest_and_update_exclusions()")


def test_layers_are_reported_separately():
    async def layers():
        with mock.patch.object(prop_bot, "TOP_N_ELIGIBLE_SYMBOLS", 1):
            return await prop_bot._exclusion_layers()
    rows = ([("SPY", -2.0, NOW - timedelta(days=d)) for d in (0, 1, 2)]
            + [("QQQ", 9.0, NOW), ("GLD", 1.0, NOW)])
    negative, outside = _run(rows, layers)
    assert "SPY" in negative and "SPY" not in outside
    assert "GLD" in outside and "QQQ" not in outside
