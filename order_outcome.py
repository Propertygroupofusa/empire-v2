#!/usr/bin/env python3
"""Why a buy did not happen - an unfilled maker order is not a rejection.

The bug this exists to kill
---------------------------
Under maker-only there is no market fallback, so a maker order that sits
unfilled for its wait window simply expires and the cycle passes. That is
the mode working exactly as designed - grid_buy()'s own comment says so:
"a buy that does not happen costs nothing and the dip will still be there
next cycle."

But grid_buy() returned a bare None for BOTH that and a real venue
rejection, and the caller could not tell them apart. It guessed
"rejection", looked up a reason that was never set (nothing rejected the
order), and wrote a durable ORDER_REJECTED event reading "no reason
reported". Three of them in one hour on 2026-09-28:

    02:53:25  PEPE-USD   buy $44.33 did not fill - no reason reported
    03:03:45  SHIB-USD   buy $36.02 did not fill - no reason reported
    03:20:30  FLOKI-USD  buy $17.99 did not fill - no reason reported

None of those was rejected by anything. All three were maker orders that
did not get taken inside the wait window.

Why it matters beyond the wrong label: the durable ORDER_REJECTED record
was added precisely so "orders rejected" could be COUNTED, on the grounds
that otherwise "an execution problem could hide behind a normal-looking
gate pass rate." Filling that same counter with routine expiries recreates
the blindness from the other side - a genuine venue rejection now hides in
a pile of events that are not rejections at all. A signal that fires on
normal behaviour cannot be used to detect abnormal behaviour.

The expiry is already recorded properly elsewhere (_record_maker_expiry),
so this was also double-counting the same event under two meanings.
"""

MAKER_EXPIRED = "maker_expired"
REJECTED = "rejected"
NO_FILL = "no_fill"

#: Causes that are routine operation, not an execution fault.
BENIGN_CAUSES = frozenset({MAKER_EXPIRED})

__all__ = ["MAKER_EXPIRED", "REJECTED", "NO_FILL", "BENIGN_CAUSES",
           "is_execution_fault", "event_for"]


def is_execution_fault(cause):
    """True when a no-fill deserves to be counted as an execution problem.

    An UNKNOWN cause (None, or anything unrecognised) counts as a fault on
    purpose: a no-fill nobody can explain is exactly the thing worth
    surfacing. Silence must not be filed away as "probably fine" - the
    same reasoning that makes an unreadable balance never a $0.00.
    """
    return cause not in BENIGN_CAUSES


def event_for(cause, spend_usd, detail=None, wait_seconds=None):
    """(event_type, message) for a buy that produced no fill.

    Returns (None, message) when there is nothing worth recording as a
    gate decision - never a fabricated event type.
    """
    try:
        amount = f"${float(spend_usd):,.2f}"
    except (TypeError, ValueError):
        amount = "an unpriced amount"

    if cause == MAKER_EXPIRED:
        window = f" within {int(wait_seconds)}s" if wait_seconds else ""
        return "MAKER_EXPIRED", (
            f"maker buy {amount} was not taken{window} and maker-only mode has no "
            f"market fallback - passed this cycle rather than paying the taker leg. "
            f"Not a rejection: nothing refused this order."
        )

    if cause == REJECTED:
        why = (detail or "").strip() or "no reason reported by the venue"
        return "ORDER_REJECTED", f"buy {amount} was rejected - {why}"

    why = (detail or "").strip() or "no reason reported"
    return "ORDER_REJECTED", f"buy {amount} did not fill - {why}"
