#!/usr/bin/env python3
"""The account owner's own standing rule, made enforceable: no single coin
may hold more than 20% of the fleet's real capital.

Why this file exists
--------------------
The rule was stated, and the number 0.20 was even written down (see
capital_velocity.MAX_SINGLE_COIN_SHARE), but nothing in the live buy path
ever consulted it. Measured 2026-09-28 against the real fleet:

    ZEC-USD   $2,341.45 of $7,193.46 real cost basis   32.6%
    XRP-USD   $2,240.54 of $7,193.46 real cost basis   31.1%

Two coins, 63.7% of every dollar the fleet has deployed, and between them
ZERO completed round trips in 26 days of trading. Over the same window the
whole fleet realized $32.53, essentially all of it on coins holding under
$200 each. That is what an unenforced ceiling costs.

A second number disagreed with it in the same repo -
fleet_capital_allocator.MAX_BRANCH_SHARE = 0.35 - which is the exact bug
class that keeps recurring here: two numbers that must agree, written in
two places, with nothing forcing them to match. This module imports the
one in capital_velocity rather than restating it, so there is only ever
one ceiling to change.

What it does and does not do
----------------------------
ONE-DIRECTIONAL, on purpose. This can only ever refuse a NEW buy into a
coin that is already over the ceiling. It never sells, never trims, never
resizes an existing position, and never relaxes any other threshold - so
it can never realize a loss on the owner's behalf, and it cannot deepen a
concentration it was built to stop.

FAILS OPEN, on purpose. If the fleet's cost basis cannot be read, the
verdict is ALLOW with a reason saying so. A gate that halts trading
whenever a database read hiccups is a worse failure than the one it
guards against - and silence must never be mistaken for a measurement.
"""

from capital_velocity import MAX_SINGLE_COIN_SHARE

__all__ = ["MAX_SINGLE_COIN_SHARE", "coin_share", "concentration_verdict",
           "headroom_usd"]


def coin_share(product_id, cost_basis_by_product, spend_usd=0.0):
    """This coin's share of the fleet AFTER a proposed spend of spend_usd.

    Returns None when it cannot be computed - an unreadable map, or a
    fleet holding nothing at all. None means UNKNOWN, never 0.0: a coin
    whose share could not be measured has not been measured at zero.
    """
    if cost_basis_by_product is None:
        return None
    try:
        spend = max(0.0, float(spend_usd or 0.0))
        held = {str(k): float(v or 0.0) for k, v in dict(cost_basis_by_product).items()}
    except (TypeError, ValueError):
        return None

    fleet_after = sum(held.values()) + spend
    if fleet_after <= 0:
        return None
    return (held.get(str(product_id), 0.0) + spend) / fleet_after


def concentration_verdict(product_id, cost_basis_by_product, spend_usd,
                          max_share=MAX_SINGLE_COIN_SHARE):
    """(allow, reason) for buying spend_usd more of product_id.

    Judged on the share the coin would hold AFTER the buy, against real
    cost basis - the dollars that actually left the wallet - not against
    allocated_usd, which is a plan, and not against live market value,
    which would tighten the ceiling on a coin purely because it rallied.
    """
    share_after = coin_share(product_id, cost_basis_by_product, spend_usd)
    if share_after is None:
        return True, ("fleet cost basis unreadable - concentration not checked this "
                      "cycle (allowed rather than halted; unknown is not a breach)")

    share_now = coin_share(product_id, cost_basis_by_product, 0.0)
    if share_now is None:
        # The fleet holds nothing yet, so share-after is 100% by
        # arithmetic rather than by concentration. A ceiling is a share OF
        # something; refusing here would mean the fleet could never place
        # its first order. Caught by test_a_first_buy_into_an_empty_fleet.
        return True, "the fleet holds nothing yet - no share to be over"

    pct_after, pct_now, cap = share_after * 100, share_now * 100, max_share * 100

    if share_after <= max_share:
        return True, (f"{product_id} would be {pct_after:.1f}% of the fleet after this buy, "
                      f"within the {cap:.0f}% ceiling")

    # Already over the line: this buy can only make it worse.
    if share_now > max_share:
        return False, (f"{product_id} already holds {pct_now:.1f}% of the fleet's real cost "
                       f"basis, over the {cap:.0f}% ceiling - no new dollars go into it while "
                       f"it is over. Nothing is sold; it comes back under the ceiling as the "
                       f"rest of the fleet grows or as its own slices sell at a profit.")
    return False, (f"this buy would take {product_id} from {pct_now:.1f}% to {pct_after:.1f}% "
                   f"of the fleet, through the {cap:.0f}% ceiling")


def headroom_usd(product_id, cost_basis_by_product, max_share=MAX_SINGLE_COIN_SHARE):
    """The most that may be added to this coin without breaching the
    ceiling. None when it cannot be computed; 0.0 when already at or over.

    Solving (basis + add) / (fleet + add) <= max_share for add gives

        add <= (max_share * fleet - basis) / (1 - max_share)

    because the money added lands in BOTH the numerator and the
    denominator - it is the coin's share of a fleet that now includes it.
    Treating the denominator as fixed understates the room by a fifth at
    a 20% ceiling, which is a quiet way to strand cash in an allocation
    a branch is then refused permission to spend.

    This exists so a caller can size a top-up to what is allowed rather
    than proposing a number the gate will refuse. concentration_verdict
    remains the thing that decides; this only says how much would pass.
    """
    if cost_basis_by_product is None or not (0 < max_share < 1):
        return None
    try:
        held = {str(k): float(v or 0.0) for k, v in dict(cost_basis_by_product).items()}
    except (TypeError, ValueError):
        return None
    fleet = sum(held.values())
    if fleet <= 0:
        return None
    room = (max_share * fleet - held.get(str(product_id), 0.0)) / (1.0 - max_share)
    return max(0.0, round(room, 2))
