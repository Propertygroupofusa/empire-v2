"""Tests for the Fibonacci gold-zone shadow backtest.

Hand-built candles so every number is checkable by eye. The failures that
would make this backtest lie are lookahead, crediting a target before the
stop when both land in one bar, and forgetting fees - tested first.
"""
import asyncio
from unittest import mock

import pytest

import fib_gold_zone as fz

K = 3


def bars(points):
    """points: list of (high, low, close)."""
    return [p[0] for p in points], [p[1] for p in points], [p[2] for p in points]


def setup_series():
    """low 100 -> high 120 -> higher low 110 -> break above 120.

    Index layout (k=3): pivot low at 3 (100), pivot high at 9 (120),
    pivot low at 15 (110). Break of structure on bar 20 (close 121, high 122).
    """
    pts = []
    pts += [(105, 103, 104)] * 3            # 0-2
    pts += [(102, 100, 101)]                # 3  pivot low 100
    pts += [(106 + i, 104 + i, 105 + i) for i in range(5)]   # 4-8 rising
    pts += [(120, 115, 118)]                # 9  pivot high 120
    pts += [(117 - i * 0.5, 113 - i * 0.5, 115 - i * 0.5) for i in range(5)]   # 10-14 falling, lows 113..111
    pts += [(112, 110, 111)]                # 15 pivot low 110 (higher low)
    pts += [(114 + i, 112 + i, 113 + i) for i in range(4)]   # 16-19 rising, no close > 120
    pts += [(122, 119, 121)]                # 20 break of structure
    return pts


def test_pivots_are_never_used_before_they_are_confirmed():
    pts = setup_series()
    # Truncate right after the break: if the code peeked ahead it would
    # need bars that do not exist. It must still set up from confirmed pivots.
    h, l, c = bars(pts + [(122, 121, 121.5)])
    assert fz.replay(h, l, c, ratio=0.5) == []
    # The pivot low at 15 is only confirmed at 18; a break on bar 17 must not count.
    early = setup_series()[:17] + [(125, 121, 124)]
    h, l, c = bars(early)
    assert fz.replay(h, l, c, ratio=0.5, k=K) == []


def test_winning_trade_hits_target_with_maker_fees():
    pts = setup_series()
    # H=122, HL=110, .5 level = 116. Pull back to it, then rally to 122.
    pts += [(121, 116, 117), (119, 117, 118), (123, 118, 122.5)]
    h, l, c = bars(pts)
    trades = fz.replay(h, l, c, ratio=0.5)
    assert len(trades) == 1
    t = trades[0]
    assert t["entry"] == pytest.approx(116.0)
    assert t["exit"] == pytest.approx(122.0)
    assert t["reason"] == "TARGET"
    expected = 122 * (1 - fz.DEFAULT_MAKER_FEE) / (116 * (1 + fz.DEFAULT_MAKER_FEE)) - 1
    assert t["net_pct"] == pytest.approx(expected)
    assert t["net_pct"] < t["gross_pct"]


def test_losing_trade_pays_taker_on_the_stop():
    pts = setup_series()
    pts += [(121, 116, 117), (117, 109, 109.5)]
    h, l, c = bars(pts)
    t = fz.replay(h, l, c, ratio=0.5)[0]
    assert t["reason"] == "STOP"
    assert t["exit"] == pytest.approx(110.0)
    expected = 110 * (1 - fz.DEFAULT_TAKER_FEE) / (116 * (1 + fz.DEFAULT_MAKER_FEE)) - 1
    assert t["net_pct"] == pytest.approx(expected)


def test_stop_and_target_in_one_bar_counts_as_stop():
    pts = setup_series()
    pts += [(121, 116, 117), (125, 108, 120)]
    h, l, c = bars(pts)
    assert fz.replay(h, l, c, ratio=0.5)[0]["reason"] == "STOP"


def test_target_is_never_credited_in_the_entry_bar():
    pts = setup_series()
    pts += [(124, 116, 123)]                 # fills at 116 and spikes past 122 in one bar
    h, l, c = bars(pts)
    trades = fz.replay(h, l, c, ratio=0.5)
    assert len(trades) == 1 and trades[0]["reason"] == "OPEN_AT_END"


def test_entry_bar_stop_is_a_loss():
    pts = setup_series()
    pts += [(121, 108, 109)]
    h, l, c = bars(pts)
    t = fz.replay(h, l, c, ratio=0.5)[0]
    assert t["reason"] == "STOP" and t["net_pct"] < 0


def test_setup_cancelled_by_timeout():
    pts = setup_series()
    pts += [(122, 119, 120.5)] * 10           # never pulls back to 116
    pts += [(121, 115, 116)]                 # would fill, but too late
    h, l, c = bars(pts)
    assert fz.replay(h, l, c, ratio=0.5, setup_timeout_bars=5) == []


def test_deeper_ratio_enters_lower():
    pts = setup_series()
    pts += [(121, 114, 115), (123, 115, 122.5)]
    h, l, c = bars(pts)
    t = fz.replay(h, l, c, ratio=0.618)[0]
    assert t["entry"] == pytest.approx(122 - 0.618 * 12)
    assert t["reason"] == "TARGET"


def test_summary_break_even_and_totals():
    trades = [{"net_pct": 0.04, "gross_pct": 0.05, "swing_pct": 0.1, "reason": "TARGET"},
              {"net_pct": -0.06, "gross_pct": -0.05, "swing_pct": 0.1, "reason": "STOP"}]
    s = fz.summarize(trades, stake_usd=100)
    assert s["trades"] == 2 and s["win_rate_pct"] == 50.0
    assert s["total_net_usd"] == pytest.approx(-2.0)
    assert s["break_even_win_rate_pct"] == 60.0
    assert s["exits"] == {"TARGET": 1, "STOP": 1}
    assert fz.summarize([])["trades"] == 0


def test_aggregate_hourly_into_4h_drops_partial_buckets():
    t = [i * 3600 for i in range(10)]        # buckets [0-3],[4-7],[8-9 partial]
    h = [float(i + 1) for i in range(10)]
    lo = [float(i) for i in range(10)]
    c = [i + 0.5 for i in range(10)]
    ot, oh, ol, oc = fz.aggregate(t, h, lo, c, 4 * 3600)
    assert ot == [0, 14400]
    assert oh == [4.0, 8.0] and ol == [0.0, 4.0] and oc == [3.5, 7.5]


def test_runner_reports_all_timeframes_and_the_grid_comparison():
    import crypto_selection_backtest as csb
    pts = setup_series() + [(121, 116, 117), (119, 117, 118), (123, 118, 122.5)]
    pts = pts * 3
    h, l, c = bars(pts)
    times = [i * 3600 for i in range(len(c))]

    async def fake_fetch(session, pid, start, end, min_candles=0, last_error_out=None, granularity=None):
        return list(c), list(h), list(l), [t * (granularity or 3600) // 3600 for t in times]

    with mock.patch.object(csb, "fetch_candles_window", fake_fetch), \
         mock.patch.object(csb, "_live_matching_grid_pct", lambda *a: 0.01):
        out = asyncio.run(csb.run_fib_gold_zone_backtest(product_ids=["DOGE-USD", "XRP-USD"]))
    assert out["shadow_mode"] is True
    labels = {s["timeframe"] for s in out["summary"]}
    assert labels == {"1m", "15m", "1h", "4h"}
    assert out["grid_on_same_1h_90d"]["coins"] == 2
    one_h = [s for s in out["summary"] if s["timeframe"] == "1h" and s["ratio"] == 0.5][0]
    assert one_h["trades"] >= 2 and one_h["coins"] == 2
    assert isinstance(out["fib_beats_grid"], bool)
    assert out["fees"]["maker_per_leg"] < out["fees"]["taker_per_leg"]
