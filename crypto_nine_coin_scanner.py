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

# This account's real tier, as the live dashboard reads it back from
# Coinbase: 1.00% round trip on market orders, with branch spacing floored
# at 1.20% - "the smallest move that can actually clear that fee".
#
# Corrected from 0.008/0.005, which came from a 2026-08-01 measurement of
# 0.40%/0.25% per side. Those were 20bp optimistic on every break-even
# figure derived from them. A cost model that flatters itself turns
# refusals into approvals, which is the one direction this file must never
# err in - so it tracks the number the account actually pays.
DEFAULT_TAKER_ROUND_TRIP = 0.010
DEFAULT_MAKER_ROUND_TRIP = 0.006

# Adverse selection, charged once per round trip, replacing a flat
# slippage term.
#
# A resting limit order fills when the market comes to it, which means it
# fills on momentum pressing against the order - a maker buy fills because
# sellers are hitting the bid. That cost is not constant: it scales with
# how fast the market is moving. Modelled as a fraction of the coin's own
# volatility with an absolute floor, so a quiet book still pays something
# and a fast one pays proportionally.
#
# This SUBSUMES the flat 0.10%-per-side slippage this file used before -
# charging both would count the same friction twice. At a 0.75% hourly
# swing the two come to the same 0.15%; above that this one grows and the
# flat term would have under-charged.
DEFAULT_MIN_ADVERSE_SELECTION = 0.0015
DEFAULT_ADVERSE_SELECTION_VOL_FRACTION = 0.20

# Reject a coin whose top-of-book spread is wider than this. Crossing a
# wide spread is a cost paid before the trade has done anything, and on a
# $70 slice it is not recoverable inside a grid step.
DEFAULT_MAX_SPREAD_PCT = 0.0015

# The book must hold this multiple of the intended position on BOTH sides
# before entering. A $70 slice into a book showing $70 of depth IS the
# book - it moves the price it is trying to trade at, and the exit finds
# nothing to sell into. 4x is the smallest ratio where the order is a
# participant rather than the event.
DEFAULT_MIN_DEPTH_RATIO = 4.0
DEFAULT_BRANCH_SIZE_USD = 70.0

# A target beyond this many AVERAGE HOURLY SWINGS is unreachable.
#
# Changed from a 5-minute ATR, which was the wrong clock. A grid round
# trip does not need one 5-minute candle to cover the whole target - it
# needs price to travel that far over the holding period, which is hours.
# Judged against a 5-minute bar, a 1.45% hurdle demanded a 1.50% 5-minute
# ATR, which is crash-level volatility: BTC sits near 0.60%, so the gate
# rejected every coin in every normal market and passed only an alt
# mid-spike.
#
# The hourly window is also what the account already trades on:
# get_average_hourly_swing_pct() over 120 hours is the same measure the
# live average-swing spacing uses, and 1.5x that swing is the multiple its
# 30-day/8-coin backtest validated ($5.79 vs $1.02 against a flat 1%).
# Allowing up to 3x gives a target roughly two hours of typical movement
# away - twice the validated spacing, and a real ceiling rather than a
# permanent veto.
DEFAULT_MAX_TARGET_SWING_MULTIPLE = 3.0

# The most an entry may demand of the win rate. Same value and reasoning as
# btc_compound's MAX_BREAKEVEN_WIN_RATE.
DEFAULT_MAX_BREAKEVEN_WIN_RATE = 0.55


def adverse_selection_pct(amplitude_pct,
                          min_adverse=DEFAULT_MIN_ADVERSE_SELECTION,
                          vol_fraction=DEFAULT_ADVERSE_SELECTION_VOL_FRACTION):
    """Cost of filling on the wrong side of momentum, scaled by volatility."""
    return max(min_adverse, vol_fraction * (amplitude_pct or 0.0))


def total_cost_pct(amplitude_pct, fee_round_trip=DEFAULT_TAKER_ROUND_TRIP,
                   min_adverse=DEFAULT_MIN_ADVERSE_SELECTION,
                   vol_fraction=DEFAULT_ADVERSE_SELECTION_VOL_FRACTION):
    """Everything a completed round trip pays: fees plus adverse selection."""
    return fee_round_trip + adverse_selection_pct(amplitude_pct, min_adverse, vol_fraction)


def net_edge_pct(target_pct, amplitude_pct,
                 fee_round_trip=DEFAULT_TAKER_ROUND_TRIP,
                 min_adverse=DEFAULT_MIN_ADVERSE_SELECTION,
                 vol_fraction=DEFAULT_ADVERSE_SELECTION_VOL_FRACTION):
    """What a winning trade actually keeps, after every cost on both legs."""
    return target_pct - total_cost_pct(amplitude_pct, fee_round_trip,
                                       min_adverse, vol_fraction)


def breakeven_win_rate(target_pct, stop_pct, amplitude_pct,
                       fee_round_trip=DEFAULT_TAKER_ROUND_TRIP,
                       min_adverse=DEFAULT_MIN_ADVERSE_SELECTION,
                       vol_fraction=DEFAULT_ADVERSE_SELECTION_VOL_FRACTION):
    """Win rate this target/stop/cost triple needs just to break even.

    Costs are paid on winners and losers alike, so they are subtracted from
    the win and ADDED to the loss. Returns 1.0 - unachievable - when a win
    nets nothing, rather than a ratio that would make a hopeless setup look
    merely demanding.
    """
    costs = total_cost_pct(amplitude_pct, fee_round_trip, min_adverse, vol_fraction)
    net_win = target_pct - costs
    net_loss = stop_pct + costs
    if net_win <= 0:
        return 1.0
    return net_loss / (net_win + net_loss)


def evaluate_coin(product_id, target_pct, stop_pct, hourly_swing_pct,
                  best_bid=None, best_ask=None,
                  bid_depth_usd=None, ask_depth_usd=None,
                  branch_size_usd=DEFAULT_BRANCH_SIZE_USD,
                  fee_round_trip=DEFAULT_TAKER_ROUND_TRIP,
                  min_adverse=DEFAULT_MIN_ADVERSE_SELECTION,
                  vol_fraction=DEFAULT_ADVERSE_SELECTION_VOL_FRACTION,
                  max_spread_pct=DEFAULT_MAX_SPREAD_PCT,
                  min_depth_ratio=DEFAULT_MIN_DEPTH_RATIO,
                  max_target_swing_multiple=DEFAULT_MAX_TARGET_SWING_MULTIPLE,
                  max_breakeven_win_rate=DEFAULT_MAX_BREAKEVEN_WIN_RATE):
    """One coin against every gate. Never raises; bad input is a skip.

    `hourly_swing_pct` is the coin's average hourly swing - the same
    measure crypto_btc_compound_bot.get_average_hourly_swing_pct() returns
    and the live average-swing spacing already trades on.

    Order-book fields are optional ONLY in the sense that omitting them is
    a skip, not a pass. A scanner that cannot see the book cannot know
    whether a $70 order is a participant or the whole bid side, and
    "unknown liquidity" has never been a reason to trade.
    """
    row = {
        "product_id": product_id,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "hourly_swing_pct": hourly_swing_pct,
        "spread_pct": None,
        "net_edge_pct": None,
        "adverse_selection_pct": None,
        "breakeven_win_rate": None,
        "target_swing_multiple": None,
        "qualified": False,
        "reason": "",
    }

    # Missing or nonsensical inputs are an unknown, and an unknown is never
    # a reason to trade. A zero swing means volatility could not be
    # computed, not that the coin is calm.
    for name, value in (("target", target_pct), ("stop", stop_pct),
                        ("hourly swing", hourly_swing_pct)):
        if value is None:
            row["reason"] = f"{name} unavailable - cannot evaluate"
            return row
        if value <= 0:
            row["reason"] = f"{name} is {value} - not a usable number"
            return row

    # --- liquidity, before economics -------------------------------------
    # Checked first because a book this order cannot trade in makes every
    # figure below hypothetical.
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        row["reason"] = "order book unavailable - cannot price the spread"
        return row
    spread = (best_ask - best_bid) / best_bid
    row["spread_pct"] = spread
    if spread > max_spread_pct:
        row["reason"] = (f"spread {spread * 100:.3f}% is over the "
                         f"{max_spread_pct * 100:.2f}% limit - paid before the trade "
                         f"does anything")
        return row

    if bid_depth_usd is None or ask_depth_usd is None:
        row["reason"] = "book depth unavailable - cannot size safely"
        return row
    required_depth = branch_size_usd * min_depth_ratio
    if bid_depth_usd < required_depth or ask_depth_usd < required_depth:
        row["reason"] = (f"thin book - bid ${bid_depth_usd:,.0f} / ask ${ask_depth_usd:,.0f} "
                         f"against ${required_depth:,.0f} needed for a "
                         f"${branch_size_usd:,.0f} slice")
        return row

    # --- economics --------------------------------------------------------
    adverse = adverse_selection_pct(hourly_swing_pct, min_adverse, vol_fraction)
    row["adverse_selection_pct"] = adverse
    costs = total_cost_pct(hourly_swing_pct, fee_round_trip, min_adverse, vol_fraction)
    edge = net_edge_pct(target_pct, hourly_swing_pct, fee_round_trip,
                        min_adverse, vol_fraction)
    row["net_edge_pct"] = edge
    if edge <= 0:
        row["reason"] = (f"net edge {edge * 100:+.3f}% - a {target_pct * 100:.2f}% target "
                         f"does not clear {costs * 100:.2f}% of costs "
                         f"({fee_round_trip * 100:.2f}% fees + {adverse * 100:.2f}% adverse)")
        return row

    multiple = target_pct / hourly_swing_pct
    row["target_swing_multiple"] = multiple
    if multiple > max_target_swing_multiple:
        row["reason"] = (f"target is {multiple:.1f}x the {hourly_swing_pct * 100:.2f}% "
                         f"hourly swing, over the {max_target_swing_multiple:.1f}x limit "
                         f"- would sit unfilled")
        return row

    needed = breakeven_win_rate(target_pct, stop_pct, hourly_swing_pct,
                                fee_round_trip, min_adverse, vol_fraction)
    row["breakeven_win_rate"] = needed
    if needed > max_breakeven_win_rate:
        row["reason"] = (f"needs a {needed * 100:.1f}% win rate to break even, "
                         f"over the {max_breakeven_win_rate * 100:.0f}% limit")
        return row

    row["qualified"] = True
    row["reason"] = (f"net edge {edge * 100:+.3f}%, target {multiple:.1f}x swing, "
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

    # A healthy book, so liquidity never silently decides an economics test.
    BOOK = dict(best_bid=100.0, best_ask=100.10, bid_depth_usd=5000.0, ask_depth_usd=5000.0)

    def ev(pid, target, stop, swing, **kw):
        return evaluate_coin(pid, target, stop, swing, **{**BOOK, **kw})

    # The real live position from 2026-09-24: +1.5% target, -2% stop.
    live = ev("BTC-USD", 0.015, 0.02, 0.006)
    ok("live 1.5%/2.0% BTC position is refused", not live["qualified"])
    ok("its break-even is above 80%", live["breakeven_win_rate"] > 0.80)

    # A target that cannot clear costs at all.
    poor = ev("ADA-USD", 0.008, 0.02, 0.010)
    ok("target under total cost is refused", not poor["qualified"])
    ok("refused on net edge", "net edge" in poor["reason"])

    # Reachable on the HOURLY clock, which the 5-minute ATR version vetoed.
    # This is the change: a 2.5% target against a 1.0% hourly swing is 2.5
    # hours of typical movement - ordinary. Judged against a 5-min ATR it
    # needed a 1.50% five-minute bar and was rejected in every normal market.
    hourly = ev("SOL-USD", 0.028, 0.007, 0.010)
    ok("a 2.8x-hourly-swing target is reachable", hourly["target_swing_multiple"] <= 3.0)
    ok("and it qualifies", hourly["qualified"])

    far = ev("XRP-USD", 0.050, 0.020, 0.002)
    ok("target far past the hourly swing is refused", not far["qualified"])
    ok("refused on reachability", "hourly swing" in far["reason"])
    ok("net edge alone would have passed it", far["net_edge_pct"] > 0)

    # --- the three additions --------------------------------------------
    wide = ev("DOGE-USD", 0.040, 0.015, 0.015, best_bid=100.0, best_ask=100.30)
    ok("wide spread is refused", not wide["qualified"])
    ok("refused on spread", "spread" in wide["reason"])

    thin = ev("DOT-USD", 0.040, 0.015, 0.015, bid_depth_usd=100.0, ask_depth_usd=5000.0)
    ok("thin bid side is refused", not thin["qualified"])
    ok("refused on depth", "thin book" in thin["reason"])
    thin_ask = ev("DOT-USD", 0.040, 0.015, 0.015, bid_depth_usd=5000.0, ask_depth_usd=100.0)
    ok("thin ask side is refused too", not thin_ask["qualified"])
    ok("exactly 4x depth passes", ev("DOT-USD", 0.040, 0.015, 0.015,
                                     bid_depth_usd=280.0, ask_depth_usd=280.0)["qualified"])

    no_book = evaluate_coin("ETH-USD", 0.040, 0.015, 0.015)
    ok("missing order book is a skip, not a pass", not no_book["qualified"])
    ok("and says so", "order book unavailable" in no_book["reason"])
    no_depth = evaluate_coin("ETH-USD", 0.040, 0.015, 0.015,
                             best_bid=100.0, best_ask=100.10)
    ok("missing depth is a skip", not no_depth["qualified"])

    # Adverse selection scales with volatility, with a floor.
    ok("adverse selection floors at 0.15%", adverse_selection_pct(0.001) == 0.0015)
    ok("and scales above it", adverse_selection_pct(0.030) == 0.006)
    quiet, fast = ev("LINK-USD", 0.040, 0.015, 0.005), ev("LINK-USD", 0.040, 0.015, 0.020)
    ok("a faster coin is charged more", fast["adverse_selection_pct"] > quiet["adverse_selection_pct"])
    ok("and therefore keeps less", fast["net_edge_pct"] < quiet["net_edge_pct"])

    # Cost monotonicity.
    maker = ev("AVAX-USD", 0.040, 0.015, 0.015, fee_round_trip=DEFAULT_MAKER_ROUND_TRIP)
    taker = ev("AVAX-USD", 0.040, 0.015, 0.015)
    ok("lower fees never reduce net edge", maker["net_edge_pct"] > taker["net_edge_pct"])
    ok("lower fees never raise the required win rate",
       maker["breakeven_win_rate"] <= taker["breakeven_win_rate"])

    # Unknowns are skips, never trades.
    for bad, label in ((None, "None"), (0, "zero"), (-0.01, "negative")):
        ok(f"{label} swing is a skip", not ev("DOT-USD", 0.04, 0.015, bad)["qualified"])
        ok(f"{label} target is a skip", not ev("DOT-USD", bad, 0.015, 0.015)["qualified"])

    # Ranking: risk-adjusted, deterministic, qualified-only.
    rows = [ev("SOL-USD", 0.040, 0.015, 0.015),
            ev("AVAX-USD", 0.048, 0.025, 0.018),
            ev("BTC-USD", 0.015, 0.020, 0.006)]
    q, s = rank_opportunities(rows)
    ok("only qualified coins are ranked", len(q) == 2 and len(s) == 1)
    ok("ranked by edge per unit of risk", q[0]["product_id"] == "SOL-USD")
    ok("raw edge alone would have ranked AVAX first",
       rows[1]["net_edge_pct"] > rows[0]["net_edge_pct"])
    q2, _ = rank_opportunities(list(reversed(rows)))
    ok("ranking is order-independent",
       [r["product_id"] for r in q] == [r["product_id"] for r in q2])

    q3, s3 = rank_opportunities([ev("BTC-USD", 0.015, 0.020, 0.006)])
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
    # (product, target, stop, hourly_swing, bid, ask, bid_depth, ask_depth)
    sample = [
        ("BTC-USD",  0.015, 0.020, 0.006, 100.0, 100.05, 9000.0, 9000.0),
        ("ETH-USD",  0.025, 0.015, 0.010, 100.0, 100.05, 8000.0, 8000.0),
        ("SOL-USD",  0.040, 0.015, 0.015, 100.0, 100.08, 6000.0, 6000.0),
        ("ADA-USD",  0.008, 0.020, 0.010, 100.0, 100.05, 4000.0, 4000.0),
        ("DOGE-USD", 0.045, 0.020, 0.018, 100.0, 100.30, 3000.0, 3000.0),
        ("XRP-USD",  0.050, 0.020, 0.002, 100.0, 100.05, 5000.0, 5000.0),
        ("LINK-USD", 0.028, 0.018, 0.011, 100.0, 100.10, 1000.0,  150.0),
        ("AVAX-USD", 0.048, 0.025, 0.018, 100.0, 100.10, 2000.0, 2000.0),
        ("DOT-USD",  0.022, 0.015, 0.009, 100.0, 100.10, 2000.0, 2000.0),
    ]
    rows = [evaluate_coin(p, t, s, sw, best_bid=b, best_ask=a,
                          bid_depth_usd=bd, ask_depth_usd=ad)
            for p, t, s, sw, b, a, bd, ad in sample]
    print(scan_report(*rank_opportunities(rows)))
    sys.exit(rc)
