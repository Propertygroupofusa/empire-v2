"""Profit ratchet: lock gains in 5% tiers; below the lock, no new buys."""
import asyncio
import inspect
import os
import tempfile
from unittest import mock

import profit_ratchet as pr


def test_first_reading_is_the_baseline():
    assert pr.advance(None, 10_000) == (10_000, 0)


def test_steps_up_only_on_a_full_tier():
    assert pr.advance(10_000, 10_499) == (10_000, 0)
    assert pr.advance(10_000, 10_500) == (10_500, 1)


def test_big_move_crosses_several_tiers_and_compounds():
    floor, n = pr.advance(10_000, 11_600)
    assert n == 3 and floor == 11_576.25          # 10,000 * 1.05^3


def test_never_steps_down():
    assert pr.advance(10_500, 9_000) == (10_500, 0)


def test_unreadable_net_worth_changes_nothing():
    assert pr.advance(10_500, None) == (10_500, 0)
    assert pr.advance(10_500, 0) == (10_500, 0)


def test_blocks_only_below_the_floor():
    assert pr.blocks_buys(10_500, 10_499.99) is True
    assert pr.blocks_buys(10_500, 10_500) is False
    assert pr.blocks_buys(None, 5) is False


def _db():
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import models
    eng = create_async_engine(f"sqlite+aiosqlite:///{path}")
    asyncio.run(_create(eng, models))
    return async_sessionmaker(eng, expire_on_commit=False)


async def _create(eng, models):
    async with eng.begin() as c:
        await c.run_sync(models.Base.metadata.create_all)


def test_refresh_end_to_end():
    import crypto_grid_bot as g
    factory = _db()
    readings = iter([10_000, 10_600, 10_200, None, None])
    events = []
    clock = {"t": 1000.0}

    async def nw(_s): return next(readings)
    async def act(*a): events.append(a[2])

    async def run():
        out = []
        for _ in range(5):
            clock["t"] += 400                      # past the 60s throttle
            st = await g.refresh_profit_ratchet(None)
            out.append((st["floor"], st["blocked"]))
        return out

    g._profit_ratchet.update(checked_at=0.0, read_at=0.0, net_worth=None, floor=None, blocked=False)
    with mock.patch.object(g, "get_session_factory", lambda: factory), \
         mock.patch.object(g, "read_crypto_net_worth", nw), \
         mock.patch.object(g, "_log_activity_safe", act), \
         mock.patch("time.time", lambda: clock["t"]):
        res = asyncio.run(run())
    assert res[0] == (10_000, False)               # baseline
    assert res[1] == (10_500, False)               # tier locked at 10,500
    assert res[2] == (10_500, True)                # 10,200 < floor: buys paused
    assert res[3] == (10_500, True)                # one blip: last good reading stands (still below)
    assert res[4][1] is True                       # stale beyond 10 min: paused
    assert events.count("TIER_LOCK") == 2 and "RATCHET_PAUSE" in events


def test_switched_off_never_blocks():
    import crypto_grid_bot as g
    factory = _db()

    async def nw(_s): return 5.0
    async def act(*a): pass

    async def run():
        await g._ratchet_state_set(g.PROFIT_RATCHET_FLOOR_KEY, 10_000)
        await g.set_profit_ratchet_active(False)
        return await g.refresh_profit_ratchet(None, force=True)

    with mock.patch.object(g, "get_session_factory", lambda: factory), \
         mock.patch.object(g, "read_crypto_net_worth", nw), \
         mock.patch.object(g, "_log_activity_safe", act):
        st = asyncio.run(run())
    assert st["blocked"] is False


def test_gate_blocks_buys_and_auto_deploy_but_not_sells():
    import crypto_grid_bot as g
    cyc = inspect.getsource(g.run_grid_branch_cycle)
    assert "if drawdown_breached or _sell_only or _ratchet_blocked:" in cyc
    assert "_profit_ratchet.get(\"blocked\")" in inspect.getsource(g._auto_deploy_idle_free_cash)
    # the sell path is not conditioned on the ratchet
    sell_part = cyc[cyc.index("if drawdown_breached or _sell_only or _ratchet_blocked:"):]
    assert "_ratchet_blocked" not in sell_part.split("\n", 1)[1]
