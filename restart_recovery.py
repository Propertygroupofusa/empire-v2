"""Whether a restarted process may trade yet, and what it does not know.

§24 asks that on restart the system load its cycles and slices, read the
exchange's open orders and balances, reconcile fills, inventory and order
states, "mark unknown states appropriately", and only then resume trading -
"do not start a new cycle before reconciliation".

The load-bearing word is "appropriately". A reconciliation has THREE
outcomes, not two, and conflating the third with the first is the whole
failure this module exists to prevent:

  RESUME   every input was read and agrees        -> may trade
  HOLD     every input was read and one disagrees -> may not trade
  REFUSED  an input could not be read at all      -> may not trade

REFUSED is not a worse HOLD. It is a different fact: HOLD says the system
knows something is wrong, REFUSED says it does not know whether anything
is wrong. A gap is not a zero, and an unread balance is not a balance of
zero. Both stop trading, but only one of them is fixed by looking at the
discrepancy.

WHAT IS NOT RECONCILABLE TODAY, AND WHY THIS SAYS SO OUT LOUD.
CryptoGridSlice persists entry_price, qty, product_id, fees and the entry
gate's diagnostic - but NO state and NO order id. A slice's existence IS
its state. So §24's step 7, "reconcile order states", has nothing to
reconcile against: there is no persisted order state in the schema. That
is §1/§2's job ("slice state is persisted, never inferred"), and until it
lands, an open exchange order cannot be matched to the slice that placed
it. This module reports that as an UNKNOWN rather than quietly skipping
the step, because a step that was never performed must not read as a step
that passed.

SHORT AND EXCESS ARE NOT SYMMETRIC. Tracked above held means the ledger
claims coin the wallet does not have; a sell sized from the ledger would
either be rejected or would reach for coin belonging to something else, so
it blocks. Held above tracked means untracked coin sitting in the wallet -
worth reporting, and the adoption path's ordinary starting condition, but
it cannot make an order wrong. Treating the two the same would halt the
fleet over its own normal state.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

RESUME = "RESUME"
HOLD = "HOLD"
REFUSED = "REFUSED"

MATCHED = "MATCHED"
SHORT = "SHORT"
EXCESS = "EXCESS"
UNKNOWN = "UNKNOWN"

# Reasons a restart may not resume. Codes, not prose, so a caller can
# branch on them and a dashboard can count them.
BALANCES_UNREADABLE = "BALANCES_UNREADABLE"
OPEN_ORDERS_UNREADABLE = "OPEN_ORDERS_UNREADABLE"
SLICES_UNREADABLE = "SLICES_UNREADABLE"
INVENTORY_SHORT = "INVENTORY_SHORT"
ORDER_STATE_NOT_PERSISTED = "ORDER_STATE_NOT_PERSISTED"
OK = "OK"


def _dec(v):
    """A Decimal, or None when the value is not a number we may rely on."""
    if v is None:
        return None
    try:
        # str() FIRST, deliberately. Decimal(True) is Decimal(1) in Python,
        # because bool is an int - so a True that reached here through a
        # JSON field or a truthiness bug would become a quantity of one.
        # Decimal("True") raises instead. A mutation test proved an explicit
        # isinstance(v, bool) guard beside this unkillable: the conversion
        # already does the work, and a guard no test can break is not a
        # protection, it is a line that looks like one.
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def reconcile_inventory(tracked, held, base_increment=None):
    """How one product's ledger compares with the wallet.

    Returns one of MATCHED / SHORT / EXCESS / UNKNOWN. UNKNOWN whenever
    either side is unreadable, or the increment needed to judge "close
    enough" is unreadable - never a guessed increment, because an assumed
    tolerance decides the answer.

    base_increment is the venue's own smallest tradeable unit, read from
    product metadata. A difference below one increment cannot be traded in
    either direction, so it is not a discrepancy. Nothing is floored here -
    this is a comparison, not a sizing decision, and the one flooring
    implementation stays in resting_stops.round_down.
    """
    t, h = _dec(tracked), _dec(held)
    if t is None or h is None:
        return UNKNOWN
    if t < 0 or h < 0:
        # A negative holding is not a small holding; it is a bad read.
        return UNKNOWN
    inc = _dec(base_increment)
    if inc is None or inc <= 0:
        return UNKNOWN
    diff = t - h
    if abs(diff) < inc:
        return MATCHED
    return SHORT if diff > 0 else EXCESS


class RecoveryPlan:
    def __init__(self, decision, reason, findings=None, unknowns=None):
        self.decision = decision
        self.reason = reason
        self.findings = list(findings or [])
        self.unknowns = list(unknowns or [])

    @property
    def may_trade(self):
        """Only an outright RESUME lets a new cycle start.

        Written as an identity rather than `!= REFUSED` or `not
        self.unknowns` so that a decision added later is refused by default
        instead of being silently permitted by a negative test.
        """
        return self.decision == RESUME

    def __repr__(self):
        return (f"RecoveryPlan({self.decision}, {self.reason}, "
                f"findings={self.findings}, unknowns={self.unknowns})")


def plan_restart(*, persisted_slices, held_balances, open_orders,
                 increments_by_product, order_state_is_persisted=False):
    """Decide whether a restarted process may begin a cycle.

    persisted_slices        list of {product_id, qty}, or None if unread
    held_balances           {product_id: available}, or None if unread
    open_orders             list of open orders, or None if unread
    increments_by_product   {product_id: base_increment}; a product absent
                            from it is UNKNOWN, never assumed
    order_state_is_persisted
                            whether the schema can match an open exchange
                            order to the slice that placed it. False today.

    None means UNREAD and is always REFUSED. An empty list or dict means
    READ AND EMPTY, which is a real and ordinary answer - no slices, no
    open orders, a flat wallet. Collapsing the two is the bug this
    signature is shaped to prevent, and it is the same bug as reporting an
    unread balance as zero.
    """
    unknowns = []
    if persisted_slices is None:
        unknowns.append(SLICES_UNREADABLE)
    if held_balances is None:
        unknowns.append(BALANCES_UNREADABLE)
    if open_orders is None:
        unknowns.append(OPEN_ORDERS_UNREADABLE)
    if not order_state_is_persisted:
        # Step 7 of §24 cannot be performed against the current schema.
        # Reported, not skipped: a step that never ran must not read like a
        # step that passed.
        unknowns.append(ORDER_STATE_NOT_PERSISTED)
    if unknowns:
        return RecoveryPlan(REFUSED, unknowns[0], unknowns=unknowns)

    tracked = {}
    for s in persisted_slices:
        pid = (s or {}).get("product_id")
        qty = _dec((s or {}).get("qty"))
        if pid is None or qty is None:
            # One unreadable slice makes the whole ledger unreliable: the
            # product it belonged to cannot be summed correctly, and which
            # product that is, is exactly what is missing.
            return RecoveryPlan(REFUSED, SLICES_UNREADABLE,
                                unknowns=[SLICES_UNREADABLE])
        tracked[pid] = tracked.get(pid, Decimal(0)) + qty

    findings, shorts, unresolved = [], [], []
    for pid in sorted(tracked):
        verdict = reconcile_inventory(tracked[pid], held_balances.get(pid),
                                      increments_by_product.get(pid))
        if verdict == MATCHED:
            continue
        findings.append({"product_id": pid, "verdict": verdict,
                         "tracked": str(tracked[pid]),
                         "held": (None if held_balances.get(pid) is None
                                  else str(held_balances.get(pid)))})
        if verdict == SHORT:
            shorts.append(pid)
        elif verdict == UNKNOWN:
            unresolved.append(pid)

    if unresolved:
        return RecoveryPlan(REFUSED, BALANCES_UNREADABLE, findings=findings,
                            unknowns=[BALANCES_UNREADABLE] + unresolved)
    if shorts:
        return RecoveryPlan(HOLD, INVENTORY_SHORT, findings=findings)
    # EXCESS alone does not block - see the module docstring.
    return RecoveryPlan(RESUME, OK, findings=findings)
