"""Putting coin the account already owns under a grid, without buying or selling it.

WHAT THIS IS FOR

$8,646 of this account is coin that no branch manages. It cannot be put
to work today because create_grid_branch() funds a branch from free
spendable CASH and refuses an amount above it - there is no path for a
branch to take charge of units already held. That single gap is why the
bots count 5.48% of an $11,397 account.

Adoption closes it with bookkeeping rather than trading: create a branch
on a coin already held, and register the existing units as open slices
at the price on the day they are adopted. No purchase. No sale. The
branch simply starts full instead of starting in cash, and from that
moment it sells into strength and rebuys lower the way every other
branch does.

WHAT IT COSTS, SAID PLAINLY

Adopted coin stops being held and starts being traded. Over the prior 90
days holding BEAT the grid by 16.13 points (+72.71% against +56.58%),
because grids underperform in a rally. So this is not free upside, and
the account owner chose a bounded test slice for exactly that reason:
prove it on a few hundred dollars while the two-week verdict runs, not
on the whole portfolio.

Hence the caps below are small and are the point, not a placeholder.

THE INVARIANT THAT MATTERS MOST

allocated_usd must equal the market value of the units adopted. A branch
that claims more than the coin behind it is an unbacked branch, which is
the exact hole allocation_backing exists to detect. Adoption adds to
BOTH sides of that ledger by construction, or it does not happen.

AND THE ONE THAT BITES IN PRACTICE

Only AVAILABLE units can be adopted. The resting-stop worker puts base
on HOLD when it places a stop, and a branch that believes it owns units
the venue has locked will try to sell coin it cannot move. The trimmer
learned this the expensive way; this module sizes against available from
the start.
"""
from __future__ import annotations

# The bounded test. Chosen by the account owner over adopting a quarter
# ($2,100) or most ($8,600) of the held coin, so that the two-week
# verdict gets real evidence about adopted coin without betting the
# portfolio on a mechanism that has never run here.
#
# RAISING IT IS THE OWNER'S CALL, WHICH IS WHY IT READS AN ENV VAR. The
# cap was written as a per-call ceiling and the hourly loop called it once
# an hour, so it never bound across passes and the fleet reached 15 coins
# and $3,082 against a stated 3 and $1,000 (see
# coin_adoption_worker._adopted_so_far, which closes that hole). The
# figure itself was not wrong - the enforcement was. So the enforcement is
# fixed here and the number is left where the owner can move it
# deliberately, rather than being quietly raised to match whatever the
# unenforced version happened to reach. Nothing in this module ever
# raises its own limit to let more through.
import os as _os


def _env_float(name, default):
    try:
        v = float(_os.getenv(name, "") or default)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        v = int(float(_os.getenv(name, "") or default))
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


MAX_TOTAL_ADOPT_USD = _env_float("COIN_ADOPTION_MAX_TOTAL_USD", 1000.0)
MAX_PER_COIN_USD = _env_float("COIN_ADOPTION_MAX_PER_COIN_USD", 400.0)
MAX_COINS = _env_int("COIN_ADOPTION_MAX_COINS", 3)

# A branch needs levels x the venue minimum to trade at all, and enough
# clear of it that a price move cannot round a slice under the floor.
MIN_TRADE_USD = 5.0
LEVELS = 3
MIN_ADOPT_USD = 60.0

# The owner's rule. A position already over it is the TRIMMER's problem -
# putting a grid on it would manage an overweight position instead of
# reducing one, and the two loops would be working against each other.
MAX_POSITION_SHARE_PCT = 20.0

# Mirrors account_census.STABLE, asserted equal in the tests rather than
# imported - importing the census would pull aiohttp into a planner that
# must not be able to reach the venue at all.
STABLE = {"USD", "USDC", "USDT", "DAI", "PYUSD", "USDS"}

# Nothing is excluded by name any more.
#
# ZEC was, and that was the wrong tool. It is $2,285.38 - the single
# largest blocked item on the dashboard - and naming it simply left the
# money dead. The real concern was never ZEC; it was that a two-way grid
# on an OVERWEIGHT position fights the trimmer, selling into strength and
# then buying the dip straight back.
#
# That is now handled by the mechanism instead of the name: a position
# over the limit is adopted SELL-ONLY, so it walks down through strength
# at a profit target - banking money and reducing the concentration in
# one move, which is what the 20% rule wanted in the first place.
DEFAULT_EXCLUDED = ()


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _pos(v):
    f = _num(v)
    return f if (f is not None and f > 0) else None


def product_for(asset):
    a = str(asset or "").upper().strip()
    return f"{a}-USD" if a else None


def levels_for(usd, min_trade=MIN_TRADE_USD, cap=LEVELS):
    u = _pos(usd)
    if u is None or u < min_trade:
        return 0
    return max(1, min(cap, int(u // min_trade)))


def slice_units(units, price, levels):
    """Split adopted units into equal slices, each clearing the minimum.

    Equal entry prices on purpose: every slice is adopted at the same
    moment at the same price, and pretending otherwise would put cost
    bases in the ledger that nobody paid.

    The last slice takes the rounding remainder so the sum is exactly the
    units adopted - a split that loses a fraction would leave coin the
    branch thinks it does not own.
    """
    u, p = _pos(units), _pos(price)
    lv = int(levels or 0)
    if u is None or p is None or lv < 1:
        return []
    each = u / lv
    if each * p < MIN_TRADE_USD:
        lv = max(1, int((u * p) // MIN_TRADE_USD))
        each = u / lv
    out = [round(each, 12) for _ in range(lv - 1)]
    out.append(round(u - sum(out), 12))
    return [{"qty": q, "entry_price": p} for q in out if q > 0]


def refuse(asset, usd, share_pct, claimed, excluded, min_usd=MIN_ADOPT_USD,
           max_share_pct=MAX_POSITION_SHARE_PCT):
    """Why this coin cannot be adopted, or None."""
    a = str(asset or "").upper()
    if not a:
        return "UNREADABLE_ASSET"
    if a in STABLE:
        return "IS_CASH"
    if a in {str(x).upper() for x in (excluded or ())}:
        return "EXCLUDED_BY_NAME"
    pid = product_for(a)
    if pid in {str(c).upper() for c in (claimed or ())} or a in {
            str(c).split("-")[0].upper() for c in (claimed or ())}:
        return "ALREADY_CLAIMED"
    if usd is None:
        return "NO_PRICE"
    if usd < min_usd:
        return "TOO_SMALL_TO_TRADE"
    # NOT a refusal any more - see plan(), which adopts these SELL-ONLY.
    return None


def plan(holdings, *, account_total_usd, claimed_products=(),
         excluded=DEFAULT_EXCLUDED, max_total_usd=MAX_TOTAL_ADOPT_USD,
         max_per_coin_usd=MAX_PER_COIN_USD, max_coins=MAX_COINS,
         min_usd=MIN_ADOPT_USD, levels=LEVELS, already_adopted=()):
    """What would be adopted, exactly, and what each refusal was for.

    `holdings` rows carry asset, units, price and usd - the census shape.
    Every refusal is reported beside the adoptions, because "why was XLM
    skipped" is the question asked after a bad week, and it only has an
    answer if the skips are written down.
    """
    total = _pos(account_total_usd)
    done = {str(a).upper() for a in (already_adopted or ())}
    rows, refusals = [], []

    for h in (holdings or ()):
        if not hasattr(h, "get"):
            continue
        asset = str(h.get("asset") or "").upper()
        usd = _pos(h.get("usd"))
        units = _pos(h.get("available_units") if h.get("available_units") is not None
                     else h.get("units"))
        price = _pos(h.get("price"))
        share = round(usd / total * 100, 2) if (usd and total) else None

        if asset in done:
            refusals.append({"asset": asset, "usd": usd, "reason": "ALREADY_ADOPTED"})
            continue
        why = refuse(asset, usd, share, claimed_products, excluded, min_usd=min_usd)
        if why:
            refusals.append({"asset": asset, "usd": usd, "share_pct": share, "reason": why})
            continue
        if units is None or price is None:
            refusals.append({"asset": asset, "usd": usd, "reason": "NO_AVAILABLE_UNITS"})
            continue
        rows.append({"asset": asset, "usd": usd, "units": units,
                     "price": price, "share_pct": share})

    # Largest first: the test learns most from the position with the most
    # capital available to put behind it, subject to every cap below.
    rows.sort(key=lambda r: -r["usd"])

    adopt, spent = [], 0.0
    for r in rows:
        if len(adopt) >= max_coins:
            refusals.append({"asset": r["asset"], "usd": r["usd"],
                             "reason": "COIN_LIMIT_REACHED"})
            continue
        room = min(max_per_coin_usd, max_total_usd - spent)
        if room < min_usd:
            refusals.append({"asset": r["asset"], "usd": r["usd"],
                             "reason": "BUDGET_EXHAUSTED"})
            continue

        # SIZE AGAINST WHAT THE VENUE WILL RELEASE, NOT WHAT IS OWNED.
        #
        # The first version sized from the holding's full USD value and
        # divided by price, which happily produced 1,878 XLM out of an
        # available 100 when a resting stop held the rest. A branch that
        # believes it owns locked units tries to sell coin it cannot
        # move, and every one of those sells fails at the venue.
        available_usd = round(r["units"] * r["price"], 2)
        take_usd = round(min(r["usd"], available_usd, room), 2)
        if take_usd < min_usd:
            refusals.append({"asset": r["asset"], "usd": r["usd"],
                             "available_usd": available_usd,
                             "reason": "TOO_LITTLE_AVAILABLE_TO_TRADE"})
            continue
        take_units = min(take_usd / r["price"], r["units"])
        # OVER THE LIMIT -> SELL-ONLY. The branch takes the units, sells
        # into strength at its profit target, and never buys back while
        # it is over. Reducing the concentration and banking the profit
        # stop being a choice between two things.
        sell_only = bool(r["share_pct"] is not None
                         and r["share_pct"] > MAX_POSITION_SHARE_PCT)

        lv = levels_for(take_usd, cap=levels)
        slices = slice_units(take_units, r["price"], lv)
        if not slices:
            refusals.append({"asset": r["asset"], "usd": r["usd"],
                             "reason": "CANNOT_SPLIT_INTO_A_TRADEABLE_SLICE"})
            continue

        # The invariant: what the branch claims IS what the coin behind it
        # is worth. Computed from the slices actually written, never from
        # the requested figure, so a rounding difference cannot open a gap.
        backed = round(sum(s["qty"] * s["entry_price"] for s in slices), 2)
        adopt.append({
            "asset": r["asset"],
            "product_id": product_for(r["asset"]),
            "units": round(sum(s["qty"] for s in slices), 12),
            "price": r["price"],
            "allocated_usd": backed,
            "num_levels": len(slices),
            "slices": slices,
            "sell_only": sell_only,
            "why_sell_only": (
                f"{r['share_pct']:.1f}% of the account is over the "
                f"{MAX_POSITION_SHARE_PCT:.0f}% rule, so this branch may SELL its slices but "
                f"never buy more. It walks the position down through strength at a profit "
                f"target instead of being bought straight back on the next dip."
                if sell_only else None),
            "share_of_holding_pct": round(take_usd / r["usd"] * 100, 1) if r["usd"] else None,
            "position_share_pct": r["share_pct"],
            "leaves_held_usd": round(r["usd"] - backed, 2),
        })
        spent = round(spent + backed, 2)

    return {
        "ok": bool(adopt),
        "adopt": adopt,
        "coins": len(adopt),
        "total_usd": round(spent, 2),
        "refusals": refusals,
        "caps": {"max_total_usd": max_total_usd, "max_per_coin_usd": max_per_coin_usd,
                 "max_coins": max_coins, "min_usd": min_usd,
                 "max_position_share_pct": MAX_POSITION_SHARE_PCT},
        "buys_nothing": True,
        "sells_nothing": True,
        "sell_only_count": sum(1 for a in adopt if a.get("sell_only")),
        "is_a_plan_not_a_change": True,
        "detail": (
            (f"Would put ${spent:,.2f} of coin already owned under {len(adopt)} grid "
             f"branch(es), spending nothing and selling nothing. Each branch claims exactly "
             f"what its adopted coin is worth, so the backing ledger gains the same amount "
             f"on both sides. From then on those units are TRADED, not held - which is the "
             f"change, and the reason the cap is ${max_total_usd:,.0f} rather than the "
             f"${8646:,.0f} available."
             if adopt else
             f"Nothing is adoptable right now. {len(refusals)} holding(s) were considered "
             f"and each refusal is listed with its reason.")),
    }
