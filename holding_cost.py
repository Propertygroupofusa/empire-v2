"""What a coin costs to HOLD, set beside what selling it has earned.

THE GAP THIS CLOSES, measured 2026-10-02. grid_learning is a real memory:
29 lessons, 187 trades, $128.95 recorded, reconciling exactly to the live
book. But record_closed_trade() is its ONLY write path, so a lesson exists
only once a coin has completed a round trip - and every verdict it has ever
produced was "earning" or "watch", never one negative word.

That is not luck. The grid sells only ABOVE entry, so closed-trade P&L is
positive by construction. The worst two coins in the whole table were ONDO
at -$1.60 and TON at -$0.38, while the account's unrealized sat at
-$406.23 against +$128.95 realized. The memory recorded the half that
always looks good.

And the clearest case: ZEC-USD held 7 slices, $2,272 allocated, -$376.20
unrealized, 18% below its own peak and past its stop - the single largest
drain in the account - with ZERO lessons, because it has never sold
anything. The learning system had never heard of the position costing the
most. A memory that only learns from completed sales is blind to one that
just sits there and bleeds.

WHAT THIS DOES AND DOES NOT CLAIM

It reports. It does not block, rotate, sell, or set a stop, and it has no
enforcement switch - deliberately. grid_learning's own docstring records
what happened the last time something retired branches on thin evidence:
four EARNING branches switched off and 64% of the account left idle. This
module adds a number to look at, not a trigger.

UNREALIZED IS NOT A LOSS. It is mark-to-market on coin still held, it
moves both ways, and nothing here books it. Every field that carries it
says so in its own name, and the verdict wording never calls it a loss.

A PHANTOM UNREALIZED IS NOT A HOLDING COST EITHER. slice_backing reports
branches claiming coin the wallet does not hold, and flags the unrealized
on them as phantom. An unrealized figure on units that do not exist is not
a cost of holding anything - it is a bookkeeping artifact, and folding it
in here would invent a cost. Pass the backing report and those coins are
reported with holding_cost_usd None and a PHANTOM verdict instead.

UNKNOWN IS A THIRD VERDICT. A branch whose price or entry could not be
read is reported UNKNOWN, never as zero cost. A gap is not a zero.
"""

from datetime import datetime, timezone

# Below this much capital tied up, a coin's holding cost is noise rather
# than a finding - it cannot move an account this size either way.
MATERIAL_CAPITAL_USD = 25.0

# A coin must have been held at least this long before "it is costing you"
# means anything. A position opened this morning has not had time to cost
# anything; calling it a lesson is the mistake grid_learning was built to
# avoid, in a new place.
MIN_DAYS_HELD_FOR_A_VERDICT = 3.0


def _num(v, default=None):
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if out == out else default          # NaN -> default


def _parse_ts(v):
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days_held(slices, now=None):
    """Days since the OLDEST OPEN SLICE ROW. None if no slice carries a date.

    THIS IS NOT HOW LONG THE COIN HAS BEEN OWNED, and the difference
    matters. opened_at is written when the slice row is created, so for
    coin the fleet ADOPTED it dates from the adoption, not the purchase -
    live on 2026-10-02 every adopted branch read 5.1 days, which is
    exactly the 09-27 adoption, while the coin itself had been held far
    longer. A partial sell that rewrites a row resets it too.

    So this measures how long the BRANCH has been carrying the position,
    which is the right denominator for "what has this branch earned per
    day of holding" and the wrong one for "how long have I owned this".
    The payload says so in branch_tracking_not_ownership.
    """
    stamps = [_parse_ts(s.get("opened_at")) for s in (slices or [])]
    stamps = [t for t in stamps if t is not None]
    if not stamps:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - min(stamps)).total_seconds() / 86400.0)


def assess(branches, lessons=None, backing=None, *, now=None):
    """Per-coin: earned by selling, versus the cost of still holding it.

    `branches` are grid-status branch dicts (product_id, slices with qty /
    entry_price / opened_at, current_price, total_unrealized_net_usd).
    `lessons` are grid_learning rows (product_id, trades, total_pnl).
    `backing` is slice_backing.assess()'s output; when given, a branch it
    reports as unbacked has its unrealized treated as PHANTOM rather than
    as a cost.
    """
    by_coin = {}
    for row in (lessons or []):
        pid = row.get("product_id")
        if pid:
            by_coin[str(pid)] = row

    phantom_pids, backing_readable = set(), None
    if isinstance(backing, dict):
        backing_readable = backing.get("readable")
        for u in (backing.get("unbacked") or []):
            # `backed` False means a sell cannot be sized against what the
            # venue will release, so the unrealized on it is not takeable.
            if u.get("product_id") and u.get("backed") is False:
                phantom_pids.add(str(u["product_id"]))

    rows, unknown = [], []
    for b in (branches or []):
        pid = str(b.get("product_id") or "")
        if not pid:
            continue
        slices = b.get("slices") or []
        if not slices:
            continue                                # flat: nothing is being held

        capital = 0.0
        unpriced = 0
        for s in slices:
            q, e = _num(s.get("qty")), _num(s.get("entry_price"))
            if q is None or e is None:
                unpriced += 1
                continue
            capital += q * e

        lesson = by_coin.get(pid)
        earned = _num((lesson or {}).get("total_pnl"), 0.0) if lesson else 0.0
        trades = int(_num((lesson or {}).get("trades"), 0) or 0) if lesson else 0
        unreal = _num(b.get("total_unrealized_net_usd"))
        days = _days_held(slices, now=now)

        base = {
            "product_id": pid,
            "slices": len(slices),
            "capital_tied_usd": round(capital, 2),
            "days_held": round(days, 2) if days is not None else None,
            "earned_by_selling_usd": round(earned, 2),
            "closed_trades": trades,
            "never_sold": lesson is None,
        }

        # A branch with no readable price, or one whose cost basis could
        # not be totalled, has an UNKNOWN holding cost - not a zero one.
        if unreal is None or capital <= 0 or unpriced:
            unknown.append({**base, "verdict": "UNKNOWN",
                            "unpriced_slices": unpriced or None,
                            "why": (f"{pid}: the holding cost could not be computed this "
                                    "pass (no readable unrealized figure, or a slice "
                                    "without a price). That is UNKNOWN, not zero.")})
            continue

        if pid in phantom_pids:
            rows.append({**base,
                         "mark_to_market_usd": round(unreal, 2),
                         "holding_cost_usd": None,
                         "net_usd": None,
                         "verdict": "PHANTOM",
                         "why": (f"{pid} claims coin the wallet does not hold, so its "
                                 f"{unreal:+,.2f} mark-to-market is a bookkeeping artifact, "
                                 "not the cost of holding anything. Reconcile the slices "
                                 "before reading any figure on this branch.")})
            continue

        net = earned + unreal
        # The cost of holding is the mark-to-market only when it is against
        # you. A position in profit has no holding COST; it has a gain, and
        # naming that a negative cost reads as income it has not booked.
        cost = round(-unreal, 2) if unreal < 0 else 0.0
        row = {**base,
               "mark_to_market_usd": round(unreal, 2),
               "holding_cost_usd": cost,
               "net_usd": round(net, 2)}

        if days is not None and days > 0 and capital > 0:
            row["earned_per_1000_per_day"] = round(
                earned / capital * 1000.0 / days, 4)

        thin = (capital < MATERIAL_CAPITAL_USD
                or days is None or days < MIN_DAYS_HELD_FOR_A_VERDICT)
        if net >= 0:
            row["verdict"] = "AHEAD"
            row["why"] = (f"{pid} is ahead overall: {earned:+,.2f} taken from "
                          f"{trades} sale(s) and {unreal:+,.2f} mark-to-market on "
                          f"${capital:,.2f} still held.")
        elif thin:
            row["verdict"] = "TOO_SOON"
            row["why"] = (f"{pid} is {net:+,.2f} net, but on ${capital:,.2f} held for "
                          f"{'an unknown time' if days is None else f'{days:.1f} days'} "
                          f"- under the ${MATERIAL_CAPITAL_USD:,.0f} / "
                          f"{MIN_DAYS_HELD_FOR_A_VERDICT:.0f}-day bar, so it is not "
                          "being held against the coin yet.")
        elif earned > 0:
            # THE CASE THE OLD MEMORY COULD NOT EXPRESS.
            row["verdict"] = "EARNING_BUT_BEHIND"
            row["why"] = (f"{pid} reads as a winner on sales alone - {earned:+,.2f} over "
                          f"{trades} closed trip(s) - and is {net:+,.2f} once the "
                          f"{unreal:+,.2f} on ${capital:,.2f} still held is counted. "
                          f"The selling works; the holding costs more than it earns.")
        else:
            row["verdict"] = "COSTING"
            row["why"] = (f"{pid} has returned {earned:+,.2f} from "
                          f"{trades} closed trip(s) while ${capital:,.2f} sat for "
                          f"{days:.1f} days, now {unreal:+,.2f} mark-to-market: "
                          f"{net:+,.2f} net.")
        rows.append(row)

    ranked = sorted(rows, key=lambda r: (r["net_usd"] is None, r["net_usd"] or 0.0))
    held_earned = round(sum(r["earned_by_selling_usd"] for r in rows), 2)
    held_mtm = round(sum(r["mark_to_market_usd"] for r in rows
                         if r["verdict"] != "PHANTOM"), 2)
    counted = [r for r in rows if r["net_usd"] is not None]

    out = {
        "is_a_measurement_not_a_change": True,
        "branches_holding_coin": len(rows) + len(unknown),
        "ranked": ranked,
        "unknown": unknown,
        "unknown_count": len(unknown),
        "earned_by_selling_usd": held_earned,
        "mark_to_market_usd": held_mtm,
        "net_usd": round(held_earned + held_mtm, 2),
        "capital_tied_usd": round(sum(r["capital_tied_usd"] for r in rows), 2),
        "never_sold": [r["product_id"] for r in rows + unknown if r["never_sold"]],
        "earning_but_behind": [r["product_id"] for r in rows
                              if r["verdict"] == "EARNING_BUT_BEHIND"],
        "costing": [r["product_id"] for r in rows if r["verdict"] == "COSTING"],
        "phantom": [r["product_id"] for r in rows if r["verdict"] == "PHANTOM"],
        "backing_readable": backing_readable,
        "mark_to_market_is_not_a_loss":
            "Nothing here is booked. The mark-to-market figures are on coin still "
            "held, they move both ways, and this module sells nothing and blocks "
            "nothing. It exists because the closed-trade memory can only report "
            "what a sale earned, and the cost of NOT selling never reached it.",
        "it_never_blocks_a_buy":
            "There is no enforcement switch here on purpose. The last mechanism that "
            "retired branches on thin evidence switched off four earning ones and "
            "left 64% of the account idle.",
        "unknown_is_not_zero_cost": True,
        "days_held_is": "days since the oldest OPEN SLICE ROW, not since the coin "
                        "was bought. Adopted coin dates from its adoption, and a "
                        "partial sell that rewrites a row resets it.",
        "earned_by_selling_is": "closed-trade P&L for coins that STILL HOLD slices "
                                "only. It is deliberately smaller than the fleet's "
                                "realised total, which also counts coins now flat "
                                "or retired - those have no holding cost to set it "
                                "against.",
    }

    worst = ranked[0] if counted else None
    if worst is not None and worst.get("net_usd") is not None and worst["net_usd"] < 0:
        out["worst"] = worst["product_id"]
        out["detail"] = (
            f"{worst['product_id']} is the costliest coin to hold: {worst['why']}"
            + (f" It has never sold anything, so the closed-trade memory has no "
               f"record of it at all." if worst["never_sold"] else ""))
    else:
        out["worst"] = None
        out["detail"] = ("no coin currently holding stock is behind once the coin "
                         "still held is counted.")
    return out
