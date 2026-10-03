"""Fibonacci "gold zone" pullback strategy - a pure, shadow-mode replay.

WHAT IS BEING TESTED

A YouTube lesson claims a repeatable edge: in an uptrend (higher lows),
wait for a break of structure (a close above the last swing high), draw a
Fibonacci retracement from the higher low to the new high, buy the
pullback into the "gold zone" between the .5 and .618 levels, stop at the
1.0 level (the higher low itself), target the prior swing high.

This module replays exactly that, long only, on any OHLC series, and
charges real fees. It never fetches data and never places an order; the
runner in crypto_selection_backtest.py feeds it real Coinbase candles.

THE RULES, MADE CONCRETE (the video gives none of these numbers)

- Swing points are fractal pivots of width `k`: a bar whose high is the
  highest of the k bars either side. A pivot is only KNOWN k bars after
  it forms, so it is only used from bar j+k onward - no lookahead.
- Uptrend = the last two confirmed pivot lows are rising.
- Break of structure = a close above the last confirmed pivot high, where
  the higher low formed AFTER that pivot high (high -> higher low ->
  break). Each pivot high can trigger at most one setup.
- The swing high H is the running max high from the break bar onward,
  using only bars before the one being tested for a fill.
- Entry is a resting limit at H - ratio*(H - HL). It fills at that level.
- Stop = HL, target = H (frozen at fill).
- Conservative intrabar resolution, because OHLC cannot say which came
  first: a stop and target in the same bar count as the stop; a stop in
  the entry bar counts as a loss; a target in the entry bar is never
  credited.
- A stop that gaps through fills at the prior close (the bar's open is
  not in the data), never better than the stop.
- The setup is cancelled if price breaks HL before filling, or after
  `setup_timeout_bars`. A position still open after `max_hold_bars` exits
  at the close.

FEES

Entry and target are resting limits, so they pay the maker rate. A stop
or a timeout is a market exit and pays taker. Defaults are this account's
real Coinbase rates (0.35% maker, 0.75% taker per leg).
"""
from __future__ import annotations

DEFAULT_PIVOT_K = 3
DEFAULT_RATIOS = (0.5, 0.559, 0.618)
DEFAULT_MAKER_FEE = 0.0035
DEFAULT_TAKER_FEE = 0.0075
DEFAULT_MAX_HOLD_BARS = 96
DEFAULT_SETUP_TIMEOUT_BARS = 48


def _is_pivot_high(highs, j, k):
    h = highs[j]
    return (all(h > highs[x] for x in range(j - k, j))
            and all(h >= highs[x] for x in range(j + 1, j + k + 1)))


def _is_pivot_low(lows, j, k):
    v = lows[j]
    return (all(v < lows[x] for x in range(j - k, j))
            and all(v <= lows[x] for x in range(j + 1, j + k + 1)))


def _net_pct(entry, exit_, entry_fee, exit_fee):
    """Net return on the dollars put in, after both legs' fees."""
    return (exit_ * (1 - exit_fee)) / (entry * (1 + entry_fee)) - 1


def replay(highs, lows, closes, *, ratio=0.618, k=DEFAULT_PIVOT_K,
           maker_fee=DEFAULT_MAKER_FEE, taker_fee=DEFAULT_TAKER_FEE,
           max_hold_bars=DEFAULT_MAX_HOLD_BARS,
           setup_timeout_bars=DEFAULT_SETUP_TIMEOUT_BARS):
    """Every trade the rules above would have taken on this series."""
    n = len(closes)
    if n != len(highs) or n != len(lows):
        raise ValueError("highs, lows and closes must be the same length")
    if not 0 < ratio < 1:
        raise ValueError("ratio must be between 0 and 1")

    pivot_highs, pivot_lows = [], []   # (index, price), confirmed only
    used_pivot_high = -1
    setup = None
    pos = None
    trades = []

    def _close(i, price, reason, exit_fee):
        trades.append({
            "entry_bar": pos["bar"], "exit_bar": i,
            "entry": pos["entry"], "exit": price, "reason": reason,
            "swing_pct": pos["swing_pct"],
            "gross_pct": price / pos["entry"] - 1,
            "net_pct": _net_pct(pos["entry"], price, maker_fee, exit_fee),
        })

    for i in range(n):
        j = i - k
        if j >= k:
            if _is_pivot_high(highs, j, k):
                pivot_highs.append((j, highs[j]))
            if _is_pivot_low(lows, j, k):
                pivot_lows.append((j, lows[j]))

        if pos is not None:
            hit_stop = lows[i] <= pos["stop"]
            hit_target = highs[i] >= pos["target"]
            if hit_stop:
                fill = min(pos["stop"], closes[i - 1])
                _close(i, fill, "STOP", taker_fee)
                pos = None
            elif hit_target:
                _close(i, pos["target"], "TARGET", maker_fee)
                pos = None
            elif i - pos["bar"] >= max_hold_bars:
                _close(i, closes[i], "TIMEOUT", taker_fee)
                pos = None
            continue

        if setup is not None:
            level = setup["h"] - ratio * (setup["h"] - setup["hl"])
            if lows[i] <= level:
                pos = {"bar": i, "entry": level, "stop": setup["hl"],
                       "target": setup["h"],
                       "swing_pct": setup["h"] / setup["hl"] - 1}
                setup = None
                if lows[i] <= pos["stop"]:
                    _close(i, pos["stop"], "STOP", taker_fee)
                    pos = None
                continue
            if lows[i] < setup["hl"] or i - setup["bar"] > setup_timeout_bars:
                setup = None
            else:
                setup["h"] = max(setup["h"], highs[i])
            continue

        if len(pivot_lows) < 2 or not pivot_highs:
            continue
        ph_idx, ph_price = pivot_highs[-1]
        (_, prev_low), (hl_idx, hl_price) = pivot_lows[-2], pivot_lows[-1]
        if (ph_idx > used_pivot_high and hl_idx > ph_idx
                and hl_price > prev_low and closes[i] > ph_price
                and highs[i] > hl_price):
            used_pivot_high = ph_idx
            setup = {"bar": i, "hl": hl_price, "h": highs[i]}

    if pos is not None:
        _close(n - 1, closes[-1], "OPEN_AT_END", taker_fee)
    return trades


def summarize(trades, stake_usd=100.0):
    """The numbers that decide whether this is an edge, not a story."""
    if not trades:
        return {"trades": 0, "win_rate_pct": None, "avg_net_pct": None,
                "total_net_usd": 0.0, "avg_swing_pct": None,
                "break_even_win_rate_pct": None, "exits": {}}
    wins = [t["net_pct"] for t in trades if t["net_pct"] > 0]
    losses = [-t["net_pct"] for t in trades if t["net_pct"] <= 0]
    exits = {}
    for t in trades:
        exits[t["reason"]] = exits.get(t["reason"], 0) + 1
    be = None
    if wins and losses:
        aw, al = sum(wins) / len(wins), sum(losses) / len(losses)
        be = round(al / (aw + al) * 100, 1)
    nets = [t["net_pct"] for t in trades]
    return {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "avg_net_pct": round(sum(nets) / len(nets) * 100, 3),
        "avg_gross_pct": round(sum(t["gross_pct"] for t in trades) / len(trades) * 100, 3),
        "total_net_usd": round(sum(nets) * stake_usd, 2),
        "avg_swing_pct": round(sum(t["swing_pct"] for t in trades) / len(trades) * 100, 3),
        "break_even_win_rate_pct": be,
        "exits": exits,
    }


def aggregate(times, highs, lows, closes, bucket_seconds):
    """Roll fine candles up into coarser ones; drops incomplete buckets.

    `times` are candle START times in seconds, oldest first.
    """
    if not times:
        return [], [], [], []
    step = times[1] - times[0] if len(times) > 1 else bucket_seconds
    per = max(1, bucket_seconds // step) if step > 0 else 1
    out_t, out_h, out_l, out_c = [], [], [], []
    group = []
    for idx, t in enumerate(times):
        if group and t // bucket_seconds != times[group[0]] // bucket_seconds:
            if len(group) == per:
                out_t.append(times[group[0]] // bucket_seconds * bucket_seconds)
                out_h.append(max(highs[g] for g in group))
                out_l.append(min(lows[g] for g in group))
                out_c.append(closes[group[-1]])
            group = []
        group.append(idx)
    if len(group) == per:
        out_t.append(times[group[0]] // bucket_seconds * bucket_seconds)
        out_h.append(max(highs[g] for g in group))
        out_l.append(min(lows[g] for g in group))
        out_c.append(closes[group[-1]])
    return out_t, out_h, out_l, out_c
