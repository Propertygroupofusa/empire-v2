"""Profit ratchet: lock verified banked profit in tiers; pause new buys if
trading capital falls back to the floor. Never sells.

Owner's final design, 2026-09-30:
  * the ONLY input that can advance a tier is realized profit, and only after
    the grid ledger reconciles with the exchange. Fleet value and unrealized
    P&L are never read here, so phantom inventory (QNT showed +$86.84 on coin
    the venue may not hold) can never move the ratchet;
  * each tier is 1% of current trading capital, recomputed after every tier;
  * half of each tier is locked in a dedicated bucket, half keeps compounding;
  * profit banked before arming is credited only if reconciliation is clean
    at arming - otherwise the seed is zero;
  * reconciliation that is unreadable or SHORT fails CLOSED: no tier advances
    until it is clean again;
  * the floor is trading capital as it stood when the last tier locked. New
    buys pause while current trading capital is below it. Nothing is sold;
    open positions keep their normal exits.

Trading capital = principal + realized - locked. Principal is grid capital
at arming minus the profit it already carried. Locked only ever rises.

Pure functions only - no database, no network.
"""

TIER_STEP_PCT = 0.01
LOCK_FRACTION = 0.5
# How long buys may stay paused with NOTHING open before the floor re-anchors.
DEADLOCK_RELEASE_HOURS = 24.0


def advance(principal, tier, consumed, locked, realized_total,
            step=TIER_STEP_PCT, lock_fraction=LOCK_FRACTION, cross=True):
    """Cross every tier realized profit now covers.

    consumed is the realized profit already used by crossed tiers (plus, if
    the seed was refused, the pre-arm profit that must not count).
    cross=False reports the state as persisted without crossing anything -
    what the ratchet shows while a due tier waits on reconciliation.
    Returns (state, events); events are (tier, tier_size, locked_add).
    """
    tier = int(tier or 0)
    consumed = float(consumed or 0.0)
    locked = float(locked or 0.0)
    principal = float(principal or 0.0)
    realized_total = float(realized_total or 0.0)
    events = []
    for _ in range(10000 if cross else 0):  # hard stop; a real account crosses a handful
        size = step * (principal + consumed - locked)
        if not size > 0 or realized_total - consumed < size:
            break
        consumed += size
        add = lock_fraction * size
        locked += add
        tier += 1
        events.append((tier, round(size, 2), round(add, 2)))
    floor = principal + consumed - locked
    current = principal + realized_total - locked
    next_size = step * floor if floor > 0 else None
    return {
        "tier": tier,
        "consumed": round(consumed, 6),
        "locked_usd": round(locked, 2),
        "trading_capital": round(current, 2),
        "floor": round(floor, 2),
        "progress_usd": round(realized_total - consumed, 2),
        "next_tier_size": round(next_size, 2) if next_size else None,
        # Below the floor only when realized losses have given back more than
        # was banked since the last tier. Exactly at it is not a breach.
        "buys_paused": current < floor - 1e-9,
    }, events


def reconcile(tracked_by_product, held_by_currency, increments_by_product):
    """Does every product the grid claims to hold actually exist on the venue?

    tracked_by_product     {product_id: qty in open grid slices}
    held_by_currency       {currency: available + hold}, or None if unread
    increments_by_product  {product_id: base_increment}

    Returns (ok, findings). ok is True only when every product is MATCHED or
    EXCESS (the account may hold more than the grid tracks - manual coin is
    not a phantom). SHORT or UNKNOWN anywhere fails closed.
    """
    from restart_recovery import reconcile_inventory, MATCHED, EXCESS
    if held_by_currency is None:
        return False, ["balances unreadable"]
    findings = []
    for pid, qty in sorted((tracked_by_product or {}).items()):
        if not qty:
            continue
        cur = pid.split("-")[0]
        verdict = reconcile_inventory(qty, held_by_currency.get(cur, 0.0),
                                      (increments_by_product or {}).get(pid))
        if verdict not in (MATCHED, EXCESS):
            findings.append(f"{pid} {verdict}: grid tracks {qty:g}, venue holds "
                            f"{held_by_currency.get(cur, 0.0):g}")
    return (not findings), findings


def deadlock_step(buys_paused, open_slices, flat_paused_since, now_ts,
                  release_hours=DEADLOCK_RELEASE_HOURS):
    """The one state the floor cannot leave on its own.

    Only a SELL moves realized profit, and a sell needs an open slice. So
    paused-with-nothing-open is permanent: no buy is allowed, no sell is
    possible. (Reached by stops liquidating the book below the floor.)

    Returns (new_flat_paused_since, release). release=True means re-anchor
    the floor to current trading capital. Locked profit is untouched - only
    the floor moves down, and only after release_hours of that exact state.
    """
    if not buys_paused or open_slices > 0:
        return 0.0, False
    since = flat_paused_since or now_ts
    return since, (now_ts - since) >= release_hours * 3600.0
