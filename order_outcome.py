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

The SECOND bug, same shape, opposite direction
----------------------------------------------
The fix above replaced one hardcoded label with another. grid_buy set
MAKER_EXPIRED on EVERY empty maker-only return, and three of
place_maker_buy's returns never place an order at all (below the minimum
trade size, unreadable book, a size that floors to zero). Those arrived
here as MAKER_EXPIRED, the one cause in BENIGN_CAUSES - so a branch that
was persistently too poor to trade, or reading an unreadable book, was
filed as "the mode working as designed" and never surfaced.

A fourth return is worse than either: _place_maker_order's POST can raise
AFTER Coinbase created the order, so whether anything rested is genuinely
UNKNOWN. Calling that MAKER_EXPIRED asserts a resting order that may never
have existed, and files a connection failure as routine.

The engine already records the answer structurally (_last_order_rested:
True / False / absent). cause_for_maker_only_no_fill turns that three-state
fact into the three causes below WITHOUT collapsing the unknown one, which
is the whole point: an unread value must never become a definite claim.
"""

MAKER_EXPIRED = "maker_expired"
REJECTED = "rejected"
#: No fill, and whether an order ever rested is UNKNOWN. Not an expiry (that
#: would assert a resting order) and not a rejection (nothing refused it).
NO_FILL = "no_fill"
#: Confirmed: the venue never received an order, so nothing could fill.
NO_ORDER_CREATED = "no_order_created"

#: Causes that are routine operation, not an execution fault.
#:
#: MAKER_EXPIRED is the only member and the only one that can be. It is the
#: single cause that means the strategy declined to pay a fee it did not
#: want to pay. NO_ORDER_CREATED is the venue or our own clamps refusing to
#: let us trade, and NO_FILL is not knowing what happened - both are exactly
#: what this counter exists to surface, so neither may be added here.
BENIGN_CAUSES = frozenset({MAKER_EXPIRED})

__all__ = ["MAKER_EXPIRED", "REJECTED", "NO_FILL", "NO_ORDER_CREATED",
           "BENIGN_CAUSES", "cause_for_maker_only_no_fill",
           "is_execution_fault", "event_for"]


def cause_for_maker_only_no_fill(order_rested):
    """Classify a maker-ONLY cycle that ended without a fill.

    `order_rested` is the engine's own structural verdict, and it has THREE
    states - True, False, and None for UNKNOWN. Each gets its own cause;
    none is folded into another. Passed in rather than inferred from the
    reason text, because a guard that reads a human-readable sentence is one
    wording change away from silently reclassifying everything.
    """
    if order_rested is True:
        return MAKER_EXPIRED
    if order_rested is False:
        return NO_ORDER_CREATED
    return NO_FILL


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

    if cause == NO_ORDER_CREATED:
        why = (detail or "").strip() or "no reason recorded"
        return "ORDER_NOT_PLACED", (
            f"buy {amount} never reached the venue - {why}. Not an expiry: no "
            f"order was created, so nothing rested and nothing could have "
            f"filled. Not a rejection either - nothing got as far as refusing "
            f"it."
        )

    if cause == NO_FILL:
        why = (detail or "").strip()
        why = f" - {why}" if why else ""
        return "ORDER_NO_FILL", (
            f"buy {amount} produced no fill and whether an order ever rested "
            f"is UNKNOWN{why}. Recorded as neither an expiry nor a rejection "
            f"on purpose: the first would assert a resting order that may "
            f"never have existed, the second a refusal nobody made."
        )

    if cause == REJECTED:
        why = (detail or "").strip() or "no reason reported by the venue"
        return "ORDER_REJECTED", f"buy {amount} was rejected - {why}"

    why = (detail or "").strip() or "no reason reported"
    return "ORDER_REJECTED", f"buy {amount} did not fill - {why}"
