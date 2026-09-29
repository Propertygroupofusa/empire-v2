"""Join Coinbase fills to the subsystem that placed the order.

WHY THIS EXISTS. `OrderAttribution` has been written since 2026-09-28 and
read by NOTHING - no endpoint, no report, no join. That is the same defect
this fleet has been fixing all week one layer down: a fact recorded
faithfully and surfaced to no one. The table was built precisely so that a
taker commission could be traced to the subsystem that caused it, and
until this module nothing performed that trace.

WHAT IT CAN AND CANNOT ANSWER. Coinbase's fills feed carries `order_id`
and NOT `client_order_id`, which is why attribution is keyed on the id
Coinbase mints at placement. So the join is exact where a row exists. Where
none exists the answer is UNKNOWN, and this module refuses to guess:

  * Attribution is written only by `_place_and_confirm`, the engine's
    MARKET-order path. A post-only maker order never passes through it, so
    a maker-only order having no row is EXPECTED and says nothing.
  * Attribution only started being written at some point. An order that
    filled before the first row was ever written could not have been
    tagged, however it was placed.

Only the third case is a finding: an order with at least one TAKER fill,
placed after attribution was already being written, carrying no row. That
is a caller nobody can name, paying real commission.

THE CUTOVER IS DERIVED, NEVER HARDCODED. The oldest `placed_at` in the
attribution table is when tagging demonstrably began. A literal standing in
for a real value is this codebase's signature bug, and a wrong constant here
would silently reclassify every order on one side of it. When the table is
empty the cutover is UNKNOWN and NOTHING is called untagged - absence of
attribution cannot be evidence against a caller when nothing was ever
attributed.
"""

UNATTRIBUTED = "unattributed"


def _order_rollup(fills):
    """Collapse fills into ORDERS. One order can fill in many pieces.

    Counting fills instead of orders is how "three quarters of executions
    went unrecorded" was once claimed - partial filling invents activity
    that never happened. An order is what a bot places and what a ledger
    row represents.
    """
    orders = {}
    skipped_no_order_id = 0
    for f in fills or []:
        oid = f.get("order_id")
        if not oid:
            # No join key, so this fill can never be attributed either way.
            # Counted rather than dropped silently.
            skipped_no_order_id += 1
            continue
        try:
            size = float(f.get("size") or 0)
            price = float(f.get("price") or 0)
            comm = float(f.get("commission") or 0)
        except (TypeError, ValueError):
            skipped_no_order_id += 1
            continue

        # SIZE IS NOT ALWAYS THE BASE QUANTITY. Coinbase sets size_in_quote
        # when `size` is denominated in USD, which is what a market order
        # placed by dollar amount returns. Multiplying that by price counts
        # the dollars once and then again at the coin's own price - the bug
        # that reported $96,544,199.94 of BTC on a $1,000 account. Same
        # handling as summarise_fills, deliberately, so the two agree.
        value = size if f.get("size_in_quote") else size * price

        liq = (f.get("liquidity_indicator") or "").upper()
        o = orders.setdefault(oid, {
            "order_id": oid,
            "product_id": f.get("product_id") or "?",
            "side": (f.get("side") or "").upper(),
            "fills": 0, "notional_usd": 0.0, "commission_usd": 0.0,
            "taker_fills": 0, "maker_fills": 0, "unknown_liquidity_fills": 0,
            "first_trade_time": None, "last_trade_time": None,
        })
        o["fills"] += 1
        o["notional_usd"] += value
        o["commission_usd"] += comm
        if liq == "TAKER":
            o["taker_fills"] += 1
        elif liq == "MAKER":
            o["maker_fills"] += 1
        else:
            # Neither MAKER nor TAKER. Its own bucket: an order whose
            # liquidity is unreadable must not be assumed to be the
            # harmless one.
            o["unknown_liquidity_fills"] += 1
        t = f.get("trade_time")
        if t:
            if o["first_trade_time"] is None or t < o["first_trade_time"]:
                o["first_trade_time"] = t
            if o["last_trade_time"] is None or t > o["last_trade_time"]:
                o["last_trade_time"] = t
    return orders, skipped_no_order_id


def classify(fills, attribution, attribution_started_at=None, truncated=False):
    """Bucket a window's fills by the subsystem that placed each order.

    `attribution` maps order_id -> source string. `attribution_started_at`
    is the oldest placed_at in the attribution table, as an ISO-8601 string
    in the same shape as a fill's trade_time, or None when the table is
    empty or unreadable - in which case NOTHING is reported as untagged.
    """
    orders, skipped = _order_rollup(fills)
    attribution = attribution or {}

    by_source = {}
    untagged_taker_orders = []
    for o in orders.values():
        src = attribution.get(o["order_id"])
        key = src if src else UNATTRIBUTED
        b = by_source.setdefault(key, {
            "source": key, "orders": 0, "fills": 0,
            "notional_usd": 0.0, "commission_usd": 0.0,
            "taker_fills": 0, "maker_fills": 0, "unknown_liquidity_fills": 0,
            "products": set(),
        })
        b["orders"] += 1
        b["fills"] += o["fills"]
        b["notional_usd"] += o["notional_usd"]
        b["commission_usd"] += o["commission_usd"]
        b["taker_fills"] += o["taker_fills"]
        b["maker_fills"] += o["maker_fills"]
        b["unknown_liquidity_fills"] += o["unknown_liquidity_fills"]
        b["products"].add(o["product_id"])

        if src:
            continue
        # THE ONLY CASE THAT IS A FINDING. A maker-only order is not
        # expected to carry a row at all, and an order that predates the
        # first attribution row could not have carried one. Both are
        # EXPECTED absences, not untagged callers.
        if o["taker_fills"] <= 0:
            continue
        if attribution_started_at is None:
            continue
        stamp = o["first_trade_time"]
        if not stamp or stamp <= attribution_started_at:
            continue
        untagged_taker_orders.append(o)

    rows = []
    for b in by_source.values():
        b["products"] = sorted(b["products"])
        b["product_count"] = len(b["products"])
        for k in ("notional_usd", "commission_usd"):
            b[k] = round(b[k], 4)
        rows.append(b)
    # Commission descending: the question this answers is "who is paying
    # the fees", so the costliest caller is first.
    rows.sort(key=lambda r: -r["commission_usd"])

    untagged_taker_orders.sort(key=lambda o: -o["commission_usd"])
    for o in untagged_taker_orders:
        for k in ("notional_usd", "commission_usd"):
            o[k] = round(o[k], 4)

    attributed_orders = sum(r["orders"] for r in rows if r["source"] != UNATTRIBUTED)
    unattributed = next((r for r in rows if r["source"] == UNATTRIBUTED), None)

    return {
        "orders": len(orders),
        "attributed_orders": attributed_orders,
        "unattributed_orders": unattributed["orders"] if unattributed else 0,
        # Not a percentage of "all trading": a percentage of the orders in
        # this window that could be joined at all.
        "attributed_pct_of_orders": (round(attributed_orders / len(orders) * 100, 2)
                                     if orders else None),
        "fills_without_an_order_id": skipped,
        "by_source": rows,
        "attribution_started_at": attribution_started_at,
        "untagged_taker_orders": untagged_taker_orders,
        "untagged_taker_order_count": len(untagged_taker_orders),
        # TRUNCATION CHANGES WHAT EVERY NUMBER MEANS. The fills fetcher caps
        # its cursor loop, and a capped window is a partial statement - so
        # each figure here becomes a floor, not a total.
        "truncated": bool(truncated),
        "counts_are_floors": bool(truncated),
        "what_unattributed_means": (
            "UNKNOWN, not a bug and not a source. Attribution is written only "
            "by the engine's market-order path, so a post-only maker order "
            "never carries a row; and no order can carry one from before "
            "tagging began. Only untagged_taker_orders - a TAKER fill, after "
            "the first attribution row, with no row - names a caller nobody "
            "can identify."),
        "what_this_does_not_say": (
            "Nothing about whether a trade was GOOD. This says who placed it "
            "and what it cost in commission, not whether it should have been "
            "placed."),
    }
