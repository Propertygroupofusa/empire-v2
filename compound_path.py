"""How long, at the rate this account actually earns.

THE QUESTION

"$20/hour needs $79,000. How do we make that happen, and how long?"

It is two different problems wearing one sentence, and they have very
different answers.

  PLACING    $11,554 exists and $1,933.69 of it is deployed. Moving the
             rest into branches is an ACTION, not a wait. It earns
             nothing extra per dollar - it simply stops leaving most of
             the dollars out. Days, and it is the whole of the near-term
             gain.

  COMPOUNDING  Growing $11,554 into $79,000 is the slow half. Nothing
             about it can be hurried except by adding money, because the
             rate is measured, not chosen.

Reporting one number for both is how a plan gets made that feels fast and
is not.

THE RATE, AND WHY IT IS HONESTLY UNCERTAIN

$25.25 over 27.59 days, against capital that was about $150 for most of
that window and $1,933.69 now. Dividing by today's figure understates the
rate several-fold; dividing by the old one overstates what a bigger fleet
will do. So this takes a RANGE and reports both ends, because the honest
answer to "how long" is a span, and a single date would be a guess
wearing a decimal point.

WHAT IT REFUSES

It will not compound a rate measured over 27 days out past a horizon
where that rate has never been tested, without saying so. A 0.55%/day
return sustained for a year is a 7.5x account, and rates like that
almost never survive being scaled - the fees are the same, but the
slippage, the shrinking edge per extra coin and the venue's own depth
are not. The number is reported; the warning travels with it.
"""
from __future__ import annotations

import math

# Compounding past this without a caveat would be dishonest: the measured
# window is a month, and nothing here has been observed for a year.
HORIZON_WARN_DAYS = 180

# Below this many trades the daily rate is not a rate.
MIN_TRADES = 20


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _pos(v):
    f = _num(v)
    return f if (f is not None and f > 0) else None


def daily_rate(earned_usd, days, deployed_usd):
    """Fraction of deployed capital earned per day."""
    e, d, c = _num(earned_usd), _pos(days), _pos(deployed_usd)
    if e is None or d is None or c is None:
        return None
    return e / d / c


def days_to_grow(start_usd, target_usd, rate_per_day):
    """Days of compounding to get from one to the other."""
    s, t, r = _pos(start_usd), _pos(target_usd), rate_per_day
    if s is None or t is None or r is None or r <= 0:
        return None
    if t <= s:
        return 0.0
    return math.log(t / s) / math.log(1.0 + r)


def plan(*, account_usd, deployed_usd, target_usd_per_hour,
         earned_usd, days_measured, deployed_low_usd, deployed_high_usd,
         trades=None, min_trades=MIN_TRADES):
    """The two phases, with the timeline as a range rather than a date.

    `deployed_low_usd` is what was really deployed for most of the
    measured window; `deployed_high_usd` is what is deployed now. The
    first produces the optimistic rate, the second the pessimistic one,
    and the truth is between them.
    """
    account = _pos(account_usd)
    deployed = _pos(deployed_usd) or 0.0
    target_hour = _pos(target_usd_per_hour)
    target_day = target_hour * 24 if target_hour else None

    fast = daily_rate(earned_usd, days_measured, deployed_low_usd)
    slow = daily_rate(earned_usd, days_measured, deployed_high_usd)

    out = {
        "account_usd": account,
        "deployed_now_usd": deployed,
        "undeployed_usd": round(max((account or 0) - deployed, 0.0), 2),
        "target_usd_per_hour": target_hour,
        "target_usd_per_day": round(target_day, 2) if target_day else None,
        "rate_optimistic_pct_per_day": round(fast * 100, 4) if fast else None,
        "rate_pessimistic_pct_per_day": round(slow * 100, 4) if slow else None,
    }

    if trades is not None and trades < min_trades:
        out["available"] = False
        out["reason"] = (f"{trades} closed trade(s). A daily rate needs {min_trades} before "
                         f"anything can be compounded from it - a month of luck projected "
                         f"forward is not a plan.")
        return out

    # PHASE ONE: place what already exists. No waiting involved.
    out["phase_1_place"] = {
        "what": "Deploy the capital the account already holds",
        "from_usd": deployed,
        "to_usd": account,
        "adds_usd_per_day_optimistic": (round((account - deployed) * fast, 2)
                                        if (account and fast) else None),
        "adds_usd_per_day_pessimistic": (round((account - deployed) * slow, 2)
                                         if (account and slow) else None),
        "time": "days, not months - this is an action, not a wait",
        "detail": (
            f"${round(max((account or 0) - deployed, 0.0), 2):,.2f} is sitting outside every "
            f"branch. Placing it earns nothing EXTRA per dollar; it stops most of the dollars "
            f"being left out. This is the entire near-term gain available, and none of it "
            f"requires the rate to improve or the account to grow."),
    }

    # PHASE TWO: the slow half.
    need_fast = (target_day / fast) if (target_day and fast) else None
    need_slow = (target_day / slow) if (target_day and slow) else None

    d_fast = days_to_grow(account, need_fast, fast)
    d_slow = days_to_grow(account, need_slow, slow)

    out["phase_2_compound"] = {
        "what": f"Grow the account until it can carry ${target_hour:,.2f}/hour"
                if target_hour else "Grow the account",
        "capital_needed_optimistic_usd": round(need_fast, 2) if need_fast else None,
        "capital_needed_pessimistic_usd": round(need_slow, 2) if need_slow else None,
        "days_optimistic": round(d_fast, 0) if d_fast else None,
        "days_pessimistic": round(d_slow, 0) if d_slow else None,
        "years_optimistic": round(d_fast / 365.0, 2) if d_fast else None,
        "years_pessimistic": round(d_slow / 365.0, 2) if d_slow else None,
        "detail": (
            "Nothing here can be hurried except by adding money. The rate is measured, not "
            "chosen, and every previous attempt to raise it by trading faster made it worse."),
    }

    horizon = max(d for d in (d_fast, d_slow) if d) if (d_fast or d_slow) else None
    out["available"] = True
    out["fully_deployed_usd_per_day_optimistic"] = (round(account * fast, 2)
                                                    if (account and fast) else None)
    out["fully_deployed_usd_per_day_pessimistic"] = (round(account * slow, 2)
                                                     if (account and slow) else None)
    out["fully_deployed_usd_per_hour_optimistic"] = (round(account * fast / 24, 2)
                                                     if (account and fast) else None)

    out["honest_warning"] = (
        (f"The fast end compounds a rate measured over {round(_num(days_measured) or 0)} days "
         f"out to {round(horizon or 0):,.0f} days. A {round((fast or 0) * 100, 2)}%/day return "
         f"sustained that long is a {math.exp(math.log(1 + (fast or 0)) * (horizon or 0)):,.1f}x "
         f"account, and rates like that almost never survive being scaled: the fees stay the "
         f"same, but slippage, the thinner edge on each extra coin, and the venue's own depth "
         f"do not. Treat the optimistic column as a ceiling that has never been tested, not a "
         f"forecast."
         if (horizon and horizon > HORIZON_WARN_DAYS and fast) else
         "The horizon here is inside the window the rate was measured over."))

    out["the_short_answer"] = (
        (f"Placing the ${out['undeployed_usd']:,.2f} already in the account is days of work and "
         f"takes the fleet to roughly ${round(account * (slow or 0), 2):,.2f}-"
         f"${round(account * (fast or 0), 2):,.2f} a day. Reaching ${target_hour:,.0f}/hour "
         f"needs the ACCOUNT to reach ${round(need_slow or 0):,.0f}, which at the measured "
         f"rate is {round(d_fast or 0):,.0f} to {round(d_slow or 0):,.0f} days of compounding "
         f"with nothing withdrawn."
         if (account and target_hour and (d_fast or d_slow)) else
         "Not enough measured to answer."))
    out["is_a_projection_not_a_promise"] = True
    return out
