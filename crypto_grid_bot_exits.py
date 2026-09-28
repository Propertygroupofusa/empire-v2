"""Coins on the way out take no new dollars.

WHY THIS EXISTS

Moving the concentration ceiling onto the account book (2026-09-28)
lowered every reading: ZEC from 31.00% to 19.33%, XRP from 30.07% to
19.68%. That is the point of the account book and the owner chose it -
but it removed an accidental brake.

ZEC is the one coin the owner has said to exit. redeploy_freed_cash has
named it DEFAULT_SOURCE since it was written, and resting_stops excludes
it. Until the book changed, the ceiling refused new ZEC buys as a SIDE
EFFECT of a 31% reading. At 19.33% it would not, and the branch is
active with buys_paused=false. The level cap (7 slices against 3 levels)
happens to stop it today; that is arithmetic that changes the moment a
slice sells.

So the brake is made explicit rather than incidental: a coin being
exited is refused by name, not by a percentage that happens to be high
enough this week.

WHAT IT DOES AND DOES NOT DO

ONE-DIRECTIONAL. It can only ever refuse a buy. It never sells, never
resizes a position, never unblocks anything another gate refused - so it
cannot realize a loss and cannot deepen the position it names.

ONE LIST. The coins being exited are read from redeploy_freed_cash's
own DEFAULT_SOURCE, with GRID_EXIT_COINS to override, so there is no
second register to fall out of step with the one the redeploy plan uses.

It does NOT sell the coin. Exiting is still the owner's to run, through
the write-guarded reconcile and close-branch endpoints. This only stops
the fleet buying more of something on its way out the door.
"""
from __future__ import annotations

import os as _os


def _coin(key):
    return str(key or "").split("-")[0].strip().upper()


def exiting_coins():
    """The coins being exited, as a set of tickers.

    GRID_EXIT_COINS overrides, comma separated, tickers or product ids.
    Otherwise the redeploy plan's own source coin - one list, not two.
    """
    raw = _os.getenv("GRID_EXIT_COINS")
    if raw is not None:
        return {_coin(p) for p in raw.split(",") if _coin(p)}
    import redeploy_freed_cash
    return {_coin(redeploy_freed_cash.DEFAULT_SOURCE)}


def exit_verdict(product_id, exits=None):
    """(allow, reason) for putting new money into product_id.

    `exits` defaults to exiting_coins(). An empty list refuses nothing,
    which is what makes this one-directional: with nothing being exited
    it cannot bite.
    """
    names = exiting_coins() if exits is None else {_coin(p) for p in (exits or ())}
    coin = _coin(product_id)
    if coin and coin in names:
        return False, (f"{coin} is being exited - no new dollars go into a position "
                       f"on its way out. Nothing is sold here; closing it is still "
                       f"the owner's to run.")
    return True, f"{coin or product_id} is not on the exit list"
