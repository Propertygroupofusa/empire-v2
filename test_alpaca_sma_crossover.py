"""Moving-average crossover added to the Alpaca 3-window strategy check
(owner, 2026-10-07). It is judged against the live strategy, never wired
live by this change."""
import asyncio
from unittest import mock

import alpaca_selection_backtest as bt


def _ramp(start, step, n):
    return [start + step * i for i in range(n)]


def test_buys_on_cross_up_and_sells_on_cross_down():
    closes = [100.0] * 40 + _ramp(100.5, 0.5, 30) + _ramp(114.5, -0.5, 40)
    trades = bt._replay_symbol_sma_crossover(closes)
    assert len(trades) == 1 and trades[0]["exit"] == "CROSS_DOWN"
    assert trades[0]["pnl_usd"] > 0


def test_no_entry_without_a_cross():
    assert bt._replay_symbol_sma_crossover(_ramp(100, 0.1, 200)) == []   # already above, never crosses
    assert bt._replay_symbol_sma_crossover([100.0] * 200) == []


def test_hard_stop_caps_the_loss():
    # Cross up, then a sudden 5% drop: exits at the 1.5% stop check, not
    # after waiting for the averages to cross back.
    closes = [100.0] * 40 + [101.0, 102.0] + [96.0] * 10
    trades = bt._replay_symbol_sma_crossover(closes)
    assert trades and trades[0]["exit"] == "STOP"
    assert abs(trades[0]["pnl_pct"]) < 0.06


def test_open_position_marked_at_end():
    closes = [100.0] * 40 + _ramp(100.5, 0.5, 10)
    trades = bt._replay_symbol_sma_crossover(closes)
    assert trades[-1]["exit"] == "OPEN_AT_END"


def test_multi_window_reports_four_strategies_and_keeps_old_keys():
    async def fake_fetch(session, ticker, days, end=None):
        return [100.0] * 40 + _ramp(100.5, 0.5, 30) + _ramp(114.5, -0.5, 40), None
    with mock.patch.object(bt, "_fetch_bars", fake_fetch):
        out = asyncio.run(bt.run_momentum_vs_mean_reversion_multi_window(
            contract_codes=["MES"], num_windows=2))
    s = out["summary"]
    for k in ("mean_reversion", "momentum", "reverse_momentum", "sma_crossover"):
        assert f"{k}_windows_won" in s and f"{k}_total_pnl" in s
        assert k in out["windows"][0]
    assert out["windows"][0]["sma_crossover"]["num_trades"] == 1
    assert sum(s[f"{k}_windows_won"] for k in
               ("mean_reversion", "momentum", "reverse_momentum", "sma_crossover")) <= 2


def test_tie_credits_nobody():
    async def flat(session, ticker, days, end=None):
        return [100.0] * 200, None
    with mock.patch.object(bt, "_fetch_bars", flat):
        out = asyncio.run(bt.run_momentum_vs_mean_reversion_multi_window(
            contract_codes=["MES"], num_windows=1))
    assert all(v == 0 for k, v in out["summary"].items() if k.endswith("_windows_won"))


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
