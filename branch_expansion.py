"""How many branches to open, on what, and for how much.

WHY A PLANNER AND NOT A LOOP

create_grid_branch already refuses a coin another branch holds, a coin a
family-tree branch holds, and an amount larger than real free cash. What
it cannot do is decide the SHAPE of an expansion: calling it fifteen times
in a loop spends the cash on whoever happens to be first in the list and
leaves the last few branches too small to trade.

The sizing constraint that decides everything: a branch is split into
levels, each level is one order, and an order under MIN_TRADE_USD is
refused by the venue. So a branch funded below levels x MIN_TRADE_USD does
not trade at all - it logs "waiting" forever. Splitting capital across
more coins is therefore not free; past a point it buys branches that
cannot act.

WHAT IT REFUSES TO DO

It will not spend the last dollar. RESERVE_USD stays behind because the
grid needs free cash to actually BUY at its rungs - an expansion that
earmarks everything into allocations leaves a fleet that is fully
committed and unable to fill anything, which is the exact state the
account was in before the trim freed $876.

It will not open a branch below the size that can trade, and it will not
silently shrink one to fit. It opens fewer, larger branches instead, and
says how many it dropped and why.
"""
from __future__ import annotations

# One order per level; the venue refuses anything smaller.
MIN_TRADE_USD = 5.0

# Levels each new branch gets. Three rungs is the live fleet's shape.
LEVELS = 3

# So this is the floor for a branch that can actually place every rung.
MIN_BRANCH_USD = MIN_TRADE_USD * LEVELS

# Cash the grid must keep free to fill its rungs. An expansion that
# allocates everything produces a fleet that cannot buy.
RESERVE_USD = 100.0

# No single new branch larger than this without a deliberate override -
# an expansion is a spread of small bets, not a concentrated one.
MAX_BRANCH_USD = 120.0

MAX_NEW_BRANCHES = 20


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def levels_for(allocated_usd, levels=LEVELS, min_trade=MIN_TRADE_USD):
    """Rungs a branch can actually place at this size, floored at 1."""
    a = _num(allocated_usd)
    if a is None or a <= 0:
        return 0
    return max(1, min(int(levels), int(a // (_num(min_trade) or MIN_TRADE_USD))))


def plan(free_cash_usd, candidates, *, existing=(), reserve_usd=RESERVE_USD,
         min_branch_usd=MIN_BRANCH_USD, max_branch_usd=MAX_BRANCH_USD,
         max_new=MAX_NEW_BRANCHES, levels=LEVELS):
    """What to open. Returns the branches and everything it refused.

    `candidates` is the ranked output of grid_universe - best first. Only
    coins it marked WORTH_A_BRANCH are eligible; the ranking's own verdict
    is not second-guessed here.

    Capital is split EVENLY across however many branches fit, rather than
    weighted by rank. That is deliberate: the walk-forward test showed the
    good/bad split holds out of sample but the order inside it does not,
    so weighting by rank would bet on an ordering that was measured to be
    unreliable.
    """
    cash = _num(free_cash_usd)
    if cash is None or cash <= 0:
        return {"ok": False, "reason": "NO_CASH",
                "detail": f"free cash reads {free_cash_usd!r}; nothing can be opened",
                "open": [], "skipped": []}

    reserve = max(_num(reserve_usd) or 0.0, 0.0)
    spendable = cash - reserve
    if spendable < min_branch_usd:
        return {"ok": False, "reason": "RESERVE_LEAVES_NOTHING",
                "detail": (f"${cash:,.2f} free less a ${reserve:,.2f} reserve is "
                           f"${spendable:,.2f}, under the ${min_branch_usd:,.2f} a branch "
                           f"needs to place all {levels} rungs"),
                "open": [], "skipped": []}

    have = {str(c).upper() for c in (existing or ())}
    skipped = []
    eligible = []
    for c in candidates or ():
        coin = str(c.get("coin", "")).upper()
        if not coin:
            continue
        if coin in have:
            skipped.append({"coin": coin, "reason": "ALREADY_A_BRANCH"})
            continue
        if c.get("verdict") != "WORTH_A_BRANCH":
            skipped.append({"coin": coin, "reason": c.get("verdict", "NOT_RANKED"),
                            "detail": c.get("why")})
            continue
        eligible.append(c)

    if not eligible:
        return {"ok": False, "reason": "NO_ELIGIBLE_COINS",
                "detail": "every candidate is already a branch or did not qualify",
                "open": [], "skipped": skipped}

    # How many branches the money supports at a size that can trade.
    affordable = int(spendable // min_branch_usd)
    count = min(len(eligible), affordable, int(max_new))
    if count <= 0:
        return {"ok": False, "reason": "CANNOT_AFFORD_ONE",
                "detail": f"${spendable:,.2f} spendable is under ${min_branch_usd:,.2f}",
                "open": [], "skipped": skipped}

    per = spendable / count
    cap = _num(max_branch_usd)
    capped_by_coins = False
    if cap and per > cap:
        # Prefer opening MORE branches over making each one oversized.
        wider = min(len(eligible), int(spendable // cap), int(max_new))
        if wider > count:
            count = wider
            per = spendable / count
        if per > cap:
            # Out of coins, not out of money. Cap the size and leave the
            # remainder unallocated rather than concentrating it - an
            # expansion is a spread of small bets by definition.
            per = cap
            capped_by_coins = True
    # Floor to the cent. round(x - 0.005, 2) looks like flooring and is
    # not: a per-branch figure landing exactly on the minimum came back a
    # cent under it and the plan refused itself.
    import math
    per = math.floor(per * 100) / 100.0

    if per < min_branch_usd:
        return {"ok": False, "reason": "SPREAD_TOO_THIN",
                "detail": (f"${spendable:,.2f} across {count} branches is ${per:,.2f} each, "
                           f"under the ${min_branch_usd:,.2f} needed to place all rungs"),
                "open": [], "skipped": skipped}

    chosen = eligible[:count]
    for c in eligible[count:]:
        skipped.append({"coin": str(c.get("coin")).upper(), "reason": "NO_CAPITAL_LEFT",
                        "detail": f"only {count} branches fit in ${spendable:,.2f}"})

    opens = [{"coin": str(c["coin"]).upper(),
              "product_id": f"{str(c['coin']).upper()}-USD",
              "allocated_usd": per,
              "levels": levels_for(per, levels),
              "slice_usd": round(per / max(levels_for(per, levels), 1), 2),
              "rank_net_per_day_pct": c.get("net_per_day_pct"),
              "why": c.get("why")} for c in chosen]

    total = round(sum(o["allocated_usd"] for o in opens), 2)
    return {
        "ok": True,
        "is_a_plan_not_a_change": True,
        "free_cash_usd": round(cash, 2),
        "reserve_usd": round(reserve, 2),
        "spendable_usd": round(spendable, 2),
        "branches": len(opens),
        "per_branch_usd": per,
        "total_usd": total,
        "left_unallocated_usd": round(cash - total, 2),
        "capped_by_available_coins": capped_by_coins,
        "open": opens,
        "skipped": skipped,
        "sizing_note": (f"${per:,.2f} each across {levels_for(per, levels)} rungs is "
                        f"${round(per / max(levels_for(per, levels), 1), 2):,.2f} a rung, "
                        f"against a ${MIN_TRADE_USD:,.2f} venue minimum."),
        "even_split_note": ("Capital is split evenly, not weighted by rank. The "
                            "walk-forward test found the good/bad split holds out of "
                            "sample while the order inside it does not, so weighting by "
                            "rank would bet on an ordering measured to be unreliable."),
        "capped_note": (("More spendable cash than ranked coins to spread it over, so "
                         f"each branch is capped at ${cap:,.2f} and the remainder stays "
                         f"free rather than concentrating it.") if capped_by_coins else None),
        "reserve_note": (f"${reserve:,.2f} stays free so the fleet can actually BUY at its "
                         f"rungs. Allocating everything produces a fully committed fleet "
                         f"that cannot fill - the state this account was in before."),
    }
