"""Run a candidate forward on live prices, with no money on it.

WHY THIS EXISTS. Five candidate changes were backtested on this fleet's
real tape and five were rejected, because each looked good on the window
it was fitted to and failed on the window it was not. Widening the sell
target looked like +$100 to +$350 across three ten-day segments and then
lost on a down window. That is not a backtesting failure; it is what a
backtest IS - a measurement of a window that has already happened.

An incubator is the missing middle step. A candidate is ARMED at a moment
in time and from then on is scored on bars that did not exist when it was
chosen. Nothing is fitted, nothing is re-chosen, and the result is a
forward test the candidate cannot have been tuned to. The question it
answers is the account owner's: how far does live incubation diverge from
what the backtest promised?

WHAT IS STORED: the candidate and the minute it was armed. NOTHING ELSE.
Every number on the page is recomputed from the public tape on each read,
so a result cannot go stale, cannot be edited after the fact, and can be
re-derived from scratch by anyone with the same candles. A stored P&L
would be a number nobody can check.

WHAT IS NOT DONE: no order, no slice, no branch, no cash. An incubating
candidate is invisible to the fleet, and the fleet is invisible to it.
"""

import math

# The live maker tier, per leg. Both legs are charged, which is the rule
# that killed the penny-profit-target candidate: a one-cent net close
# still has to clear 0.70%, so it pays full fare to collect a penny.
FEE_LEG = 0.0035
FEE_ROUND_TRIP = FEE_LEG * 2

# How a cohort is stored: one row in the existing key-value state table,
# whose NAME carries the candidate and whose VALUE is the unix second it
# was armed. No new table, no migration, and the definition is legible
# without a decoder.
COHORT_PREFIX = "incubator:"


def cohort_key(product_id, param, value):
    """incubator:BTC-USD:step=0.040 - readable in a database client."""
    return f"{COHORT_PREFIX}{product_id}:{param}={value}"


def parse_cohort_key(key):
    """The inverse. Returns None for anything that is not a cohort row, so
    a stray row in a shared table can never be read as a candidate."""
    if not key or not key.startswith(COHORT_PREFIX):
        return None
    rest = key[len(COHORT_PREFIX):]
    if ":" not in rest or "=" not in rest:
        return None
    product_id, assignment = rest.split(":", 1)
    param, _, value = assignment.partition("=")
    if not product_id or not param or not value:
        return None
    try:
        value = float(value)
    except ValueError:
        return None
    return {"product_id": product_id, "param": param, "value": value}


def simulate(bars, step, levels=3, alloc=1000.0):
    """One grid branch over one window. Returns the closed-trade record.

    THIS IS THE BACKTEST'S OWN FILL MODEL, deliberately identical, because
    the whole question is "how far did live diverge from the backtest" and
    two different engines would answer a different question - the gap would
    be partly the candidate and partly the simulator, with no way to tell
    which. test_incubator.py asserts the two implementations agree bar for
    bar on random inputs, so the day they drift, that test fails rather
    than the comparison quietly becoming meaningless.

    `bars` are (t, low, high, close), oldest first.
    """
    if not bars:
        return None
    slice_usd = alloc / levels
    ref = bars[0][3]
    open_slices, closed = [], []
    for _t, low, high, close in bars:
        # A FLAT BRANCH TRACKS THE MARKET. Without this the reference stays
        # pinned where the last fill happened, so after any sustained rise
        # the buy trigger sits permanently out of reach. The live fleet
        # re-anchors flat branches for exactly this reason.
        if not open_slices:
            ref = close
        # SELL FIRST, and a bar that both fills a rung and closes one counts
        # only as the close. Counting both would invent a round trip out of
        # a single bar, which is the classic way a simulator beats reality.
        if open_slices:
            best = min(open_slices, key=lambda s: s["entry"])
            target = best["entry"] * (1 + step)
            if high > target:
                net = (target / best["entry"] - 1.0) - FEE_ROUND_TRIP
                if net > 0:
                    closed.append(net * best["usd"])
                    open_slices.remove(best)
                    ref = target
                    continue
        trigger = ref * (1 - step)
        if len(open_slices) < levels and low < trigger:
            open_slices.append({"entry": trigger, "usd": slice_usd})
            ref = trigger
    return {
        "round_trips": len(closed),
        "net_usd": round(sum(closed), 2),
        "open_at_end": len(open_slices),
        "net_pct_of_alloc": round(100.0 * sum(closed) / alloc, 3),
    }


def split_bars(bars, armed_at_epoch):
    """Bars BEFORE arming and bars AFTER, and never the two mixed.

    This split is the only thing standing between an incubator and a
    second backtest. A bar at the arming instant belongs to NEITHER side:
    it was partly formed when the decision was made, so counting it as
    evidence for the candidate hands it a sliver of hindsight.
    """
    before = [b for b in bars if b[0] < armed_at_epoch]
    after = [b for b in bars if b[0] > armed_at_epoch]
    return before, after


def _per_day(record, bars, gran_seconds):
    """Normalize a window's result to a daily rate, or UNKNOWN."""
    if not record or not bars:
        return None, None
    span_days = (len(bars) * gran_seconds) / 86400.0
    if span_days <= 0:
        return None, None
    return record["net_usd"] / span_days, record["round_trips"] / span_days


def incubate(bars, armed_at_epoch, step, levels=3, alloc=1000.0,
             gran_seconds=900, min_live_trips=20):
    """What the backtest promised, what live has delivered, and the gap.

    Both sides run the same engine on the same product at the same
    granularity. The only difference between them is which side of the
    arming moment the bars fall on, which is the point.
    """
    before, after = split_bars(bars, armed_at_epoch)
    expected = simulate(before, step, levels, alloc)
    live = simulate(after, step, levels, alloc)
    exp_day, exp_trips_day = _per_day(expected, before, gran_seconds)
    live_day, live_trips_day = _per_day(live, after, gran_seconds)

    live_days = (len(after) * gran_seconds) / 86400.0 if after else 0.0
    trips = live["round_trips"] if live else 0

    # THE GATE. A candidate three days into incubation with two closed
    # trips has not been tested; it has been glanced at. Reporting a
    # divergence from that is how a fitted number gets a second life as a
    # "forward result". The bar is closed ROUND TRIPS, not elapsed days,
    # because a quiet market can produce a long window with no evidence
    # in it at all.
    enough = trips >= min_live_trips
    if exp_trips_day and exp_trips_day > 0:
        days_needed = max(0.0, (min_live_trips - trips) / exp_trips_day)
    else:
        days_needed = None

    gap_pct = None
    if exp_day not in (None, 0) and live_day is not None:
        gap_pct = (live_day - exp_day) / abs(exp_day) * 100.0

    return {
        "armed_at_epoch": armed_at_epoch,
        "step": step, "levels": levels, "alloc_usd": alloc,
        "backtest": {
            "bars": len(before),
            "span_days": round((len(before) * gran_seconds) / 86400.0, 2) if before else 0,
            "round_trips": expected["round_trips"] if expected else None,
            "net_usd": expected["net_usd"] if expected else None,
            "net_usd_per_day": round(exp_day, 4) if exp_day is not None else None,
            "round_trips_per_day": round(exp_trips_day, 3) if exp_trips_day is not None else None,
            "means": ("The window BEFORE the candidate was armed. It is the "
                      "number the candidate was chosen on, which is exactly why "
                      "it cannot be the number it is judged on."),
        },
        "live": {
            "bars": len(after),
            "span_days": round(live_days, 2),
            "round_trips": trips,
            "net_usd": live["net_usd"] if live else None,
            "net_usd_per_day": round(live_day, 4) if live_day is not None else None,
            "round_trips_per_day": round(live_trips_day, 3) if live_trips_day is not None else None,
            "means": ("Bars that did not exist when the candidate was chosen. "
                      "Nothing here was fitted and nothing was re-chosen."),
        },
        "gap_pct_per_day": round(gap_pct, 1) if gap_pct is not None else None,
        "verdict": ("READABLE" if enough else "TOO EARLY"),
        "closed_trips_needed": min_live_trips,
        "more_days_at_this_rate": round(days_needed, 1) if days_needed is not None else None,
        "verdict_means": (
            f"A divergence is only worth reading once the live side has "
            f"{min_live_trips} closed round trips. Below that the gap is "
            "arithmetic on a handful of trades, and reporting it would give a "
            "fitted number a second life as a 'forward result'."),
        "placed_nothing": True,
    }
