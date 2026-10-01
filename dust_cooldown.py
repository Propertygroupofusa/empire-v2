"""Stop re-asking the venue a question whose answer cannot have changed.

THE MEASUREMENT THAT PROMPTED THIS, live 2026-10-01 05:21-05:22Z. Three
branches tried to sell a profitable slice, and all three were refused for
the same structural reason, in the same 60 seconds:

    QNT-USD   available 0.00097323  < base_min_size 0.001   DUST
    PEPE-USD  available 0.06879848  < base_increment 1      DUST
    TIA-USD   available 0.0         < base_min_size 0.01    DUST

Each refusal costs three Coinbase calls before the refusal is even
computed - the asset balance, the product rules, and the order book - and
the grid retries on its next 30s cycle, forever. slice_backing.py records
QNT alone being refused 182 times in 24 hours. The same boot that produced
those three refusals also produced:

    HTTP 429 fetching USD        HTTP 429 fetching USDC
    [GRID] real fee-tier lookup failed (HTTP 429)
    [DEPLOY] UNKNOWN: free cash unreadable - a gap is not a zero

That last line is a deploy pass that funded nothing because a rate-limited
balance read came back empty. WHETHER THESE REFUSALS CAUSED THOSE 429s IS
NOT ESTABLISHED HERE - the boot burst alone could account for them. What is
established is that the calls are spent on an outcome that is already known.

WHAT THIS DOES NOT DO

It does not make an unsellable slice sellable. Nothing in code can: the
coin is not in the wallet, and the venue's minimum is the venue's. Taking
QNT's gain needs the books corrected, which is reconcile-slices, which is
write-guarded and the account owner's to run.

It does not lower a floor. base_increment, base_min_size and quote_min_size
are the venue's numbers and are untouched. It does not disable maker-only,
widen a price, or change what the fleet is willing to sell.

All it does is decline to ASK again for a while.

WHY A TIMER AND NOT A BALANCE CHECK

The obvious design - re-read the balance and skip only if it is unchanged -
spends the exact call this exists to save. So the skip is time-based, and
the two events that can genuinely change the answer clear it immediately:
a buy on that product (inventory grew), and the cooldown expiring.

ONLY A COMPUTED DUST VERDICT ARMS THIS

A balance that could not be read, product rules that could not be read, an
unreadable book - none of those arm a cooldown. They are gaps, and a gap is
not a fact about inventory. Arming on one would turn a transient rate limit
into fifteen minutes of deliberate blindness. `note_dust` takes the decision
the planner actually computed and refuses anything else.
"""
from __future__ import annotations

import os
import time

#: How long to stay quiet after a computed DUST verdict. The slices this
#: targets have been stuck for days, so a quarter hour of latency on a
#: recovery costs nothing; the grid's own cycle is 30s, so this is a 30x
#: reduction in attempts on a branch that cannot trade.
COOLDOWN_SECONDS = float(os.getenv("GRID_DUST_COOLDOWN_SECONDS", "900"))

#: The planner decision that means "the venue cannot express this size".
#: Spelled out rather than imported so this module stays dependency-free
#: and testable on its own; execution_quantity.DUST is the same string.
DUST = "DUST"

_armed: dict[str, dict] = {}


def clear(product_id) -> bool:
    """Forget any cooldown for this product. True if one was dropped.

    Called when inventory may have grown - after a buy fills - because the
    whole premise of the skip is that nothing changed.
    """
    return _armed.pop(str(product_id), None) is not None


def note_dust(product_id, decision, *, available_units=None, reason=None,
              now=None) -> bool:
    """Arm the cooldown, but ONLY for a computed DUST decision.

    Returns True if a cooldown is now armed. Anything that is not exactly
    DUST clears instead of arming: an EXECUTE obviously, and equally an
    unreadable anything, which must never buy fifteen minutes of silence.
    """
    pid = str(product_id)
    if decision != DUST:
        clear(pid)
        return False
    _armed[pid] = {"at": float(now if now is not None else time.time()),
                   "available_units": available_units,
                   "reason": reason}
    return True


def skip_reason(product_id, *, now=None, cooldown=None):
    """Why to skip this attempt, or None to go ahead and try.

    None is the default answer. A product with no armed cooldown, and one
    whose cooldown has expired, both get None - the expired one also drops
    its record, so the next refusal logs fresh rather than compounding.
    """
    pid = str(product_id)
    rec = _armed.get(pid)
    if rec is None:
        return None
    window = float(cooldown if cooldown is not None else COOLDOWN_SECONDS)
    if window <= 0:                      # a zero or negative window disables it
        clear(pid)
        return None
    elapsed = float(now if now is not None else time.time()) - rec["at"]
    if elapsed >= window:
        clear(pid)
        return None
    avail = rec.get("available_units")
    avail_txt = "unknown" if avail is None else f"{avail}"
    return (f"{rec.get('reason') or DUST} {int(window - elapsed)}s ago-window: "
            f"the venue held {avail_txt} at the last attempt, under one "
            f"tradeable unit. Not re-asking for another "
            f"{int(window - elapsed)}s. This is a cooldown on the QUESTION, "
            f"not on the sale - nothing is cancelled and nothing is refused "
            f"that the venue would have accepted.")


def armed_products():
    """What is currently on cooldown, for a status panel. Never the store."""
    return {pid: dict(rec) for pid, rec in _armed.items()}
