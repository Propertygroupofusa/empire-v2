"""Profit ratchet: lock banked profit in tiers; pause new buys if the fleet
falls back to the locked floor. Never sells.

Owner's choices, 2026-09-30:
  * tiers move on BANKED (realized) profit only - never on price or paper
    gains, so a phantom unrealized gain can never raise the floor;
  * each tier is 1% of trading capital, recomputed after every tier, so
    tiers grow as the account does;
  * half of each tier's profit is locked; half keeps compounding;
  * profit already banked before arming counts toward the first tier;
  * at the floor, new buys pause. Nothing is sold.

Locked only ever goes UP. A realized loss moves the counter back (the next
tier needs the loss earned back first) but never unlocks anything; only the
owner disarming releases it.

What this cannot promise: open P&L is just price. Coin already held can sit
below any floor, and this code never sells to defend one. The guarantees are
that banked profit, once locked, is never respent, and that the fleet stops
buying into a fall.

Pure functions only - no database, no network.
"""

TIER_STEP_PCT = 0.01
LOCK_FRACTION = 0.5


def advance(principal, tier, consumed, locked, realized_total,
            step=TIER_STEP_PCT, lock_fraction=LOCK_FRACTION):
    """Cross every tier that realized profit now covers.

    principal       trading capital at arming, net of profit already banked
    consumed        realized profit already used up by crossed tiers
    realized_total  all realized grid profit counted (incl. pre-arm profit)

    Returns (state, events); events are (tier, tier_size, locked_add).
    """
    tier = int(tier or 0)
    consumed = float(consumed or 0.0)
    locked = float(locked or 0.0)
    realized_total = float(realized_total or 0.0)
    events = []
    principal = float(principal or 0.0)
    for _ in range(10000):  # hard stop; a real account crosses a handful
        trading_capital = principal + consumed - locked
        size = step * trading_capital
        if size <= 0 or realized_total - consumed < size:
            break
        consumed += size
        add = lock_fraction * size
        locked += add
        tier += 1
        events.append((tier, round(size, 2), round(add, 2)))
    trading_capital = principal + consumed - locked
    next_size = step * trading_capital if trading_capital > 0 else None
    return {
        "tier": tier,
        "consumed": round(consumed, 6),
        "locked_usd": round(locked, 2),
        "trading_capital": round(trading_capital, 2),
        "progress_usd": round(realized_total - consumed, 2),
        "next_tier_size": round(next_size, 2) if next_size else None,
    }, events


def floor_value(anchor_equity, locked, locked_at_arm):
    """The fleet value new buys must stay above: where the fleet stood when
    armed, plus everything locked SINCE. Profit locked from pre-arm earnings
    is already inside the anchor, so it does not raise the floor twice."""
    if not anchor_equity:
        return None
    return round(anchor_equity + max(0.0, locked - (locked_at_arm or 0.0)), 2)


def buys_paused(fleet_equity, floor):
    """Paused only once the fleet is genuinely BELOW the floor - at the floor
    exactly (the moment it is anchored, for one) is not a breach. An unknown
    fleet value never pauses anything."""
    return floor is not None and fleet_equity is not None and fleet_equity < floor
