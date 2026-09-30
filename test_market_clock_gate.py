"""New Alpaca entries only while the market is open; exits never gated.

A DAY market order sent while the market is closed is queued and filled at
the next open, at the opening price - not the price the decision was made
on. The bot ran entries around the clock on bars hundreds of minutes old.
"""
import asyncio
import inspect

import prop_bot


class _R:
    def __init__(self, status, body): self.status, self._b = status, body
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self): return self._b


class _S:
    def __init__(self, status, body): self.status, self.body, self.n = status, body, 0
    def get(self, *a, **k):
        self.n += 1
        return _R(self.status, self.body)


def _reason(status, body):
    prop_bot._market_clock_cache.update(at=0.0, is_open=None, next_open=None)
    s = _S(status, body)
    return asyncio.run(prop_bot.market_entry_block_reason(s)), s


def test_open_market_allows_entries():
    r, _ = _reason(200, {"is_open": True})
    assert r is None


def test_closed_market_blocks_and_explains_the_queued_fill():
    r, _ = _reason(200, {"is_open": False, "next_open": "2026-10-01T09:30:00-04:00"})
    assert r.startswith(prop_bot.MARKET_CLOSED_PREFIX)
    assert "queued" in r and "2026-10-01" in r


def test_unreadable_clock_fails_closed():
    r, _ = _reason(503, {})
    assert r is not None and "could not be read" in r
    assert not r.startswith(prop_bot.MARKET_CLOSED_PREFIX)   # a real warning, not the quiet path


def test_clock_is_cached_across_subsystems():
    prop_bot._market_clock_cache.update(at=0.0, is_open=None, next_open=None)
    s = _S(200, {"is_open": True})
    asyncio.run(prop_bot.market_entry_block_reason(s))
    asyncio.run(prop_bot.market_entry_block_reason(s))
    assert s.n == 1


def test_all_three_entry_paths_consult_the_clock():
    src = inspect.getsource(prop_bot)
    assert src.count("await market_entry_block_reason(session)") == 3
    cyc = inspect.getsource(prop_bot.run_prop_cycle)
    # the gate only sets entries_halted, which try_open reads; exits never read it
    assert "entries_halted = await market_entry_block_reason(session)" in cyc


def test_order_is_a_day_market_order():
    # The reason this gate exists: a DAY market order is queued when closed.
    src = inspect.getsource(prop_bot.execute_futures_trade)
    assert 'time_in_force = "day"' in src and '"type": "market"' in src
