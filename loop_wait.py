"""Is the loop dead, or is it waiting? Two facts, one marker.

THE DAY THIS WAS NEEDED. 2026-10-08, 17:26:39Z: the fleet loop passed its
gate on XLM-USD, placed a post-only buy, and entered _place_maker_order's
in-line wait. The wait budget under maker-only is GRID_MAKER_ONLY_WAIT_SECONDS,
set to 3600 on this account, and the loop is sequential over branches - so all
21 branches stood still. The order filled at 18:30:50Z and paid; nothing was
broken. But for 64 minutes /grid-status said:

    heartbeat: {"alive": false, "age_seconds": 3316.3, "stage": "cycled"}

`alive: false` means "the heartbeat is older than 300 seconds". It is read as
"the fleet is dead", and it was reported as very nearly that. Establishing the
truth took fourteen hand-polls of the activity feed over ten minutes.

WHAT THIS MODULE DOES, AND WHAT IT DELIBERATELY DOES NOT.

It does not change `alive`. Other code reads that field and a measurement
change must not become a behaviour change. It adds a second answer beside it:
a marker the waiting code sets before it waits and clears when it returns, so
a stale heartbeat can be told apart from a dead one.

It places no order, cancels none, waits for nothing itself, and has no
network, database or venue reach. It cannot move a dollar. Its only state is
one process-local dict.

THE FOOTGUN, AND THE GUARD AGAINST IT. A marker that leaks - set and never
cleared because the wait raised - would make a genuinely dead loop read as
"just waiting", forever. That is worse than the bug this fixes: a false alarm
becomes a missed one. So a marker past its own budget plus ABANDON_GRACE_SECONDS
stops claiming the loop is waiting and starts reporting that it leaked. An
abandoned marker never produces a WAITING verdict.

AND THE SECOND HONESTY PROBLEM. The marker lives in the memory of whichever
process is doing the waiting. If the reader is not that process, the ABSENCE
of a marker proves nothing at all - so the verdict is UNKNOWN_WHETHER_WAITING
rather than STALLED whenever this process does not hold the loop lease. Same
doctrine as every other reading in this fleet: unknown is a third verdict, not
a quiet no.
"""
from __future__ import annotations

import time
import uuid

# product -> wait record, keyed by an opaque token so a caller can only ever
# clear its own. Process-local on purpose; see the docstring.
_IN_FLIGHT: dict = {}

# How far past its own budget a marker may go before it is read as leaked
# rather than as a wait. Generous: a venue call can overrun its budget by a
# poll interval or two without anything being wrong, and calling a healthy
# wait abandoned would reintroduce the false alarm from the other side.
ABANDON_GRACE_SECONDS = 300.0

# A heartbeat older than this is not fresh. Mirrors the 300s that
# get_grid_heartbeat already uses for `alive`, named here so the two cannot
# drift apart silently.
STALE_AFTER_SECONDS = 300.0

ALIVE = "ALIVE"
WAITING = "WAITING_ON_A_MAKER_ORDER"
STALLED = "STALLED"
UNKNOWN = "UNKNOWN_WHETHER_WAITING"
NEVER_SEEN = "NEVER_SEEN"


def _num(v):
    """A number, or None. A string that will not parse is None, not zero."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def mark(product_id, side, budget_seconds) -> str:
    """Record that a wait has started. Returns the token to clear it with.

    Never raises: this runs immediately before real money rests at the
    venue, and instrumentation that can throw there would be a far worse
    bug than the blind spot it was added to fix. Rubbish input is stored as
    rubbish and reported as rubbish rather than refused.
    """
    token = uuid.uuid4().hex
    try:
        _IN_FLIGHT[token] = {
            "product_id": product_id,
            "side": side,
            "budget_seconds": _num(budget_seconds),
            "started_at": time.time(),
        }
    except Exception:
        pass
    return token


def clear(token) -> None:
    """End a wait. Safe to call twice, and safe for a token nobody knows.

    Called from a finally, so it runs on the exception path too - which is
    the path that would otherwise leak the marker.
    """
    try:
        _IN_FLIGHT.pop(token, None)
    except Exception:
        pass
    return None


def in_flight() -> dict | None:
    """The OLDEST wait currently marked, or None.

    The oldest is the one doing the damage: with a sequential loop, that is
    the wait every other branch is queued behind.
    """
    try:
        if not _IN_FLIGHT:
            return None
        now = time.time()
        rows = []
        for rec in _IN_FLIGHT.values():
            started = _num(rec.get("started_at"))
            rows.append((started if started is not None else now, rec))
        rows.sort(key=lambda r: r[0])
        started, rec = rows[0]
        elapsed = max(0.0, now - started)
        budget = rec.get("budget_seconds")
        # No budget is UNKNOWN, not infinite: without one, "within budget"
        # cannot be answered, so it is not asserted either way.
        within = None if budget is None else bool(elapsed <= budget)
        abandoned = (False if budget is None
                     else bool(elapsed > budget + ABANDON_GRACE_SECONDS))
        return {
            "product_id": rec.get("product_id"),
            "side": rec.get("side"),
            "budget_seconds": budget,
            "started_at": started,
            "elapsed_seconds": round(elapsed, 1),
            "within_budget": within,
            "abandoned": abandoned,
            "waits_in_flight": len(rows),
        }
    except Exception:
        return None


def verdict(age_seconds, holds_loop) -> dict:
    """Dead, waiting, or fine - with the marker consulted, never assumed.

    `age_seconds` is the heartbeat's own age; `holds_loop` is whether THIS
    process owns the loop lease, which is what decides whether a missing
    marker means anything.
    """
    f = in_flight()
    age = _num(age_seconds)

    if age is not None and age < STALE_AFTER_SECONDS:
        return {"verdict": ALIVE, "waiting_on": None,
                "detail": (f"the loop cycled {age:.0f}s ago, inside the "
                           f"{STALE_AFTER_SECONDS:.0f}s freshness window.")}

    # Stale from here down. The marker is what separates the two causes.
    if f is not None and f.get("abandoned"):
        return {
            "verdict": STALLED, "waiting_on": f,
            "detail": (
                f"the heartbeat is stale and a wait marker for "
                f"{f.get('product_id')} is {f.get('elapsed_seconds')}s old "
                f"against a {f.get('budget_seconds')}s budget - past budget "
                f"plus the {ABANDON_GRACE_SECONDS:.0f}s grace, so the marker "
                f"is treated as abandoned and NOT as evidence of a live wait. "
                f"A leaked marker must never make a dead loop look busy."),
        }

    if f is not None:
        return {
            "verdict": WAITING, "waiting_on": f,
            "detail": (
                f"the loop is inside an in-line maker wait on "
                f"{f.get('product_id')} ({f.get('side')}), "
                f"{f.get('elapsed_seconds')}s into a {f.get('budget_seconds')}s "
                f"budget. The loop is NOT dead: it is sequential over branches, "
                f"so one resting order holds every other branch behind it until "
                f"it fills or its budget runs out."),
        }

    if age is None:
        return {"verdict": NEVER_SEEN, "waiting_on": None,
                "detail": "no heartbeat age could be read."}

    if not holds_loop:
        return {
            "verdict": UNKNOWN, "waiting_on": None,
            "detail": (
                f"the heartbeat is {age:.0f}s old and no wait is marked, but "
                f"this process does not hold the loop lease - the marker is "
                f"process-local, so its absence here is not evidence that the "
                f"loop which does hold the lease is idle."),
        }

    return {
        "verdict": STALLED, "waiting_on": None,
        "detail": (
            f"the heartbeat is {age:.0f}s old, this process holds the loop "
            f"lease, and no maker wait is marked - so the stall is not "
            f"explained by an order resting."),
    }
