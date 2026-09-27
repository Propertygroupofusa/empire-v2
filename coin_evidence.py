"""How to know which coins deserve capital WITHOUT waiting for the trips.

THE BAR THAT DOES NOT MEASURE ANYTHING

The fleet's rule is that a coin's verdict needs 10 closed round trips. That
bar is on WIN RATE, and win rate is the one thing a grid cannot vary much:
a NORMAL exit only ever sells above its own entry, so almost every closed
trip is green by construction. Measured live, 9 of 14 coins sit between 75%
and 100% against a fleet baseline of 77%.

Run the two-proportion power calculation against that 77% baseline:

    coin at 70% win   ->   623 closed trips to tell it apart
    coin at 60% win   ->   116
    coin at 50% win   ->    49
    coin at 20% win   ->    11

Ten trips can only catch a coin below roughly 20%. Nothing else. So waiting
for the bar is not slow evidence-gathering - it is waiting for a number that
will still not separate the field when it arrives. At four trades a day
across the fleet, separating a merely mediocre coin on win rate would take
over a year.

WHAT ACTUALLY SEPARATES COINS

Return per trip as a PERCENTAGE of the slice, not dollars and not win rate.
Live it ranges 245x across the fleet, and much of the dollar spread is
allocation rather than edge, which is why it is normalised here.

And unlike win rate, it does not have to be waited for. The same rule can be
replayed over historical candles - horizon_study's rung_profile already does
exactly this, filling a resting limit exit only when a candle high reaches
through it - which produces hundreds of simulated trips per coin from 21
days of 5-minute bars, today.

THE ASYMMETRY THAT GOVERNS HOW REPLAY MAY BE USED

Already recorded in beta_check and re-stated here because it is the rule
that keeps this honest: over a window where the instruments ROSE, a
long-only result that is NEGATIVE is robust - the sample was favourable and
the coin still lost. A result that is POSITIVE proves nothing, because it
has never been shown a falling market.

So replay may ELIMINATE a coin. It may never crown one. This module enforces
that asymmetry rather than leaving it to a reader's discipline.
"""
from __future__ import annotations

import math
import os as _os

# Pseudo-counts for shrinking a coin's own rate toward the fleet's. A coin
# with two trips should read as "the fleet, slightly nudged", not as its own
# 100%. Not tuned - chosen so a coin needs roughly a handful of trips before
# its own evidence outweighs the prior.
PRIOR_STRENGTH = float(_os.getenv("COIN_EVIDENCE_PRIOR_STRENGTH", "8"))
# Two-sided 95% / 80% power, the usual pair. Stated so the sample sizes this
# reports can be checked rather than believed.
Z_ALPHA = 1.959964
Z_BETA = 0.8416212

FUND = "FUND"            # positive live edge, and replay has not refuted it
HOLD = "HOLD"            # not yet separable - keep it, do not add to it
STARVE = "STARVE"        # evidence it is below the fleet
ELIMINATED = "ELIMINATED"  # replay says it loses even in a favourable window


def trips_to_separate_win_rate(fleet_rate, coin_rate,
                               z_alpha=Z_ALPHA, z_beta=Z_BETA):
    """Closed trips needed to tell `coin_rate` apart from `fleet_rate`.

    Returns None when the two are equal - no sample size separates a
    difference that is not there, and reporting a huge number instead of
    None would read as "keep waiting" rather than "there is nothing here".
    """
    p0, p1 = float(fleet_rate), float(coin_rate)
    if not (0 < p0 < 1) or not (0 <= p1 <= 1) or abs(p0 - p1) < 1e-9:
        return None
    pbar = (p0 + p1) / 2.0
    num = (z_alpha * math.sqrt(2 * pbar * (1 - pbar))
           + z_beta * math.sqrt(p0 * (1 - p0) + p1 * (1 - p1))) ** 2
    return math.ceil(num / (p0 - p1) ** 2)


def detectable_win_rate(fleet_rate, n, *, lo=0.001, hi=None):
    """The worst win rate `n` trips can actually catch. None if none."""
    hi = fleet_rate if hi is None else hi
    best = None
    p = lo
    while p < hi:
        need = trips_to_separate_win_rate(fleet_rate, p)
        if need is not None and need <= n:
            best = p
        p += 0.01
    return best


def shrink(coin_value, coin_n, fleet_value, strength=None):
    """Coin's own number pulled toward the fleet's by its own thinness."""
    m = PRIOR_STRENGTH if strength is None else strength
    n = max(0, int(coin_n or 0))
    if n == 0:
        return float(fleet_value)
    return (coin_value * n + fleet_value * m) / (n + m)


def assess(coin, *, fleet_win_rate, fleet_mean_pct, replay_pct=None,
           replay_window_rose=None, min_trips_for_own_edge=None):
    """One coin's verdict, and which evidence decided it.

    `coin` carries: product_id, trips, wins, mean_pct (percentage return per
    trip, NOT dollars - dollars conflate edge with how much was allocated).

    `replay_pct` is the simulated per-attempt return from replaying the live
    rule over candles. `replay_window_rose` says whether the instruments rose
    over that window; when they did, a POSITIVE replay is not evidence and is
    reported as such rather than counted.
    """
    pid = coin.get("product_id")
    n = int(coin.get("trips") or 0)
    wins = int(coin.get("wins") or 0)
    rate = (wins / n) if n else None
    mean_pct = coin.get("mean_pct")

    shrunk_rate = shrink(rate if rate is not None else fleet_win_rate, n, fleet_win_rate)
    shrunk_pct = shrink(mean_pct if mean_pct is not None else fleet_mean_pct,
                        n, fleet_mean_pct)

    need = trips_to_separate_win_rate(fleet_win_rate, rate) if rate is not None else None
    out = {
        "product_id": pid, "trips": n, "wins": wins,
        "win_rate": round(rate * 100, 1) if rate is not None else None,
        "mean_pct": mean_pct,
        "shrunk_win_rate": round(shrunk_rate * 100, 1),
        "shrunk_mean_pct": round(shrunk_pct, 4),
        "trips_to_separate_on_win_rate": need,
        "replay_pct": replay_pct,
    }

    # Replay first: it is the only source that can act on day one, and the
    # only verdict it is allowed to deliver is a negative one.
    if replay_pct is not None and replay_pct < 0:
        out["verdict"] = ELIMINATED
        out["decided_by"] = "replay"
        out["why"] = (f"replaying the live rule over the sample window returns "
                      f"{replay_pct:+.3f}% per attempt"
                      + (" - and the instruments ROSE over that window, so a "
                         "negative result is robust: the sample was favourable "
                         "and it still lost" if replay_window_rose else ""))
        return out

    if replay_pct is not None and replay_pct > 0 and replay_window_rose:
        out["replay_is_not_evidence"] = True
        out["replay_note"] = ("positive replay over a RISING window is not evidence "
                              "of an edge - it has not been shown a falling market")

    # Live evidence. Judged on return, because win rate cannot separate.
    if mean_pct is not None and n > 0 and shrunk_pct < 0:
        out["verdict"] = STARVE
        out["decided_by"] = "live return"
        out["why"] = (f"{n} trip(s) pull its shrunk return to {shrunk_pct:+.4f}% "
                      f"against a fleet mean of {fleet_mean_pct:+.4f}%")
        return out

    floor = (min_trips_for_own_edge if min_trips_for_own_edge is not None
             else max(3, int(PRIOR_STRENGTH // 2)))
    if mean_pct is not None and n >= floor and shrunk_pct > fleet_mean_pct:
        out["verdict"] = FUND
        out["decided_by"] = "live return"
        out["why"] = (f"{n} trip(s) at {mean_pct:+.4f}% shrink to {shrunk_pct:+.4f}%, "
                      f"above the fleet's {fleet_mean_pct:+.4f}%")
        return out

    out["verdict"] = HOLD
    out["decided_by"] = "not separable yet"
    out["why"] = (f"{n} trip(s) is not enough to separate it from the fleet on "
                  f"return, and win rate would need "
                  + (f"{need:,} trips to separate at all" if need else
                     "a sample no fleet will ever produce, since its rate matches "
                     "the fleet's")
                  + " - the replay is the faster question to ask of this coin")
    return out


def report(coins, *, fleet_win_rate, fleet_mean_pct, replay=None,
           replay_window_rose=None, verdict_bar_trips=10):
    """Every coin assessed, plus what the current trips bar can actually catch."""
    replay = replay or {}
    rows = [assess(c, fleet_win_rate=fleet_win_rate, fleet_mean_pct=fleet_mean_pct,
                   replay_pct=replay.get(c.get("product_id")),
                   replay_window_rose=replay_window_rose)
            for c in (coins or ()) if hasattr(c, "get")]
    order = {STARVE: 0, ELIMINATED: 1, HOLD: 2, FUND: 3}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), -(r["trips"] or 0)))

    catches = detectable_win_rate(fleet_win_rate, verdict_bar_trips)
    buckets = {}
    for r in rows:
        buckets.setdefault(r["verdict"], []).append(r["product_id"])

    return {
        "coins": rows,
        "by_verdict": buckets,
        "fleet_win_rate_pct": round(fleet_win_rate * 100, 1),
        "fleet_mean_pct": fleet_mean_pct,
        "verdict_bar_trips": verdict_bar_trips,
        "bar_catches_below_win_rate_pct": (round(catches * 100, 1)
                                           if catches is not None else None),
        "bar_is_informative": catches is not None and catches > 0.35,
        "decides_nothing": True,
        "detail": (
            f"The {verdict_bar_trips}-trip bar is on win rate, which a grid holds near "
            f"{fleet_win_rate * 100:.0f}% by construction - a NORMAL exit only sells above "
            f"its own entry. At that baseline {verdict_bar_trips} trips can only catch a coin "
            + (f"below {catches * 100:.0f}% green" if catches is not None
               else "at no win rate at all")
            + ". Waiting for it is not slow evidence, it is a number that will not separate "
              "the field when it arrives. Return per trip separates; replay supplies it today, "
              "and may only ELIMINATE."),
    }
