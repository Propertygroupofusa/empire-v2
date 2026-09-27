"""Which branches are actually idle, and which only look it.

FLAT IS NOT IDLE, AND THE DIFFERENCE IS THE WHOLE POINT

A grid branch holding no slices looks stopped. Measured on the live fleet,
three branches were flat at the same moment:

    ONDO   $69.58   last trade   4 hours ago
    TIA    $69.67   last trade   5 hours ago
    BONK   $69.23   last trade  18 DAYS ago

The first two are working grids between fills - a rung sold, and the next
buy is waiting for its dip. Rotating them would pay fees to move money that
is doing its job. The third has not traded in eighteen days: that is not a
wait, it is capital in the wrong coin.

crypto_grid_bot's auto-rotation keys on branch.created_at and flatness, so
it cannot tell those three apart - it would treat the working pair exactly
like the dead one. This module measures the thing that actually separates
them: TIME SINCE THE LAST CLOSED TRADE.

WHY 72 HOURS

Not a round number picked for feel. horizon_study measures how long a move
takes to clear a round trip on this account's own coins: 8.1% inside 30
minutes, 34.1% inside 6 hours, 92.5% inside 72 hours. So a branch that has
not completed a round trip in 72 hours is not experiencing an ordinary
wait - it is in the 7.5% tail, which is the honest place to draw the line
between "waiting" and "stuck".

WHAT IT DOES NOT DO

It does not rotate anything, and it cannot. Rotation is GRID_AUTO_ROTATE,
which the account owner has deliberately switched off; this reports what a
rotation WOULD be for, so that decision is made on measured idleness rather
than on a branch looking empty at a glance.
"""
from __future__ import annotations

import os as _os
from datetime import datetime, timezone

# Tied to horizon_study's own measurement - see the module docstring.
STALE_AFTER_HOURS = float(_os.getenv("GRID_IDLE_STALE_HOURS", "72"))
# Below this a branch is too new to judge: it has not been given a chance.
GRACE_HOURS = float(_os.getenv("GRID_IDLE_GRACE_HOURS", "6"))

WORKING = "WORKING"          # holds slices - its money is in the market
WAITING = "WAITING"          # flat, but traded inside the horizon window
STALE = "STALE"              # flat and past the window: genuinely idle
TOO_NEW = "TOO_NEW"          # not yet had time to trade
UNKNOWN_AGE = "UNKNOWN_AGE"  # the trade window did not reach back far enough


def _parse(ts):
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def last_trade_by_branch(trades):
    """Newest closed_at per bot_name, from whatever window was supplied."""
    out = {}
    for t in (trades or ()):
        if not hasattr(t, "get"):
            continue
        bot, ca = t.get("bot_name"), _parse(t.get("closed_at"))
        if bot and ca and (bot not in out or ca > out[bot]):
            out[bot] = ca
    return out


def classify(branch, last_trade_at, *, now, window_starts_at=None,
             window_truncated=False, stale_after_hours=None, grace_hours=None):
    """One branch's state, and the hours behind it.

    `window_truncated` and `window_starts_at` matter more than they look. The
    trade feed is capped, so a branch missing from it has not necessarily
    never traded - it may simply have traded before the window begins. Calling
    that "never traded" would invent a dead branch out of a short list, the
    same way an unread census once drew a crash out of a gap. When the window
    is truncated and the branch is absent, the age is reported as a LOWER
    BOUND and the state is UNKNOWN_AGE rather than STALE.
    """
    stale_after = STALE_AFTER_HOURS if stale_after_hours is None else stale_after_hours
    grace = GRACE_HOURS if grace_hours is None else grace_hours

    slices = branch.get("open_slices")
    if slices is None:
        slices = 0
    if slices > 0:
        return {"state": WORKING, "idle_hours": 0.0, "at_least": False,
                "why": f"holding {slices} open slice(s) - this money is in the market"}

    created = _parse(branch.get("created_at"))
    if last_trade_at is not None:
        hours = max(0.0, (now - last_trade_at).total_seconds() / 3600.0)
        if hours >= stale_after:
            return {"state": STALE, "idle_hours": round(hours, 1), "at_least": False,
                    "why": (f"flat and no completed round trip for {hours / 24:.1f} days - "
                            f"past the {stale_after:.0f}h window in which 92.5% of moves "
                            f"on this account complete")}
        return {"state": WAITING, "idle_hours": round(hours, 1), "at_least": False,
                "why": (f"flat, but it closed a trade {hours:.1f}h ago - a grid between "
                        f"fills, not stopped")}

    # Never seen in the window.
    if created is not None and (now - created).total_seconds() / 3600.0 < grace:
        age = (now - created).total_seconds() / 3600.0
        return {"state": TOO_NEW, "idle_hours": round(age, 1), "at_least": False,
                "why": f"created {age:.1f}h ago - inside the {grace:.0f}h grace period"}

    if window_truncated:
        floor_h = None
        ws = _parse(window_starts_at)
        if ws is not None:
            floor_h = max(0.0, (now - ws).total_seconds() / 3600.0)
        return {"state": UNKNOWN_AGE,
                "idle_hours": round(floor_h, 1) if floor_h is not None else None,
                "at_least": True,
                "why": ("absent from a truncated trade window, so its last trade is older "
                        "than the window reaches - a floor, not a measurement")}

    hours = None
    if created is not None:
        hours = (now - created).total_seconds() / 3600.0
    if hours is not None and hours >= stale_after:
        return {"state": STALE, "idle_hours": round(hours, 1), "at_least": False,
                "why": (f"flat and has never closed a trade in {hours / 24:.1f} days "
                        f"since it was created")}
    return {"state": UNKNOWN_AGE, "idle_hours": round(hours, 1) if hours is not None else None,
            "at_least": False,
            "why": "no closed trade on record and no creation time to measure against"}


def report(branches, trades, *, now=None, total_trade_count=None,
           stale_after_hours=None, grace_hours=None):
    """Every branch classified, with the money behind each state."""
    now = now or datetime.now(timezone.utc)
    trades = list(trades or ())
    last = last_trade_by_branch(trades)

    window_truncated = bool(total_trade_count and len(trades) < total_trade_count)
    starts = min((_parse(t.get("closed_at")) for t in trades
                  if hasattr(t, "get") and _parse(t.get("closed_at"))), default=None)

    rows, buckets = [], {}
    for b in (branches or ()):
        if not hasattr(b, "get"):
            continue
        c = classify(b, last.get(b.get("bot_name")), now=now,
                     window_starts_at=starts, window_truncated=window_truncated,
                     stale_after_hours=stale_after_hours, grace_hours=grace_hours)
        usd = float(b.get("allocated_usd") or 0.0)
        row = {"bot_name": b.get("bot_name"), "product_id": b.get("product_id"),
               "allocated_usd": round(usd, 2), **c}
        rows.append(row)
        s = buckets.setdefault(c["state"], {"count": 0, "usd": 0.0})
        s["count"] += 1
        s["usd"] = round(s["usd"] + usd, 2)

    rows.sort(key=lambda r: (r["state"] != STALE, -(r["idle_hours"] or 0)))
    stale = [r for r in rows if r["state"] == STALE]
    return {
        "as_of": now.isoformat(),
        "branches": rows,
        "by_state": buckets,
        "stale": stale,
        "stale_usd": round(sum(r["allocated_usd"] for r in stale), 2),
        "window_truncated": window_truncated,
        "window_starts_at": starts.isoformat() if starts else None,
        "stale_after_hours": STALE_AFTER_HOURS if stale_after_hours is None else stale_after_hours,
        "rotates_nothing": True,
        "detail": (
            (f"{len(stale)} branch(es) holding ${sum(r['allocated_usd'] for r in stale):,.2f} "
             f"have not completed a round trip in "
             f"{(STALE_AFTER_HOURS if stale_after_hours is None else stale_after_hours) / 24:.0f}+ days. "
             f"Every other flat branch traded inside the window and is between fills, not stopped.")
            if stale else
            "No branch is past the idle window. Every flat branch has traded recently enough "
            "to be a grid waiting for its dip rather than capital in the wrong coin."),
    }
