"""The tightest grid spacing a coin can actually survive, measured.

WHAT WAS WRONG

fee_safe_floor_pct() floors spacing at TARGET_NET_MARGIN_PCT + 2 legs of
the worst-case fee. With the live numbers that is 0.90%, and the dashboard
says so: "every branch's spacing is floored at 0.90%, the smallest move
that can actually clear that fee."

That sentence is true and the floor is still too low, because clearing the
fee is not the same as making money. A completed round trip at 0.90% does
clear its fees. What the fee calculation cannot see is the rungs that fill
and never close - the inventory a tight grid accumulates on the way down.
Measured out of sample across three timeframes, a 0.90% step lost:

    5-minute   -0.055%/hour
    15-minute  -0.104%/hour
    1-hour     -0.088%/hour

Every one negative, on a setting the floor certifies as safe. The loss is
not in the fees; it is in the bag.

WHAT THIS ADDS

A second floor, derived from what a coin actually DID rather than from
what its fees cost: the tightest step at which a full replay - completed
round trips AND open rungs marked to market - comes out positive, plus a
margin so a step does not qualify by a hair.

The fleet floor becomes max(fee_safe, measured). Neither replaces the
other: the fee floor is an arithmetic guarantee about one round trip, this
is an empirical statement about a whole cycle, and a spacing needs to pass
both.

WHAT IT CANNOT DO

It is a measurement of the past on one coin over one window. A coin that
oscillated last month can trend next month and its measured floor will
move. So this reports the floor WITH the evidence behind it - how many
round trips, how many stranded, over how long - and refuses rather than
guesses when the window is too thin to support an answer.
"""
from __future__ import annotations

# Steps considered, tightest first. The answer is the first one that
# survives, so the order is the search.
CANDIDATE_STEPS = (0.9, 1.2, 1.5, 2.0, 2.5, 3.0, 3.75, 5.0)

# A step must beat zero by this much to count. Qualifying by a hair is
# how a floor ends up one bad week from being wrong.
MARGIN_PCT_PER_DAY = 0.05

# Below this there is not enough evidence to name a floor at all.
MIN_COMPLETIONS = 5
MIN_BARS = 120


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def measured_floor(bars, *, steps=CANDIDATE_STEPS, fee_pct=0.70, levels=3,
                   bars_per_day=24, margin=MARGIN_PCT_PER_DAY,
                   min_completions=MIN_COMPLETIONS, min_bars=MIN_BARS):
    """Tightest step this coin survived, with the evidence for it.

    Returns `floor_pct` None when no step qualifies - which is a real
    answer meaning "nothing this coin did supports any spacing", not a
    reason to fall back on the fee floor quietly.
    """
    import grid_universe

    rows = [b for b in (bars or ()) if isinstance(b, dict)]
    if len(rows) < min_bars:
        return {"ok": False, "reason": "NOT_ENOUGH_HISTORY",
                "detail": f"{len(rows)} bars, {min_bars} needed to name a floor",
                "floor_pct": None}

    tried = []
    floor = None
    for step in steps:
        r = grid_universe.replay(rows, step_pct=step, levels=levels,
                                 fee_pct=fee_pct, bars_per_day=bars_per_day)
        if not r.get("ok"):
            tried.append({"step_pct": step, "ok": False, "reason": r.get("reason")})
            continue
        rate = r["net_per_day_pct"]
        enough = r["completions"] >= min_completions
        passes = enough and rate > margin
        tried.append({"step_pct": step, "net_per_day_pct": rate,
                      "completions": r["completions"], "stranded": r["stranded"],
                      "qualifies": passes,
                      "why": (None if passes else
                              ("only %d completed round trips, %d needed" %
                               (r["completions"], min_completions) if not enough else
                               "%+.4f%%/day does not clear the %+.2f%% margin"
                               % (rate, margin)))})
        if passes and floor is None:
            floor = step

    return {
        "ok": True,
        "floor_pct": floor,
        "bars": len(rows),
        "days": round(len(rows) / float(bars_per_day), 2),
        "margin_pct_per_day": margin,
        "tried": tried,
        "detail": (f"tightest surviving step {floor}%" if floor is not None else
                   "no step in the candidate set came out positive on this window"),
    }


def fleet_floor(book, *, fee_safe_pct, **kw):
    """One floor for the fleet: max(fee-safe, the worst coin's measured).

    The MAXIMUM across coins, not the average. A fleet-wide spacing has to
    be survivable on every coin it is applied to, and averaging lets a
    coin that needs 2.5% run at 1.2% because others did better.
    """
    per, unmeasurable = {}, []
    for coin, bars in (book or {}).items():
        r = measured_floor(bars, **kw)
        per[coin] = r
        if not r.get("ok") or r.get("floor_pct") is None:
            unmeasurable.append(coin)

    floors = [r["floor_pct"] for r in per.values()
              if r.get("ok") and r.get("floor_pct") is not None]
    measured = max(floors) if floors else None
    fee = _num(fee_safe_pct)

    if measured is None:
        recommended = fee
        basis = ("no coin produced a measurable floor, so the fee floor stands alone - "
                 "which is the state that allowed a 0.90% spacing to lose money")
    elif fee is None:
        recommended = measured
        basis = "fee floor unavailable; measured floor used alone"
    else:
        recommended = max(fee, measured)
        basis = (f"max(fee-safe {fee:.2f}%, measured {measured:.2f}%) - a spacing has to "
                 f"pass both: the fee floor guarantees one round trip clears its costs, "
                 f"the measured floor says the whole cycle came out positive")

    return {
        "recommended_floor_pct": recommended,
        "fee_safe_pct": fee,
        "measured_floor_pct": measured,
        "basis": basis,
        "coins_measured": len(per) - len(unmeasurable),
        "coins_unmeasurable": unmeasurable,
        "per_coin": {c: {"floor_pct": r.get("floor_pct"), "days": r.get("days"),
                         "detail": r.get("detail")} for c, r in per.items()},
        "caveat": ("A measured floor is the past on one window. A coin that oscillated "
                   "can trend, and its floor moves with it - this is a bound to re-measure, "
                   "not a constant to set once."),
    }
