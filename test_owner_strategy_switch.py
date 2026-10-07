"""The owner's one-time switch to mean-reversion (2026-10-07).

It must take effect on the first restart, and must NEVER override a later
dashboard flip back to momentum."""
import asyncio
import os
import tempfile
from unittest import mock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models
import prop_bot


def _run(coro_fn):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        with mock.patch.object(prop_bot, "AsyncSessionLocal", factory):
            return await coro_fn()
    try:
        return asyncio.run(go())
    finally:
        os.unlink(path)


def test_first_start_switches_to_mean_reversion():
    async def go():
        assert await prop_bot.get_live_strategy_family() == "momentum"
        assert await prop_bot.apply_owner_strategy_switch_once() is True
        return await prop_bot.get_live_strategy_family()
    assert _run(go) == "mean_reversion"
    assert prop_bot.APEX_MANDATE["entry"] is prop_bot.MEAN_REVERSION_ENTRY


def test_overrides_an_explicit_momentum_row():
    async def go():
        await prop_bot.set_live_strategy_family("momentum")
        await prop_bot.apply_owner_strategy_switch_once()
        return await prop_bot.get_live_strategy_family()
    assert _run(go) == "mean_reversion"


def test_never_overrides_a_later_flip_back():
    async def go():
        await prop_bot.apply_owner_strategy_switch_once()
        await prop_bot.set_live_strategy_family("momentum")      # owner flips back
        changed = await prop_bot.apply_owner_strategy_switch_once()  # next restart
        return changed, await prop_bot.get_live_strategy_family()
    assert _run(go) == (False, "momentum")


def test_switch_runs_before_the_startup_banner():
    import inspect
    src = inspect.getsource(prop_bot.run)
    assert src.index("apply_owner_strategy_switch_once") < src.index("get_live_strategy_family()")


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
