"""What a given hourly target would actually take, in capital.

THE QUESTION, AND WHY IT NEEDS ARITHMETIC RATHER THAN AN OPINION

"It should be at least $20 an hour per coin."

That is a capital question wearing a strategy question's clothes. A grid
earns a RATE on the money deployed behind it, so the hourly figure is
fixed once you know two things: how much each deployed dollar earns per
hour, and how many of them there are. Wanting a bigger number sets the
first; only the second is a lever.

So this computes the capital a target implies, from the account's own
measured rate, and says plainly when the answer is larger than the
account.

TWO RATES, AND THEY DISAGREE BY SIXTY TIMES

  recent    $5.64 earned over 6.89h on $169.45 deployed
            = 0.4828% of deployed capital per hour
  all time  $25.25 over 27.45 days on roughly $150 deployed
            = 0.0256% per hour

The recent rate is twenty times the long one because it rests on two
closed trades. Quoting it alone would promise a return the fleet has
never sustained; quoting only the long one would ignore that the current
configuration really is trading better. So both are reported, always,
with the sample behind each - and the REQUIRED CAPITAL is computed from
both, because the honest answer is a range whose ends are far apart.

WHAT THIS REFUSES

It will not compute a required capital from fewer trades than can carry
a rate. It will not annualise. And it will not present the flattering
rate as the expected one.
"""
from __future__ import annotations

# A rate from fewer closed trades than this describes luck.
MIN_TRADES_FOR_RATE = 20

# Hours in a day, for turning an hourly target into a daily one. Nothing
# here is ever scaled past a day - a year of this is not evidence.
HOURS_PER_DAY = 24.0


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _pos(v):
    f = _num(v)
    return f if (f is not None and f > 0) else None


def rate_per_dollar_hour(earned_usd, hours, deployed_usd):
    """Dollars earned per hour, per dollar of deployed capital."""
    e, h, d = _num(earned_usd), _pos(hours), _pos(deployed_usd)
    if e is None or h is None or d is None:
        return None
    return e / h / d


def required_capital(target_usd_per_hour, rate):
    """Capital that target implies at that rate."""
    t, r = _pos(target_usd_per_hour), rate
    if t is None or r is None or r <= 0:
        return None
    return t / r


def assess(*, target_usd_per_hour, coins, account_total_usd, deployed_usd,
           recent_earned_usd, recent_hours, alltime_earned_usd, alltime_hours,
           alltime_avg_deployed_usd=None, trades=None,
           min_trades=MIN_TRADES_FOR_RATE):
    """What the target costs, measured both ways, against what exists."""
    target = _pos(target_usd_per_hour)
    n_coins = int(coins or 0)
    total = _pos(account_total_usd)
    deployed = _pos(deployed_usd)

    recent = rate_per_dollar_hour(recent_earned_usd, recent_hours, deployed)
    alltime = rate_per_dollar_hour(alltime_earned_usd, alltime_hours,
                                   alltime_avg_deployed_usd or deployed)

    fleet_target = (target * n_coins) if (target and n_coins) else target

    out = {
        "target_usd_per_hour_per_coin": target,
        "coins": n_coins,
        "fleet_target_usd_per_hour": round(fleet_target, 2) if fleet_target else None,
        "fleet_target_usd_per_day": (round(fleet_target * HOURS_PER_DAY, 2)
                                     if fleet_target else None),
        "account_total_usd": total,
        "deployed_usd": deployed,

        "recent_rate_pct_per_hour": round(recent * 100, 5) if recent else None,
        "alltime_rate_pct_per_hour": round(alltime * 100, 5) if alltime else None,
        "rates_disagree_by": (round(recent / alltime, 1)
                              if (recent and alltime and alltime > 0) else None),
    }

    if trades is not None and trades < min_trades:
        out["available"] = False
        out["reason"] = (
            f"{trades} closed trade(s). A rate needs {min_trades} before it can carry a "
            f"capital calculation - multiplying a lucky fortnight by a target is how a "
            f"plan gets built on a number that was never there.")
        return out

    need_recent = required_capital(fleet_target, recent)
    need_alltime = required_capital(fleet_target, alltime)
    out["available"] = True
    out["capital_needed_at_recent_rate_usd"] = round(need_recent, 2) if need_recent else None
    out["capital_needed_at_alltime_rate_usd"] = (round(need_alltime, 2)
                                                 if need_alltime else None)
    out["multiple_of_account_recent"] = (round(need_recent / total, 1)
                                         if (need_recent and total) else None)
    out["multiple_of_account_alltime"] = (round(need_alltime / total, 1)
                                          if (need_alltime and total) else None)

    # WHAT THE WHOLE ACCOUNT WOULD ACTUALLY PRODUCE. The useful figure,
    # because it is the ceiling available without adding money.
    out["whole_account_at_recent_rate_usd_per_hour"] = (round(total * recent, 2)
                                                        if (total and recent) else None)
    out["whole_account_at_alltime_rate_usd_per_hour"] = (round(total * alltime, 2)
                                                         if (total and alltime) else None)
    out["deployed_now_usd_per_hour"] = (round(deployed * recent, 4)
                                        if (deployed and recent) else None)

    reachable = bool(need_recent and total and need_recent <= total)
    out["reachable_with_this_account"] = reachable
    out["verdict"] = "REACHABLE" if reachable else "NEEDS_MORE_CAPITAL_THAN_EXISTS"
    out["detail"] = (
        (f"${target:,.2f}/hour across {n_coins} coin(s) is ${fleet_target:,.2f}/hour, or "
         f"${fleet_target * HOURS_PER_DAY:,.2f} a day. "
         + (f"At the recent measured rate that needs ${need_recent:,.0f} of DEPLOYED capital"
            + (f" - {need_recent / total:.1f}x the whole ${total:,.0f} account. "
               if total else ". ")
            if need_recent else "")
         + (f"At the all-time rate it needs ${need_alltime:,.0f}"
            + (f", {need_alltime / total:.0f}x the account. " if total else ". ")
            if need_alltime else "")
         + (f"Deploying every dollar of the account would produce about "
            f"${total * recent:,.2f}/hour at the recent rate and ${total * alltime:,.2f}/hour "
            f"at the all-time one. The gap between those two is the sample, not the strategy."
            if (total and recent and alltime) else "")))
    out["what_moves_it"] = (
        "Only two things set the hourly figure: the rate each deployed dollar earns, and how "
        "many dollars are deployed. Right now ${:,.2f} is deployed out of ${:,.2f} - that is "
        "the lever with room in it. The rate is not a dial; it is a measurement, and the one "
        "way to raise it that this account has actually tested is a WIDER grid step, not a "
        "faster one.".format(deployed or 0.0, total or 0.0))
    return out
