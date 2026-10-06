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


# The live fleet's maker wait, in seconds. A rung is placed post-only and
# cancelled unfilled after this long. 3739 orders have expired against it.
LIVE_MAKER_WAIT_SECONDS = 3600


def _wait_bars(wait_seconds, gran_seconds):
    """How many bars an order may rest for, INCLUDING the bar it was placed on.

    Counting the placement bar is what makes a one-bar wait identical to the
    step engine's same-bar fill, which is the property test_incubator.py
    asserts. Without it the two engines would disagree by construction and
    every wait comparison would be measuring the change in model rather than
    the change in the rule.
    """
    if not gran_seconds or gran_seconds <= 0:
        return 1
    return max(1, int(round((wait_seconds or 0) / float(gran_seconds))))


def simulate_wait(bars, step, levels=3, alloc=1000.0, wait_seconds=None,
                  gran_seconds=900):
    """The same grid, with a post-only order that can rest for `wait_seconds`.

    WHAT CHANGES, AND ONLY THIS. The step engine fills a rung the instant a
    bar's range covers the price - a same-bar fill, which is the most
    generous assumption available. Here an order is PLACED when the cycle
    condition fires and then has to be crossed by some bar inside its
    lifetime; if no bar crosses it, it is cancelled and the cycle passes,
    exactly as the live fleet cancels a post-only rung it could not get
    filled. Nothing else differs: the same reference, the same re-anchor,
    the same fee floor on the sell, the same one-close-per-bar rule.

    WHY A LONGER WAIT IS NOT OBVIOUSLY BETTER. It collects fills the short
    wait gave up on, and the dashboard measures that at 1.2367% per expiry
    over 72h on n=53. It also holds a rung open through moves that keep
    going against it, which is where adverse selection lives - and the same
    panel measures adverse selection at -0.083% on a sample that never saw a
    falling market. Those two numbers point opposite ways on small samples,
    which is the entire reason this is a candidate to incubate rather than a
    change to make.

    With wait_seconds <= one bar this reduces EXACTLY to simulate().
    """
    if not bars:
        return None
    if wait_seconds is None:
        wait_seconds = LIVE_MAKER_WAIT_SECONDS
    span = _wait_bars(wait_seconds, gran_seconds)
    slice_usd = alloc / levels
    ref = bars[0][3]
    open_slices, closed = [], []
    expired_buys = expired_sells = 0
    # PLACEMENTS, counted. The live panel reports "attempted" beside
    # "filled" and "expired" for exactly this reason: without it, a rule
    # that refuses to place is indistinguishable from one that places and
    # never fills. It is also the only way to observe the fee floor at
    # PLACEMENT - mutation testing removed that floor and no assertion
    # noticed, because the fill-time check silently covered for it.
    placed_buys = placed_sells = 0
    # One resting order at a time, which is what the live fleet does: the
    # cycle is sequential and a branch places its next rung only after the
    # last one resolved.
    resting = None            # {"side","price","bars_left","slice"}
    for _t, low, high, close in bars:
        # ---- PLACE only once the TRIGGER has been reached ----
        #
        # This matches the live bot, which does not rest a rung at a price
        # the market has not come to: run_grid_branch_cycle places a sell
        # only after price >= reference x (1 + step), and a buy only once
        # price <= the trigger. The order is then POST-ONLY at the bid or
        # ask, and what expires it is not "price never arrived" - it is
        # "price arrived and nobody crossed me". The live expiry rows say
        # exactly that: "the order rested at the ask and no buyer crossed".
        #
        # TWO EARLIER DRAFTS OF THIS FUNCTION WERE WRONG, in opposite
        # directions, and both are worth leaving written down.
        #   1. The first consumed the placement bar and checked the fill
        #      only from the next one. 593 of 1200 random tapes then
        #      disagreed with the step engine on a one-bar wait.
        #   2. The second placed a resting order every bar regardless of
        #      the trigger, which made the rung a standing order the live
        #      fleet never places. It expired constantly and blocked its
        #      own branch's buys.
        # The second draft also produced the tell that caught it: three
        # different coins returned an identical 6 round trips and $46.00 at
        # every wait from 15 minutes to 12 hours.
        if resting is None:
            if not open_slices:
                ref = close
            if open_slices:
                best = min(open_slices, key=lambda s: s["entry"])
                target = best["entry"] * (1 + step)
                if (target / best["entry"] - 1.0) - FEE_ROUND_TRIP > 0 and high > target:
                    resting = {"side": "sell", "price": target,
                               "bars_left": span, "slice": best}
                    placed_sells += 1
            if resting is None:
                trigger = ref * (1 - step)
                if len(open_slices) < levels and low < trigger:
                    resting = {"side": "buy", "price": trigger,
                               "bars_left": span, "slice": None}
                    placed_buys += 1
            # The placement bar is NOT a fill. At the moment of placing,
            # price was AT the level and a post-only order there sits behind
            # everything already resting. It has to be crossed by a LATER
            # bar. This is where this engine is deliberately stricter than
            # simulate(), which fills on the same bar - so the two do NOT
            # agree at a one-bar wait, and must not be expected to. The
            # comparison this engine exists for is candidate wait against
            # LIVE wait on identical bars, where the engine is the same on
            # both sides and cancels out.
            continue

        # ---- RESOLVE, on every bar of the order's remaining lifetime ----
        crossed = (low < resting["price"]) if resting["side"] == "buy" \
                  else (high > resting["price"])
        if crossed:
            if resting["side"] == "buy":
                open_slices.append({"entry": resting["price"], "usd": slice_usd})
                ref = resting["price"]
            else:
                sl = resting["slice"]
                net = (resting["price"] / sl["entry"] - 1.0) - FEE_ROUND_TRIP
                # UNREACHABLE BY CONSTRUCTION, and kept deliberately.
                #
                # The resting price is entry x (1 + step), fixed at
                # placement, and placement already requires that to clear
                # both fee legs - so net is a constant and this branch
                # cannot currently be false. A comment here used to claim it
                # protected "a rung that rested through a fall", which was
                # wrong: the rung's price does not move, so a fall cannot
                # reach it. Mutation testing caught it - deleting the check
                # changed no test, because no tape can exercise it.
                #
                # It stays as a guard against the obvious next change: the
                # moment a resting sell is RE-PRICED to the live ask rather
                # than held at the target, this becomes reachable and is the
                # only thing standing between a rested order and a booked
                # loss. The floor that does the work today is at placement.
                if net > 0:
                    closed.append(net * sl["usd"])
                    if sl in open_slices:
                        open_slices.remove(sl)
                    ref = resting["price"]
            resting = None
            continue
        resting["bars_left"] -= 1
        if resting["bars_left"] <= 0:
            if resting["side"] == "buy":
                expired_buys += 1
            else:
                expired_sells += 1
            resting = None
    return {
        "round_trips": len(closed),
        "net_usd": round(sum(closed), 2),
        "open_at_end": len(open_slices),
        "net_pct_of_alloc": round(100.0 * sum(closed) / alloc, 3),
        "expired_buys": expired_buys,
        "expired_sells": expired_sells,
        "placed_buys": placed_buys,
        "placed_sells": placed_sells,
        "wait_seconds": wait_seconds,
        "wait_bars": span,
    }


def incubate_wait(bars, armed_at_epoch, wait_seconds, step, levels=3,
                  alloc=1000.0, gran_seconds=900, min_live_trips=20,
                  baseline_wait_seconds=None):
    """A longer rest, scored forward, AGAINST THE LIVE WAIT ON THE SAME BARS.

    This candidate differs from a step candidate in what it is compared to.
    A step candidate's backtest window is the same rule on older bars. A
    wait candidate's real question is "would a longer rest have done better
    THAN THE ONE WE RUN", so the live wait is simulated on the identical
    bars and the difference between them is the answer. Comparing a long
    wait on new bars against a long wait on old bars would measure the
    market, not the rule.
    """
    if baseline_wait_seconds is None:
        baseline_wait_seconds = LIVE_MAKER_WAIT_SECONDS
    before, after = split_bars(bars, armed_at_epoch)
    cand_live = simulate_wait(after, step, levels, alloc, wait_seconds, gran_seconds)
    base_live = simulate_wait(after, step, levels, alloc, baseline_wait_seconds,
                              gran_seconds)
    cand_back = simulate_wait(before, step, levels, alloc, wait_seconds, gran_seconds)
    base_back = simulate_wait(before, step, levels, alloc, baseline_wait_seconds,
                              gran_seconds)

    def per_day(rec, window):
        if not rec or not window:
            return None
        days = (len(window) * gran_seconds) / 86400.0
        return (rec["net_usd"] / days) if days > 0 else None

    trips = cand_live["round_trips"] if cand_live else 0
    enough = trips >= min_live_trips
    cl, bl = per_day(cand_live, after), per_day(base_live, after)
    edge = None if (cl is None or bl is None) else round(cl - bl, 4)
    return {
        "candidate": "maker_wait",
        "armed_at_epoch": armed_at_epoch,
        "wait_seconds": wait_seconds,
        "baseline_wait_seconds": baseline_wait_seconds,
        "step": step, "levels": levels, "alloc_usd": alloc,
        "live": {
            "bars": len(after),
            "span_days": round((len(after) * gran_seconds) / 86400.0, 2),
            "candidate": cand_live, "live_wait": base_live,
            "candidate_usd_per_day": round(cl, 4) if cl is not None else None,
            "live_wait_usd_per_day": round(bl, 4) if bl is not None else None,
        },
        "backtest": {
            "bars": len(before),
            "span_days": round((len(before) * gran_seconds) / 86400.0, 2),
            "candidate": cand_back, "live_wait": base_back,
        },
        "edge_usd_per_day": edge,
        "verdict": "READABLE" if enough else "TOO EARLY",
        "closed_trips_needed": min_live_trips,
        "compared_against": ("the LIVE wait on the IDENTICAL bars, not the same "
                             "candidate on older bars. The question is whether a "
                             "longer rest beats the one we run; comparing new bars "
                             "to old ones would measure the market instead."),
        "placed_nothing": True,
    }
