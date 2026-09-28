"""Where today's equity actually sits in the account's own history.

THE QUESTION THIS ANSWERS. The owner, 28 Sep: the account "hasn't been
this low in months". That has been repeated all day and NOTHING had
measured it - the only figure anyone had was today's session P&L. A
belief about a multi-month low, carried forward unchecked, is exactly
the kind of claim that starts driving decisions.

Alpaca's /v2/account/portfolio/history returns parallel arrays -
timestamp, equity, profit_loss - so the curve already exists. Nobody
was reading it.

WHAT THIS REFUSES TO DO.

 - It never answers beyond its window. "Lowest in 90 days" is a
   different statement from "lowest ever", and a summary that does not
   name its own span invites the stronger reading. The span, its first
   and last date, and the number of real points are always returned.
 - It never treats a null as a zero. Alpaca returns nulls for points
   with no data (holidays, gaps, an account younger than the window).
   A zero equity reading would make every other day look like a high.
   Nulls are dropped and COUNTED, so a curve that is mostly gaps
   reports as mostly gaps.
 - It refuses the comparison outright when the window is too short to
   support it. If someone asks "lowest in months" of three weeks of
   data, the honest answer is that the data does not go back far
   enough - not a confident verdict drawn from what happens to exist.
"""

from datetime import datetime, timezone

# A "months" claim needs at least this much real history behind it.
# Below it, this module says so instead of answering.
MONTHS_CLAIM_MIN_DAYS = 60


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _points(history):
    """(timestamp, equity) pairs with every unreadable point dropped."""
    ts = (history or {}).get("timestamp") or []
    eq = (history or {}).get("equity") or []
    pairs, dropped = [], 0
    for i in range(max(len(ts), len(eq))):
        t = ts[i] if i < len(ts) else None
        e = _num(eq[i]) if i < len(eq) else None
        # A ZERO EQUITY READING IS A GAP, NOT A BALANCE. Alpaca sends 0
        # or null for a point it has no data for, and a single one of
        # those would make every other day in the window look like a
        # high-water mark.
        if t is None or e is None or e <= 0:
            dropped += 1
            continue
        pairs.append((int(t), e))
    pairs.sort(key=lambda p: p[0])
    return pairs, dropped


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def summarise(history, current_equity=None):
    """Place current equity in its own history, with the span stated.

    current_equity overrides the last point when given - the live
    account is more current than the last daily bar, and using a bar
    from this morning to answer "where are we now" would be a stale
    denominator.
    """
    pairs, dropped = _points(history)
    if not pairs:
        return {"readable": False,
                "reason": ("no usable equity points - every value was "
                           "missing, unparseable or non-positive"),
                "dropped_points": dropped}

    now = _num(current_equity)
    if now is None:
        now = pairs[-1][1]
        current_is_live = False
    else:
        current_is_live = True

    values = [e for _, e in pairs]
    lo_ts, lo = min(pairs, key=lambda p: p[1])
    hi_ts, hi = max(pairs, key=lambda p: p[1])
    span_days = (pairs[-1][0] - pairs[0][0]) / 86400.0

    below = [p for p in pairs if p[1] < now]
    at_or_above = len(pairs) - len(below)
    pct_rank = round(len(below) / len(pairs) * 100.0, 1)

    # How far back you have to go to find a lower reading. None means
    # there is no lower reading in the window at all.
    days_since_lower = None
    for ts, e in reversed(pairs):
        if e < now:
            days_since_lower = round((pairs[-1][0] - ts) / 86400.0, 1)
            break

    out = {
        "readable": True,
        "current_equity": round(now, 2),
        "current_is_live_account": current_is_live,
        "window": {
            "first": _iso(pairs[0][0]),
            "last": _iso(pairs[-1][0]),
            "span_days": round(span_days, 1),
            "points": len(pairs),
            "dropped_points": dropped,
        },
        "low": {"equity": round(lo, 2), "on": _iso(lo_ts)},
        "high": {"equity": round(hi, 2), "on": _iso(hi_ts)},
        "days_below_current": len(below),
        "days_at_or_above_current": at_or_above,
        "percentile_in_window": pct_rank,
        "days_since_a_lower_reading": days_since_lower,
        "caveat": (
            f"Every statement here is bounded by this window: "
            f"{_iso(pairs[0][0])} to {_iso(pairs[-1][0])}, "
            f"{round(span_days, 1)} days, {len(pairs)} readable points. "
            f"It says nothing about equity before that."),
    }
    if dropped:
        out["caveat_gaps"] = (
            f"{dropped} point(s) had no usable equity and were dropped, not "
            f"read as zero. The window is thinner than it looks.")
    return out


def assess_multi_month_low(history, current_equity=None,
                           min_days=MONTHS_CLAIM_MIN_DAYS):
    """Answer 'is the account at a multi-month low?' or refuse to.

    Three outcomes, never two:
      TRUE       nothing in the window is lower, and the window is long
                 enough to support a claim about months.
      FALSE      something in the window IS lower. The verdict names
                 how many days and the most recent one.
      UNKNOWN    the window is too short to speak to months at all.
                 This is a real answer, not a failure.
    """
    s = summarise(history, current_equity)
    if not s.get("readable"):
        s["verdict"] = "UNKNOWN"
        s["verdict_detail"] = s.get("reason")
        return s

    span = s["window"]["span_days"]
    if span < min_days:
        s["verdict"] = "UNKNOWN"
        s["verdict_detail"] = (
            f"the history only goes back {span:.0f} days, and a claim about "
            f"MONTHS needs at least {min_days}. What happened before "
            f"{s['window']['first']} is not in this data, so this cannot be "
            f"answered either way - it is not evidence that the claim is "
            f"wrong.")
        return s

    if s["days_below_current"] == 0:
        s["verdict"] = "TRUE"
        s["verdict_detail"] = (
            f"${s['current_equity']:,.2f} is the lowest reading in "
            f"{span:.0f} days. The window low is ${s['low']['equity']:,.2f} "
            f"on {s['low']['on']}.")
        return s

    s["verdict"] = "FALSE"
    s["verdict_detail"] = (
        f"${s['current_equity']:,.2f} is NOT a {span:.0f}-day low. Equity was "
        f"lower on {s['days_below_current']} of {s['window']['points']} days "
        f"in the window, most recently "
        f"{s['days_since_a_lower_reading']:.0f} days ago. The real low is "
        f"${s['low']['equity']:,.2f} on {s['low']['on']}; the high is "
        f"${s['high']['equity']:,.2f} on {s['high']['on']}.")
    return s
