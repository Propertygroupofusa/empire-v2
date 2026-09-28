"""Parked capital, split by WHAT WOULD MOVE IT.

THE QUESTION THIS EXISTS TO ANSWER. The owner, 28 Sep:

    "Parked capital is unchanged at $4,219.67 - over half the book -
     with $3,623.17 one fill behind it. well make it better"

The fleet already reported both halves of that, in two different
invariants that never met:

  no_dead_capital        says HOW MUCH cannot buy.
  grid_inventory_is_free says some of the coin is reserved at the venue.

Read separately they suggest one problem with one size. They are not.
A branch full on its rungs and underwater is parked by PRICE and
nothing but price or a sale moves it. A branch whose coin is reserved
by a resting order is parked by a LOCK, and a cancel - which places no
order and buys nothing - frees it today. Blending them produces a
single scary number nobody can act on, which is the state the owner was
looking at when they asked.

TWO DENOMINATORS, NEVER ADDED. A branch's allocated_usd is what the
fleet committed to it. A locked_usd is units x price that the venue is
holding. They are different measurements of different things, and
summing them would invent capital. The locked bucket carries both and
this module never adds one to the other.

PRECEDENCE, because a branch can be parked twice over. A branch that is
locked AND underwater appears once, under LOCKED - not because the lock
is the bigger problem but because it is the only one of the two that
anybody can do something about today. The precedence is stated in the
payload so nobody has to infer it from the totals.
"""

# Matches invariants.NEAR_EXIT_PCT's role: a branch marginally under but
# within this of a profitable exit is a grid between fills, not stuck.
NEAR_EXIT_PCT = 1.0


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _is_full(branch):
    n = branch.get("open_slices")
    lv = branch.get("num_levels")
    if n is None or lv is None:
        return None
    try:
        return int(n) >= int(lv)
    except (TypeError, ValueError):
        return None


def _is_one_fill_away(branch):
    n = branch.get("open_slices")
    lv = branch.get("num_levels")
    if n is None or lv is None:
        return None
    try:
        n, lv = int(n), int(lv)
    except (TypeError, ValueError):
        return None
    return lv >= 1 and n == lv - 1


def split_by_cause(branches, locked_positions=None, near_exit_pct=NEAR_EXIT_PCT):
    """Split parked capital into buckets by what would release each one.

    branches: the same dicts no_dead_capital takes - product_id,
        allocated_usd, open_slices, num_levels, best_slice_net_pct. Taking
        the status payload's own shape is deliberate: a second
        reimplementation of "full" is a second thing to drift.
    locked_positions: grid_inventory_is_free's own `locked_positions`
        extra - product_id, locked_pct, locked_usd. Passing it rather
        than recomputing it means the two can never disagree.

    None for locked_positions means THE LOCK STATE WAS NOT READ, which
    is not the same as nothing being locked, and is reported as such.
    """
    if not branches:
        return {"readable": False,
                "reason": "no branch data - nothing can be said about parked capital"}

    locked_by_product = {}
    lock_state_read = locked_positions is not None
    for row in (locked_positions or []):
        pid = (row or {}).get("product_id")
        if pid:
            locked_by_product[str(pid)] = row

    buckets = {
        "locked_at_venue": [],
        "underwater": [],
        "full_but_profitable": [],
        "one_fill_from_full": [],
        "unreadable": [],
    }

    for b in branches:
        pid = str(b.get("product_id") or "")
        alloc = _num(b.get("allocated_usd")) or 0.0
        # A LOCK DOES NOT WAIT FOR A BRANCH TO FILL ITS RUNGS.
        #
        # This check used to sit BELOW the full/not-full split, so a
        # locked branch that still had a rung free was never counted as
        # locked. On the first live run that reported $219.64 across 2
        # branches while grid_inventory_is_free was reporting $945.90
        # across 7 - the same condition, two different numbers, because
        # one of them was asking an irrelevant question first. A branch
        # whose coin is reserved at the venue cannot sell a single unit,
        # full or not, and a cancel frees it either way.
        lock = locked_by_product.get(pid)
        if lock is not None:
            buckets["locked_at_venue"].append({
                "product_id": pid,
                "allocated_usd": alloc,
                "locked_usd": _num(lock.get("locked_usd")),
                "locked_pct": _num(lock.get("locked_pct")),
                "was_full": _is_full(b),
            })
            continue

        full = _is_full(b)
        if full is None:
            buckets["unreadable"].append(
                {"product_id": pid, "allocated_usd": alloc,
                 "why": "slice count or level count missing"})
            continue

        if not full:
            if _is_one_fill_away(b):
                buckets["one_fill_from_full"].append(
                    {"product_id": pid, "allocated_usd": alloc})
            continue

        pct = _num(b.get("best_slice_net_pct"))
        if pct is None:
            # A GAP IS NOT A ZERO. An unpriced slice used to arrive as 0
            # and read exactly like break-even, fabricating a verdict.
            buckets["unreadable"].append(
                {"product_id": pid, "allocated_usd": alloc,
                 "why": "best slice could not be priced"})
        elif pct < -abs(near_exit_pct):
            buckets["underwater"].append(
                {"product_id": pid, "allocated_usd": alloc,
                 "best_slice_net_pct": pct})
        else:
            buckets["full_but_profitable"].append(
                {"product_id": pid, "allocated_usd": alloc,
                 "best_slice_net_pct": pct})

    def _sum(name, key="allocated_usd"):
        return round(sum((r.get(key) or 0.0) for r in buckets[name]), 2)

    parked_now = round(
        _sum("locked_at_venue") + _sum("underwater")
        + _sum("full_but_profitable"), 2)

    out = {
        "readable": True,
        "buckets": {
            "locked_at_venue": {
                "allocated_usd": _sum("locked_at_venue"),
                "coin_reserved_usd": _sum("locked_at_venue", "locked_usd"),
                "branches": buckets["locked_at_venue"],
                "moves_when": (
                    "a resting order is cancelled. A cancel places no order and "
                    "buys nothing; it also gives up whatever protection that "
                    "order was armed for, so it is a deliberate decision."),
                "actionable_today": True,
            },
            "underwater": {
                "allocated_usd": _sum("underwater"),
                "branches": buckets["underwater"],
                "moves_when": (
                    "price recovers to where a slice can sell at a profit, or the "
                    "position is sold at a loss. Nothing else releases it."),
                "actionable_today": False,
            },
            "full_but_profitable": {
                "allocated_usd": _sum("full_but_profitable"),
                "branches": buckets["full_but_profitable"],
                "moves_when": (
                    "the next sell fills. This is a grid working, not capital "
                    "stranded - it releases itself."),
                "actionable_today": False,
            },
            "one_fill_from_full": {
                "allocated_usd": _sum("one_fill_from_full"),
                "branches": buckets["one_fill_from_full"],
                "moves_when": (
                    "nothing - it is not parked yet. One more buy fill and it "
                    "joins the parked figure."),
                "actionable_today": False,
            },
            "unreadable": {
                "allocated_usd": _sum("unreadable"),
                "branches": buckets["unreadable"],
                "moves_when": "unknown - these could not be classified at all",
                "actionable_today": False,
            },
        },
        "parked_now_usd": parked_now,
        "parked_if_every_near_branch_fills_usd": round(
            parked_now + _sum("one_fill_from_full"), 2),
        "actionable_today_usd": _sum("locked_at_venue"),
        "lock_state_read": lock_state_read,
        "precedence": (
            "locked_at_venue first, for EVERY branch whether or not it is full "
            "- a reserved position cannot sell either way - then underwater, "
            "then full_but_profitable. A branch parked twice over is counted "
            "ONCE, under locked, because that is the only cause anyone can act "
            "on today."),
        "two_denominators": (
            "allocated_usd is what the fleet committed to a branch. "
            "coin_reserved_usd is units x price the venue is holding. They "
            "measure different things and are never added together."),
    }

    if not lock_state_read:
        out["caveat"] = (
            "THE LOCK STATE WAS NOT READ, so no branch could be placed in the "
            "locked bucket. Capital parked by a resting order is in here "
            "counted as parked by price, which overstates what only price can "
            "fix. An unread lock is not an absent lock.")
    if buckets["unreadable"]:
        out["caveat_unreadable"] = (
            f"{len(buckets['unreadable'])} branch(es) could not be classified "
            f"and are in NO cause bucket. They are not counted as fine.")
    return out
