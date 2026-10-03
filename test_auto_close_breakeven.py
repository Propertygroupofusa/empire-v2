"""The 10-day max hold must not sell a losing position for its age alone."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest import mock

import routers.trading_dashboard as td

D = td.auto_close_decision


def test_profit_target_closes_at_any_age():
    assert D(0.081, 1, profit_pct=0.08, max_hold_days=10, aged_require_breakeven=True) == (True, "profit target")


def test_aged_and_losing_is_held():
    assert D(-0.06, 12, profit_pct=0.08, max_hold_days=10, aged_require_breakeven=True) == \
        (False, "aged, waiting for breakeven")


def test_aged_at_breakeven_or_better_closes():
    assert D(0.0, 10, profit_pct=0.08, max_hold_days=10, aged_require_breakeven=True) == (True, "max hold")
    assert D(0.02, 15, profit_pct=0.08, max_hold_days=10, aged_require_breakeven=True) == (True, "max hold")


def test_switch_off_restores_close_at_any_pnl():
    assert D(-0.06, 12, profit_pct=0.08, max_hold_days=10, aged_require_breakeven=False) == (True, "max hold")


def test_young_position_untouched():
    assert D(0.03, 2, profit_pct=0.08, max_hold_days=10, aged_require_breakeven=True) == (False, None)


def test_unknown_age_never_triggers_max_hold():
    assert D(-0.5, None, profit_pct=0.08, max_hold_days=10, aged_require_breakeven=True) == (False, None)


class _Del:
    def __init__(self, log, url): log.append(url)
    async def __aenter__(self):
        r = mock.Mock(); r.status = 200
        async def t(): return ""
        r.text = t
        return r
    async def __aexit__(self, *a): return False


class _S:
    def __init__(self): self.deleted = []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def delete(self, url, **k): return _Del(self.deleted, url)


def test_loop_closes_only_the_right_positions():
    now = datetime.now(timezone.utc)
    positions = [
        {"symbol": "SLV", "qty": "3", "avg_entry_price": "30", "current_price": "28.2", "unrealized_plpc": "-0.06"},
        {"symbol": "GLD", "qty": "1", "avg_entry_price": "400", "current_price": "404", "unrealized_plpc": "0.01"},
        {"symbol": "NVDA", "qty": "1", "avg_entry_price": "100", "current_price": "109", "unrealized_plpc": "0.09"},
        {"symbol": "QQQ", "qty": "1", "avg_entry_price": "700", "current_price": "690", "unrealized_plpc": "-0.014"},
    ]
    opened = {"SLV": now - timedelta(days=12), "GLD": now - timedelta(days=11),
              "NVDA": now - timedelta(days=1), "QQQ": now - timedelta(days=2)}
    s = _S()

    async def is_open(_s): return True
    async def pos(_s): return positions
    async def opened_at(_s, sym): return opened[sym].isoformat()

    class _NoDB:
        async def __aenter__(self): raise RuntimeError("no db in test")
        async def __aexit__(self, *a): return False

    with mock.patch.object(td, "ALPACA_KEY", "k"), mock.patch.object(td, "ALPACA_SECRET", "s"), \
         mock.patch.object(td, "ALPACA_AUTO_CLOSE_AGED_REQUIRE_BREAKEVEN", True), \
         mock.patch.object(td, "ALPACA_AUTO_CLOSE_PROFIT_PCT", 0.08), \
         mock.patch.object(td, "ALPACA_AUTO_CLOSE_MAX_HOLD_DAYS", 10.0), \
         mock.patch.object(td.aiohttp, "ClientSession", lambda: s), \
         mock.patch.object(td, "_is_market_open", is_open), \
         mock.patch.object(td, "_fetch_alpaca_positions", pos), \
         mock.patch.object(td, "_fetch_position_opened_at", opened_at), \
         mock.patch.object(td, "AsyncSessionLocal", _NoDB):
        asyncio.run(td.check_and_auto_close_positions())
    closed = sorted(u.split("/v2/positions/")[1].split("?")[0] for u in s.deleted)
    assert closed == ["GLD", "NVDA"]          # SLV aged but losing: held. QQQ young: untouched.


def test_startup_summary_is_accurate():
    with mock.patch.object(td, "ALPACA_PROFIT_SKIM_PCT", 0.0), \
         mock.patch.object(td, "ALPACA_AUTO_CLOSE_AGED_REQUIRE_BREAKEVEN", True):
        s = td.auto_close_summary()
    assert "no profit skim" in s and "breakeven" in s and "10%" not in s
