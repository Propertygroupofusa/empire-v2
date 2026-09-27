"""Adverse selection, tagged by the market it was measured in.

THE PROBLEM THIS EXISTS FOR

The live gate prices a round trip at 1.37%: 0.70% of real fees plus 0.67%
of assumed adverse selection. The instrumentation has since MEASURED
adverse selection at -0.023%. If that measurement were swapped in, the
cost would fall to about 0.68% - half the bar - and a great many of the
1,614 scans that produced zero qualified trades would start passing.

The dashboard already says why it is not swapped in, and it is right:

    "adverse selection now MEASURED at -0.02% - not swapped in: that
     sample never saw a falling market"

Adverse selection is the cost of being filled by someone who knows more
than you. In a rising market a resting buy that fills is usually a
bargain; in a falling one it is usually a knife. Measuring only in the
first regime measures the one place the cost does not bite. Swapping that
number in would not be replacing an assumption with evidence - it would be
replacing a conservative assumption with an optimistic one and calling it
evidence, which is the most expensive mistake available here.

WHAT THIS DOES

It refuses to let that happen by accident. Every measurement carries the
regime it came from, and the assumed figure may only be replaced once
there are enough samples from a FALLING market to have tested the case
that matters. Until then the conservative number stands and the endpoint
says exactly how many falling-market samples are still needed.

This is the difference between "the gate is too strict" and "the gate is
strict on a number nobody has checked where it counts". Only the second
one is fixable, and only by waiting for the right data.
"""
from __future__ import annotations

import statistics

RISING, FALLING, FLAT, UNKNOWN = "RISING", "FALLING", "FLAT", "UNKNOWN"

# How far a benchmark must move over the measurement window to call the
# regime. Inside the band is FLAT - not a third opinion, an admission that
# the window does not identify a regime.
REGIME_BAND_PCT = 1.0

# Falling-market samples required before the measured figure may replace
# the assumption. Set from what the question needs, not from what is
# available: adverse selection in a fall is the entire reason the
# conservative number exists, and a handful of prints cannot speak for it.
MIN_FALLING_SAMPLES = 30

# The assumption in force until then.
ASSUMED_ADVERSE_PCT = 0.67


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def classify_regime(benchmark_move_pct, band_pct=REGIME_BAND_PCT):
    """Which market a measurement happened in. UNKNOWN when it cannot say."""
    m = _num(benchmark_move_pct)
    if m is None:
        return UNKNOWN
    b = abs(_num(band_pct) or 0.0)
    if m > b:
        return RISING
    if m < -b:
        return FALLING
    return FLAT


def tag(samples, band_pct=REGIME_BAND_PCT):
    """Group adverse-selection samples by the regime they came from.

    A sample is {"adverse_pct": float, "benchmark_move_pct": float}.
    Samples that cannot be classified are kept under UNKNOWN rather than
    dropped - a measurement whose regime is unknown must not quietly count
    toward the falling-market evidence it may not be.
    """
    out = {RISING: [], FALLING: [], FLAT: [], UNKNOWN: []}
    for s in samples or ():
        if not hasattr(s, "get"):
            continue
        a = _num(s.get("adverse_pct"))
        if a is None:
            continue
        out[classify_regime(s.get("benchmark_move_pct"), band_pct)].append(a)
    return out


def summarise(samples, *, assumed_pct=ASSUMED_ADVERSE_PCT,
              min_falling=MIN_FALLING_SAMPLES, band_pct=REGIME_BAND_PCT):
    """What the measurements say, and whether they may replace the assumption."""
    by = tag(samples, band_pct)
    counts = {k: len(v) for k, v in by.items()}
    means = {k: (round(statistics.mean(v), 4) if v else None) for k, v in by.items()}
    falling_n = counts[FALLING]
    enough = falling_n >= int(min_falling)

    # The figure to USE. Not an average across regimes: a blend is
    # dominated by whichever regime happened to be sampled most, and this
    # account's sample is almost entirely rising.
    if enough:
        use = means[FALLING]
        basis = (f"{falling_n} falling-market samples, mean {use:+.4f}% - the regime the "
                 f"conservative assumption existed to cover has now been measured")
    else:
        use = _num(assumed_pct)
        basis = (f"{falling_n} of {min_falling} falling-market samples. The measured "
                 f"figure comes almost entirely from rising markets, where a resting buy "
                 f"that fills is usually a bargain rather than a knife. The assumption "
                 f"stands until the case that matters has been tested.")

    return {
        "may_replace_assumption": enough,
        "adverse_pct_in_force": use,
        "assumed_pct": _num(assumed_pct),
        "basis": basis,
        "counts": counts,
        "means_by_regime": means,
        "falling_samples_needed": max(int(min_falling) - falling_n, 0),
        "worst_regime_mean": means[FALLING],
        "why_not_a_blend": ("A mean across regimes is dominated by whichever regime was "
                            "sampled most, and this sample is almost entirely rising. The "
                            "figure that must hold is the one from the worst regime."),
    }


def round_trip_cost_pct(fee_pct, adverse_pct):
    """Fees plus adverse selection. Never below the fees alone.

    A measured adverse figure can be NEGATIVE - being filled can be
    favourable on average in a rising market. Letting that reduce the cost
    below the fees actually paid would produce a gate that believes trading
    is cheaper than its own receipts.
    """
    f = _num(fee_pct)
    a = _num(adverse_pct)
    if f is None:
        return None
    if a is None:
        return round(f, 4)
    return round(max(f + a, f), 4)
