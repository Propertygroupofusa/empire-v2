"""The mandate's size limits, enforced BEFORE an order, summed across a pass.

On 2026-09-28 at 13:30:03Z prop_bot placed SIX separate market orders for
META in the same second - 0.163475 shares each at $748.91, $122.42 a
piece, $734.57 in total - on a $1,007 account. It was the day's entire
-$27.31.

Three ceilings existed and none of them stopped it:

    mandate max_per_position   $120   zero pre-trade references anywhere;
                                      read only by mandate_compliance.py,
                                      which reports AFTER the fact
    mandate max_total_notional $300   ZERO references in the whole
                                      codebase. Dead configuration.
    code MAX_RISK_PERCENT      50%    checked against positions already
                                      OPEN - and none of the six had
                                      filled yet when the next was sized

That last line is the actual defect. Every one of the six orders was
$122.42, comfortably under every per-order limit. NOTHING SUMMED THEM.
Six entries sized in one pass, each against a denominator that did not
include the other five, add up to 73% of equity while each one looks
modest.

So this module tracks COMMITTED notional - orders submitted in this pass
whether or not they have filled - and refuses the order that would cross
a line. Held in the caller's own dict, passed in and back out, so there
is no module-level state to leak between cycles or threads.

DIRECTION OF FAILURE. This is a gate on live money, so it fails CLOSED:
an unreadable cap, an unreadable price, a NaN, a missing mandate - every
one of them refuses the order. A protection that cannot evaluate itself
must not wave money through. It can only ever REFUSE an order; there is
no path here that causes one, which is the shape every guard in this
repo is held to.
"""

__all__ = ["CapLedger", "check_order", "ALLOW", "REFUSE"]

ALLOW = "ALLOW"
REFUSE = "REFUSE"


class CapLedger(dict):
    """Notional committed during one pass, by symbol.

    A plain dict subclass so a caller can hold it per-cycle and throw it
    away, rather than this module keeping state that two threads could
    share.
    """

    def committed_total(self):
        return sum(self.values())

    def committed_for(self, symbol):
        return self.get(symbol, 0.0)

    def record(self, symbol, notional):
        self[symbol] = self.get(symbol, 0.0) + float(notional)


def _num(v):
    """A number, or None. None means UNREADABLE and always refuses."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def check_order(symbol, qty, price, capital, ledger=None, open_notional=0.0,
                open_position_count=0):
    """(verdict, reason, notional) for ONE prospective order.

    capital: the mandate's capital dict. Missing or unreadable caps
    refuse - a cap that cannot be read is not a cap that passes.
    ledger:  CapLedger of what this pass has already committed. None is
             treated as an empty pass, which is correct for the first
             order and wrong for the sixth, so callers pass one.
    open_notional: dollar value already held in FILLED positions.
    """
    q, p = _num(qty), _num(price)
    if q is None or p is None:
        return REFUSE, f"unreadable order: qty={qty!r} price={price!r}", None
    if q <= 0 or p <= 0:
        return REFUSE, f"non-positive order: qty={q} price={p}", None
    notional = q * p

    if not isinstance(capital, dict):
        return REFUSE, "no capital mandate to check against", notional

    per_position = _num(capital.get("max_per_position"))
    total_cap = _num(capital.get("max_total_notional"))
    max_open = _num(capital.get("max_open_positions"))
    if per_position is None or total_cap is None:
        return (REFUSE,
                "mandate caps unreadable (max_per_position="
                f"{capital.get('max_per_position')!r}, max_total_notional="
                f"{capital.get('max_total_notional')!r})", notional)

    led = ledger if ledger is not None else CapLedger()
    already_open = _num(open_notional)
    if already_open is None:
        return REFUSE, f"unreadable open notional: {open_notional!r}", notional

    # PER POSITION - counting what this pass already sent for this symbol.
    # Six $122.42 orders for one symbol is a $734.57 position, and the
    # per-position cap is about the POSITION, not about one ticket.
    # NOTE the caller passes the ACCOUNT's total open notional, not a
    # per-symbol figure, so this check covers what this pass commits and
    # not what is already filled in the same name. Stated rather than
    # silently assumed; a caller that can supply per-symbol filled
    # notional should pass it and this becomes exact.
    symbol_after = led.committed_for(symbol) + notional
    if symbol_after > per_position:
        return (REFUSE,
                f"{symbol}: ${symbol_after:,.2f} would exceed max_per_position "
                f"${per_position:,.2f} (${led.committed_for(symbol):,.2f} already "
                f"committed this pass + ${notional:,.2f} now)", notional)

    # TOTAL NOTIONAL - filled positions plus everything committed this
    # pass. This is the line the six META orders crossed.
    total_after = already_open + led.committed_total() + notional
    if total_after > total_cap:
        return (REFUSE,
                f"${total_after:,.2f} would exceed max_total_notional "
                f"${total_cap:,.2f} (${already_open:,.2f} open + "
                f"${led.committed_total():,.2f} committed this pass + "
                f"${notional:,.2f} now)", notional)

    # OPEN POSITION COUNT - a new symbol this pass is a new position.
    if max_open is not None:
        n = _num(open_position_count)
        if n is None:
            return REFUSE, f"unreadable position count: {open_position_count!r}", notional
        # Distinct symbols this pass has committed to, plus this one if
        # it is not already among them.
        new_this_pass = len(set(led) | {symbol})
        if n + new_this_pass > max_open:
            return (REFUSE,
                    f"{int(n)} open + {new_this_pass} new this pass would exceed "
                    f"max_open_positions {int(max_open)}", notional)

    return ALLOW, None, notional
