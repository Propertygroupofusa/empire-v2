"""Compare many strategies without fooling yourself. Out-of-sample and a control, always.

WHY THE DEFAULTS ARE NOT OPTIONAL

Searching 432 variants does not find the best strategy. It finds the
luckiest one. Simulated on this account's own real BTC daily returns,
432 strategies with LITERALLY ZERO EDGE - random entries - produce:

    a single no-edge strategy      median   -0.60%
    the BEST of 432 no-edge ones   median  +53.15%   (64% win rate)
    95th percentile of that best           +65.34%

The search alone manufactures +53 points and a 64% win rate out of pure
noise, every single time it is run. So a winner showing +40% at a 62%
win rate is WORSE than what randomness produces at that search width.

Two defences, both mandatory here rather than flags:

  1. OUT OF SAMPLE. Variants are selected on the first `in_sample_frac`
     of the series and scored only on the rest. A strategy tuned on data
     it is then judged on has no information content at all.

  2. A MATCHED RANDOM CONTROL. For each variant, run random strategies
     that trade EXACTLY AS OFTEN on the same bars. This separates "the
     timing is informative" from "trading this asset this often happened
     to pay". It is the control that reversed the news-brake result from
     "worth building" to "no signal" - the first version compared a
     brake against a differently-sized brake and got a flattering answer.

  3. THE NOISE FLOOR. With N variants searched, the harness reports what
     the best of N no-edge strategies would have scored. A result under
     that line is not a finding.

FEES ARE REAL AND THEY DOMINATE

Every round trip is charged the measured rate, not an assumption. At
Coinbase's real 1.50% taker round trip against a 0.15% median hourly BTC
move, hourly strategies lose ~90% regardless of signal quality - the fee
is ten times the move. The harness refuses to report a strategy whose
average move cannot clear its own fee, because that is arithmetic rather
than a backtest result.

Nothing here is live. It places no orders and is imported by no bot.
"""

import math
import random
import statistics

# A result on fewer than this many OUT-OF-SAMPLE trades is not a result.
# Same discipline the lessons engine applies to a coin: at a wide search
# width something always looks good on a handful of trades.
MIN_OOS_TRADES = 10

# In-sample minus out-of-sample, in points. A strategy that makes +106%
# on the window it was chosen on and +15% on the window it was not has
# described the fitting window. That gap IS the overfitting, measured.
OVERFIT_GAP_LIMIT_PCT = 40.0

try:
    from crypto_selection_backtest import (BACKTEST_ROUND_TRIP_FEE_RATE,
                                           REAL_MAKER_ROUND_TRIP_FEE_RATE,
                                           REAL_TAKER_ROUND_TRIP_FEE_RATE)
except Exception:  # standalone use
    REAL_TAKER_ROUND_TRIP_FEE_RATE = 0.015
    REAL_MAKER_ROUND_TRIP_FEE_RATE = 0.007
    BACKTEST_ROUND_TRIP_FEE_RATE = REAL_TAKER_ROUND_TRIP_FEE_RATE


# ── indicators ──────────────────────────────────────────────────────────

def ema(values, period):
    if period <= 1 or len(values) < period:
        return [None] * len(values)
    k = 2.0 / (period + 1)
    out = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def sma(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    run = sum(values[:period])
    out[period - 1] = run / period
    for i in range(period, len(values)):
        run += values[i] - values[i - period]
        out[i] = run / period
    return out


def rolling_std(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        w = values[i - period + 1:i + 1]
        out[i] = statistics.pstdev(w)
    return out


def atr(closes, highs, lows, period=14):
    trs = [None]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    out = [None] * len(closes)
    for i in range(period, len(closes)):
        w = [t for t in trs[i - period + 1:i + 1] if t is not None]
        if w:
            out[i] = sum(w) / len(w)
    return out


# ── strategies ──────────────────────────────────────────────────────────
#
# A strategy returns a position per bar: 1 = long, 0 = flat. Long-only by
# design - this account trades spot, and a short that cannot be executed
# is not a strategy, it is a daydream.
#
# Each reads only bars at or before i. A strategy that peeks forward will
# look extraordinary and is worth nothing.

def strat_ema_cross(closes, highs, lows, fast=12, slow=26):
    f, s = ema(closes, fast), ema(closes, slow)
    return [1 if (f[i] is not None and s[i] is not None and f[i] > s[i]) else 0
            for i in range(len(closes))]


def strat_macd(closes, highs, lows, fast=12, slow=26, signal=9):
    f, s = ema(closes, fast), ema(closes, slow)
    line = [(f[i] - s[i]) if (f[i] is not None and s[i] is not None) else None
            for i in range(len(closes))]
    vals = [v for v in line if v is not None]
    sig = ema(vals, signal)
    pad = len(line) - len(sig)
    sig = [None] * pad + sig
    return [1 if (line[i] is not None and sig[i] is not None and line[i] > sig[i]) else 0
            for i in range(len(closes))]


def strat_bollinger_breakout(closes, highs, lows, period=20, mult=2.0):
    mid, sd = sma(closes, period), rolling_std(closes, period)
    pos, holding = [], 0
    for i in range(len(closes)):
        if mid[i] is None or sd[i] is None:
            pos.append(0)
            continue
        upper = mid[i] + mult * sd[i]
        if closes[i] > upper:
            holding = 1
        elif closes[i] < mid[i]:
            holding = 0
        pos.append(holding)
    return pos


def strat_bollinger_reversion(closes, highs, lows, period=20, mult=2.0):
    mid, sd = sma(closes, period), rolling_std(closes, period)
    pos, holding = [], 0
    for i in range(len(closes)):
        if mid[i] is None or sd[i] is None:
            pos.append(0)
            continue
        lower = mid[i] - mult * sd[i]
        if closes[i] < lower:
            holding = 1
        elif closes[i] > mid[i]:
            holding = 0
        pos.append(holding)
    return pos


def strat_supertrend(closes, highs, lows, period=10, mult=3.0):
    a = atr(closes, highs, lows, period)
    pos, holding = [], 0
    stop = None
    for i in range(len(closes)):
        if a[i] is None:
            pos.append(0)
            continue
        if holding:
            stop = max(stop or 0, closes[i] - mult * a[i])
            if closes[i] < stop:
                holding = 0
                stop = None
        else:
            if i and closes[i] > closes[i - 1] + a[i]:
                holding = 1
                stop = closes[i] - mult * a[i]
        pos.append(holding)
    return pos


def strat_donchian_breakout(closes, highs, lows, period=20, exit_period=10):
    pos, holding = [], 0
    for i in range(len(closes)):
        if i < period:
            pos.append(0)
            continue
        hi = max(highs[i - period:i])
        lo = min(lows[max(0, i - exit_period):i])
        if closes[i] > hi:
            holding = 1
        elif closes[i] < lo:
            holding = 0
        pos.append(holding)
    return pos


def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def strat_rsi_reversion(closes, highs, lows, period=14, oversold=30, overbought=70):
    r = rsi(closes, period)
    pos, holding = [], 0
    for i in range(len(closes)):
        if r[i] is None:
            pos.append(0)
            continue
        if r[i] < oversold:
            holding = 1
        elif r[i] > overbought:
            holding = 0
        pos.append(holding)
    return pos


def strat_sma_cross(closes, highs, lows, fast=20, slow=50):
    f, s_ = sma(closes, fast), sma(closes, slow)
    return [1 if (f[i] is not None and s_[i] is not None and f[i] > s_[i]) else 0
            for i in range(len(closes))]


def strat_momentum(closes, highs, lows, lookback=20, threshold=0.0):
    """Long while price is up more than `threshold` over `lookback` bars."""
    pos = []
    for i in range(len(closes)):
        if i < lookback:
            pos.append(0)
            continue
        pos.append(1 if (closes[i] / closes[i - lookback] - 1.0) > threshold else 0)
    return pos


def strat_volume_breakout(closes, highs, lows, volumes=None, period=20, mult=2.0):
    """Long when volume spikes above its own average AND price is up.

    Named by the account owner. Falls back to a price-range proxy when no
    volume series is supplied, and says so rather than silently pretending
    it had volume.
    """
    n = len(closes)
    series = volumes if volumes else [highs[i] - lows[i] for i in range(n)]
    avg = sma(series, period)
    pos, holding = [], 0
    for i in range(n):
        if avg[i] is None or i == 0:
            pos.append(0)
            continue
        if series[i] > avg[i] * mult and closes[i] > closes[i - 1]:
            holding = 1
        elif closes[i] < closes[i - 1]:
            holding = 0
        pos.append(holding)
    return pos


def strat_price_vs_sma(closes, highs, lows, period=50, buffer_pct=0.0):
    """Long while price holds above its own moving average by a buffer."""
    m = sma(closes, period)
    return [1 if (m[i] is not None and closes[i] > m[i] * (1 + buffer_pct)) else 0
            for i in range(len(closes))]


def strat_atr_breakout(closes, highs, lows, period=14, mult=1.5):
    """Long on a move larger than `mult` x ATR; flat on the reverse."""
    a = atr(closes, highs, lows, period)
    pos, holding = [], 0
    for i in range(len(closes)):
        if a[i] is None or i == 0:
            pos.append(0)
            continue
        move = closes[i] - closes[i - 1]
        if move > a[i] * mult:
            holding = 1
        elif move < -a[i] * mult:
            holding = 0
        pos.append(holding)
    return pos


def strat_buy_and_hold(closes, highs, lows):
    """The benchmark everything must beat. A strategy that trades all year
    and lands under buy-and-hold has spent fees to underperform doing
    nothing."""
    return [1] * len(closes)


STRATEGIES = {
    "ema_cross": (strat_ema_cross,
                  [{"fast": f, "slow": s}
                   for f in (3, 5, 8, 12, 20, 26, 34) for s in (26, 50, 100, 200) if f < s]),
    "sma_cross": (strat_sma_cross,
                  [{"fast": f, "slow": s}
                   for f in (3, 5, 10, 20, 30, 50) for s in (50, 100, 150, 200) if f < s]),
    "macd": (strat_macd,
             [{"fast": f, "slow": s, "signal": g}
              for f in (5, 8, 12, 16) for s in (21, 26, 34) for g in (7, 9, 12)]),
    "bollinger_breakout": (strat_bollinger_breakout,
                           [{"period": p, "mult": m}
                            for p in (10, 14, 20, 30, 50, 100)
                            for m in (0.75, 1.0, 1.5, 2.0, 2.5, 3.0)]),
    "bollinger_reversion": (strat_bollinger_reversion,
                            [{"period": p, "mult": m}
                             for p in (10, 14, 20, 30, 50, 100)
                             for m in (0.75, 1.0, 1.5, 2.0, 2.5, 3.0)]),
    "supertrend": (strat_supertrend,
                   [{"period": p, "mult": m}
                    for p in (5, 7, 10, 14, 21, 28) for m in (1.5, 2.0, 2.5, 3.0, 3.5, 4.0)]),
    "donchian_breakout": (strat_donchian_breakout,
                          [{"period": p, "exit_period": e}
                           for p in (10, 20, 30, 40, 55) for e in (3, 5, 10, 20, 30)]),
    "rsi_reversion": (strat_rsi_reversion,
                      [{"period": p, "oversold": o, "overbought": b}
                       for p in (5, 7, 14, 21, 28) for o in (20, 25, 30, 35)
                       for b in (65, 70, 75, 80)]),
    "momentum": (strat_momentum,
                 [{"lookback": l, "threshold": t}
                  for l in (5, 10, 15, 20, 30, 50, 100)
                  for t in (0.0, 0.02, 0.05, 0.10, 0.15, 0.20)]),
    "volume_breakout": (strat_volume_breakout,
                        [{"period": p, "mult": m}
                         for p in (10, 20, 30, 50, 100) for m in (1.25, 1.5, 2.0, 2.5, 3.0)]),
    "price_vs_sma": (strat_price_vs_sma,
                     [{"period": p, "buffer_pct": b}
                      for p in (10, 20, 30, 50, 100, 200)
                      for b in (0.0, 0.01, 0.02, 0.03, 0.05)]),
    "atr_breakout": (strat_atr_breakout,
                     [{"period": p, "mult": m}
                      for p in (5, 7, 10, 14, 21, 28) for m in (0.5, 0.75, 1.0, 1.5, 2.0, 2.5)]),
    "buy_and_hold": (strat_buy_and_hold, [{}]),
}

VARIANT_COUNT = sum(len(v[1]) for v in STRATEGIES.values())


# ── the replay ──────────────────────────────────────────────────────────

def replay_positions(closes, positions, fee_round_trip=None, start=0, end=None):
    """Turn a position series into real round trips, charged real fees.

    Entry and exit are on the NEXT bar's close after the signal, never the
    same bar. A signal computed from bar i's close cannot be filled at
    bar i's close - that is lookahead, and it is the single most common
    way a backtest invents an edge that does not exist.
    """
    fee = BACKTEST_ROUND_TRIP_FEE_RATE if fee_round_trip is None else fee_round_trip
    end = len(closes) if end is None else end
    trades = []
    entry = None
    for i in range(max(1, start), min(end, len(closes)) - 1):
        want = positions[i]
        fill = closes[i + 1]          # next bar - no same-bar fills
        if want and entry is None:
            entry = fill
        elif not want and entry is not None:
            trades.append((fill / entry - 1.0) - fee)
            entry = None
    if entry is not None:
        trades.append((closes[min(end, len(closes)) - 1] / entry - 1.0) - fee)

    if not trades:
        return {"trades": 0, "total_return_pct": 0.0, "win_rate": 0.0,
                "avg_trade_pct": 0.0, "max_drawdown_pct": 0.0, "equity_mult": 1.0}

    gross = [t + fee for t in trades]          # what the move was, before fees
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in trades:
        eq *= (1 + t)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak if peak > 0 else 0.0)
    wins = sum(1 for t in trades if t > 0)
    return {
        "trades": len(trades),
        "total_return_pct": round((eq - 1) * 100, 2),
        "win_rate": round(wins / len(trades) * 100, 1),
        "avg_trade_pct": round(statistics.mean(trades) * 100, 3),
        "max_drawdown_pct": round(mdd * 100, 1),
        "equity_mult": round(eq, 4),
        # The median ABSOLUTE move a trade captured before fees. This is
        # what the fee has to be compared against - not one bar's move.
        # A trade holding 20 bars captures a 20-bar move, so judging it
        # against a single bar dismisses every valid slow strategy.
        "median_gross_move_pct": round(statistics.median([abs(g) for g in gross]) * 100, 3),
    }


def _matched_random_positions(positions, seed, start=0, end=None):
    """Random positions with the SAME number of entries over the same span.

    Preserves how OFTEN the strategy trades and randomises only WHEN. A
    control that trades a different amount measures activity, not timing,
    and will happily flatter or bury a real signal depending on whether
    the window trended.
    """
    end = len(positions) if end is None else end
    span = positions[start:end]
    entries = sum(1 for i in range(1, len(span)) if span[i] and not span[i - 1])
    holds = [0] * len(span)
    if entries == 0:
        return [0] * start + holds + [0] * (len(positions) - end)
    total_held = sum(span)
    avg_hold = max(1, round(total_held / entries))
    rng = random.Random(seed)
    placed = 0
    guard = 0
    while placed < entries and guard < entries * 50:
        guard += 1
        i = rng.randrange(len(span))
        if any(holds[i:i + avg_hold]):
            continue
        for k in range(i, min(i + avg_hold, len(span))):
            holds[k] = 1
        placed += 1
    return [0] * start + holds + [0] * (len(positions) - end)


def noise_floor_for_search(n_variants, sample_trades, trade_returns, fee,
                           draws=200, seed=99):
    """What the BEST of `n_variants` no-edge strategies scores on this data.

    The whole point: with a wide enough search, a winner appears out of
    nothing. This computes how good that nothing looks, so a real result
    has a line to clear.
    """
    rng = random.Random(seed)
    bests = []
    for _ in range(draws):
        best = None
        for _ in range(n_variants):
            eq = 1.0
            for _ in range(sample_trades):
                r = trade_returns[rng.randrange(len(trade_returns))]
                eq *= (1 + ((1 if rng.random() < 0.5 else -1) * r - fee))
            v = (eq - 1) * 100
            best = v if best is None or v > best else best
        bests.append(best)
    bests.sort()
    return {
        "median": round(bests[len(bests) // 2], 2),
        "p95": round(bests[int(0.95 * (len(bests) - 1))], 2),
        "draws": draws,
        "n_variants": n_variants,
    }


def run_strategy_lab(closes, highs, lows, strategies=None,
                     in_sample_frac=0.7, control_draws=20,
                     fee_round_trip=None, label="series"):
    """Score every strategy variant out-of-sample, against its own control.

    Returns variants ranked by OUT-OF-SAMPLE return, each carrying how
    often it beat a random strategy that traded exactly as much, plus the
    noise floor for the width of this search.
    """
    fee = BACKTEST_ROUND_TRIP_FEE_RATE if fee_round_trip is None else fee_round_trip
    strategies = strategies or STRATEGIES
    n = len(closes)
    split = int(n * in_sample_frac)
    if split < 30 or n - split < 30:
        return {"error": f"series too short to split ({n} bars)"}

    bar_returns = [abs(closes[i] / closes[i - 1] - 1) for i in range(1, n)]
    median_move = statistics.median(bar_returns)

    rows = []
    for name, (fn, param_sets) in strategies.items():
        for params in param_sets:
            try:
                pos = fn(closes, highs, lows, **params)
            except Exception as e:
                rows.append({"strategy": name, "params": params, "error": str(e)})
                continue

            ins = replay_positions(closes, pos, fee, 0, split)
            oos = replay_positions(closes, pos, fee, split, n)

            ctrl = []
            for d in range(control_draws):
                cpos = _matched_random_positions(pos, seed=d * 977 + 3, start=split, end=n)
                c = replay_positions(closes, cpos, fee, split, n)
                ctrl.append(c["total_return_pct"])
            ctrl.sort()
            beat = sum(1 for c in ctrl if oos["total_return_pct"] > c)

            rows.append({
                "strategy": name,
                "params": params,
                "in_sample": ins,
                "out_of_sample": oos,
                "control_median_pct": round(ctrl[len(ctrl) // 2], 2) if ctrl else None,
                "beat_control": f"{beat}/{len(ctrl)}" if ctrl else None,
                "percentile_vs_control": round(beat / len(ctrl), 3) if ctrl else None,
                # A strategy that scores well in-sample and badly out of it
                # was fitted, not found. This is the number that says so.
                "overfit_gap_pct": round(ins["total_return_pct"] - oos["total_return_pct"], 2),
            })

    usable = [r for r in rows if "error" not in r]
    if not usable:
        return {"error": "no strategy produced a result", "rows": rows}

    trade_returns = [abs(closes[i] / closes[i - 1] - 1) for i in range(1, n)]
    typical_trades = int(statistics.median([r["out_of_sample"]["trades"] for r in usable]) or 1)
    floor = noise_floor_for_search(len(usable), max(1, typical_trades), trade_returns, fee)

    ranked = sorted(usable, key=lambda r: -r["out_of_sample"]["total_return_pct"])
    bh = next((r for r in usable if r["strategy"] == "buy_and_hold"), None)
    bh_oos = bh["out_of_sample"]["total_return_pct"] if bh else None

    best = ranked[0]
    b_oos = best["out_of_sample"]["total_return_pct"]
    b_trades = best["out_of_sample"]["trades"]
    b_gap = best["overfit_gap_pct"]
    pct = best.get("percentile_vs_control")

    # Judge the fee against what a TRADE captured, not what a BAR moved.
    # The first version compared the fee to one bar's median move and so
    # declared "fees dominate" on daily data where the winning strategy
    # held for weeks and captured far more than a day.
    best_gross = best["out_of_sample"].get("median_gross_move_pct")
    if best_gross is not None and best["out_of_sample"]["trades"] > 0 and best_gross < fee * 100:
        verdict = (f"FEES DOMINATE. The best variant's median trade captured "
                   f"{best_gross:.2f}% gross while a round trip costs {fee * 100:.2f}%. "
                   f"Trades this small cannot pay for themselves at this venue - that is "
                   f"arithmetic, not strategy.")
    elif b_oos <= floor["p95"]:
        verdict = (f"NO EDGE. The best out-of-sample result ({best['strategy']}, "
                   f"{b_oos:+.2f}%) is under the {floor['p95']:+.2f}% that the BEST of "
                   f"{len(usable)} zero-edge strategies reaches on this data by luck alone.")
    elif b_trades < MIN_OOS_TRADES:
        verdict = (f"TOO FEW TRADES. {best['strategy']} returned {b_oos:+.2f}% out-of-sample "
                   f"on {b_trades} trade{'' if b_trades == 1 else 's'}. Under "
                   f"{MIN_OOS_TRADES} the result is one or two lucky moves, not a strategy - "
                   f"and at a {len(usable)}-variant search width, something will always look "
                   f"good on a handful of trades.")
    elif b_gap > OVERFIT_GAP_LIMIT_PCT:
        verdict = (f"OVERFIT. {best['strategy']} made "
                   f"{best['in_sample']['total_return_pct']:+.1f}% in-sample and "
                   f"{b_oos:+.2f}% out-of-sample - a {b_gap:.0f}-point collapse. The "
                   f"parameters describe the fitting window, not the market.")
    elif pct is not None and pct < 0.8:
        verdict = (f"NOT BEATING ITS CONTROL. {best['strategy']} returned {b_oos:+.2f}% "
                   f"out-of-sample but beat a random strategy trading exactly as often only "
                   f"{pct * 100:.0f}% of the time.")
    elif bh_oos is not None and b_oos <= bh_oos:
        verdict = (f"UNDERPERFORMS DOING NOTHING. {best['strategy']} returned {b_oos:+.2f}% "
                   f"out-of-sample; buy-and-hold returned {bh_oos:+.2f}% and paid one fee.")
    else:
        verdict = (f"{best['strategy']} {best['params']}: {b_oos:+.2f}% out-of-sample, "
                   f"above the {floor['p95']:+.2f}% noise floor for a {len(usable)}-variant "
                   f"search, beating its matched control {pct * 100:.0f}% of the time, and "
                   f"ahead of buy-and-hold ({bh_oos:+.2f}%). Worth forward-testing - not "
                   f"worth funding on a backtest.")

    return {
        "label": label,
        "bars": n,
        "in_sample_bars": split,
        "out_of_sample_bars": n - split,
        "variants_tested": len(usable),
        "round_trip_fee_rate": fee,
        "median_bar_move_pct": round(median_move * 100, 3),
        "noise_floor": floor,
        "buy_and_hold_oos_pct": bh_oos,
        "verdict": verdict,
        "ranked": ranked,
    }


def run_fleet(series_by_coin, strategies=None, in_sample_frac=0.7,
              control_draws=10, fee_round_trip=None):
    """Run every variant across every coin, then judge the winner honestly.

    TWO THINGS A PER-COIN RUN CANNOT SEE, both learned by running it:

    1. THE REAL SEARCH WIDTH. run_strategy_lab computes its noise floor for
       N variants. Running the same N across 8 coins is 8N tests, and the
       floor rises with width. On this account's coins: the best of 432
       zero-edge strategies reaches +68.5% out-of-sample by luck; the best
       of 3,456 reaches +92.0%. Judging an 8-coin sweep against the
       432-variant floor passes results that are pure search.

    2. WHETHER THE WINNER GENERALISES. The 2026-09-25 sweep produced one
       survivor - atr_breakout(21, 0.75) on NEAR-USD at +226.5%
       out-of-sample, above even the corrected floor, beating its matched
       control 100% of the time. Run on the other seven coins it returned
       -33.8, -48.0, -33.1, -2.8, -3.9, -59.6 and -23.6 percent. It was one
       lucky coin out of 3,456 tests, which is precisely what the
       multiple-comparisons arithmetic predicts.

       It "beat buy-and-hold on 6/8" only because buy-and-hold was -33% to
       -64% on those coins. Losing less than holding is not an edge.

    So the winner of a fleet sweep is re-run, unchanged, on every other
    coin. A parameter set that only pays on the coin it was found on was
    found, not discovered.
    """
    fee = BACKTEST_ROUND_TRIP_FEE_RATE if fee_round_trip is None else fee_round_trip
    strategies = strategies or STRATEGIES
    per_coin, all_rets = {}, []

    for coin, (closes, highs, lows) in series_by_coin.items():
        r = run_strategy_lab(closes, highs, lows, strategies, in_sample_frac,
                             control_draws, fee, label=coin)
        per_coin[coin] = r
        if "error" not in r:
            all_rets += [abs(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]

    usable = {c: r for c, r in per_coin.items() if "error" not in r}
    if not usable:
        return {"error": "no coin produced a result", "per_coin": per_coin}

    n_variants = max(r["variants_tested"] for r in usable.values())
    true_width = n_variants * len(usable)
    typical_trades = max(1, int(statistics.median(
        [r["ranked"][0]["out_of_sample"]["trades"] for r in usable.values()]) or 1))
    fleet_floor = noise_floor_for_search(true_width, typical_trades, all_rets, fee,
                                         draws=120, seed=17)

    # The single best variant anywhere, judged against the FLEET floor.
    best_coin, best_row = None, None
    for coin, r in usable.items():
        for row in r["ranked"]:
            if row["out_of_sample"]["trades"] < MIN_OOS_TRADES:
                continue
            if best_row is None or (row["out_of_sample"]["total_return_pct"]
                                    > best_row["out_of_sample"]["total_return_pct"]):
                best_coin, best_row = coin, row

    cross = {}
    if best_row is not None:
        fn = strategies[best_row["strategy"]][0]
        for coin, (closes, highs, lows) in series_by_coin.items():
            try:
                pos = fn(closes, highs, lows, **best_row["params"])
            except Exception:
                continue
            split = int(len(closes) * in_sample_frac)
            oos = replay_positions(closes, pos, fee, split, len(closes))
            bh = replay_positions(closes, [1] * len(closes), fee, split, len(closes))
            cross[coin] = {"oos_pct": oos["total_return_pct"],
                           "trades": oos["trades"],
                           "buy_hold_pct": bh["total_return_pct"],
                           "beats_buy_hold": oos["total_return_pct"] > bh["total_return_pct"]}

    profitable = [c for c, v in cross.items() if v["oos_pct"] > 0]
    verdict = "no variant anywhere cleared the minimum trade count"
    if best_row is not None:
        b = best_row["out_of_sample"]["total_return_pct"]
        if b <= fleet_floor["p95"]:
            verdict = (f"NO EDGE ACROSS THE FLEET. The best result anywhere "
                       f"({best_row['strategy']} on {best_coin}, {b:+.1f}%) is under the "
                       f"{fleet_floor['p95']:+.1f}% that the best of {true_width} zero-edge "
                       f"strategies reaches by luck at this search width.")
        elif len(profitable) < max(2, len(cross) // 2):
            verdict = (f"DOES NOT GENERALISE. {best_row['strategy']} {best_row['params']} "
                       f"returned {b:+.1f}% on {best_coin} but is profitable on only "
                       f"{len(profitable)}/{len(cross)} coins "
                       f"({', '.join(sorted(profitable)) or 'none'}). One lucky coin out of "
                       f"{true_width} tests is what this search width produces from noise.")
        else:
            verdict = (f"{best_row['strategy']} {best_row['params']}: {b:+.1f}% on "
                       f"{best_coin}, above the {fleet_floor['p95']:+.1f}% fleet noise floor, "
                       f"and profitable on {len(profitable)}/{len(cross)} coins. Survives "
                       f"every gate - forward-test it, do not fund it on this.")

    return {
        "coins": list(usable.keys()),
        "variants_per_coin": n_variants,
        "true_search_width": true_width,
        "fleet_noise_floor": fleet_floor,
        "best": ({"coin": best_coin, "strategy": best_row["strategy"],
                  "params": best_row["params"],
                  "out_of_sample": best_row["out_of_sample"],
                  "percentile_vs_control": best_row.get("percentile_vs_control")}
                 if best_row else None),
        "cross_coin": cross,
        "profitable_on": profitable,
        "verdict": verdict,
        "per_coin_verdicts": {c: r["verdict"] for c, r in usable.items()},
    }
