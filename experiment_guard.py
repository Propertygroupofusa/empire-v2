"""When does the experiment end? Decided here, so it can be tested cold.

WHY IT ENDS ON ITS OWN

The gates come off as a trial with a stated cost, and the failure mode is
nobody deciding to spend the money. An open-ended "gates off" is how a
-$34-a-month wash becomes -$400 with no single moment where anyone chose
that. So the switch carries a budget and a deadline, and this decides,
on every check, whether either has been reached.

THE THREE WAYS IT ENDS

  BUDGET     spend since switch-on reached the amount agreed
  DEADLINE   the clock ran out
  BLIND      the spend could not be measured, repeatedly

That last one is the one people leave out. If the ledger cannot be read,
the honest state is not "carry on" - being unable to see what an experiment
is costing is precisely the condition the budget exists to prevent. But a
single API blip must not end a two-week trial either, so blindness has to
persist before it counts.

SPEND IS MEASURED AGAINST A BASELINE, NOT AN ABSOLUTE

baseline_realized_pnl is the ledger's combined realized P&L at the instant
the gates came off. Spend is baseline minus current. That way the trial is
charged for what IT did, not for what the market did to coin that was
already sitting there before it started.
"""
from __future__ import annotations

from datetime import datetime, timedelta

BUDGET, DEADLINE, BLIND, MANUAL = "BUDGET", "DEADLINE", "BLIND", "MANUAL"

# How many consecutive unreadable checks before the trial is ended as BLIND.
# At the worker's 10-minute cadence this is an hour of not knowing, which
# is long enough to ride out a deploy and short enough that nothing runs
# unwatched for a day.
MAX_BLIND_CHECKS = 6


def _f(x, default=None):
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def spend_usd(baseline_pnl, current_pnl):
    """What the experiment has cost so far. None when it cannot be known.

    Positive means it has LOST money since the switch. Negative means it is
    up, which does not extend the deadline - a trial that is winning still
    ends when it said it would, because the decision to continue should be
    made deliberately rather than by default.
    """
    b, c = _f(baseline_pnl), _f(current_pnl)
    if b is None or c is None:
        return None
    return round(b - c, 2)


def decide(*, baseline_pnl, current_pnl, budget_usd, deadline_at, now=None,
           blind_checks=0) -> dict:
    """Should this experiment end right now, and why."""
    now = now or datetime.utcnow()
    spend = spend_usd(baseline_pnl, current_pnl)
    budget = _f(budget_usd)

    # DEADLINE FIRST, and deliberately so: a trial past its end date ends
    # even if the spend is unreadable. "We could not tell, so we kept going
    # past the date" is not a defensible sentence.
    if deadline_at is not None and now >= deadline_at:
        return {"end": True, "reason": DEADLINE, "spend_usd": spend,
                "detail": f"the {_days(deadline_at, now)} are up"}

    if spend is None:
        n = int(blind_checks or 0) + 1
        if n >= MAX_BLIND_CHECKS:
            return {"end": True, "reason": BLIND, "spend_usd": None,
                    "blind_checks": n,
                    "detail": (f"the spend could not be measured {n} checks in a "
                               f"row. Not knowing what this is costing is the "
                               f"condition the budget exists to prevent.")}
        return {"end": False, "reason": None, "spend_usd": None,
                "blind_checks": n,
                "detail": (f"spend unreadable ({n}/{MAX_BLIND_CHECKS}) - "
                           f"riding it out, this is usually a deploy")}

    if budget is not None and spend >= budget:
        return {"end": True, "reason": BUDGET, "spend_usd": spend,
                "detail": f"spent ${spend:,.2f} of the ${budget:,.2f} agreed"}

    return {"end": False, "reason": None, "spend_usd": spend, "blind_checks": 0,
            "detail": (f"${spend:,.2f} of ${budget:,.2f} spent"
                       if budget is not None else f"${spend:,.2f} spent")}


def _days(deadline_at, now):
    try:
        d = (deadline_at - now).days
        return "two weeks" if abs(d) > 900 else "days"
    except Exception:
        return "days"


def status(exp: dict, current_pnl=None, now=None) -> dict:
    """A running experiment described for a human, or the record of a finished one."""
    now = now or datetime.utcnow()
    if not exp:
        return {"running": False,
                "note": "No experiment running. The gates are on."}
    if exp.get("ended_at"):
        return {
            "running": False, "profile": exp.get("profile"),
            "ended_reason": exp.get("ended_reason"),
            "ended_at": exp.get("ended_at"),
            "cost_usd": exp.get("ended_spend_usd"),
            "note": (f"Ended on {exp.get('ended_reason')}. "
                     f"It cost ${_f(exp.get('ended_spend_usd'), 0):,.2f}."),
        }
    spend = spend_usd(exp.get("baseline_realized_pnl"), current_pnl)
    budget = _f(exp.get("budget_usd"))
    deadline = exp.get("deadline_at")
    if isinstance(deadline, str):
        try:
            deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            deadline = None
    return {
        "running": True,
        "profile": exp.get("profile"),
        "started_at": exp.get("started_at"),
        "budget_usd": budget,
        "spend_usd": spend,
        "remaining_usd": (None if spend is None or budget is None
                          else round(budget - spend, 2)),
        "deadline_at": exp.get("deadline_at"),
        "days_left": (None if deadline is None
                      else max(0, (deadline - now).days)),
        "blind_checks": exp.get("blind_checks") or 0,
        "note": (
            "Spend is unreadable right now, which is being counted - if it "
            "stays unreadable the experiment ends on its own."
            if spend is None else
            f"${spend:,.2f} of ${budget:,.2f} spent."
            if budget is not None else f"${spend:,.2f} spent."),
    }
