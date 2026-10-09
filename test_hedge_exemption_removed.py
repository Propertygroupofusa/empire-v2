"""Owner, 2026-10-09: SH/DOG/RWM no longer bypass the top-N ROI filter.
In a rising market they rank low and must sit out; when a decline lifts
their backtest ROI into the top N they come back on their own."""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from unittest import mock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import prop_bot


def _excluded(rois, top_n=3):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        async with factory() as db:
            now = datetime.utcnow()
            for sym, roi in rois.items():
                db.add(models.AlpacaBacktestRun(product_id=sym, num_trades=5, win_rate=50.0,
                                                roi_pct_of_spend=roi, run_at=now - timedelta(hours=1)))
            await db.commit()
        with mock.patch.object(prop_bot, "AsyncSessionLocal", factory), \
             mock.patch.object(prop_bot, "TOP_N_ELIGIBLE_SYMBOLS", top_n):
            ex = await prop_bot.get_effective_excluded_symbols()
            reason = await prop_bot.describe_symbol_exclusion_reason("SH")
            return ex, reason
    try:
        return asyncio.run(go())
    finally:
        os.unlink(path)


RISING = {"SPY": 4.0, "QQQ": 5.0, "AAPL": 6.0, "META": 3.0, "SH": -2.0, "DOG": -1.5, "RWM": -3.0}
FALLING = {"SPY": -4.0, "QQQ": -5.0, "AAPL": -2.0, "META": -3.0, "SH": 4.0, "DOG": 3.5, "RWM": 5.0}


def test_exemption_is_empty():
    assert prop_bot.INDEX_HEDGE_SYMBOLS == set()


def test_rising_market_sits_the_hedges_out():
    ex, reason = _excluded(RISING)
    assert {"SH", "DOG", "RWM"} <= ex
    assert "outside the current top" in reason
    assert not ({"QQQ", "AAPL", "SPY"} & ex)


def test_falling_market_brings_them_back_automatically():
    ex, _ = _excluded(FALLING)
    assert not ({"SH", "DOG", "RWM"} & ex)


def test_swing_bot_follows_the_same_exclusions():
    import alpaca_swing_bot as sb

    async def fake_excluded():
        return {"SH", "DOG", "SPY"}
    with mock.patch.object(prop_bot, "get_effective_excluded_symbols", fake_excluded):
        got = asyncio.run(sb._excluded_hedges())
    assert got == {"SH", "DOG"}          # only falling-market bets; SPY is not its concern


def test_swing_bot_fails_closed_on_hedges():
    import alpaca_swing_bot as sb

    async def broken():
        raise RuntimeError("db down")
    with mock.patch.object(prop_bot, "get_effective_excluded_symbols", broken):
        got = asyncio.run(sb._excluded_hedges())
    assert {"SH", "DOG", "RWM"} <= got


def test_swing_bot_checks_it_in_both_entry_loops():
    import inspect
    import alpaca_swing_bot as sb
    for fn in (sb.run_swing_check, sb.run_intraday_check):
        src = inspect.getsource(fn)
        assert "excluded_hedges = await _excluded_hedges()" in src, fn.__name__
        assert "if proxy in excluded_hedges" in src, fn.__name__


if __name__ == "__main__":
    import sys
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try:
                f(); print("PASS", n)
            except Exception as e:
                fails += 1; print("FAIL", n, repr(e))
    sys.exit(1 if fails else 0)
