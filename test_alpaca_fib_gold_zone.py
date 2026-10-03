"""The Alpaca gold-zone backtest: shared rules, Alpaca costs, live comparison."""
import asyncio
from unittest import mock

import pytest

import alpaca_selection_backtest as asb
import fib_gold_zone as fz
from test_fib_gold_zone import bars, setup_series


def _winning_series():
    return setup_series() + [(121, 116, 117), (119, 117, 118), (123, 118, 122.5)]


def test_no_commission_on_the_limit_legs():
    h, l, c = bars(_winning_series())
    t = fz.replay(h, l, c, ratio=0.5, maker_fee=0.0, taker_fee=asb.ALPACA_FIB_SLIPPAGE)[0]
    assert t["reason"] == "TARGET"
    assert t["net_pct"] == pytest.approx(122 / 116 - 1)


def test_stop_is_charged_slippage():
    h, l, c = bars(setup_series() + [(121, 116, 117), (117, 109, 109.5)])
    t = fz.replay(h, l, c, ratio=0.5, maker_fee=0.0, taker_fee=asb.ALPACA_FIB_SLIPPAGE)[0]
    assert t["reason"] == "STOP"
    assert t["net_pct"] == pytest.approx(110 * (1 - 0.0005) / 116 - 1)
    assert t["net_pct"] < t["gross_pct"]


def test_fetch_follows_pagination():
    pages = [
        {"bars": [{"h": 2, "l": 1, "c": 1.5}] * 20, "next_page_token": "abc"},
        {"bars": [{"h": 3, "l": 2, "c": 2.5}] * 20, "next_page_token": None},
    ]
    seen = []

    class Resp:
        status = 200
        def __init__(self, d): self.d = d
        async def json(self): return self.d
        async def text(self): return ""
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class Sess:
        def get(self, url, **kw):
            seen.append(url)
            return Resp(pages[len(seen) - 1])

    with mock.patch.object(asb, "get_headers", lambda: {}):
        got, err = asyncio.run(asb._fetch_ohlc(Sess(), "SPY", "15Min", 30))
    assert err is None and len(got[2]) == 40
    assert "page_token=abc" in seen[1] and "feed=iex" in seen[0]


def test_runner_compares_against_the_live_strategy_on_the_same_bars():
    h, l, c = bars(_winning_series() * 3)
    calls = []

    async def fake_fetch(session, symbol, timeframe, days):
        calls.append((symbol, timeframe))
        return (list(h), list(l), list(c)), None

    async def family():
        return "momentum"

    seen_live = []

    def fake_live(closes, ticker, fam):
        seen_live.append((len(closes), ticker, fam))
        return [{"pnl_usd": 1.0}, {"pnl_usd": -0.5}]

    with mock.patch.object(asb, "_fetch_ohlc", fake_fetch), \
         mock.patch.object(asb, "_live_strategy_family", family), \
         mock.patch.object(asb, "_replay_for_live_family", fake_live):
        out = asyncio.run(asb.run_fib_gold_zone_backtest(contract_codes=["MES", "MNQ"]))

    assert out["shadow_mode"] is True
    assert {tf for _s, tf in calls} == {"1Min", "15Min", "1Hour", "4Hour"}
    assert {s["timeframe"] for s in out["summary"]} == {"1m", "15m", "1h", "4h"}
    # Live strategy replayed once per symbol, on the 15-minute closes only.
    assert len(seen_live) == 2 and all(n == len(c) and f == "momentum" for n, _t, f in seen_live)
    live = out["live_on_same_15m_30d"]
    assert live["trades"] == 4 and live["total_net_usd"] == 1.0 and live["strategy_family"] == "momentum"
    assert out["best_fib_on_15m"]["timeframe"] == "15m"
    assert isinstance(out["fib_beats_live"], bool)
    assert out["costs"]["commission"] == 0.0


def test_a_failed_fetch_is_reported_not_crashed():
    async def fake_fetch(session, symbol, timeframe, days):
        return None, "HTTP 429: slow down"

    async def family():
        return "mean_reversion"

    with mock.patch.object(asb, "_fetch_ohlc", fake_fetch), \
         mock.patch.object(asb, "_live_strategy_family", family):
        out = asyncio.run(asb.run_fib_gold_zone_backtest(contract_codes=["MES"]))
    assert out["summary"] == [] and out["best_fib_on_15m"] is None
    assert out["fib_beats_live"] is False
    assert any("429" in e for e in out["per_coin"][0]["errors"])
