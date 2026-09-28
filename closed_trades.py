"""Pair real buy and sell orders into round trips.

THE BUG THIS REPLACES. GET /trades/closed showed 39 rows and every one
of them was wrong. Live on 28 Sep:

    USO   entry 2026-09-28T16:40:14Z   exit 2026-09-21T13:30:32Z

An exit SEVEN DAYS BEFORE its own entry, on every row. Two defects,
stacked:

  1. WRONG DIRECTION. The orders were fetched with direction=desc -
     NEWEST FIRST - and then walked by an algorithm that assumes a buy
     is seen before the sell that closes it. Walking newest-first, a
     sell is reached before its own buy, so it got paired with whatever
     buy came AFTER it in time. Every pairing was backwards.

  2. ONE LOT PER SYMBOL. Open buys were held in a dict keyed by symbol,
     so each new buy OVERWROTE the last. The six META buys of 28 Sep
     collapsed to one; five real entries vanished and the P&L was
     computed against a single lot as though the other $612 had never
     been bought.

Both are fixed here by doing the only thing that can be right: sort
ASCENDING by fill time and keep a FIFO queue of open lots per symbol,
so a sell consumes the oldest open lots in order and can span several.

WHAT THIS REFUSES TO INVENT. A sell with no open lot in the window is
UNMATCHED, not a trade with a guessed entry - the position was opened
before the window starts, and a fabricated entry price would produce a
fabricated P&L on a page the owner reads. A buy still open at the end
is an OPEN position, not a closed trade with a guessed exit.
"""
from collections import deque


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _usable(order):
    """A filled order this can pair, or None with the reason it cannot."""
    if not isinstance(order, dict):
        return None
    if not order.get("filled_at"):
        return None
    side = str(order.get("side") or "").lower()
    if side not in ("buy", "sell"):
        return None
    qty = _num(order.get("filled_qty"))
    price = _num(order.get("filled_avg_price"))
    if qty is None or price is None or qty <= 0 or price <= 0:
        # A zero price is not a free trade and a zero qty is not a fill.
        return None
    return {
        "symbol": order.get("symbol") or "?",
        "side": side,
        "qty": qty,
        "price": price,
        "filled_at": order.get("filled_at"),
        "client_order_id": order.get("client_order_id"),
        "id": order.get("id"),
    }


def _source(client_order_id, known):
    if not client_order_id:
        return None
    head = str(client_order_id).split("-", 1)[0]
    return head if head in known else None


def pair_round_trips(orders, known_sources=()):
    """Pair filled orders into round trips, oldest lot first.

    Returns {"trades": [...], "open_lots": [...], "unmatched_sells": [...],
             "totals": {...}}.

    Every quantity is carried at full precision. Rounding a fractional
    0.163475 share to 2dp - which the old endpoint did - displays 0.16
    and makes the arithmetic on the page not add up.
    """
    known = set(known_sources)
    rows = [u for u in (_usable(o) for o in (orders or [])) if u]
    # ASCENDING. This one line is the first of the two bugs.
    rows.sort(key=lambda r: (str(r["filled_at"]), str(r["id"] or "")))

    open_lots = {}
    trades, unmatched = [], []

    for r in rows:
        sym = r["symbol"]
        if r["side"] == "buy":
            open_lots.setdefault(sym, deque()).append(dict(r))
            continue

        remaining = r["qty"]
        queue = open_lots.get(sym) or deque()
        matched_any = False
        while remaining > 1e-12 and queue:
            lot = queue[0]
            take = min(lot["qty"], remaining)
            pnl = (r["price"] - lot["price"]) * take
            trades.append({
                "symbol": sym,
                "qty": take,
                "entry_price": lot["price"],
                "exit_price": r["price"],
                "entry_at": lot["filled_at"],
                "exit_at": r["filled_at"],
                "pnl": round(pnl, 4),
                "pnl_pct": round((r["price"] - lot["price"]) / lot["price"] * 100.0, 4),
                "entry_source": _source(lot.get("client_order_id"), known),
                "exit_source": _source(r.get("client_order_id"), known),
                "entry_order_id": lot.get("id"),
                "exit_order_id": r.get("id"),
                "status": "closed",
            })
            matched_any = True
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 1e-12:
                queue.popleft()

        if remaining > 1e-12:
            # A GAP, NOT A TRADE. The opening buy is older than this
            # window. Inventing an entry price here would put a
            # fabricated P&L on a page the owner reads.
            unmatched.append({
                "symbol": sym,
                "qty": remaining,
                "exit_price": r["price"],
                "exit_at": r["filled_at"],
                "exit_source": _source(r.get("client_order_id"), known),
                "why": ("no open lot for it in this window - its buy is older "
                        "than the orders fetched, so no entry price is known"),
                "partially_matched": matched_any,
            })

    still_open = []
    for sym, queue in open_lots.items():
        for lot in queue:
            if lot["qty"] > 1e-12:
                still_open.append({
                    "symbol": sym, "qty": lot["qty"], "entry_price": lot["price"],
                    "entry_at": lot["filled_at"],
                    "entry_source": _source(lot.get("client_order_id"), known),
                })

    trades.sort(key=lambda t: str(t["exit_at"]), reverse=True)
    realised = sum(t["pnl"] for t in trades)
    return {
        "trades": trades,
        "open_lots": still_open,
        "unmatched_sells": unmatched,
        "totals": {
            "round_trips": len(trades),
            "realised_pnl": round(realised, 2),
            "winners": len([t for t in trades if t["pnl"] > 0]),
            "losers": len([t for t in trades if t["pnl"] < 0]),
            "open_lots": len(still_open),
            "unmatched_sells": len(unmatched),
        },
        "caveats": {
            "unmatched_sells": (
                "A sell with no open lot in this window is listed separately "
                "and contributes NOTHING to realised P&L. Its entry is older "
                "than the fetched orders; a guessed entry would be a "
                "fabricated number on a page that is read for decisions."),
            "lot_matching": "FIFO - a sell consumes the oldest open lots first.",
        },
    }
