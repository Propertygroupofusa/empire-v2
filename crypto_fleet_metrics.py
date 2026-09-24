"""Fleet measurement: what the grid actually did, separated from what it might do.

Built to answer three questions together, because any one of them alone
misleads:

    NET P&L            did it make money
    CAPITAL VELOCITY   how hard the money worked to make it
    RISK / DRAWDOWN    what it risked doing so

A system earning $0.32 per round trip is not better than one earning $0.10
if the first completes one trade while the second completes twenty. And
85% capital utilization means nothing if none of that capital ever moves -
utilization counts dollars committed, velocity counts dollars turned over.

WHAT IS MEASURED AND WHAT IS NOT

Everything here is computed from CryptoGridTradeHistory rows, which record
entry_price, exit_price, qty, net pnl, opened_at and closed_at. From those:

    net P&L          recorded directly (pnl is already net of fees)
    gross P&L        (exit - entry) * qty
    fee drag         gross - net
    hold time        closed_at - opened_at
    win rate         count(pnl > 0) / count
    profit factor    sum(wins) / |sum(losses)|
    velocity         round-trip notional / deployed / days

Three things this repo does NOT record, and which are therefore reported
as unavailable rather than estimated:

    slippage         needs the expected price at order time next to the
                     actual fill; only the fill is stored
    order counts     submissions, rejections and fills are logged but not
                     counted anywhere durable
    time to fill     market orders fill on submission, so this is only
                     meaningful once maker orders are in use

They are named explicitly in the output. A metric that cannot be computed
is worth more as an honest gap than as a plausible-looking estimate - this
account has already been burned once by a number that looked measured and
was not.
"""

import statistics
from datetime import datetime, timedelta

# A profit factor needs both winners and losers to mean anything. With no
# losses yet it is not "infinite", it is undefined, and saying so keeps an
# early lucky streak from reading as a proven edge.
MIN_TRADES_FOR_CONFIDENCE = 30

NOT_CAPTURED = {
    "time_to_fill_seconds": "not meaningful for market orders; they fill on submission",
}


def _safe_div(a, b):
    return None if not b else a / b


def round_trip_stats(trades):
    """Everything derivable from closed round trips.

    `trades` are dicts with entry_price, exit_price, qty, pnl (net),
    opened_at and closed_at. Returns None-valued fields rather than zeros
    where a figure is undefined, because 0.00 and "no data" are different
    answers and only one of them is a result.
    """
    rows = []
    for t in trades or []:
        try:
            entry = float(t["entry_price"]); exit_ = float(t["exit_price"])
            qty = float(t["qty"]); net = float(t["pnl"])
        except (KeyError, TypeError, ValueError):
            continue
        gross = (exit_ - entry) * qty
        rows.append({
            "product_id": t.get("product_id"),
            "notional": entry * qty,
            "gross": gross, "net": net, "fees": gross - net,
            "hold_seconds": _hold_seconds(t.get("opened_at"), t.get("closed_at")),
        })

    n = len(rows)
    if n == 0:
        return {
            "round_trips": 0,
            "gross_pnl": None, "fees": None, "net_pnl": None,
            "win_rate_pct": None, "profit_factor": None, "avg_net_per_trade": None,
            "median_hold_hours": None, "total_notional": 0.0,
            "confident": False,
            "note": "No round trip has closed yet. Nothing here is a result.",
        }

    wins = [r["net"] for r in rows if r["net"] > 0]
    losses = [r["net"] for r in rows if r["net"] < 0]
    holds = [r["hold_seconds"] for r in rows if r["hold_seconds"] is not None]
    loss_sum = abs(sum(losses))

    return {
        "round_trips": n,
        "gross_pnl": round(sum(r["gross"] for r in rows), 4),
        "fees": round(sum(r["fees"] for r in rows), 4),
        "net_pnl": round(sum(r["net"] for r in rows), 4),
        "win_rate_pct": round(len(wins) / n * 100, 1),
        # Undefined, not infinite, while nothing has lost yet.
        "profit_factor": (round(sum(wins) / loss_sum, 3) if loss_sum else None),
        "profit_factor_note": (None if loss_sum else
                               "undefined - no losing round trip yet, so there is nothing to divide by"),
        "avg_net_per_trade": round(sum(r["net"] for r in rows) / n, 4),
        "median_hold_hours": (round(statistics.median(holds) / 3600, 2) if holds else None),
        "total_notional": round(sum(r["notional"] for r in rows), 2),
        "confident": n >= MIN_TRADES_FOR_CONFIDENCE,
        "note": (None if n >= MIN_TRADES_FOR_CONFIDENCE else
                 f"{n} round trip(s) is too few to separate edge from luck - "
                 f"{MIN_TRADES_FOR_CONFIDENCE} is the point where these figures start to mean something."),
    }


def slippage_stats(trades):
    """How far fills landed from the price the bot decided on.

    Slippage here is fill minus decision price, signed so that NEGATIVE is
    money lost: paying above the price that triggered the buy, or selling
    below the price that triggered the sell. It therefore includes half the
    spread and any movement between the decision and the fill, which is
    what actually costs the account - not the narrower "vs the touch"
    definition an exchange would use. Named precisely so it is not read as
    pure execution slippage.

    Rows from before the decision price was recorded are counted as
    unmeasurable rather than dropped silently, so the coverage is visible
    and a small measured sample cannot masquerade as the whole picture.
    """
    measured, unmeasurable = [], 0
    for t in trades or []:
        try:
            qty = float(t["qty"])
            entry_exp, exit_exp = t.get("entry_expected_price"), t.get("exit_expected_price")
            if entry_exp is None or exit_exp is None:
                unmeasurable += 1
                continue
            entry, exit_ = float(t["entry_price"]), float(t["exit_price"])
            # Buying above the decision price costs; selling below it costs.
            measured.append({
                "entry_usd": -(entry - float(entry_exp)) * qty,
                "exit_usd": (exit_ - float(exit_exp)) * qty,
                "notional": float(entry_exp) * qty,
            })
        except (KeyError, TypeError, ValueError):
            unmeasurable += 1

    n = len(measured)
    if n == 0:
        return {"measured_round_trips": 0, "unmeasurable_round_trips": unmeasurable,
                "entry_slippage_usd": None, "exit_slippage_usd": None,
                "total_slippage_usd": None, "slippage_pct_of_notional": None,
                "note": ("No round trip carries a decision price yet. Slippage becomes "
                         "measurable from the first trip opened after this shipped.")}

    entry_sum = sum(m["entry_usd"] for m in measured)
    exit_sum = sum(m["exit_usd"] for m in measured)
    notional = sum(m["notional"] for m in measured)
    total = entry_sum + exit_sum
    return {
        "measured_round_trips": n,
        "unmeasurable_round_trips": unmeasurable,
        "entry_slippage_usd": round(entry_sum, 4),
        "exit_slippage_usd": round(exit_sum, 4),
        "total_slippage_usd": round(total, 4),
        "avg_slippage_per_trade_usd": round(total / n, 4),
        "slippage_pct_of_notional": (None if not notional else round(total / notional * 100, 4)),
        "note": (None if unmeasurable == 0 else
                 f"{unmeasurable} older round trip(s) predate the decision price and are "
                 f"excluded rather than guessed."),
    }


def drawdown_stats(trades, equity_usd=None):
    """The risk leg: how far the realized curve fell from its own peak.

    Measured in DOLLARS first, and as a percentage OF EQUITY, never as a
    percentage of cumulative P&L. That distinction is not pedantic - a
    $1.50 loss after a $1.00 win is a 150% drawdown of cumulative P&L and
    a 0.25% drawdown of a $600 account, and the first framing has already
    caused a profitable configuration to be refused in this codebase.

    Trades are ordered by close time, because a drawdown computed over an
    arbitrary ordering is not a drawdown of anything.
    """
    rows = []
    for t in trades or []:
        try:
            rows.append((t.get("closed_at"), float(t["pnl"])))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda r: (r[0] is None, str(r[0])))

    if not rows:
        return {"max_drawdown_usd": None, "max_drawdown_pct_of_equity": None,
                "current_drawdown_usd": None, "worst_trade_usd": None,
                "longest_losing_streak": 0,
                "note": "No closed round trips, so there is no realized curve to draw down."}

    cum = peak = 0.0
    max_dd = cur_dd = 0.0
    streak = worst_streak = 0
    for _, pnl in rows:
        cum += pnl
        peak = max(peak, cum)
        cur_dd = peak - cum
        max_dd = max(max_dd, cur_dd)
        streak = streak + 1 if pnl < 0 else 0
        worst_streak = max(worst_streak, streak)

    return {
        "max_drawdown_usd": round(max_dd, 4),
        "max_drawdown_pct_of_equity": (None if not equity_usd else round(max_dd / equity_usd * 100, 3)),
        "current_drawdown_usd": round(cur_dd, 4),
        "worst_trade_usd": round(min(p for _, p in rows), 4),
        "longest_losing_streak": worst_streak,
        "equity_usd": _round(equity_usd),
        "note": (None if equity_usd else
                 "Equity unavailable, so drawdown is shown in dollars only - a percentage "
                 "without the account behind it is the misleading version."),
    }


def _hold_seconds(opened_at, closed_at):
    for value in (opened_at, closed_at):
        if value is None:
            return None
    try:
        o = opened_at if isinstance(opened_at, datetime) else datetime.fromisoformat(str(opened_at))
        c = closed_at if isinstance(closed_at, datetime) else datetime.fromisoformat(str(closed_at))
    except (TypeError, ValueError):
        return None
    return max(0.0, (c - o).total_seconds())


def capital_stats(equity_usd, reserved_usd, deployed_usd, stats, window_days):
    """Utilization and velocity, which only mean something side by side.

    UTILIZATION is how much of the pool is committed to branches. It says
    nothing about whether that money moves.

    VELOCITY is how many times the deployed capital turned over per day:
    total round-trip notional / deployed / days. This is the number that
    exposes a fleet sitting still - utilization can read 85% while
    velocity reads 0.00, which means every dollar is allocated and none of
    it is working.
    """
    util = _safe_div(deployed_usd, equity_usd)
    notional = (stats or {}).get("total_notional") or 0.0
    velocity = _safe_div(notional, (deployed_usd or 0) * (window_days or 0))

    idle = velocity is not None and velocity < 0.05 and (stats or {}).get("round_trips", 0) >= 0
    return {
        "equity_usd": _round(equity_usd), "reserved_usd": _round(reserved_usd),
        "deployed_usd": _round(deployed_usd),
        "utilization_pct": (None if util is None else round(util * 100, 1)),
        "window_days": window_days,
        "turnover_usd": round(notional, 2),
        "velocity_per_day": (None if velocity is None else round(velocity, 3)),
        "reading": (
            "capital could not be priced" if util is None else
            "allocated but not moving - utilization is high and velocity is near zero"
            if idle else
            "allocated and turning over"
        ),
    }


def _round(v, dp=2):
    return None if v is None else round(float(v), dp)


def branch_row(branch, swing_pct=None, gate=None, live_price=None, fee_round_trip=0.010,
               realized=None):
    """One coin's line: what it holds, what it needs, and what the gate said.

    Expected edge is computed the same way the live gate computes it, so
    the number shown here and the number that decides the buy cannot
    disagree. Any input that is missing leaves its field None rather than
    defaulting to something plausible.
    """
    import crypto_nine_coin_scanner as scanner

    step = branch.get("grid_pct")
    ref = branch.get("reference_price")
    price = live_price if live_price is not None else branch.get("current_price")

    distance_pct = None
    if ref and price:
        distance_pct = (price - ref) / ref

    gross_edge = step
    adverse = (scanner.adverse_selection_pct(swing_pct) if swing_pct is not None else None)
    net_edge = (None if (step is None or adverse is None)
                else step - (fee_round_trip + adverse))

    swings_away = (None if (step is None or not swing_pct) else step / swing_pct)

    return {
        "product_id": branch.get("product_id"),
        "allocated_usd": _round(branch.get("allocated_usd")),
        "open_slices": branch.get("open_slices"),
        "num_levels": branch.get("num_levels"),
        "hourly_volatility_pct": (None if swing_pct is None else round(swing_pct * 100, 3)),
        "reference_price": ref,
        "current_price": price,
        # Negative means price is BELOW the reference, i.e. moving toward a buy.
        "distance_to_grid_pct": (None if distance_pct is None else round(distance_pct * 100, 3)),
        "buy_trigger_price": (None if not (ref and step) else round(ref * (1 - step), 8)),
        "sell_target_price": (None if not (ref and step) else round(ref * (1 + step), 8)),
        "expected_gross_edge_pct": (None if gross_edge is None else round(gross_edge * 100, 3)),
        "estimated_fees_pct": round(fee_round_trip * 100, 3),
        "estimated_adverse_pct": (None if adverse is None else round(adverse * 100, 3)),
        "expected_net_edge_pct": (None if net_edge is None else round(net_edge * 100, 3)),
        "target_swings_away": (None if swings_away is None else round(swings_away, 2)),
        "gate_decision": (gate or {}).get("event_type"),
        "gate_reason": (gate or {}).get("message"),
        "gate_at": (gate or {}).get("created_at"),
        "realized_pnl": _round((realized or {}).get("total_pnl")),
        "round_trips": (realized or {}).get("trade_count"),
        "unrealized_net_usd": _round(branch.get("total_unrealized_net_usd")),
    }


def fleet_report(branch_rows, stats, capital, gate_tally, slippage=None, drawdown=None,
                 orders=None):
    """The aggregate: P&L, velocity and RISK together, plus execution quality.

    All three legs, because the first two alone flatter a system that is
    making money by taking a risk nobody measured.
    """
    evaluated = sum((gate_tally or {}).values())
    return {
        "branches": branch_rows,
        "slippage": slippage,
        "drawdown": drawdown,
        "orders": orders,
        "signals": {
            "dips_considered": evaluated,
            "gate_passes": (gate_tally or {}).get("GATE_PASS", 0),
            "gate_blocks": (gate_tally or {}).get("GATE_BLOCK", 0),
            "gate_would_block": (gate_tally or {}).get("GATE_OBSERVE", 0),
            "gate_errors": (gate_tally or {}).get("GATE_ERROR", 0),
        },
        "pnl": stats,
        "capital": capital,
        "not_captured": NOT_CAPTURED,
    }


# --- self-test ------------------------------------------------------------


def _self_test():
    checks = []

    def ok(label, cond):
        checks.append((label, bool(cond)))

    def trade(entry, exit_, qty, net, hours=2.0):
        o = datetime(2026, 9, 24, 10, 0)
        return {"product_id": "ARB-USD", "entry_price": entry, "exit_price": exit_,
                "qty": qty, "pnl": net, "opened_at": o,
                "closed_at": o + timedelta(hours=hours)}

    empty = round_trip_stats([])
    ok("no trades reports zero round trips", empty["round_trips"] == 0)
    ok("and every figure is None, never 0.00",
       empty["net_pnl"] is None and empty["win_rate_pct"] is None
       and empty["profit_factor"] is None)
    ok("and says plainly that nothing here is a result", "Nothing here is a result" in empty["note"])

    # One winner: gross 2.5%, net 1.5% after a 1% round trip on $24.16.
    s = round_trip_stats([trade(1.0, 1.025, 24.16, 0.362)])
    ok("gross is computed from the prices", abs(s["gross_pnl"] - 0.604) < 0.001)
    ok("fee drag is gross minus net", abs(s["fees"] - (0.604 - 0.362)) < 0.001)
    ok("net is taken as recorded, not recomputed", abs(s["net_pnl"] - 0.362) < 1e-9)
    ok("a lone winner gives no profit factor", s["profit_factor"] is None)
    ok("and says why rather than showing infinity", "nothing to divide by" in s["profit_factor_note"])
    ok("one trade is not confident", s["confident"] is False)
    ok("and says how many are needed", "30" in s["note"])

    mixed = round_trip_stats([trade(1.0, 1.02, 50, 0.60), trade(1.0, 1.02, 50, 0.40),
                              trade(1.0, 0.99, 50, -0.50)])
    ok("win rate counts winners", mixed["win_rate_pct"] == round(2/3*100, 1))
    ok("profit factor divides wins by losses", mixed["profit_factor"] == 2.0)
    # Compared against the same rounding the field applies. A 1e-6 tolerance
    # against an unrounded 0.1666... failed on the 4-dp value the field
    # deliberately returns - the assertion was wrong, not the arithmetic.
    ok("average net per trade is net over count",
       mixed["avg_net_per_trade"] == round(0.5 / 3, 4))
    ok("median hold is reported in hours", mixed["median_hold_hours"] == 2.0)

    ok("a malformed row is skipped, not guessed",
       round_trip_stats([{"entry_price": "x"}, trade(1.0, 1.02, 10, 0.2)])["round_trips"] == 1)

    # The scenario the whole file exists for.
    idle = capital_stats(595.29, 88.0, 507.29, round_trip_stats([]), 1.0)
    ok("utilization is high even with nothing trading", idle["utilization_pct"] == 85.2)
    ok("velocity exposes it as still", idle["velocity_per_day"] == 0.0)
    ok("and the reading says so in words", "not moving" in idle["reading"])

    busy = capital_stats(595.29, 88.0, 507.29,
                         round_trip_stats([trade(1.0, 1.025, 24.16, 0.36) for _ in range(20)]), 1.0)
    ok("a turning fleet reads as turning", "turning over" in busy["reading"])
    ok("velocity counts notional per deployed dollar per day",
       abs(busy["velocity_per_day"] - (24.16 * 20) / 507.29) < 0.01)

    ok("unpriceable capital is not reported as 0%",
       capital_stats(None, 88.0, 507.29, empty, 1.0)["utilization_pct"] is None)

    # Branch row.
    b = {"product_id": "ARB-USD", "allocated_usd": 72.47, "open_slices": 1, "num_levels": 3,
         "grid_pct": 0.025, "reference_price": 0.22001, "current_price": 0.21926,
         "total_unrealized_net_usd": -0.12}
    row = branch_row(b, swing_pct=0.012,
                     gate={"event_type": "GATE_BLOCK", "message": "too tight", "created_at": "t"},
                     realized={"total_pnl": 0.94, "trade_count": 3})
    ok("distance to grid is signed toward the buy", row["distance_to_grid_pct"] < 0)
    ok("the buy trigger is a step below the reference",
       abs(row["buy_trigger_price"] - 0.22001 * 0.975) < 1e-9)
    ok("the sell target is a step above", abs(row["sell_target_price"] - 0.22001 * 1.025) < 1e-9)
    ok("expected net edge subtracts fees and adverse selection",
       abs(row["expected_net_edge_pct"] - (2.5 - 1.0 - 0.24)) < 0.01)
    ok("the target is measured in hourly swings", row["target_swings_away"] == round(0.025/0.012, 2))
    ok("the gate's own verdict is carried through", row["gate_decision"] == "GATE_BLOCK")
    ok("realized P&L is per branch", row["realized_pnl"] == 0.94)

    thin = branch_row({"product_id": "X-USD", "grid_pct": 0.025}, swing_pct=None)
    ok("a missing volatility leaves edge None, not optimistic",
       thin["expected_net_edge_pct"] is None and thin["target_swings_away"] is None)

    # --- slippage ---------------------------------------------------------
    def st(entry_exp, entry, exit_exp, exit_, qty=100.0, net=0.0):
        return {"entry_expected_price": entry_exp, "entry_price": entry,
                "exit_expected_price": exit_exp, "exit_price": exit_,
                "qty": qty, "pnl": net}

    sl = slippage_stats([])
    ok("no trades leaves slippage unmeasured, not zero", sl["total_slippage_usd"] is None)
    ok("and says when it becomes measurable", "first trip opened after this shipped" in sl["note"])

    # Paid 0.01 above the decision price, sold 0.01 below it: both cost.
    sl = slippage_stats([st(1.00, 1.01, 1.05, 1.04)])
    ok("paying above the decision price reads as a loss", sl["entry_slippage_usd"] == -1.0)
    ok("selling below the decision price reads as a loss", sl["exit_slippage_usd"] == -1.0)
    ok("and they sum", sl["total_slippage_usd"] == -2.0)
    ok("expressed against the notional it was measured on",
       abs(sl["slippage_pct_of_notional"] - (-2.0 / 100.0 * 100)) < 1e-6)

    sl = slippage_stats([st(1.00, 0.99, 1.05, 1.06)])
    ok("a favourable fill reads positive, not as an error", sl["total_slippage_usd"] == 2.0)

    sl = slippage_stats([st(1.0, 1.01, 1.05, 1.04),
                         {"entry_price": 1.0, "exit_price": 1.05, "qty": 10, "pnl": 0.1}])
    ok("rows without a decision price are counted as unmeasurable",
       sl["measured_round_trips"] == 1 and sl["unmeasurable_round_trips"] == 1)
    ok("and the exclusion is stated, not silent", "excluded rather than guessed" in sl["note"])

    # --- drawdown ---------------------------------------------------------
    def dt(when, pnl):
        return {"closed_at": when, "pnl": pnl, "entry_price": 1.0, "exit_price": 1.0, "qty": 1.0}

    dd = drawdown_stats([])
    ok("no trades means no drawdown curve", dd["max_drawdown_usd"] is None)

    seq = [dt("2026-09-24T10:00", 1.00), dt("2026-09-24T11:00", -1.50),
           dt("2026-09-24T12:00", 0.20), dt("2026-09-24T13:00", -0.40)]
    dd = drawdown_stats(seq, equity_usd=595.29)
    ok("drawdown is peak-to-trough on the realized curve", dd["max_drawdown_usd"] == 1.70)
    ok("expressed against EQUITY, not against cumulative P&L",
       abs(dd["max_drawdown_pct_of_equity"] - (1.70 / 595.29 * 100)) < 1e-3)
    ok("that percentage stays small and honest", dd["max_drawdown_pct_of_equity"] < 1.0)
    ok("the worst single trade is reported", dd["worst_trade_usd"] == -1.50)
    ok("and the longest losing streak", dd["longest_losing_streak"] == 1)

    dd2 = drawdown_stats(seq)
    ok("without equity it gives dollars only, never a fake percentage",
       dd2["max_drawdown_pct_of_equity"] is None and dd2["max_drawdown_usd"] == 1.70)
    ok("and explains why", "misleading version" in dd2["note"])

    ok("trades are ordered by close time before the curve is drawn",
       drawdown_stats(list(reversed(seq)), 595.29)["max_drawdown_usd"] == 1.70)

    rep = fleet_report([row], mixed, busy, {"GATE_PASS": 4, "GATE_BLOCK": 11},
                       slippage=sl, drawdown=dd,
                       orders={"submitted": 6, "filled": 4, "rejected": 2})
    ok("the report carries the risk leg", rep["drawdown"]["max_drawdown_usd"] == 1.70)
    ok("and execution quality", rep["slippage"] is not None and rep["orders"]["rejected"] == 2)
    ok("the fleet report totals every verdict as signals evaluated",
       rep["signals"]["dips_considered"] == 15)
    ok("only genuinely uncapturable metrics remain listed",
       "time_to_fill_seconds" in rep["not_captured"] and "slippage_usd" not in rep["not_captured"])

    width = max(len(l) for l, _ in checks)
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
    failed = [l for l, p in checks if not p]
    print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    print("=" * 72)
    print("  FLEET METRICS - self-test")
    print("=" * 72)
    raise SystemExit(_self_test())
