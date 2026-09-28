"""market_brain's 60% exposure gate, measured against the REAL account.

THE DESIGN IS THE ACCOUNT OWNER'S AND IT IS SOUND. market_brain.py:

    def can_open_position(positions, new_alloc):
        \"\"\"Never exceed 60% total exposure.\"\"\"
        total = sum(p.get("alloc", 0.20) for p in positions.values())
        return (total + new_alloc) <= CONFIG["max_exposure"]

That sum is the check whose absence let six META orders - $734.57, 73%
of a $1,007 account - through on 2026-09-28.

WHAT `positions` IS, THOUGH, IS THE PROBLEM. It is State.positions: a
dict of the trades MARKET_BRAIN ITSELF opened, persisted to
brain_state.json. Two consequences, both live-money:

  1. IT CANNOT SEE THE OTHER BOT. prop_bot trades the same Alpaca
     account through the same ALPACA_BASE_URL. With prop_bot holding
     $734 of META, market_brain starting fresh measures exposure at 0%
     and will add up to 60% more. Combined: 133%.

  2. brain_state.json IS ON RAILWAY'S EPHEMERAL DISK, wiped on every
     redeploy - models.py says exactly this about its sibling
     ml_trades.json, and it is why market_brain's ML filter never
     accumulated a training set. So after any deploy the gate forgets
     its own open positions and resets to 0% while the real positions
     are still open.

"Never exceed 60% total exposure" is plainly a statement about the
ACCOUNT. This module measures it that way: from the broker's own
positions, which include every bot's, and which no redeploy can wipe.

It only ever ANSWERS a question. It places nothing, cancels nothing,
and holds no state. Unreadable input refuses - a gate on live money
that cannot evaluate itself must not wave money through.
"""

__all__ = ["exposure_fraction", "can_open", "alloc_book_from_positions"]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def alloc_book_from_positions(positions, equity):
    """Broker positions -> {symbol: alloc_fraction}, the shape
    can_open_position already expects.

    positions: Alpaca /v2/positions rows (market_value or qty*price).
    Returns None if equity or any row is unreadable, because a partial
    book understates exposure, which is the dangerous direction.
    """
    eq = _num(equity)
    if eq is None or eq <= 0:
        return None
    book = {}
    for p in (positions or ()):
        get = p.get if hasattr(p, "get") else (lambda k, d=None: getattr(p, k, d))
        sym = get("symbol")
        mv = _num(get("market_value"))
        if mv is None:
            q, pr = _num(get("qty")), _num(get("current_price"))
            if q is None or pr is None:
                return None          # a row we cannot price is not a zero
            mv = q * pr
        if not sym:
            return None
        book[sym] = book.get(sym, 0.0) + abs(mv) / eq
    return book


def exposure_fraction(positions, equity):
    """Total account exposure as a fraction of equity, or None."""
    book = alloc_book_from_positions(positions, equity)
    return None if book is None else sum(book.values())


def can_open(positions, equity, new_notional, max_exposure):
    """(ok, reason, projected_fraction) for one prospective order.

    The same question market_brain's can_open_position asks, against the
    whole account rather than one bot's memory of itself.
    """
    cap = _num(max_exposure)
    add = _num(new_notional)
    eq = _num(equity)
    if cap is None or cap <= 0:
        return False, f"unreadable max_exposure: {max_exposure!r}", None
    if add is None or add <= 0:
        return False, f"unreadable order notional: {new_notional!r}", None
    if eq is None or eq <= 0:
        return False, f"unreadable equity: {equity!r}", None
    current = exposure_fraction(positions, eq)
    if current is None:
        return False, "account positions unreadable - exposure cannot be established", None
    projected = current + add / eq
    if projected > cap:
        return (False,
                f"{projected*100:.1f}% would exceed the {cap*100:.0f}% exposure ceiling "
                f"({current*100:.1f}% already open across the whole account "
                f"+ ${add:,.2f} now)", projected)
    return True, None, projected
