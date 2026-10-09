#!/usr/bin/env python3
"""Can this slice actually be sold right now, and if not, WHICH not.

WHY THIS EXISTS, AND WHAT THE MEASUREMENT SAID.

The brief named this bug as the one to fix first:

    "available 0.0009732300 floors to 0 at 3 decimals"
    -> remove the hard-coded precision assumption

The precision assumption is not hard-coded. `get_product_size_decimals`
fetches QNT-USD's real `base_increment` from Coinbase and it really is 0.001,
so 0.00097323 QNT really is below one tradeable unit and that log line is
telling the truth. Measured 2026-09-29:

    QNT-USD   base_increment=0.001   LINK-USD  base_increment=0.01
    ALGO-USD  base_increment=0.1     all three: min_market_funds=1

What the same measurement found instead is worse, and it is the reason the
fleet does not rotate:

    QNT   wallet held 0.00097323, LOCKED 0.0, available 0.00097323
          ledger: 2 slices x 0.337991 QNT @ $161.01, now $249.93
          -> $169 of claimed inventory that IS NOT IN THE WALLET, and
             nothing is reserving it. Two slices showing +55% on coin the
             account does not hold.

    ALGO  wallet held 1134.346389, LOCKED 1134.3, available 0.046389
          ledger: 279.4 units
          -> the coin EXISTS and is reserved by a resting order. Entirely
             different problem, identical log line.

    LINK  wallet held 6.85, LOCKED 6.63, available 0.22
          ledger: 0.00999... + 2.99 + 3.1
          -> 0.22 IS sellable at a 0.01 increment. But the 0.00999 slice
             floors to 0.00 and is worth $0.15 against a $1 venue minimum.
             THIS one is real dust.

Three different causes. One log line, which openly guesses between two of
them: "Usually means the coin is reserved by a resting order, or the branch
holds less than one tradeable unit." The wallet read can tell them apart -
`hold` answers it - and the answer was being thrown away.

So this module does not "fix rounding". It makes the engine say which of five
things is true, with the exchange's own numbers attached, and it separates the
one state that is nobody's fault (DUST) from the one that means the ledger is
lying (UNBACKED).

FAIL CLOSED, EVERY TIME. Every path that cannot prove a quantity is legal
returns a refusal, never a guess. This is a sell path with a next cycle always
behind it, so refusing costs one cycle; guessing costs an INVALID_SIZE
rejection at best and an order sized off a number nobody confirmed at worst.
The old `get_product_size_decimals` returned 8 when the fetch failed, which is
the most permissive value on the venue - a fail-OPEN default on the one input
that decides whether an order is legal.

EXACT DECIMAL ARITHMETIC, and not for tidiness. `len(increment.split(".")[1])`
turns an increment into a decimal count, which cannot represent an increment
that is not a power of ten. ALGO's is 0.1 and QNT's is 0.001, so the count
happens to be right for them - but a 0.05 increment becomes "2 decimals" and
then 0.03 passes a check the venue rejects, and a 2.5 increment becomes "1
decimal" and 0.1 passes. floor_to_increment divides by the increment itself,
so it is right for every increment the venue can state.
"""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

# The five things that can be true of a slice the strategy wants to sell.
EXECUTE = "EXECUTE"
# Below the venue's own floor. Not an error, not a rejection, not a failure,
# and not "nothing sellable" - the units are real and still owned.
DUST = "DUST"
# The units exist and are held by another order. Selling needs that order
# cancelled, which is a different decision made by a different layer.
RESERVED = "RESERVED"
# The ledger claims units the wallet does not have, and nothing is holding
# them. Never a reason to place an order; always a reason to say so loudly.
UNBACKED = "UNBACKED"
# Could not prove the order would be legal. Refuse.
BLOCKED = "BLOCKED"

# Reason codes, so the funnel can count causes without parsing prose.
BELOW_BASE_INCREMENT = "BELOW_BASE_INCREMENT"
BELOW_BASE_MIN_SIZE = "BELOW_BASE_MIN_SIZE"
BELOW_QUOTE_MINIMUM = "BELOW_QUOTE_MINIMUM"
HELD_BY_ANOTHER_ORDER = "HELD_BY_ANOTHER_ORDER"
LEDGER_EXCEEDS_WALLET = "LEDGER_EXCEEDS_WALLET"
NO_PRODUCT_METADATA = "NO_PRODUCT_METADATA"
BAD_PRODUCT_METADATA = "BAD_PRODUCT_METADATA"
UNREADABLE_BALANCE = "UNREADABLE_BALANCE"
UNREADABLE_PRICE = "UNREADABLE_PRICE"
NOTHING_REQUESTED = "NOTHING_REQUESTED"

# A decision that is not EXECUTE must never be counted as a maker order that
# rested and expired. That conflation is what put ~2,600 phantom attempts a
# day into the funnel the owner reads to find the bottleneck; only EXECUTE
# leads to an order that can rest at all.
NO_ORDER_PLACED = frozenset({DUST, RESERVED, UNBACKED, BLOCKED})

# How far the wallet may fall short of the ledger before it is called UNBACKED
# rather than ordinary float drift. One part in 10,000 of the claim, with an
# absolute floor so a tiny claim is not judged by a ratio. Tuned to catch the
# QNT case (wallet has 0.14% of the claim) and to ignore the LINK slice whose
# qty is 0.009999999999998899 because a float round-tripped.
LEDGER_DRIFT_TOLERANCE = Decimal("0.0001")


def _dec(value, field):
    """Decimal(value), or (None, why). Strings preferred - float('0.1') is not
    0.1, and an increment is exactly the place that matters."""
    if value is None:
        return None, f"{field} is absent"
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None, f"{field} is not a number ({value!r})"
    if d.is_nan() or d.is_infinite():
        return None, f"{field} is not finite ({value!r})"
    return d, None


def floor_to_increment(quantity, increment):
    """The largest multiple of `increment` that is <= `quantity`, exactly.

    Returns (Decimal, None) or (None, why). Refuses a non-positive increment
    rather than dividing by it - an increment of 0 is missing metadata wearing
    a number, and the answer to "how much of this may I sell" is then not 0,
    it is "don't".
    """
    q, err = _dec(quantity, "quantity")
    if err:
        return None, err
    inc, err = _dec(increment, "base_increment")
    if err:
        return None, err
    if inc <= 0:
        return None, f"base_increment must be positive, got {increment!r}"
    if q < 0:
        return None, f"quantity must not be negative, got {quantity!r}"
    # Divide by the increment itself, so a 0.05 or 2.5 increment is handled
    # as exactly as a 0.001 one. A decimal-place count cannot do this.
    steps = (q / inc).to_integral_value(rounding=ROUND_FLOOR)
    return steps * inc, None


def _quote_minimum(meta):
    """The venue's minimum order VALUE, under whichever name it used.

    Coinbase states this as `quote_min_size` on the brokerage products
    endpoint and as `min_market_funds` on the exchange one. It is checked
    nowhere in this repo today - `quote_min_size` does not appear in it at all
    - so an order above the increment but worth $0.15 is submitted against a
    $1 floor and rejected by the venue.
    """
    for key in ("quote_min_size", "min_market_funds"):
        if meta.get(key) is not None:
            d, err = _dec(meta[key], key)
            if err is None and d > 0:
                return d
    return None


def classify_sell(product_id, requested_qty, wallet_available, product_meta,
                  price=None, wallet_hold=None, ledger_qty=None):
    """Decide whether this slice can be sold, and if not, exactly why.

    Pure. Every input is passed in, so all of it is testable and none of it is
    a read that might differ between the decision and the order.

      requested_qty    what the strategy wants to sell (a slice's qty)
      wallet_available what the venue says is free to sell now; None = UNREADABLE
      product_meta     the venue's product rules; None or {} = refuse
      price            the price the order would rest at; needed for the value
                       floor, and None makes that check UNKNOWN, not passed
      wallet_hold      units the venue is holding behind other orders; None is
                       UNKNOWN and is never read as 0
      ledger_qty       what our own books claim this branch holds, when the
                       caller knows it. This is what turns "nothing sellable"
                       into "the books and the wallet disagree by X"

    Returns a dict with `decision`, `reason`, `detail`, `executable_qty` and
    every exchange number that went into it - which is also exactly the
    diagnostic the brief asks the log to print.
    """
    out = {
        "product_id": product_id,
        "decision": BLOCKED,
        "reason": None,
        "detail": None,
        "requested_qty": requested_qty,
        "wallet_available": wallet_available,
        "wallet_hold": wallet_hold,
        "ledger_qty": ledger_qty,
        "price": price,
        "base_increment": None,
        "base_min_size": None,
        "quote_minimum": None,
        # What the venue's size grid allows, as a diagnostic. May be a positive
        # number on a REFUSED decision - a 0.05 LINK order is a legal size that
        # is worth $0.74 against a $1 floor, and zeroing it here would hide the
        # reason it was refused behind the same 0 that means "below one unit".
        "executable_qty": None,
        # The size to actually submit. Set ONLY on EXECUTE. Separate from
        # executable_qty precisely so that reporting an honest number and
        # authorising an order cannot be the same act.
        "sendable_qty": None,
        "executable_value": None,
    }

    def refuse(decision, reason, detail):
        out["decision"], out["reason"], out["detail"] = decision, reason, detail
        out["sendable_qty"] = None
        return out

    # ---- metadata first, because nothing below is decidable without it ----
    meta = product_meta or {}
    if not meta:
        return refuse(BLOCKED, NO_PRODUCT_METADATA,
                      "the product's trading rules could not be read, so no "
                      "size can be shown to be legal. Not placed; there is "
                      "always a next cycle, and a guessed precision is how an "
                      "order gets rejected for INVALID_SIZE or, worse, "
                      "accepted at a size nobody checked")
    inc, err = _dec(meta.get("base_increment"), "base_increment")
    if err or inc <= 0:
        return refuse(BLOCKED, BAD_PRODUCT_METADATA,
                      f"the product's base_increment is unusable ({err or inc}), "
                      f"so the legal size grid is unknown")
    out["base_increment"] = str(inc)
    bms, _ = _dec(meta.get("base_min_size"), "base_min_size")
    out["base_min_size"] = str(bms) if bms is not None else None
    qmin = _quote_minimum(meta)
    out["quote_minimum"] = str(qmin) if qmin is not None else None

    # ---- the wallet, which is the only authority on what can be sold ----
    if wallet_available is None:
        return refuse(BLOCKED, UNREADABLE_BALANCE,
                      "the wallet balance could not be read. An unreadable "
                      "balance is never 0.00 - it is unknown, and an order "
                      "sized off an unknown is an order sized off nothing")
    avail, err = _dec(wallet_available, "wallet_available")
    if err:
        return refuse(BLOCKED, UNREADABLE_BALANCE, err)
    asked, err = _dec(requested_qty, "requested_qty")
    if err:
        return refuse(BLOCKED, NOTHING_REQUESTED, err)
    if asked <= 0:
        return refuse(BLOCKED, NOTHING_REQUESTED,
                      f"nothing was requested ({requested_qty})")

    # ---- does our own book agree with the wallet? ----
    #
    # Asked BEFORE the size is clamped, because clamping is what hides it. The
    # old path silently set qty = real_balance and reported the floor of that
    # as "nothing sellable", which is how $169 of QNT the account does not own
    # sat on the books for as long as it did, marked +55%.
    claim, _ = _dec(ledger_qty if ledger_qty is not None else requested_qty,
                    "ledger_qty")
    if claim is not None and claim > 0:
        shortfall = claim - avail
        tolerance = max(claim * LEDGER_DRIFT_TOLERANCE, inc)
        if shortfall > tolerance:
            held, _ = _dec(wallet_hold, "wallet_hold")
            if held is None:
                return refuse(
                    BLOCKED, LEDGER_EXCEEDS_WALLET,
                    f"our books claim {claim} and the wallet frees {avail}, a "
                    f"shortfall of {shortfall}. How much the venue is holding "
                    f"behind other orders could not be read, so whether this "
                    f"is reserved coin or coin we do not own is UNKNOWN. Not "
                    f"placed, and not written off")
            if held >= shortfall - tolerance:
                return refuse(
                    RESERVED, HELD_BY_ANOTHER_ORDER,
                    f"the coin exists and is not free: {held} units are held "
                    f"behind other orders, which covers the {shortfall} this "
                    f"sale is short. Selling it means cancelling that order "
                    f"first - a different decision, made by whichever layer "
                    f"placed it. Nothing is wrong with this slice")
            return refuse(
                UNBACKED, LEDGER_EXCEEDS_WALLET,
                f"our books claim {claim} but the wallet holds {avail} free "
                f"with only {held} on hold - {shortfall - held} of this slice "
                f"is not in the account at all. NO ORDER, and this is not "
                f"dust: dust is owned. Until this reconciles, the slice's "
                f"unrealized P&L is measured against coin that is not there")

    # ---- the size the venue will actually accept ----
    sellable = min(asked, avail)
    executable, err = floor_to_increment(sellable, inc)
    if err:
        return refuse(BLOCKED, BAD_PRODUCT_METADATA, err)
    out["executable_qty"] = executable
    if executable <= 0:
        return refuse(
            DUST, BELOW_BASE_INCREMENT,
            f"{sellable} is less than one tradeable unit of {inc}. The units "
            f"are real and still owned - this is not a failed sale and no "
            f"order was created. Kept on the books, and re-checked whenever "
            f"the balance changes")
    if bms is not None and executable < bms:
        return refuse(
            DUST, BELOW_BASE_MIN_SIZE,
            f"{executable} is under the product's {bms} minimum size. Owned, "
            f"not sellable yet, no order created")

    # ---- and the venue's minimum order VALUE, which nothing checked ----
    if qmin is not None:
        if price is None:
            return refuse(
                BLOCKED, UNREADABLE_PRICE,
                f"the product has a {qmin} minimum order value and the price "
                f"to measure it against could not be read. Whether this order "
                f"clears that floor is UNKNOWN, so it is not sent")
        px, err = _dec(price, "price")
        if err or px <= 0:
            return refuse(BLOCKED, UNREADABLE_PRICE,
                          err or f"price must be positive, got {price!r}")
        value = executable * px
        out["executable_value"] = str(value)
        if value < qmin:
            return refuse(
                DUST, BELOW_QUOTE_MINIMUM,
                f"{executable} at {px} is worth {value}, under the product's "
                f"{qmin} minimum order value. Owned, not sellable at this "
                f"price, no order created")
    elif price is not None:
        px, err = _dec(price, "price")
        if err is None and px > 0:
            out["executable_value"] = str(executable * px)

    out["decision"] = EXECUTE
    out["reason"] = None
    out["sendable_qty"] = executable
    out["detail"] = (f"{executable} of {asked} requested is executable on a "
                     f"{inc} increment")
    return out


def log_line(d):
    """The brief's diagnostic, one line per field, from a classify_sell dict.

    Replaces "nothing sellable" with the numbers that decide it. Every value
    is printed as it was read; a field that was UNKNOWN prints as `unknown`
    rather than as a zero, because those are different facts.
    """
    def show(v):
        return "unknown" if v is None else str(v)
    parts = [
        f"{d['product_id']}",
        f"decision={d['decision']}",
        f"reason={show(d['reason'])}",
        f"requested={show(d['requested_qty'])}",
        f"ledger={show(d['ledger_qty'])}",
        f"wallet_available={show(d['wallet_available'])}",
        f"wallet_hold={show(d['wallet_hold'])}",
        f"base_increment={show(d['base_increment'])}",
        f"base_min_size={show(d['base_min_size'])}",
        f"quote_minimum={show(d['quote_minimum'])}",
        f"executable_quantity={show(d['executable_qty'])}",
        f"sendable_quantity={show(d.get('sendable_qty'))}",
        f"executable_value={show(d['executable_value'])}",
        f"price={show(d['price'])}",
        "maker_only=true",
    ]
    return " ".join(parts)
