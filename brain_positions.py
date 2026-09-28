"""Rebuild market_brain's book from the broker, so a redeploy cannot
orphan a position.

market_brain manages every exit by iterating state.positions - its own
dict, persisted to brain_state.json:

    for symbol, pos in list(state.positions.items()):
        ... mtf_exit(entry_px, current, peak_px) ...

brain_state.json is on Railway's ephemeral disk and is wiped on every
redeploy. models.py names this about its sibling ml_trades.json and
calls it the reason the ML filter never accumulated a training set.

So the failure is not "it forgets its statistics". It is:

    deploy -> market_brain opens a position with a 1.5% stop
    redeploy (every push; ~20 on 28 Sep alone)
    -> state.positions is empty
    -> the exit loop iterates nothing
    -> that position has NO stop, NO take-profit, NO trailing exit and
       NO force-close, for as long as it is held

A bot that opens positions and then forgets them is worse than a bot
that never opened them. This is the prerequisite to running the cycle
at all, not a refinement of it.

THE FIX IS THE SAME ONE account_exposure MAKES: the broker knows what
the account holds; ask it. Reconciling each cycle means a redeploy
costs nothing, and the book cannot silently disagree with reality.

What is lost on a rebuild degrades safely, which is why this works:
    entry_price  the broker has it (avg_entry_price)
    peak_price   missing -> market_brain falls back to the current
                 price, so the trailing stop restarts from here rather
                 than from a high it never saw. Conservative.
    cycles       missing -> 0, so the 6-cycle force-close timer
                 restarts. Conservative.
    alloc        recomputed from market_value / equity.

Pure and side-effect free: it returns a new book and a list of what
changed. It places nothing and closes nothing.
"""

__all__ = ["reconcile", "ADOPTED", "DROPPED", "KEPT"]

ADOPTED = "adopted"   # the broker holds it; the book did not
DROPPED = "dropped"   # the book held it; the broker does not
KEPT = "kept"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def reconcile(book, broker_positions, equity):
    """(new_book, changes) - the book the broker's holdings imply.

    book: market_brain's state.positions (symbol -> dict).
    broker_positions: Alpaca /v2/positions rows.
    equity: account equity, for the alloc fraction.

    Returns (None, reason) if the broker rows cannot be read. A partial
    reconcile is worse than none: dropping a symbol the broker actually
    holds would orphan exactly the position this exists to protect.
    """
    eq = _num(equity)
    if eq is None or eq <= 0:
        return None, f"unreadable equity: {equity!r}"

    held = {}
    for p in (broker_positions or ()):
        get = p.get if hasattr(p, "get") else (lambda k, d=None: getattr(p, k, d))
        sym = get("symbol")
        if not sym:
            return None, "a broker position has no symbol - refusing a partial reconcile"
        mv = _num(get("market_value"))
        qty, cur = _num(get("qty")), _num(get("current_price"))
        if mv is None:
            if qty is None or cur is None:
                return None, f"{sym}: cannot be priced - refusing a partial reconcile"
            mv = qty * cur
        entry = _num(get("avg_entry_price"))
        held[sym] = {"market_value": mv, "entry_price": entry, "qty": qty}

    old = dict(book or {})
    new, changes = {}, []

    for sym, info in held.items():
        prior = old.get(sym)
        if prior is None:
            # The broker holds it and the book does not. Adopt it so it
            # gets managed - the whole point.
            new[sym] = {
                "entry_price": info["entry_price"] if info["entry_price"] is not None
                               else (info["market_value"] / info["qty"]
                                     if info["qty"] else None),
                "alloc": abs(info["market_value"]) / eq,
                "cycles": 0,
                "qty": info["qty"],
                "adopted_from_broker": True,
            }
            changes.append((ADOPTED, sym))
        else:
            # Keep everything the book already knows - peak_price above
            # all, since re-deriving it would reset a trailing stop that
            # is currently protecting a gain.
            merged = dict(prior)
            merged["alloc"] = abs(info["market_value"]) / eq
            if merged.get("entry_price") is None and info["entry_price"] is not None:
                merged["entry_price"] = info["entry_price"]
            new[sym] = merged
            changes.append((KEPT, sym))

    for sym in old:
        if sym not in held:
            # Closed somewhere else - by a human, another bot, or a
            # broker stop. Managing it further would be managing a
            # position that no longer exists.
            changes.append((DROPPED, sym))

    return new, changes
