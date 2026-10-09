"""The sell rule, written once, for every READER of fleet state.

The EXECUTOR is crypto_grid_bot.py and it is not this file. This is the
model readers use to answer "could that branch sell right now?" without
re-deriving the rule and getting it wrong - which has now happened three
times, each in a different function:

    d7f0bb7   the slice's own gain used as the branch rule   (advice went out)
    284228b   the parked floor applied to an unparked branch (advice went out)
    04fb15c   parked and rise treated as exclusive           (understated ready)

test_trigger_consistency.py asserts this file agrees with the executor's
source across thousands of generated branches, and pins the executor's own
text so a change there fails loudly instead of letting readers drift.

THE RULE, from crypto_grid_bot.py:7004 and :7046-7051:

    _parked   = bool(slices) and (len(slices) >= num_levels
                                  or branch_is_adopted_only(slices))
    _rise_hit = bool(slices and price >= reference_price * (1 + grid_pct))
    if _stop_slice is not None or _parked_sell or _rise_hit:   # OR, not elif

UNITS: grid_pct is a FRACTION. 0.03 means 3%. Not a percent - a spec that
writes (1 + grid_pct / 100) is the same rule off by 100x.

The stop leg is deliberately NOT modelled. A stop is a loss being booked,
never a sale a reader should describe as available.
"""

# GRID_PARKED_MIN_NET_PCT - the floor a PARKED branch's slice must clear.
PARKED_FLOOR_PCT = 0.010


def is_parked(branch):
    """The executor's own rule, imported - the FOURTH re-derivation failed too.

    This read:

        return len(slices) >= levels or all(s.get("adopted") for s in slices)

    while the executor had moved to counting TRADEABLE slices, because a rung
    filled by a sub-$1 remnant the venue will not sell is stuck, not full -
    BCH-USD placed a sell for 0.00000022 BCH every ~10 minutes for 200
    attempts before that landed. So this file, written to stop readers
    re-deriving the rule after three separate times that sent wrong advice to
    the owner, re-derived it wrong a fourth time in the one function it exists
    to provide. test_trigger_consistency could not see it, because its own
    reference model had been written from a reader instead of the executor.

    So the rule is not written here any more. The import sits inside the
    function to keep this module light for the scripts that read it, and it is
    allowed to RAISE: a reader that cannot load the rule has to say so rather
    than answer from a copy, which is the entire history above.
    """
    import crypto_grid_bot as _grid
    return _grid.branch_is_parked(branch.get("slices"),
                                  branch.get("num_levels"))


def can_buy(branch):
    """Can this branch open a rung? Parked stops it, and so does the breaker.

    buys_paused and drawdown_breached are separate from the parked rule and
    also stop a buy. Leaving them out counted JASMY and ONDO as tradeable
    while both had stopped buying.
    """
    if is_parked(branch):
        return False
    return not (branch.get("buys_paused") or branch.get("drawdown_breached"))


def can_sell(branch):
    """Can this branch close a rung RIGHT NOW? True / False / None=UNKNOWN.

    Rise is evaluated FIRST and unconditionally, because the executor
    computes _rise_hit before the branch and ORs it. A parked branch whose
    price has cleared the trigger still sells.
    """
    slices = branch.get("slices") or []
    if not slices:
        return False
    gp = branch.get("grid_pct")
    ref = branch.get("reference_price")
    px = branch.get("current_price")
    readable = bool(gp and ref and px)
    if readable and px >= ref * (1 + gp):
        return True
    if is_parked(branch):
        best = max((s.get("unrealized_net_pct") or 0) for s in slices)
        if best >= PARKED_FLOOR_PCT:
            return True
        # Parked floor missed and the rise leg unreadable: the executor
        # could still sell on rise, so this is UNKNOWN, never False.
        return False if readable else None
    return False if readable else None


def mobility(branches):
    """The capital split that explains why the fleet is not cycling.

    Returns can-buy, can-sell and can-do-both as SEPARATE figures. They are
    never blended: measured live, 18.7% could buy, 1.8% could sell and 0.0%
    could do both, so a single "mobility %" would have read 20% while not
    one dollar could complete a cycle. A branch that can only buy is
    ACCUMULATING; one that can only sell is DRAINING.
    """
    def _usd(bs):
        return round(sum((b.get("allocated_usd") or 0.0) for b in bs), 2)

    buy = [b for b in branches if can_buy(b)]
    sell = [b for b in branches if can_sell(b) is True]
    unknown = [b for b in branches if can_sell(b) is None]
    both = [b for b in buy if can_sell(b) is True]
    total = round(sum((b.get("allocated_usd") or 0.0) for b in branches), 2)

    def _pct(bs):
        return round(100.0 * _usd(bs) / total, 1) if total else None

    return {
        "total_allocated_usd": total,
        "can_buy": {"usd": _usd(buy), "pct": _pct(buy), "branches": len(buy),
                    "products": sorted(b.get("product_id") for b in buy)},
        "can_sell": {"usd": _usd(sell), "pct": _pct(sell), "branches": len(sell),
                     "products": sorted(b.get("product_id") for b in sell)},
        "can_do_both": {"usd": _usd(both), "pct": _pct(both), "branches": len(both),
                        "products": sorted(b.get("product_id") for b in both)},
        "sell_unreadable": {"usd": _usd(unknown), "pct": _pct(unknown),
                            "branches": len(unknown),
                            "products": sorted(b.get("product_id") for b in unknown)},
        "these_are_not_one_number": (
            "can_buy and can_sell are different capabilities and must not be "
            "averaged or blended. A branch that can only buy is accumulating; "
            "one that can only sell is draining. can_do_both is the only figure "
            "that means a complete round trip is available right now."),
        "sell_unreadable_is_not_a_no": (
            "branches whose sell rule could not be read are counted in NONE of "
            "the three above. UNKNOWN is a third verdict, never folded into "
            "False."),
    }
