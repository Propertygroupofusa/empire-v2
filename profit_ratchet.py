"""Profit ratchet: lock gains in tiers, pause new buys if the fleet falls back.

Chosen by the account owner on 2026-09-30:
  * tiers every 5% of fleet value, so they grow with the account;
  * when a tier is crossed, HALF of that tier's gain is locked and half
    keeps compounding;
  * if fleet value falls back to the locked tier, new buys PAUSE. Nothing
    is sold - that is the owner's explicit choice over forced liquidation.

What this can and cannot promise
--------------------------------
It cannot make a negative number impossible. A pause stops the fleet adding
to a falling market; it does not stop open coin from falling further, and
this code will never force a sale to defend a line. What it does guarantee:

  * locked cash is never spent by a grid buy, so profit that was banked
    stays banked;
  * the fleet stops buying once it has given back a whole tier.

Locked money is REAL cash only. A tier can be crossed on unrealized gains,
but only realized (banked) profit exists in the wallet, so the locked amount
is capped at realized profit since arming. The unfunded remainder is carried
and locks itself as sells bank more.

Pure functions only - no database, no network - so every number here can be
tested by hand.
"""

TIER_STEP_PCT = 0.05
LOCK_FRACTION = 0.5


def tier_level(base: float, k: int, step: float = TIER_STEP_PCT) -> float:
    """Fleet value at tier k: base grown by `step` k times."""
    return base * (1.0 + step) ** k


def advance(base, tier, locked_target, fleet_equity, realized_since_arm,
            step=TIER_STEP_PCT, lock_fraction=LOCK_FRACTION):
    """One measurement. Returns (new_state, events).

    new_state: dict with tier, locked_target, locked_usd, floor, next_tier,
    buys_paused. events: list of crossed tiers (k, level, locked_add).

    `tier` never goes down: a tier, once reached, stays reached. That is the
    ratchet. A fleet_equity of None (any branch unreadable this pass) changes
    nothing and pauses nothing - an unknown is not a breach.
    """
    tier = int(tier or 0)
    locked_target = float(locked_target or 0.0)
    events = []
    if base and base > 0 and fleet_equity is not None:
        while fleet_equity >= tier_level(base, tier + 1, step):
            prev = tier_level(base, tier, step)
            tier += 1
            level = tier_level(base, tier, step)
            add = lock_fraction * (level - prev)
            locked_target += add
            events.append((tier, round(level, 2), round(add, 2)))

    banked = max(0.0, float(realized_since_arm or 0.0))
    locked_usd = round(min(locked_target, banked), 2)

    floor = tier_level(base, tier - 1, step) if (base and tier >= 1) else None
    buys_paused = (floor is not None and fleet_equity is not None
                   and fleet_equity <= floor)
    return {
        "tier": tier,
        "locked_target": round(locked_target, 2),
        "locked_usd": locked_usd,
        "locked_unfunded": round(max(0.0, locked_target - locked_usd), 2),
        "floor": round(floor, 2) if floor is not None else None,
        "next_tier": round(tier_level(base, tier + 1, step), 2) if base else None,
        "buys_paused": buys_paused,
    }, events
