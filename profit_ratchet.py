"""Profit ratchet: lock gains in tiers, and stop NEW risk below the lock.

Asked for by the account owner, 2026-09-30: once the account reaches a new
level, lock it, and let it only bounce UP to the next tier from there -
compounding - with no new losses taken below the locked level.

THE RULE
    floor starts at the Coinbase net worth when the ratchet first runs.
    Every time net worth reaches floor * (1 + step), the floor steps up to
    that tier (repeatedly, if a move crosses several). It never steps down.
    While net worth is below the floor, NO NEW BUYS. Sells always run.

Net worth is the whole Coinbase account the bot trades: real USD wallet
plus the live market value of every coin the grid holds - the owner's own
choice over a cash-only floor, which would have stopped nearly all buying
on day one because the branches' reserved cash is part of the wallet.

WHAT THIS CAN AND CANNOT PROMISE, stated in the code because it will be
read as a promise otherwise: it stops the bot committing NEW money while
the account is under its locked level. It cannot stop coin already held
from falling in price, so the account can still read below the floor - it
just will not add to the position while it does. Nothing here sells.

The tiers are geometric, so they compound: 5% steps from $10,000 lock
$10,500, then $11,025, then $11,576 - each step bigger in dollars as the
account grows.
"""
from __future__ import annotations

DEFAULT_STEP_PCT = 0.05


def advance(floor, net_worth, step=DEFAULT_STEP_PCT):
    """(new_floor, tiers_crossed). Never lowers the floor.

    With no floor yet, the first readable net worth becomes the baseline.
    An unreadable (None) or non-positive net worth changes nothing.
    """
    if net_worth is None or net_worth <= 0 or step <= 0:
        return floor, 0
    if floor is None or floor <= 0:
        return round(float(net_worth), 2), 0
    new, crossed = float(floor), 0
    while net_worth >= new * (1 + step) - 1e-9:
        new *= (1 + step)
        crossed += 1
    return round(new, 2), crossed


def next_tier(floor, step=DEFAULT_STEP_PCT):
    return None if not floor else round(floor * (1 + step), 2)


def blocks_buys(floor, net_worth):
    """True below the floor. Unknown net worth is decided by the caller."""
    return floor is not None and net_worth is not None and net_worth < floor
