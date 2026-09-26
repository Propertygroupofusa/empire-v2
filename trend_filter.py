"""Can the fleet be told to sit out a downtrend?

WHY THIS EXISTS

run_regime_study.py settled that the strategy is a bull-market bet: in both
falling windows tested, 0 of 6 coins were positive at any rung or horizon,
and holding longer made it worse rather than better. The only door left
open was a strategy that stands aside when price is falling instead of
buying all the way down.

This measures whether that door leads anywhere.

THE TRAP, STATED FIRST

A trend filter is the most over-fitted object in trading. Given four windows
and enough rules, something will look brilliant on all four by accident. So:

  * The rules here are the plainest ones that exist - price against its own
    moving average, and one moving average against another. No thresholds
    were tuned, no lookback was searched, no rule was added after seeing a
    result.
  * Every rule is run on every window and ALL results are reported, not the
    best one.
  * The headline rule is chosen A PRIORI: price above its own 7-day average.
    It is the oldest and least clever trend filter in existence, which is
    exactly why it is the honest one to lead with.
  * PARTICIPATION is reported beside expectancy. A filter that skips 99% of
    entries can post a wonderful per-trade number while earning nothing.
    Expectancy alone would hide that completely.

CAUSALITY

Every filter reads closes[:i+1] and never index i+1 or beyond. A filter that
peeks one bar into the future will look superb and is worthless. The test
suite asserts this directly by feeding a series whose future is garbage.
"""
from __future__ import annotations

BARS_PER_HOUR = 12          # 5-minute candles
H24 = 24 * BARS_PER_HOUR
H72 = 72 * BARS_PER_HOUR
D7 = 7 * 24 * BARS_PER_HOUR


def sma(closes, i: int, n: int):
    """Simple mean of the n closes ending AT i. None if history is short.

    Deliberately returns None rather than a short-window average: a "7-day
    average" computed from two hours of data is not a 7-day average, and
    letting it pass is how a filter ends up making its boldest calls on the
    thinnest evidence - the same defect that gave a 3-day-old coin the
    tightest stop in the fleet.
    """
    if i < 0 or n <= 0 or i + 1 < n or i >= len(closes):
        return None
    window = closes[i + 1 - n:i + 1]
    return sum(window) / float(n)


def _above_sma(closes, n: int):
    def allow(i: int) -> bool:
        m = sma(closes, i, n)
        # No history yet -> do NOT trade. The alternative, trading while the
        # filter cannot speak, quietly restores the unfiltered strategy for
        # exactly the early bars where a downtrend is least visible.
        return m is not None and closes[i] > m
    return allow


def _fast_above_slow(closes, fast: int, slow: int):
    def allow(i: int) -> bool:
        f, s = sma(closes, i, fast), sma(closes, i, slow)
        return f is not None and s is not None and f > s
    return allow


def _rising_sma(closes, n: int, lookback: int):
    """The average itself is higher than it was `lookback` bars ago."""
    def allow(i: int) -> bool:
        now, then = sma(closes, i, n), sma(closes, i - lookback, n)
        return now is not None and then is not None and now > then
    return allow


def build(closes) -> dict:
    """Every rule, by name. Fixed list, declared before any result was seen."""
    return {
        "none (baseline)": None,
        "price > 24h avg": _above_sma(closes, H24),
        "price > 72h avg": _above_sma(closes, H72),
        "price > 7d avg": _above_sma(closes, D7),
        "24h avg > 7d avg": _fast_above_slow(closes, H24, D7),
        "7d avg rising": _rising_sma(closes, D7, H24),
    }


# The one named in advance, so the headline is not chosen after the fact.
HEADLINE = "price > 7d avg"
