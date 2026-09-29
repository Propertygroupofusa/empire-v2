"""How many units may actually be sent to the venue, and why not, when not.

WHY THIS EXISTS. The grid's execution path decided order size like this:

    decimals = await get_product_size_decimals(session, product_id)
    factor = 10 ** decimals
    qty = math.floor(qty * factor) / factor
    if qty <= 0: ... "nothing sellable"

Four things are wrong with that, and one of them is a live safety bug.

1. IT FAILS OPEN. get_product_size_decimals returns 8 on ANY failure - a
   non-200, a timeout, a raised exception. Eight is the most permissive
   value on the venue, so an unreadable product becomes "assume maximum
   precision" and an order ships against guessed rules. ALGO-USD's real
   base_increment is 0.1; sized at 8 decimals it is rejected outright.
   A protection that guesses when it cannot read is not a protection.

2. IT IS FLOAT. math.floor(x * 10**n) / 10**n is float arithmetic on a
   boundary. The venue's rules are decimal, and a binary representation
   that lands a hair under an exact increment floors a whole unit away.

3. IT READS A DECIMAL COUNT, NOT THE INCREMENT. len(increment.split(".")[1])
   turns 0.001 into "3 decimals", which is only equivalent while the
   increment is a power of ten. All 23 products this fleet trades are
   powers of ten TODAY (measured 2026-09-29), so this has not yet produced
   a wrong number - but an increment of 0.05 would floor 0.07 to 0.07,
   which is not a multiple of 0.05 and is rejected. A rule that happens to
   agree with the data is not the same as a correct rule.

4. IT HAS NO NOTIONAL FLOOR. The venue also imposes a minimum order VALUE.
   Size can clear the increment and the order still be too small to send.

WHAT IS AND IS NOT DUST. QNT-USD was measured at 0.00097323 units against
a real base_increment of 0.001. That is genuinely less than one tradeable
unit - the old code's arithmetic was right about QNT even though its
method was wrong. What it got wrong was the WORDS: it called a confirmed
non-order "nothing sellable", which reads as a failure, and the branch
then showed 0/3. Dust is not a failure and not a rejection. It is a
position the venue's own rules cannot express yet, and it becomes
executable again the moment enough inventory accumulates. So the raw
quantity is preserved exactly here and never rounded into the ledger as
zero.

THE FLOOR ITSELF IS NOT REIMPLEMENTED. resting_stops.round_down already
does exact-Decimal flooring to an arbitrary increment, and the stop path
has used it all along. Two functions computing the same quantity two ways
is how two parts of one system come to disagree about what is sellable,
so this imports that one rather than writing a second.

NOTHING HERE PERFORMS I/O. Every input is passed in, which is what makes
the decision testable against the real venue shapes without a key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from resting_stops import round_down

# --- decisions -------------------------------------------------------------
#
# EXECUTE  the quantity clears every rule the venue imposes; send it.
# DUST     confirmed too small to express. NOT an error, NOT a rejection,
#          and NOT "nothing sellable" - it is a real holding below one
#          tradeable unit, and it recovers on its own as inventory grows.
# REFUSED  the decision could not be made. Fails CLOSED: no order.
#
# REJECTED deliberately does not appear. That belongs to the order
# lifecycle, after a submission the venue turned down; using it here
# would claim the venue saw something it never did.
EXECUTE = "EXECUTE"
DUST = "DUST"
REFUSED = "REFUSED"

# --- reason codes ----------------------------------------------------------
# Machine-readable, because the old reason was prose and every consumer
# had to pattern-match sentences to tell one cause from another.
BELOW_BASE_INCREMENT = "BELOW_BASE_INCREMENT"
# THE WALLET AND THE REQUEST ARE DIFFERENT CONSTRAINTS.
#
# Found 2026-09-29 by reading the live payload rather than the code: LINK-USD
# reported "0.22 available is less than one tradeable unit of 0.01". It is
# not - 0.22 is twenty-two units. The message named raw_available while the
# decision was made on min(requested, available), so when the REQUEST was the
# sub-increment side the message blamed the wallet and told the reader to
# wait for inventory that was already there.
#
# They need different answers: a wallet under one unit becomes sellable when
# inventory grows; a request under one unit is the caller asking for dust and
# no amount of waiting fixes it. Nothing branches on these codes - they are
# reported, not dispatched on - so splitting them costs nothing and the
# combined message cost a wrong diagnosis.
REQUEST_BELOW_BASE_INCREMENT = "REQUEST_BELOW_BASE_INCREMENT"
BELOW_BASE_MIN_SIZE = "BELOW_BASE_MIN_SIZE"
BELOW_QUOTE_MIN_SIZE = "BELOW_QUOTE_MIN_SIZE"
METADATA_UNAVAILABLE = "METADATA_UNAVAILABLE"
BALANCE_UNREADABLE = "BALANCE_UNREADABLE"
PRICE_UNREADABLE = "PRICE_UNREADABLE"
NOTHING_REQUESTED = "NOTHING_REQUESTED"
OK = "OK"


def _dec(value):
    """Decimal or None. None means UNKNOWN and never zero.

    str(value) first on purpose: Decimal(0.1) is 0.1000000000000000055511...
    and carries the float error straight into the venue's arithmetic.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def quantity_decimals(base_increment) -> int | None:
    """How many decimal places the order string may carry.

    Derived from the increment so the format can never disagree with the
    flooring - the two used to be computed separately, which is how a
    correctly floored size could still be formatted into an invalid one.
    """
    inc = _dec(base_increment)
    if inc is None or inc <= 0:
        return None
    exp = -inc.as_tuple().exponent
    return exp if exp > 0 else 0


def format_quantity(quantity, base_increment) -> str | None:
    """The exact string to put in the order payload, or None if it cannot
    be formed. Never falls back to a guessed precision."""
    q = _dec(quantity)
    dp = quantity_decimals(base_increment)
    if q is None or dp is None:
        return None
    return f"{q:.{dp}f}"


@dataclass(frozen=True)
class QuantityPlan:
    """One decision, with every figure it was made from.

    The inputs are carried on the result so a log line or a dashboard row
    can show WHY without re-deriving anything - re-derivation is how a
    displayed reason drifts from the decision that was actually taken.
    """
    decision: str
    reason: str
    detail: str
    executable_quantity: Decimal = Decimal(0)
    raw_available: Decimal | None = None
    requested_quantity: Decimal | None = None
    base_increment: Decimal | None = None
    base_min_size: Decimal | None = None
    quote_min_size: Decimal | None = None
    price: Decimal | None = None
    notional: Decimal | None = None
    order_size_string: str | None = None

    @property
    def should_execute(self) -> bool:
        return self.decision == EXECUTE

    @property
    def is_dust(self) -> bool:
        return self.decision == DUST

    def to_dict(self):
        def s(v):
            return None if v is None else str(v)
        return {
            "decision": self.decision, "reason": self.reason, "detail": self.detail,
            "executable_quantity": s(self.executable_quantity),
            "raw_available": s(self.raw_available),
            "requested_quantity": s(self.requested_quantity),
            "base_increment": s(self.base_increment),
            "base_min_size": s(self.base_min_size),
            "quote_min_size": s(self.quote_min_size),
            "price": s(self.price), "notional": s(self.notional),
            "order_size_string": self.order_size_string,
        }


def plan_order_quantity(*, requested_quantity, available_quantity, price,
                       base_increment, base_min_size=None, quote_min_size=None):
    """What may actually be sold, or why nothing may be.

    For a SELL, `available_quantity` is the wallet's AVAILABLE units - not
    held units. Coin behind a resting order cannot be sold, and sizing
    against held produces an order the venue refuses.

    For a BUY, it is the units the cash can afford (usd / bid). The venue's
    size rules are identical on both sides, so one planner serves both; the
    caller has already decided how much cash to commit.

    None anywhere that matters is UNKNOWN and REFUSES. That is the whole
    point: the function this replaces turned an unreadable product into
    eight decimals and shipped the order anyway.

    `base_min_size` and `quote_min_size` are OPTIONAL because the venue
    does not always publish them - the public Exchange API returns neither,
    while the Advanced Trade brokerage API returns both. Absent means the
    rule is not asserted, NOT that it is satisfied; the increment floor
    below still applies and is never skipped.
    """
    inc = _dec(base_increment)
    if inc is None or inc <= 0:
        return QuantityPlan(
            decision=REFUSED, reason=METADATA_UNAVAILABLE,
            detail=("the product's base_increment could not be read, so the "
                    "venue's size rule is unknown. FAILING CLOSED - no order. "
                    "This is the case the old code answered with 8 decimals."),
            requested_quantity=_dec(requested_quantity),
            raw_available=_dec(available_quantity), price=_dec(price))

    avail = _dec(available_quantity)
    if avail is None:
        return QuantityPlan(
            decision=REFUSED, reason=BALANCE_UNREADABLE,
            detail=("the available balance could not be read. UNKNOWN is not "
                    "zero and not a quantity; no order."),
            requested_quantity=_dec(requested_quantity),
            base_increment=inc, price=_dec(price))

    req = _dec(requested_quantity)
    if req is None or req <= 0:
        return QuantityPlan(
            decision=REFUSED, reason=NOTHING_REQUESTED,
            detail="no positive quantity was requested.",
            requested_quantity=req, raw_available=avail,
            base_increment=inc, price=_dec(price))

    # Clamp to what the wallet can actually deliver, THEN floor. Flooring
    # first and clamping second can leave a size the wallet cannot cover.
    target = req if req < avail else avail
    executable = round_down(target, inc)

    px = _dec(price)
    notional = (executable * px) if (px is not None and executable > 0) else None
    bms = _dec(base_min_size)
    qms = _dec(quote_min_size)

    common = dict(raw_available=avail, requested_quantity=req,
                  base_increment=inc, base_min_size=bms,
                  quote_min_size=qms, price=px, notional=notional)

    if executable <= 0:
        # WHICH SIDE ACTUALLY BOUND. The wallet takes precedence when both
        # are under one unit, because raising the request would not help.
        if avail < inc:
            return QuantityPlan(
                decision=DUST, reason=BELOW_BASE_INCREMENT,
                detail=(f"{avail} available is less than one tradeable unit "
                        f"of {inc} (requested {req}). This is a real holding "
                        f"the venue's rules cannot express yet, not a failed "
                        f"sale and not an empty wallet. It becomes sellable "
                        f"again once inventory reaches {inc}."),
                executable_quantity=Decimal(0), **common)
        return QuantityPlan(
            decision=DUST, reason=REQUEST_BELOW_BASE_INCREMENT,
            detail=(f"{req} was requested, which is less than one tradeable "
                    f"unit of {inc}. The wallet is not the constraint - "
                    f"{avail} is available, which is {avail / inc:f} units. "
                    f"Waiting for inventory will not change this; the "
                    f"requested size is what is under the increment."),
            executable_quantity=Decimal(0), **common)

    if bms is not None and executable < bms:
        return QuantityPlan(
            decision=DUST, reason=BELOW_BASE_MIN_SIZE,
            detail=(f"{executable} clears the {inc} increment but is under the "
                    f"product's {bms} minimum order size."),
            executable_quantity=Decimal(0), **common)

    # The notional floor needs a price. Without one the rule cannot be
    # evaluated, and asserting it satisfied would be a guess.
    if qms is not None:
        if px is None:
            return QuantityPlan(
                decision=REFUSED, reason=PRICE_UNREADABLE,
                detail=("this product has a minimum order VALUE and no price "
                        "could be read, so whether the order clears it is "
                        "unknown. No order."),
                executable_quantity=Decimal(0), **common)
        if notional is not None and notional < qms:
            return QuantityPlan(
                decision=DUST, reason=BELOW_QUOTE_MIN_SIZE,
                detail=(f"{executable} at {px} is {notional}, under the "
                        f"product's {qms} minimum order value."),
                executable_quantity=Decimal(0), **common)

    size_str = format_quantity(executable, inc)
    if size_str is None:
        return QuantityPlan(
            decision=REFUSED, reason=METADATA_UNAVAILABLE,
            detail="the order size could not be formatted to the venue's precision.",
            executable_quantity=Decimal(0), **common)

    return QuantityPlan(
        decision=EXECUTE, reason=OK,
        detail=f"{size_str} clears every published rule for this product.",
        executable_quantity=executable, order_size_string=size_str, **common)


def log_line(product_id, plan: QuantityPlan, *, cycle=None, slice_label=None,
             target_price=None, maker_only=True):
    """The diagnostic the spec asks for, in place of 'nothing sellable'.

    Every figure is the one the decision used. None renders as UNKNOWN and
    never as 0 - a zero here would be the same lie the reason codes exist
    to stop.
    """
    def v(x):
        return "UNKNOWN" if x is None else str(x)
    parts = [str(product_id)]
    if cycle is not None:
        parts.append(f"cycle={cycle}")
    if slice_label is not None:
        parts.append(f"slice={slice_label}")
    parts += [
        f"available_quantity={v(plan.raw_available)}",
        f"requested_quantity={v(plan.requested_quantity)}",
        f"base_increment={v(plan.base_increment)}",
        f"base_min_size={v(plan.base_min_size)}",
        f"executable_quantity={v(plan.executable_quantity)}",
        f"quote_min_size={v(plan.quote_min_size)}",
        f"price={v(plan.price)}",
        f"notional={v(plan.notional)}",
        f"target={v(target_price)}",
        f"maker_only={'true' if maker_only else 'false'}",
        f"decision={plan.decision}",
        f"reason={plan.reason}",
    ]
    return " ".join(parts)
