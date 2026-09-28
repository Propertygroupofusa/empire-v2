#!/usr/bin/env python3
"""Bringing a branch's tracked units down to what the wallet actually holds.

WHY A BRANCH CAN CLAIM COIN THAT IS NOT THERE

Another subsystem sold it. Measured on this account 2026-09-27/28:

    the concentration trimmer   $885.43 of ZEC, $244.78 of XRP
    a resting stop-loss         0.347873 ETH

None of those go through the grid, so none of them touched a slice row.
The branch went on claiming units the wallet no longer had, and the next
sale of those slices would have been an order for coin that does not
exist. invariants.coin_tracked_is_held found it; this is what fixes it.

WHAT THIS IS NOT

It is NOT a loss. The coin was sold and the proceeds are already sitting
in the wallet as cash - that is where the free cash came from. What is
being corrected is bookkeeping that never learned about a sale.

It is also NOT a P&L entry, and it deliberately does not invent one. The
trimmer's log records the USD it sold and when, not the units or the
fill price, so the realised figure per slice is not recoverable from it.
Writing a number there would be a guess in the one place a guess is
indistinguishable from a measurement afterwards. The preview says what
the cost basis was and leaves the P&L unstated.

WHICH SLICES GO

Oldest first, matching the grid's own FIFO sell order. A trim sells from
the venue's pool rather than from any particular slice, so there is no
true answer - but FIFO is the convention this codebase already trades on,
and picking the cheapest slices to delete would flatter the remaining
book by choosing which cost basis survives.

A slice is reduced rather than removed when only part of it is covered,
so a single partial fill does not delete a whole rung.
"""
from __future__ import annotations

#: Below this many units a remainder is not worth keeping as a slice - it
#: cannot be sold at the venue minimum and would sit forever. Expressed
#: as a fraction of the original slice, not an absolute, because the
#: fleet holds everything from 0.017 BTC to 5,862,000 JASMY.
DUST_FRACTION = 0.01

__all__ = ["DUST_FRACTION", "plan"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def plan(slices, held_units, price=None, dust_fraction=DUST_FRACTION):
    """What to remove or shrink so tracked units equal held units.

    `slices` is the branch's open slices, each with id, qty, entry_price
    and opened_at. Returns (actions, report). Changes nothing.
    """
    held = _num(held_units)
    if held is None or held < 0:
        return [], {"status": "UNKNOWN",
                    "detail": "the wallet holding is unreadable - a gap is not a zero, "
                              "and nothing is written off on one"}

    rows = []
    for s in (slices or ()):
        get = s.get if hasattr(s, "get") else (lambda k, d=None: getattr(s, k, d))
        qty = _num(get("qty"))
        if qty is None or qty <= 0:
            continue
        rows.append({"id": get("id"), "qty": qty,
                     "entry_price": _num(get("entry_price")),
                     "opened_at": get("opened_at")})
    if not rows:
        return [], {"status": "NOTHING", "detail": "the branch holds no priced slices"}

    tracked = sum(r["qty"] for r in rows)
    surplus = tracked - held
    if surplus <= 0:
        return [], {"status": "OK",
                    "detail": (f"tracked {tracked:.8f} against {held:.8f} held - nothing "
                               f"is over-claimed, so nothing is written off"),
                    "tracked_units": tracked, "held_units": held}

    # Oldest first. A missing opened_at sorts last rather than first: an
    # unknown age must not make a slice the first thing deleted.
    rows.sort(key=lambda r: (r["opened_at"] is None, r["opened_at"] or ""))

    actions, remaining, basis_off = [], surplus, 0.0
    for r in rows:
        if remaining <= 0:
            break
        entry = r["entry_price"]
        if r["qty"] <= remaining + r["qty"] * dust_fraction:
            # Whole slice, including a remainder too small to keep.
            cut = r["qty"]
            actions.append({"slice_id": r["id"], "action": "REMOVE",
                            "qty_before": r["qty"], "qty_after": 0.0,
                            "units_removed": cut, "entry_price": entry,
                            "cost_basis_removed": round(cut * entry, 2) if entry else None,
                            "opened_at": r["opened_at"]})
        else:
            cut = remaining
            actions.append({"slice_id": r["id"], "action": "REDUCE",
                            "qty_before": r["qty"], "qty_after": round(r["qty"] - cut, 12),
                            "units_removed": cut, "entry_price": entry,
                            "cost_basis_removed": round(cut * entry, 2) if entry else None,
                            "opened_at": r["opened_at"]})
        if entry:
            basis_off += cut * entry
        remaining -= cut

    removed = sum(a["units_removed"] for a in actions)
    unpriced = [a["slice_id"] for a in actions if a["cost_basis_removed"] is None]
    report = {
        "status": "READY",
        "tracked_units": tracked,
        "held_units": held,
        "surplus_units": round(surplus, 12),
        "units_removed": round(removed, 12),
        "cost_basis_removed_usd": round(basis_off, 2),
        "market_value_removed_usd": round(removed * price, 2) if price else None,
        "slices_removed": sum(1 for a in actions if a["action"] == "REMOVE"),
        "slices_reduced": sum(1 for a in actions if a["action"] == "REDUCE"),
        "unpriced_slices": unpriced or None,
        "detail": (
            f"tracked {tracked:.8f} against {held:.8f} held - {surplus:.8f} units are "
            f"claimed and not there. Removing them clears ${basis_off:,.2f} of tracked "
            f"cost basis across {len(actions)} slice(s), oldest first. This is NOT a loss: "
            f"the coin was sold by another subsystem and the proceeds are already in the "
            f"wallet as cash. No P&L is booked - the trim log records USD and a timestamp, "
            f"not the units or the fill price, so a realised figure here would be a guess."),
    }
    if unpriced:
        report["detail"] += (f" {len(unpriced)} slice(s) carry no entry price, so the cost "
                             f"basis above excludes them and the real figure is larger.")
    return actions, report
