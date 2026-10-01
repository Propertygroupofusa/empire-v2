"""Rotate an over-concentrated branch out, but only at a real profit.

THE PROBLEM THIS SOLVES

ZEC and XRP hold $4,513 - 53% of the fleet - against an owner rule that no
coin may exceed 20%. Between them they have returned $0.55. The capital is
not working, and at the median edge of the proven coins the same money
would earn about $10.29/day instead of $1.86.

The obvious move is to sell them and redeploy. Measured 2026-10-01, that
books $383.31 of real, permanent loss: every one of ZEC's 7 slices and
XRP's 9 slices is underwater, and not one of them clears the round trip.

THE RULE THIS MODULE IS BUILT ON

Sell nothing at a loss. Ever. A slice is a candidate only when the exit
beats the measured cost of the round trip with a margin to spare, so the
rotation costs nothing to switch on and simply waits. Today it would move
zero dollars. The day XRP recovers ~3% it starts freeing capital, and ZEC
at ~12-16%.

That turns "book $383 now for $8.43/day" into "pay nothing, get the same
$8.43/day later", which is a trade the owner can take without a decision.

WHAT IT WILL NOT DO

It does not place orders. It returns a PLAN - which slices clear, what they
free, and where that money should go - and something else executes it.

It does not touch a branch that is inside the concentration limit. The
fleet's ordinary grid already manages those, and a second system selling
the same slices is how a ledger ends up claiming coin that is gone.

It does not invent a destination. A coin needs a measured positive edge
over a real sample and room under the limit, or the freed cash waits.
"""
from __future__ import annotations

# The owner's own rule. Not lowered here, and not read from anywhere that
# could lower it.
CONCENTRATION_LIMIT_PCT = 20.0

# What one round trip costs, measured: 0.70% maker fees both legs plus
# 0.5407% adverse selection taken from FALLING markets. Passed in by the
# caller so it tracks the live measurement rather than going stale here.
DEFAULT_ROUND_TRIP_COST_PCT = 1.2407

# A slice must beat the round trip by this much before it is worth doing.
# Selling at +0.01% net is not a profit, it is churn that pays the venue
# and moves the inventory sideways.
MIN_NET_MARGIN_PCT = 0.25

# A destination needs a real sample behind its edge. Five closed round
# trips is the same floor the coin league uses to rank a coin at all.
MIN_TRADES_FOR_DESTINATION = 5


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else f


def exit_net_pct(entry_price, current_price, cost_pct=DEFAULT_ROUND_TRIP_COST_PCT):
    """What this slice nets, as a percent, after the round trip.

    None when either price is unreadable. A missing price must never
    resolve to 0.0 here - zero would read as "flat", and flat is one
    rounding error away from being treated as sellable.
    """
    e, c, k = _num(entry_price), _num(current_price), _num(cost_pct)
    if e is None or c is None or k is None or e <= 0:
        return None
    return (c / e - 1.0) * 100.0 - k


def sellable_slices(branch, cost_pct=DEFAULT_ROUND_TRIP_COST_PCT,
                    min_margin_pct=MIN_NET_MARGIN_PCT):
    """The slices that could be sold at a genuine profit right now.

    Returns [] when the branch's price cannot be read - never a partial
    list computed against a stale price.
    """
    price = _num((branch or {}).get("current_price"))
    if price is None:
        return []
    out = []
    for i, s in enumerate(branch.get("slices") or []):
        entry, qty = _num(s.get("entry_price")), _num(s.get("qty"))
        if entry is None or qty is None or qty <= 0:
            continue
        net = exit_net_pct(entry, price, cost_pct)
        if net is None or net <= min_margin_pct:
            continue
        stake = entry * qty
        out.append({"index": i, "entry_price": entry, "qty": qty,
                    "stake_usd": round(stake, 2),
                    "net_pct": round(net, 4),
                    "frees_usd": round(stake * (1 + net / 100.0), 2),
                    "profit_usd": round(stake * net / 100.0, 2)})
    return out


def over_limit(branches, limit_pct=CONCENTRATION_LIMIT_PCT):
    """Branches whose allocation exceeds the concentration rule.

    UNREADABLE IS NOT ZERO. If the fleet total cannot be computed there is
    no percentage to compare against, so this returns [] - nothing is
    "over" a limit nobody could measure.
    """
    rows = [b for b in (branches or []) if _num(b.get("allocated_usd")) is not None]
    total = sum(_num(b.get("allocated_usd")) for b in rows)
    if not total or total <= 0:
        return []
    out = []
    for b in rows:
        a = _num(b.get("allocated_usd"))
        pct = a / total * 100.0
        if pct > _num(limit_pct):
            out.append({"branch": b, "product_id": b.get("product_id"),
                        "allocated_usd": round(a, 2), "share_pct": round(pct, 2),
                        "over_by_usd": round(a - total * limit_pct / 100.0, 2)})
    return sorted(out, key=lambda r: -r["share_pct"])


def next_destination(candidates, taken=(), limit_pct=CONCENTRATION_LIMIT_PCT,
                     fleet_total_usd=None,
                     min_trades=MIN_TRADES_FOR_DESTINATION):
    """The best coin to put freed capital into, or None.

    A candidate is {"coin","edge_pct","trades","allocated_usd"}. Rejected
    for a thin sample, a non-positive edge, being already at the limit, or
    being one of the coins this rotation is selling - moving money from an
    over-weight coin into itself is not a rotation.
    """
    best = None
    for c in candidates or ():
        coin = (c.get("coin") or "").upper()
        edge, n = _num(c.get("edge_pct")), c.get("trades")
        if not coin or coin in {t.upper() for t in taken}:
            continue
        if edge is None or edge <= 0:
            continue
        if not isinstance(n, int) or n < min_trades:
            continue
        if fleet_total_usd:
            a = _num(c.get("allocated_usd")) or 0.0
            if a / fleet_total_usd * 100.0 >= _num(limit_pct):
                continue
        if best is None or edge > best["edge_pct"]:
            best = {"coin": coin, "edge_pct": edge, "trades": n}
    return best


def plan(branches, destinations, *, cost_pct=DEFAULT_ROUND_TRIP_COST_PCT,
         limit_pct=CONCENTRATION_LIMIT_PCT,
         min_margin_pct=MIN_NET_MARGIN_PCT):
    """What to sell, what it frees, and where it goes. Places nothing."""
    over = over_limit(branches, limit_pct)
    rows = [b for b in (branches or []) if _num(b.get("allocated_usd")) is not None]
    total = sum(_num(b.get("allocated_usd")) for b in rows) or None

    moves, freed, booked = [], 0.0, 0.0
    for o in over:
        sl = sellable_slices(o["branch"], cost_pct, min_margin_pct)
        if not sl:
            moves.append({"product_id": o["product_id"], "action": "WAIT",
                          "share_pct": o["share_pct"], "sell": [],
                          "why": ("over the limit, but no slice clears the "
                                  f"{cost_pct}% round trip with a "
                                  f"{min_margin_pct}% margin - selling any of "
                                  "them would book a loss, so nothing moves")})
            continue
        f = sum(s["frees_usd"] for s in sl)
        p = sum(s["profit_usd"] for s in sl)
        freed += f; booked += p
        moves.append({"product_id": o["product_id"], "action": "SELL",
                      "share_pct": o["share_pct"], "sell": sl,
                      "frees_usd": round(f, 2), "profit_usd": round(p, 2),
                      "why": (f"{len(sl)} slice(s) clear the round trip; every "
                              "one is sold at a profit, none at a loss")})

    taken = [m["product_id"].split("-")[0] for m in moves]
    dest = next_destination(destinations, taken=taken, limit_pct=limit_pct,
                            fleet_total_usd=total) if freed > 0 else None
    return {
        "is_a_plan_not_an_order": True,
        "over_limit": [{"product_id": o["product_id"], "share_pct": o["share_pct"],
                        "over_by_usd": o["over_by_usd"]} for o in over],
        "moves": moves,
        "frees_usd": round(freed, 2),
        "profit_booked_usd": round(booked, 2),
        "destination": dest,
        "note": ("Nothing is sold at a loss. When no slice clears the round "
                 "trip the plan is to WAIT, which costs nothing and is the "
                 "normal state until price recovers."),
    }
