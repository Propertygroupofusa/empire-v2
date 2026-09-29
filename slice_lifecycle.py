"""The states a grid slice may be in, and which moves between them are legal.

WHY THIS EXISTS. The engine had no slice state at all. What a branch was
doing had to be INFERRED - from the wallet balance, from how many rows
existed, from whether the last sell returned None. Inference is how a
holding of 0.00097323 QNT against a 0.001 increment came to be displayed as
0/3: available inventory divided by three, with a confirmed non-order read
as a failure.

So the rules here are stated rather than derived:

  - DUST is not an error, not a rejection, and not "nothing sellable". It
    is a real holding the venue's rules cannot express yet, and it recovers
    on its own once inventory reaches one increment. It has its own state
    and its own exit back to READY.
  - REJECTED means the VENUE refused a submitted order. It is reachable
    only from SUBMITTING, because nothing else has been submitted.
  - A maker order that reaches its target and does not fill is still OPEN
    or REPRICE. It never becomes FILLED, and it never becomes a taker sale
    to force completion - that would spend the slice's whole expected
    profit on an unauthorised leg.
  - ACCOUNTED is a separate state from FILLED on purpose. A fill is not
    finished until the P&L, the fee and the inventory have been written;
    treating FILLED as the end is how a cycle advances on a trade whose
    accounting never landed.

NOTHING HERE PERFORMS I/O AND NOTHING HERE DECIDES A TRADE. It answers one
question - is this move legal - so the caller cannot quietly skip a step.
"""
from __future__ import annotations

# --- the states (spec §2) --------------------------------------------------
CREATED = "CREATED"
ARMED = "ARMED"
READY = "READY"
SUBMITTING = "SUBMITTING"
OPEN = "OPEN"
PARTIAL = "PARTIAL"
FILLED = "FILLED"
ACCOUNTED = "ACCOUNTED"
DUST = "DUST"
REPRICE = "REPRICE"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
ERROR = "ERROR"

ALL_STATES = (CREATED, ARMED, READY, SUBMITTING, OPEN, PARTIAL, FILLED,
              ACCOUNTED, DUST, REPRICE, CANCELLED, REJECTED, ERROR)

# States a slice can sit in indefinitely without anything being wrong.
# DUST is here deliberately: it is a resting condition, not a fault.
RESTING = frozenset({CREATED, ARMED, READY, OPEN, PARTIAL, DUST, ACCOUNTED,
                     CANCELLED})

# States that mean a cycle's slice is finished with, one way or another.
TERMINAL = frozenset({ACCOUNTED, CANCELLED, REJECTED})

# NOT faults. Being unable to express a size, or having an order rest
# untaken, is the system working as designed under maker-only.
NOT_A_FAULT = frozenset({DUST, OPEN, PARTIAL, REPRICE, CANCELLED})

# --- legal transitions (spec §13) ------------------------------------------
_TRANSITIONS = {
    CREATED:    {ARMED, ERROR},
    ARMED:      {READY, DUST, ERROR},
    # READY -> DUST is the quantity rule refusing; it is not a rejection,
    # because nothing has been submitted for the venue to reject.
    READY:      {SUBMITTING, DUST, ERROR},
    SUBMITTING: {OPEN, REJECTED, ERROR},
    # A maker order that reaches its target and does not fill stays here or
    # goes to REPRICE. It never jumps to FILLED.
    OPEN:       {PARTIAL, FILLED, REPRICE, CANCELLED, ERROR},
    PARTIAL:    {FILLED, REPRICE, CANCELLED, PARTIAL, ERROR},
    REPRICE:    {SUBMITTING, CANCELLED, ERROR},
    FILLED:     {ACCOUNTED, ERROR},
    # Accounting is the end of a slice's life. A new cycle creates new ones.
    ACCOUNTED:  set(),
    # Dust recovers on its own once inventory reaches one increment (§11).
    DUST:       {READY, ERROR},
    CANCELLED:  {READY, ERROR},
    REJECTED:   {READY, ERROR},
    # ERROR is a holding pen, not a bin. It can be reconciled back.
    ERROR:      {READY, DUST, CANCELLED},
}


class IllegalTransition(Exception):
    """Raised rather than returned. A caller that skips a step is a bug in
    the caller, and silently allowing it is how FILLED comes to be treated
    as the end of a trade whose accounting never landed."""


def legal_moves(state):
    return frozenset(_TRANSITIONS.get(state, frozenset()))


def can_move(state, to):
    return to in _TRANSITIONS.get(state, frozenset())


def move(state, to):
    """The new state, or IllegalTransition. Never a silent no-op."""
    if state not in _TRANSITIONS:
        raise IllegalTransition(f"{state!r} is not a slice state")
    if to not in ALL_STATES:
        raise IllegalTransition(f"{to!r} is not a slice state")
    if to not in _TRANSITIONS[state]:
        raise IllegalTransition(
            f"{state} -> {to} is not a legal move. Legal from {state}: "
            f"{sorted(_TRANSITIONS[state]) or 'nothing - it is terminal'}")
    return to


def is_fault(state) -> bool:
    """Whether this state means something went wrong.

    DUST, OPEN, PARTIAL, REPRICE and CANCELLED are NOT faults. A counter
    that fires on normal behaviour cannot detect abnormal behaviour.
    """
    return state in (REJECTED, ERROR)


# --- slice identity (spec §1) ---------------------------------------------
def slice_label(index, total=3) -> str:
    """'1/3', '2/3', '3/3'. `index` is 1-based.

    The system must KNOW which slice this is. It was previously implied by
    FIFO order over opened_at, which is a property of the rows that happen
    to exist rather than a fact about the position - so a slice that could
    not be expressed simply vanished from the count.
    """
    if not isinstance(index, int) or not isinstance(total, int):
        raise ValueError("slice index and total must be integers")
    if index < 1 or total < 1 or index > total:
        raise ValueError(f"slice {index} of {total} is not a valid slice")
    return f"{index}/{total}"


def cycle_is_complete(states, total=3) -> bool:
    """True only when every slice of the cycle has finished accounting.

    FILLED is not enough (§15): a fill whose P&L, fee and inventory have not
    been written is not a completed trip, and starting the next cycle on it
    loses the accounting. An incomplete set of slices is not complete
    either - a cycle of three is not finished because the two that exist
    are done.
    """
    states = list(states)
    if len(states) != total:
        return False
    return all(s == ACCOUNTED for s in states)


def state_from_inventory(*_args, **_kwargs):
    """Deliberately not implemented.

    Spec §1: do NOT infer slice state from wallet balance, and do NOT infer
    cycle progress by dividing available inventory by three. That inference
    is the origin of the 0/3 display - it cannot tell a slice that was sold
    from one whose size the venue cannot currently express, and it reads
    dust as nothing at all.

    It exists as a named refusal so the next person looking for it finds
    this note rather than writing it.
    """
    raise NotImplementedError(
        "slice state is persisted, never inferred from inventory - see §1")
