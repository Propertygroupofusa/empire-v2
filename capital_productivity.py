"""What each half of the fleet's capital earned, per dollar of it.

Item 1 of the standing system review, which has been computed by hand
from /grid-status and /grid-status/trade-history on every single pass.
Hand-computed numbers drift from the checks they are supposed to agree
with, which is this codebase's recurring failure, so it reads the same
branch payload no_dead_capital reads and applies the same full/has-room
rule - literally the same predicate, imported, not a second copy of it.

MEASURED 2026-09-28: the 14 branches with room earned $37.90 on
$4,486.63 over 48h and carried positive unrealized; the 9 full branches
earned $1.65 on $3,611.79 and carried -$137.13.

TWO THINGS THIS NUMBER IS NOT, both stated in the payload because a
figure that omits them gets read as more than it is:

1. IT IS NOT A RATE PER DAY. The numerator is a window's earnings; the
   denominator is allocated capital at ONE INSTANT, which moves on
   price. Dividing a long measurement by an instantaneous denominator
   is the exact error withdrawn from the performance page on 28 Sep
   (0.1183%/day, which read 0.0509%/day 22 minutes later with nothing
   traded). So this reports dollars earned per $100 of capital OVER THE
   STATED WINDOW and never converts it to a day. /edge-rate answers the
   rate question, with a span floor.

2. IT IS NOT PROOF THAT FREEING CAPITAL CAUSES EARNINGS. Branches are
   bucketed by their state NOW, and selling is what moves a branch out
   of the full bucket - so a branch can be in the "has room" half
   BECAUSE it earned. The relationship is partly circular and the
   payload says so. What the figure does establish is narrower and
   still worth knowing: capital that currently cannot buy is currently
   not earning, and how much of it there is.
"""
import invariants as inv

__all__ = ["split_branches", "productivity", "WINDOW_HOURS_DEFAULT"]

WINDOW_HOURS_DEFAULT = 48.0


def _f(v, default=0.0):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return default if out != out else out          # NaN is not a number


def split_branches(branches):
    """(full, has_room, unreadable) by the SAME rule no_dead_capital uses.

    A branch whose open_slices or num_levels cannot be read goes in
    neither bucket and is reported separately. It is not quietly counted
    as having room, which would understate the parked figure.
    """
    full, room, unreadable = [], [], []
    for b in branches:
        n, lv = b.get("open_slices"), b.get("num_levels")
        if n is None or lv is None:
            unreadable.append(b)
        elif n >= lv:
            full.append(b)
        else:
            room.append(b)
    return full, room, unreadable


def _bucket(branches, earned_by_product, trades_by_product, label):
    capital = sum(_f(b.get("allocated_usd")) for b in branches)
    unreal = sum(_f(b.get("total_unrealized_net_usd")) for b in branches)
    earned = sum(_f(earned_by_product.get(b.get("product_id"))) for b in branches)
    trades = sum(int(trades_by_product.get(b.get("product_id")) or 0) for b in branches)
    out = {
        "label": label,
        "branches": len(branches),
        "products": [b.get("product_id") for b in branches],
        "capital_usd": round(capital, 2),
        "earned_usd": round(earned, 2),
        "trades": trades,
        "unrealized_usd": round(unreal, 2),
    }
    # Per $100 of capital, over the window. Never per day - see the
    # module docstring. None, not 0, when there is no capital to divide
    # by: a bucket holding nothing has no productivity, and 0 would read
    # as "it earned nothing", which is a different claim.
    out["earned_per_100_usd"] = round(earned / capital * 100, 4) if capital > 0 else None
    return out


def productivity(branches, closed_trades, window_hours=WINDOW_HOURS_DEFAULT):
    """branches: the /grid-status branch payload, unchanged.
    closed_trades: rows with product_id and pnl, ALREADY filtered to the
    window by the caller (the caller owns the clock; this stays pure).
    """
    branches = list(branches or ())
    if not branches:
        return {"status": inv.UNKNOWN, "detail": "no branch data",
                "window_hours": window_hours}

    earned, counts = {}, {}
    unattributed_usd, unattributed_n = 0.0, 0
    known = {b.get("product_id") for b in branches}
    for t in (closed_trades or ()):
        pid = t.get("product_id") if hasattr(t, "get") else getattr(t, "product_id", None)
        pnl = _f(t.get("pnl") if hasattr(t, "get") else getattr(t, "pnl", None))
        if pid in known:
            earned[pid] = earned.get(pid, 0.0) + pnl
            counts[pid] = counts.get(pid, 0) + 1
        else:
            # A trade on a branch that has since closed. Real money, and
            # it belongs to neither bucket - reported, never dropped and
            # never folded into a bucket it did not come from.
            unattributed_usd += pnl
            unattributed_n += 1

    full, room, unreadable = split_branches(branches)
    b_full = _bucket(full, earned, counts, "cannot buy - full on rungs")
    b_room = _bucket(room, earned, counts, "can still buy")

    out = {
        "window_hours": window_hours,
        "can_buy": b_room,
        "cannot_buy": b_full,
        "unreadable_branches": [b.get("product_id") for b in unreadable],
        "unattributed": {"earned_usd": round(unattributed_usd, 2),
                         "trades": unattributed_n,
                         "note": "closed round trips on branches no longer open"},
        "denominator_is_a_snapshot": True,
        "not_a_daily_rate": ("earned_per_100_usd is dollars per $100 of capital OVER "
                             "THIS WINDOW. The denominator is allocated capital at one "
                             "instant and moves on price, so this must not be converted "
                             "to a per-day figure - see /edge-rate, which has a span "
                             "floor for exactly that reason."),
        "causation_caveat": ("Branches are bucketed by their state NOW, and selling is "
                             "what moves a branch out of the full bucket, so a branch "
                             "can be in the can-buy half BECAUSE it earned. This shows "
                             "that capital which cannot buy is not earning; it is not "
                             "evidence that freeing it would earn."),
    }

    rp = b_room["earned_per_100_usd"]
    fp = b_full["earned_per_100_usd"]
    if rp is None or fp is None:
        out["ratio"] = None
        out["status"] = inv.UNKNOWN
        out["detail"] = ("one half of the book holds no capital, so the two cannot be "
                         "compared. " + _plain(b_room, b_full, window_hours))
        return out

    if fp == 0:
        out["ratio"] = None
        out["status"] = inv.FAIL if b_full["capital_usd"] > 0 else inv.OK
        out["detail"] = (f"${b_full['capital_usd']:,.2f} across {b_full['branches']} "
                         f"branch(es) that cannot buy earned NOTHING in {window_hours:g}h, "
                         f"so there is no ratio to state. "
                         + _plain(b_room, b_full, window_hours))
        return out

    out["ratio"] = round(rp / fp, 1)
    out["status"] = inv.OK
    out["detail"] = (f"Capital that can still buy earned {out['ratio']}x more per dollar. "
                     + _plain(b_room, b_full, window_hours))
    return out


def _plain(room, full, hours):
    return (f"Over {hours:g}h: {room['branches']} branch(es) with room earned "
            f"${room['earned_usd']:,.2f} on ${room['capital_usd']:,.2f} "
            f"({room['earned_per_100_usd']} per $100) carrying "
            f"${room['unrealized_usd']:+,.2f} unrealized; {full['branches']} full "
            f"branch(es) earned ${full['earned_usd']:,.2f} on "
            f"${full['capital_usd']:,.2f} ({full['earned_per_100_usd']} per $100) "
            f"carrying ${full['unrealized_usd']:+,.2f}.")
