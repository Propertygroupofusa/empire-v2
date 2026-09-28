"""Fund named coins from free cash, or fund fewer of them properly.

The owner asked for $193.26 of freed cash to go into PRIME, TON and APE -
the three coins coin_scan.py ranked above everything the fleet holds. By the
time the branches could be created the figure was $137.79 and falling,
because the branches whose claims the reconciliation had just lowered went
and spent the cash it released on dips. That is the system working, and it
means the amount available is not the amount quoted.

So the plan is computed from the cash that exists at write time, never from
a figure carried over from a conversation, and it has one opinion of its
own: IT WOULD RATHER FUND FEWER COINS PROPERLY THAN ALL OF THEM USELESSLY.
A branch below MIN_VIABLE_BRANCH_USD cannot place a slice that clears
MIN_TRADE_USD across its levels, so creating it spends real money on
something that will never trade - the exact "stranded capital" this fleet
has spent the whole day digging out of.

Nothing here places an order. It creates branches, which then buy their own
dips through every gate that already exists.
"""

# A branch needs at least this much to be worth creating: three levels that
# each clear the venue minimum. Below it the branch exists, holds cash, and
# never trades - which is worse than not funding that coin at all.
MIN_TRADE_USD = 5.0
MIN_LEVELS = 3
MIN_VIABLE_BRANCH_USD = MIN_TRADE_USD * MIN_LEVELS

# No coin over this share of the fleet, whatever it scored. The owner's
# standing rule, and it binds before any target here.
MAX_SINGLE_COIN_SHARE = 0.20


def plan(coins, free_cash_usd, reserve_usd, held_product_ids=None,
         fleet_total_usd=None, min_viable=MIN_VIABLE_BRANCH_USD,
         max_share=MAX_SINGLE_COIN_SHARE):
    """What to create, in priority order. Returns (plan, report).

    `coins` is the ranked wishlist, best first - the order decides who gets
    funded when there is not enough for everyone.

    Refuses rather than guessing on: unreadable cash, nothing above the
    reserve, or a per-coin share too small to trade. Every refusal names
    its arithmetic so nobody has to re-derive it.
    """
    if free_cash_usd is None:
        return [], {"status": "UNKNOWN",
                    "detail": "free cash unreadable - a gap is not a zero, nothing funded"}
    held = {p for p in (held_product_ids or [])}
    wanted = [c for c in coins if c not in held]
    already = [c for c in coins if c in held]
    if not wanted:
        return [], {"status": "OK",
                    "detail": f"every coin on the list already has a branch: "
                              f"{', '.join(already)}",
                    "already_held": already}

    deployable = float(free_cash_usd) - max(0.0, float(reserve_usd or 0.0))
    if deployable < min_viable:
        return [], {"status": "HOLD",
                    "detail": (f"${free_cash_usd:,.2f} free cash less the "
                               f"${reserve_usd:,.2f} reserve leaves ${deployable:,.2f}, "
                               f"under one viable ${min_viable:,.2f} branch - nothing "
                               f"funded rather than something that cannot trade"),
                    "deployable_usd": round(deployable, 2)}

    # FEWER, PROPERLY. Drop the lowest-ranked coin until each survivor gets
    # a branch that can actually place a slice.
    n = len(wanted)
    while n > 1 and deployable / n < min_viable:
        n -= 1
    each = deployable / n
    funded = wanted[:n]
    deferred = wanted[n:]

    if fleet_total_usd:
        cap = float(fleet_total_usd) * max_share
        if each > cap:
            each = cap

    # FLOORED to the cent, never rounded: three rounded shares of $49.79 come
    # to $49.80 and the last branch finds a cent that is not there. Same
    # property as every other even split in this codebase.
    each = int(each * 100) / 100.0
    rows = [{"product_id": c, "usd": each} for c in funded]
    total = round(sum(r["usd"] for r in rows), 2)
    detail = (f"${free_cash_usd:,.2f} free cash less the ${reserve_usd:,.2f} reserve "
              f"leaves ${deployable:,.2f}; funding {len(rows)} branch(es) at "
              f"${each:,.2f} each (${total:,.2f} total)")
    if deferred:
        detail += (f". {', '.join(deferred)} deferred - splitting further would put every "
                   f"branch under the ${min_viable:,.2f} it needs to trade")
    if already:
        detail += f". Already held: {', '.join(already)}"
    return rows, {"status": "FUND", "detail": detail,
                  "deployable_usd": round(deployable, 2),
                  "total_usd": total, "deferred": deferred, "already_held": already}


# The most of the deployable cash that may be held back for coins not yet
# funded. Half, deliberately.
#
# The owner authorised giving the deployer first claim so new coins are not
# outrun by a 30-second loop, AND said "make sure the money keeps flipping".
# Those pull against each other: a pure first claim starves every dip buy in
# the fleet until three branches exist, and a fleet that stops buying dips
# stops selling rises a few hours later. So this is a RESERVATION, not a
# monopoly - the deployer gets first call on up to half, and everything
# above that keeps flowing to branches that are already trading.
#
# It extinguishes itself. Once every target coin has a branch the reserve is
# 0.00 and the sizing path is byte-for-byte what it was before.
MAX_DEPLOYMENT_RESERVE_SHARE = 0.50


def deployment_reserve_usd(unfunded_count, per_coin_usd, deployable_usd,
                           max_share=MAX_DEPLOYMENT_RESERVE_SHARE):
    """Cash to hold back from dip buys for coins not yet funded. (usd, why).

    Returns 0.0 the moment there is nothing left to fund, which is what
    makes this safe to leave switched on: it is a temporary claim on a
    temporary shortage, not a standing tax on the fleet.
    """
    n = max(0, int(unfunded_count or 0))
    if n == 0:
        return 0.0, "every target coin already has a branch - nothing reserved"
    if deployable_usd is None:
        return 0.0, "deployable cash unreadable - a gap is not a zero, nothing reserved"
    dep = float(deployable_usd)
    if dep <= 0:
        return 0.0, f"${dep:,.2f} deployable - nothing to reserve from"
    needed = n * max(0.0, float(per_coin_usd or 0.0))
    ceiling = dep * max_share
    reserve = min(needed, ceiling)
    reserve = int(reserve * 100) / 100.0
    if reserve >= needed - 0.01:
        why = (f"${reserve:,.2f} held for {n} unfunded coin(s) - the full "
               f"${needed:,.2f} they need, inside the "
               f"{max_share*100:.0f}% cap on ${dep:,.2f} deployable")
    else:
        why = (f"${reserve:,.2f} held for {n} unfunded coin(s) - capped at "
               f"{max_share*100:.0f}% of ${dep:,.2f} deployable rather than the "
               f"${needed:,.2f} they need, so the branches already trading keep "
               f"buying their dips")
    return reserve, why
