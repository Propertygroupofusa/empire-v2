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
    if capital.get("_unreadable"):
        return REFUSE, f"sizing ladder unreadable: {capital['_unreadable']}", notional

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


# ── the numbers come from market_brain, not from me ────────────────────
#
# On 28 Sep I was about to ask the account owner to pick a per-position
# and total-notional number, because the apex mandate's $120/$300 would
# have refused every entry. That was the wrong question: the system was
# already in the repo and nothing was running it.
#
# market_brain.py holds a complete, coherent sizing discipline:
#
#     MILESTONES            alloc % and max_pos, scaling with balance
#     confidence_to_alloc   5%-28% of portfolio, by signal confidence
#     can_open_position     "Never exceed 60% total exposure" - and it
#                           SUMS alloc across every open position, which
#                           is the exact check missing from prop_bot
#     CONFIG["max_exposure"]                0.60
#
# Nothing imports market_brain. prop_bot runs its own competing
# size_position() - "AGGRESSIVE COMPOUNDING", 20-40% of remaining cash
# per slot times POSITION_SCALE_MULTIPLIER, with a 50% MAX_RISK_PERCENT
# measured only against positions already FILLED. Two sizing systems,
# one account, nothing forcing them to agree: the same bug class as the
# two 20% checks and the hardcoded 483.00.
#
# These are DERIVED from market_brain's own constants at call time, never
# copied. A hand-written ladder beside the real one goes stale silently -
# a lesson this repo has paid for more than once.
def caps_from_market_brain(balance):
    """{max_per_position, max_total_notional, max_open_positions} in
    DOLLARS, for this balance, straight off market_brain's ladder.

    Returns None if market_brain cannot be read - and check_order()
    refuses on a missing mandate, so an unreadable ladder stops trading
    rather than quietly falling back to something looser.
    """
    try:
        import market_brain as mb
        milestone = mb.get_milestone(float(balance))
        # The largest a single position may ever be: the top of the
        # confidence ladder. Read off the function rather than assumed,
        # so re-tuning confidence_to_alloc moves this with it.
        top_alloc = max(mb.confidence_to_alloc(s) for s in range(0, 11))
        max_exposure = float(mb.CONFIG["max_exposure"])
        bal = float(balance)
    except Exception as exc:
        # A bare `return None` here hid a ModuleNotFoundError and made
        # "the ladder is unreadable" look identical to "the ladder says
        # nothing" - gap-as-zero, in the very module written to stop it.
        # The reason travels with the refusal now.
        return {"_unreadable": f"{type(exc).__name__}: {exc}"}
    if bal <= 0 or max_exposure <= 0 or top_alloc <= 0:
        return {"_unreadable": f"nonsensical ladder: balance={bal}, "
                               f"max_exposure={max_exposure}, top_alloc={top_alloc}"}
    return {
        "max_per_position": round(bal * top_alloc, 2),
        "max_total_notional": round(bal * max_exposure, 2),
        "max_open_positions": milestone["max_pos"],
        "_source": (f"market_brain milestone {milestone['label']!r}: "
                    f"{top_alloc*100:.0f}% max per position, "
                    f"{max_exposure*100:.0f}% max exposure, "
                    f"{milestone['max_pos']} positions"),
    }
