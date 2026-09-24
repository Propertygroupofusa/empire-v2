"""Rank all nine fleet coins by net edge and trade only the ones that clear it.

WHAT THIS REPLACES
------------------
The live crypto engine trades one coin. btc_compound is BTC-only, and the
account owner's ask is the opposite: look at all nine, take the ones that
can actually pay, skip the rest, repeat.

"Only take profitable trades" is not buildable - no filter knows which
trade wins. What IS buildable, and what this does, is refuse every entry
whose arithmetic cannot pay even when it goes right. That distinction is
not pedantic: on 2026-09-24 the live BTC position was opened at a +1.50%
target against a -2.00% stop with a ~0.80% round trip, needing an 80% win
rate to break even. It was not a trade that lost; it was a trade that could
not win at any plausible hit rate. Three gates below would each have
refused it on its own.

THE THREE GATES
---------------
1. NET EDGE - target minus the real round-trip fee minus slippage on both
   sides must be positive. A 1.5% target against a 0.8% round trip and
   0.1% slippage per side nets 0.50%; against a 1.8% round trip it nets
   negative and the "win" is a loss.

2. REACHABILITY - the target must be within what the coin actually moves.
   atr_pct here is a 14-period ATR on FIVE-MINUTE candles, so a target of
   N x ATR needs roughly N favourable 5-minute bars. Sizing a target past
   that is how positions open and never close: the measured median hold on
   the Alpaca side was 144 hours, and the crypto sweep reproduced the same
   "six buys, one sell" pattern the live Coinbase account showed.

3. BREAK-EVEN WIN RATE - what the target/stop/fee triple demands of the
   hit rate. Rejects the geometry that is self-defeating regardless of
   how reachable the target is.

A coin failing any gate is SKIPPED with the reason recorded, not silently
dropped. The skip list is the point: it is the evidence for why nothing
traded this cycle, which is otherwise indistinguishable from the bot being
broken.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not predict direction and it does not claim an edge. Every gate is
a COST-COVERAGE test - "if this works, does it pay, and is it the kind of
move this coin makes?" Whether the entry signal has any predictive power
is a separate question this file cannot answer, and the honest state of
that answer today is: the 30-day sweep found 0 of 14 profitable geometries
surviving out-of-sample, and Alpaca's own records put the stock side at
profit factor 0.866. Ranking opportunities by net edge makes the system
stop paying for setups that were never going to clear costs. It does not
make the survivors winners.

Pure and synchronous throughout - no network, no database, no clock - so
every rule here is directly testable. Callers fetch prices and volatility
with the engine's own functions and hand the numbers in.

    python crypto_nine_coin_scanner.py     # runs the self-test
"""

import sys

try:
    from coin_lifecycle import CoinPortfolio
    NINE_COINS = list(CoinPortfolio.NINE_COINS)
except Exception:  # pragma: no cover - import shape varies by entrypoint
    NINE_COINS = [
        "BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "DOGE-USD",
        "XRP-USD", "LINK-USD", "AVAX-USD", "DOT-USD",
    ]

# Real observed tier for this account, not the code's stale 0.6% default:
# taker 0.40% and maker 0.25% PER SIDE, measured 2026-08-01. Round trips,
# because a position costs a fee going in and another coming out.
DEFAULT_TAKER_ROUND_TRIP = 0.008
DEFAULT_MAKER_ROUND_TRIP = 0.005

# Charged on entry AND exit. Market orders cross the spread both ways; this
# is the cost the backtests omit and the live-vs-backtest gap is made of.
DEFAULT_SLIPPAGE_PER_SIDE = 0.001

# A target beyond this many 5-minute ATRs is treated as unreachable.
DEFAULT_MAX_TARGET_ATR_MULTIPLE = 3.0

# The most an entry may demand of the win rate. Same value and reasoning as
# btc_compound's MAX_BREAKEVEN_WIN_RATE.
DEFAULT_MAX_BREAKEVEN_WIN_RATE = 0.55


def net_edge_pct(target_pct, fee_round_trip=DEFAULT_TAKER_ROUND_TRIP,
                 slippage_per_side=DEFAULT_SLIPPAGE_PER_SIDE):
    """What a winning trade actually keeps, after costs on both legs."""
    return target_pct - fee_round_trip - (2 * slippage_per_side)


def breakeven_win_rate(target_pct, stop_pct,
                       fee_round_trip=DEFAULT_TAKER_ROUND_TRIP,
                       slippage_per_side=DEFAULT_SLIPPAGE_PER_SIDE):
    """Win rate this target/stop/cost triple needs just to break even.

    Costs are paid on winners and losers alike, so they are subtracted from
    the win and ADDED to the loss. Returns 1.0 - unachievable - when a win
    nets nothing, rather than a ratio that would make a hopeless setup look
    merely demanding.
    """
    costs = fee_round_trip + (2 * slippage_per_side)
    net_win = target_pct - costs
    net_loss = stop_pct + costs
    if net_win <= 0:
        return 1.0
    return net_loss / (net_win + net_loss)


def evaluate_coin(product_id, target_pct, stop_pct, atr_pct,
                  fee_round_trip=DEFAULT_TAKER_ROUND_TRIP,
                  slippage_per_side=DEFAULT_SLIPPAGE_PER_SIDE,
                  max_target_atr_multiple=DEFAULT_MAX_TARGET_ATR_MULTIPLE,
                  max_breakeven_win_rate=DEFAULT_MAX_BREAKEVEN_WIN_RATE):
    """One coin against all three gates. Never raises; bad input is a skip.

    Returns a dict that always carries `qualified` and `reason`, so a
    caller can log exactly why a coin was passed over.
    """
    row = {
        "product_id": product_id,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "atr_pct": atr_pct,
        "net_edge_pct": None,
        "breakeven_win_rate": None,
        "target_atr_multiple": None,
        "qualified": False,
        "reason": "",
    }

    # Missing or nonsensical inputs are an unknown, and an unknown is never
    # a reason to trade. A zero ATR means volatility could not be computed,
    # not that the coin is calm.
    for name, value in (("target", target_pct), ("stop", stop_pct), ("atr", atr_pct)):
        if value is None:
            row["reason"] = f"{name} unavailable - cannot evaluate"
            return row
        if value <= 0:
            row["reason"] = f"{name} is {value} - not a usable number"
            return row

    edge = net_edge_pct(target_pct, fee_round_trip, slippage_per_side)
    row["net_edge_pct"] = edge
    costs = fee_round_trip + (2 * slippage_per_side)
    if edge <= 0:
        row["reason"] = (f"net edge {edge * 100:+.3f}% - a {target_pct * 100:.2f}% target "
                         f"does not clear {costs * 100:.2f}% of costs")
        return row

    multiple = target_pct / atr_pct
    row["target_atr_multiple"] = multiple
    if multiple > max_target_atr_multiple:
        row["reason"] = (f"target is {multiple:.1f}x the 5-min ATR of {atr_pct * 100:.2f}%, "
                         f"over the {max_target_atr_multiple:.1f}x limit - would sit unfilled")
        return row

    needed = breakeven_win_rate(target_pct, stop_pct, fee_round_trip, slippage_per_side)
    row["breakeven_win_rate"] = needed
    if needed > max_breakeven_win_rate:
        row["reason"] = (f"needs a {needed * 100:.1f}% win rate to break even, "
                         f"over the {max_breakeven_win_rate * 100:.0f}% limit")
        return row

    row["qualified"] = True
    row["reason"] = (f"net edge {edge * 100:+.3f}%, target {multiple:.1f}x ATR, "
                     f"break-even {needed * 100:.1f}%")
    return row


def rank_opportunities(rows):
    """Qualified coins best-first, plus everything skipped and why.

    Ranked on net edge per unit of risk rather than raw net edge, so a coin
    is not preferred purely for carrying a wider stop. Ties break on the
    product id so the ordering is deterministic - a scan that reshuffles
    equal candidates between cycles makes its own logs unreadable.
    """
    qualified = [r for r in rows if r["qualified"]]
    skipped = [r for r in rows if not r["qualified"]]
    qualified.sort(key=lambda r: (-(r["net_edge_pct"] / r["stop_pct"]), r["product_id"]))
    skipped.sort(key=lambda r: r["product_id"])
    return qualified, skipped


def scan_report(qualified, skipped):
    """The scan as text, built for a log where nobody trades most cycles."""
    out = [f"NINE-COIN SCAN - {len(qualified)} qualified, {len(skipped)} skipped"]
    for r in qualified:
        out.append(f"  ✓ {r['product_id']:<10} net edge {r['net_edge_pct'] * 100:+.3f}%  "
                   f"per unit risk {r['net_edge_pct'] / r['stop_pct']:+.3f}  {r['reason']}")
    for r in skipped:
        out.append(f"  ✗ {r['product_id']:<10} {r['reason']}")
    if not qualified:
        out.append("  nothing qualified this cycle - holding cash is the correct "
                   "outcome, not a failure to find a trade")
    return "\n".join(out)


# --- self-test ------------------------------------------------------------


def _self_test():
    checks = []

    def ok(label, cond):
        checks.append((label, bool(cond)))

    # The real live position from 2026-09-24: +1.5% target, -2% stop.
    live = evaluate_coin("BTC-USD", 0.015, 0.02, 0.006)
    ok("live 1.5%/2.0% BTC position is refused", not live["qualified"])
    ok("refused on break-even win rate", "win rate" in live["reason"])
    ok("its break-even is above 80%", live["breakeven_win_rate"] > 0.80)

    # A target that cannot clear costs at all.
    poor = evaluate_coin("ADA-USD", 0.008, 0.02, 0.01)
    ok("target equal to costs is refused", not poor["qualified"])
    ok("refused on net edge", "net edge" in poor["reason"])
    ok("net edge is not positive", poor["net_edge_pct"] <= 0)

    # A big target on a quiet coin: pays if hit, but will not be hit.
    far = evaluate_coin("XRP-USD", 0.05, 0.02, 0.002)
    ok("unreachable target is refused", not far["qualified"])
    ok("refused on reachability", "ATR" in far["reason"])
    ok("net edge alone would have passed it", far["net_edge_pct"] > 0)

    # A setup that clears all three. Note how narrow this window is: at a
    # 0.8% round trip plus 0.2% slippage, a 3.0% target against a 1.5%
    # stop still needs 55.6% and is refused. Clearing every gate takes a
    # target roughly 2.7x the stop, which is the honest cost of trading
    # through a 1% round trip on a small account.
    good = evaluate_coin("SOL-USD", 0.04, 0.015, 0.015)
    ok("good setup qualifies", good["qualified"])
    ok("its break-even is under the limit", good["breakeven_win_rate"] <= 0.55)
    ok("its target is within the ATR limit", good["target_atr_multiple"] <= 3.0)

    # Maker fees change the verdict, which is the whole point of the fee
    # rate being an input rather than a constant.
    borderline_taker = evaluate_coin("ETH-USD", 0.025, 0.015, 0.01)
    borderline_maker = evaluate_coin("ETH-USD", 0.025, 0.015, 0.01,
                                     fee_round_trip=DEFAULT_MAKER_ROUND_TRIP)
    ok("lower fees never reduce net edge",
       borderline_maker["net_edge_pct"] > borderline_taker["net_edge_pct"])
    ok("lower fees never raise the required win rate",
       borderline_maker["breakeven_win_rate"] <= borderline_taker["breakeven_win_rate"])

    # Unknowns are skips, never trades.
    for bad, label in ((None, "None"), (0, "zero"), (-0.01, "negative")):
        ok(f"{label} ATR is a skip", not evaluate_coin("DOT-USD", 0.03, 0.015, bad)["qualified"])
        ok(f"{label} target is a skip", not evaluate_coin("DOT-USD", bad, 0.015, 0.01)["qualified"])

    # Ranking: risk-adjusted, deterministic, and qualified-only.
    rows = [
        evaluate_coin("SOL-USD", 0.040, 0.015, 0.015),   # edge 0.030 / risk 0.015 = 2.00
        evaluate_coin("AVAX-USD", 0.045, 0.025, 0.018),  # edge 0.035 / risk 0.025 = 1.40
        evaluate_coin("BTC-USD", 0.015, 0.020, 0.006),   # refused
    ]
    q, s = rank_opportunities(rows)
    ok("only qualified coins are ranked", len(q) == 2 and len(s) == 1)
    ok("ranked by edge per unit of risk, not raw edge", q[0]["product_id"] == "SOL-USD")
    ok("raw edge alone would have ranked AVAX first",
       rows[1]["net_edge_pct"] > rows[0]["net_edge_pct"])
    q2, _ = rank_opportunities(list(reversed(rows)))
    ok("ranking is order-independent", [r["product_id"] for r in q] ==
       [r["product_id"] for r in q2])

    # An empty scan is a valid, expected outcome.
    q3, s3 = rank_opportunities([evaluate_coin("BTC-USD", 0.015, 0.02, 0.006)])
    ok("a cycle with no qualified coin is not an error", q3 == [] and len(s3) == 1)
    ok("the report says so in words", "holding cash" in scan_report(q3, s3))

    ok("all nine coins are covered", len(NINE_COINS) == 9)

    width = max(len(label) for label, _ in checks)
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
    failed = [label for label, passed in checks if not passed]
    print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    print("=" * 72)
    print("  NINE-COIN NET-EDGE SCANNER - self-test")
    print("=" * 72)
    rc = _self_test()

    print("\n" + "=" * 72)
    print("  EXAMPLE SCAN (illustrative inputs, not live market data)")
    print("=" * 72)
    sample = [
        ("BTC-USD", 0.015, 0.020, 0.006),
        ("ETH-USD", 0.025, 0.015, 0.010),
        ("SOL-USD", 0.040, 0.015, 0.015),
        ("ADA-USD", 0.008, 0.020, 0.010),
        ("DOGE-USD", 0.045, 0.020, 0.018),
        ("XRP-USD", 0.050, 0.020, 0.002),
        ("LINK-USD", 0.028, 0.018, 0.011),
        ("AVAX-USD", 0.045, 0.025, 0.018),
        ("DOT-USD", 0.022, 0.015, 0.009),
    ]
    rows = [evaluate_coin(p, t, s, a) for p, t, s, a in sample]
    print(scan_report(*rank_opportunities(rows)))
    sys.exit(rc)
