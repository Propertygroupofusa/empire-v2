"""Give the grid back the coin a resting stop is holding.

WHY THIS EXISTS

A resting stop-limit at the venue reserves the units it covers. At
2026-09-28T09:44Z $923.23 was reserved across six live grid branches:

    XLM  $419.30   ALGO $141.64   SOL  $91.50
    LINK  $90.11   NEAR  $81.29   ACH  $70.14

ALGO had 0.046 units free out of 1134.35 - the whole position was
spoken for, so that branch could not sell anything at all.

The placer no longer puts new stops on grid coin (b050507), and
invariants.grid_inventory_is_free now names the condition (8420c0b).
Neither undoes what is already resting. This does, and only this way:

  * CANCEL ONLY. There is no place verb in this file. The worst
    outcome available here is that a position ends up unprotected -
    never that something is sold.
  * Only orders this system placed, identified by its own
    client_order_id prefix. A stop the owner set by hand at the venue
    is not ours to touch.
  * Only coins a grid branch actually tracks. JASMY is reserved too
    and is not a branch; cancelling it would be tidying somebody
    else's account.
  * FAILS CLOSED. An unreadable order book or an unreadable grid
    cancels nothing. Every other protection in this codebase fails
    open, because failing open leaves behaviour as it was; this one
    moves live orders, so not knowing means not acting.

WHAT IT COSTS

Each cancelled stop is downside protection given up. The branch keeps
its own adaptive per-slice stop, which sells ONE slice through the
grid's own path and leaves the books consistent - but that stop only
runs while the service is running, and a venue-side order does not
need us to be awake. That is the trade, and it is the owner's to make.
"""
from __future__ import annotations

# Set by the caller from resting_stops_worker.COID_PREFIX. Restating it
# here would be a second copy of the thing that decides whose orders we
# are allowed to cancel.
CANCEL = "CANCEL"
SKIP = "SKIP"


def _asset(product_or_asset):
    return str(product_or_asset or "").split("-")[0].upper()


def plan(open_stops, tracked_units_by_product, holdings=None):
    """What to cancel, and for everything else, why not.

    `open_stops` maps ASSET -> {order_id, stop_price, base_size}, already
    filtered to this system's own orders by the caller. None means the
    order book could not be read.

    `tracked_units_by_product` maps PRODUCT -> units the grid claims.
    None means the grid could not be read.

    Returns {"ok": bool, "reason": str|None, "actions": [...]}. ok=False
    means nothing should be cancelled at all.
    """
    if open_stops is None:
        return {"ok": False, "actions": [],
                "reason": "the venue's open orders could not be read, so it is not "
                          "known what is resting. Cancelling on a guess could take "
                          "an order that is not ours."}
    if tracked_units_by_product is None:
        return {"ok": False, "actions": [],
                "reason": "the grid's tracked positions could not be read, so it is "
                          "not known which coin is working inventory. Nothing is "
                          "cancelled without that."}

    tracked = {_asset(p): v for p, v in dict(tracked_units_by_product).items()}
    held = {}
    for row in (holdings or ()):
        a = _asset((row or {}).get("asset"))
        if a:
            held[a] = row

    actions = []
    for asset in sorted(open_stops):
        info = open_stops[asset] or {}
        order_id = info.get("order_id")
        row = {"asset": asset, "order_id": order_id,
               "stop_price": info.get("stop_price"),
               "base_size": info.get("base_size")}

        if not order_id:
            row.update(action=SKIP, reason="NO_ORDER_ID",
                       detail="the open-orders reading carried no order id for this "
                              "asset, and an order cannot be cancelled by name")
            actions.append(row); continue

        if asset not in tracked:
            row.update(action=SKIP, reason="NOT_GRID_INVENTORY",
                       detail=f"no grid branch tracks {asset}, so this stop is not "
                              f"holding working inventory. Leaving it alone.")
            actions.append(row); continue

        h = held.get(asset) or {}
        units, avail = h.get("units"), h.get("available_units")
        frees = None
        try:
            if units is not None and avail is not None:
                frees = round(float(units) - float(avail), 8)
        except (TypeError, ValueError):
            frees = None
        row["frees_units"] = frees
        price = h.get("price")
        try:
            row["frees_usd"] = round(frees * float(price), 2) if (frees and price) else None
        except (TypeError, ValueError):
            row["frees_usd"] = None

        row.update(action=CANCEL, reason="LOCKS_GRID_INVENTORY",
                   detail=f"a grid branch trades {asset} and this resting sell is "
                          f"holding its units. Cancelling places no order and frees "
                          f"them; the branch keeps its own per-slice stop.")
        actions.append(row)

    return {"ok": True, "reason": None, "actions": actions}


def summarise(result, dry_run=True):
    """One line, and the numbers behind it."""
    actions = result.get("actions") or []
    doing = [a for a in actions if a.get("action") == CANCEL]
    freed = [a["frees_usd"] for a in doing if a.get("frees_usd") is not None]
    total = round(sum(freed), 2) if freed else None
    unpriced = len(doing) - len(freed)

    if not result.get("ok"):
        headline = f"Nothing can be cancelled: {result.get('reason')}"
    elif not doing:
        headline = "No resting stop is holding grid inventory."
    else:
        # A partial total is never printed as if it were the whole figure.
        amount = (f"${total:,.2f}" if total is not None else "an unknown amount")
        if unpriced:
            amount += f" (plus {unpriced} position(s) with no readable price)"
        verb = "would be freed" if dry_run else "freed"
        headline = (f"{len(doing)} resting stop(s) hold grid inventory; "
                    f"{amount} {verb}. No order is placed either way.")

    return {"ok": result.get("ok", False),
            "dry_run": bool(dry_run),
            "cancel_count": len(doing),
            "frees_usd": total,
            "positions_without_price": unpriced,
            "headline": headline,
            "actions": actions,
            "places_no_orders": True}
