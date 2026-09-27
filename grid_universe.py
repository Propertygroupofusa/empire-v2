"""Which coins deserve a grid branch, measured rather than assumed.

WHY A SELECTOR AND NOT A LONGER LIST

A grid earns when price oscillates through its rungs and loses when price
leaves and does not come back. So the property that decides whether a coin
is worth a branch is not "is it liquid" or "do I like it" - it is how
often a round trip of the fleet's own step width actually COMPLETES, and
how much gets stranded when it does not.

Both halves matter and only counting the first is how a grid looks
profitable right up until the market trends. ADAPTIVE_FLEET_STAGES is a
hand-written sequence gated on realized P&L; it says nothing about whether
any coin on it oscillates. This scores that directly.

WHAT THE SCORE IS

    completions per day      round trips the step width actually closed
    stranded fraction        rungs left open at the end, marked to market
    net per day              the two combined, as a percent of the capital
                             that coin's branch would hold

net_per_day is the one that ranks. A coin with many completions and a
heavy bag is worse than a coin with fewer and none, and only the combined
figure knows that.

WHAT THIS DOES NOT DO

It does not create branches, move capital, or place an order. Adding a
branch earmarks real cash, so this produces a ranked recommendation and
the reasons behind it, and a human decides. It also cannot tell you the
future: every number here is what a coin DID over the lookback, on the
fleet's current step, and a coin that oscillated last month can trend
next month.
"""
from __future__ import annotations

# The fleet's live geometry. Passed in rather than imported so the scoring
# can be run against a proposed step as easily as the current one.
DEFAULT_STEP_PCT = 3.75
DEFAULT_LEVELS = 3
DEFAULT_FEE_PCT = 0.70

# A coin must actually complete round trips to earn. One a week is not a
# branch, it is capital parked with extra steps.
MIN_COMPLETIONS_PER_DAY = 0.25

# Above this share of rungs left stranded, the coin is trending, not
# oscillating, and the branch is a slow way of buying the dip forever.
MAX_STRANDED_FRACTION = 0.60


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def replay(bars, *, step_pct=DEFAULT_STEP_PCT, levels=DEFAULT_LEVELS,
           fee_pct=DEFAULT_FEE_PCT, bars_per_day=24):
    """What a grid on this coin would have done. Wins AND the bag.

    `bars` is oldest-first with low/high/close. A rung may not open and
    close on the same bar: intrabar ordering is unknowable, and allowing
    it is the single easiest way to manufacture a profitable backtest.
    """
    rows = [b for b in (bars or ()) if isinstance(b, dict)]
    if len(rows) < 30:
        return {"ok": False, "reason": "NOT_ENOUGH_HISTORY",
                "detail": f"{len(rows)} bars; at least 30 are needed"}
    step = _num(step_pct)
    if step is None or step <= 0:
        return {"ok": False, "reason": "BAD_STEP", "detail": f"step {step_pct!r}"}
    fee = _num(fee_pct) or 0.0
    if step <= fee:
        return {"ok": False, "reason": "STEP_UNDER_FEE",
                "detail": f"a {step}% step cannot clear a {fee}% round trip"}

    first = _num(rows[0].get("close"))
    if first is None or first <= 0:
        return {"ok": False, "reason": "UNPRICED", "detail": "no opening close"}

    anchor = first
    open_rungs = []          # (entry_price, bar_index)
    completions = 0
    realised = 0.0
    for i, bar in enumerate(rows):
        hi = _num(bar.get("high"))
        lo = _num(bar.get("low"))
        cl = _num(bar.get("close"))
        if hi is None or lo is None or cl is None:
            continue
        keep = []
        for entry, opened in open_rungs:
            if opened < i and hi >= entry * (1 + step / 100.0):
                completions += 1
                realised += step - fee
            else:
                keep.append((entry, opened))
        open_rungs = keep
        while len(open_rungs) < levels:
            nxt = anchor * (1 - step / 100.0 * (len(open_rungs) + 1))
            if lo <= nxt:
                open_rungs.append((nxt, i))
            else:
                break
        if not open_rungs and cl > anchor:
            anchor = cl

    last = _num(rows[-1].get("close")) or first
    unrealised = sum((last / e - 1) * 100 - fee / 2.0 for e, _ in open_rungs)
    days = max(len(rows) / float(bars_per_day), 1e-9)
    opened_total = completions + len(open_rungs)

    return {
        "ok": True,
        "bars": len(rows),
        "days": round(days, 2),
        "completions": completions,
        "completions_per_day": round(completions / days, 3),
        "stranded": len(open_rungs),
        "stranded_fraction": round(len(open_rungs) / opened_total, 3) if opened_total else 0.0,
        "realised_pct": round(realised, 3),
        "unrealised_pct": round(unrealised, 3),
        "net_pct": round(realised + unrealised, 3),
        "net_per_day_pct": round((realised + unrealised) / days, 4),
        "step_pct": step,
        "levels": levels,
    }


def verdict(r, *, min_completions=MIN_COMPLETIONS_PER_DAY,
            max_stranded=MAX_STRANDED_FRACTION):
    """Worth a branch, or the reason it is not."""
    if not r.get("ok"):
        return "NO_DATA", r.get("detail", "")
    # Order matters. A coin in freefall completes NOTHING, so a
    # completions-first check labels a crash "too still - capital parked",
    # which is the opposite of what is happening and would read as
    # harmless. Stranding is tested first because it is the condition that
    # distinguishes "nothing moved" from "everything moved one way".
    if r["stranded_fraction"] > max_stranded:
        return "TRENDING", (f"{r['stranded_fraction']:.0%} of rungs left open - the price "
                            f"left and did not come back, so the branch is a slow way of "
                            f"buying a falling coin")
    if r["completions_per_day"] < min_completions:
        return "TOO_STILL", (f"{r['completions_per_day']}/day round trips - below "
                             f"{min_completions}/day this is capital parked, not traded")
    if r["net_per_day_pct"] <= 0:
        return "LOSES", (f"{r['net_per_day_pct']:+.4f}%/day once the bag is counted, "
                         f"despite {r['completions']} completed round trips")
    return "WORTH_A_BRANCH", (f"{r['completions_per_day']}/day round trips, "
                              f"{r['stranded_fraction']:.0%} stranded, "
                              f"{r['net_per_day_pct']:+.4f}%/day net")


def rank(candles_by_coin, *, current=(), step_pct=DEFAULT_STEP_PCT,
         levels=DEFAULT_LEVELS, fee_pct=DEFAULT_FEE_PCT, bars_per_day=24):
    """Every candidate scored and ordered. Current branches are marked.

    Ordered by net_per_day_pct, because completions alone rewards exactly
    the coins that fill fast on the way down and never come back.
    """
    have = {c.upper() for c in (current or ())}
    out = []
    for coin, bars in sorted((candles_by_coin or {}).items()):
        r = replay(bars, step_pct=step_pct, levels=levels, fee_pct=fee_pct,
                   bars_per_day=bars_per_day)
        v, why = verdict(r)
        out.append({"coin": coin.upper(), "in_fleet": coin.upper() in have,
                    "verdict": v, "why": why, **r})
    # `or` treats 0.0 as missing, which sorted a coin earning exactly
    # nothing BELOW one losing 14% a day. Missing has to be tested for,
    # not inferred from falsiness, wherever zero is a real value.
    def _key(x):
        v = x.get("net_per_day_pct")
        return -(v if v is not None else -1e9)
    out.sort(key=_key)
    return out


def recommend(ranked, *, max_branches=None, keep_current=True):
    """What to add, what to keep, what to drop - and the honest caveat.

    Splitting fixed capital across more branches makes each one smaller,
    so adding a coin is only worth it if that coin earns more per dollar
    than the dollars it takes away from the others. That comparison is
    exactly `net_per_day_pct`, which is why the ranking is on a per-dollar
    rate and not on totals.
    """
    good = [r for r in ranked if r["verdict"] == "WORTH_A_BRANCH"]
    if max_branches:
        good = good[:int(max_branches)]
    chosen = {r["coin"] for r in good}
    add = [r for r in good if not r["in_fleet"]]
    drop = [r for r in ranked if r["in_fleet"] and r["coin"] not in chosen]
    keep = [r for r in good if r["in_fleet"]]
    return {
        "is_a_recommendation_not_a_change": True,
        "branches_now": sum(1 for r in ranked if r["in_fleet"]),
        "branches_recommended": len(good),
        "add": [{"coin": r["coin"], "net_per_day_pct": r["net_per_day_pct"],
                 "completions_per_day": r["completions_per_day"],
                 "stranded_fraction": r["stranded_fraction"], "why": r["why"]} for r in add],
        "keep": [r["coin"] for r in keep],
        "drop": [{"coin": r["coin"], "verdict": r["verdict"], "why": r["why"]}
                 for r in drop] if keep_current is False else [],
        "flagged_current": [{"coin": r["coin"], "verdict": r["verdict"], "why": r["why"]}
                            for r in ranked if r["in_fleet"] and r["coin"] not in chosen],
        "caveat": ("These are the rates each coin DID produce over the lookback at the "
                   "fleet's current step. A coin that oscillated last month can trend "
                   "next month, and adding branches splits the same capital thinner - "
                   "more coins is only better if the added ones earn more per dollar "
                   "than the dollars they take from the rest."),
    }
