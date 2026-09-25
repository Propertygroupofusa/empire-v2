"""The exported Pine must be the SAME strategy, or it is worse than nothing.

An export that quietly differs from the engine is the most expensive kind of
bug in this whole system, because it does not look like a bug. It looks like
a strategy that "didn't hold up in TradingView" - and the natural response
is to discard a working strategy, or to trust a broken one, on evidence that
was never comparing the same thing.

Two real mismatches were caught here while writing the exporter, both of the
"nearest built-in" kind:

  * ta.atr() is Wilder's RMA smoothing. This lab's atr() is a plain MEAN of
    true range. On a 14-period ATR those diverge enough to move trades, not
    just decimals.
  * ta.supertrend() builds locked bands off the median price. This lab's
    strat_supertrend is a trailing-stop rule: enter on a >1 ATR up-move,
    ratchet a stop at close - mult x ATR, exit when close loses it. Same
    name, different strategy.

So this file does not check that an export exists. It checks that where a
strategy is exportable, the Pine says what the Python says - and that where
it cannot, the exporter REFUSES rather than emitting something close.

Run: python3 test_pine_export.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import strategy_lab as LAB            # noqa: E402
import strategy_pine_export as PX     # noqa: E402

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


FEE = 0.015

# --- refusal is a feature --------------------------------------------------
ok("a strategy with no Pine body is NOT exportable", not PX.exportable("volume_breakout"))
try:
    PX.to_pine("volume_breakout", {"period": 20}, FEE)
    refused = False
except ValueError:
    refused = True
ok("and exporting it RAISES rather than emitting an approximation", refused)
ok("every Pine body names a real lab strategy",
   all(name in LAB.STRATEGIES for name in PX.PINE_BODIES))

# --- every exportable variant actually emits a script ----------------------
emitted = {}
for name, (fn, param_sets) in LAB.STRATEGIES.items():
    if not PX.exportable(name):
        continue
    params = param_sets[0] if param_sets else {}
    emitted[name] = PX.to_pine(name, params, FEE, coin="BTC-USD", timeframe="1d")

ok("something is exportable at all", len(emitted) >= 8)

for name, src in emitted.items():
    ok(f"{name}: declares Pine v5", "//@version=5" in src)
    ok(f"{name}: is a strategy(), not a study", "\nstrategy(" in src)
    # The title is a double-quoted Pine literal with no escape available, so
    # an inner quote is a script that will not compile. json.dumps() in the
    # title did exactly that before this check existed.
    title = src.split('strategy("', 1)[1].split('"', 1)[0]
    ok(f"{name}: the title cannot break its own string literal", '"' not in title)
    ok(f"{name}: defines both sides of the trade",
       "longCond" in src and "exitCond" in src)
    ok(f"{name}: fills on the NEXT bar's close, not the signal bar",
       "longCond[1]" in src and "exitCond[1]" in src
       and "process_orders_on_close=true" in src)
    ok(f"{name}: states its execution contract in the header",
       "EXECUTION CONTRACT" in src)
    ok(f"{name}: warns that a different number is expected, and why",
       "EXPECT A DIFFERENT NUMBER" in src)
    ok(f"{name}: charges the round trip as two sides",
       f"commission_value={round(FEE * 100 / 2, 6)}" in src)
    ok(f"{name}: every line of Pine is indented with spaces, never tabs",
       "\t" not in src)

# --- the two mismatches that were actually found ---------------------------
atr_src = emitted.get("atr_breakout", "")
ok("REGRESSION: atr_breakout does NOT use ta.atr (Wilder) when the lab uses a mean",
   "ta.atr(" not in atr_src)
ok("it uses a simple mean of true range, matching lab.atr()",
   "ta.sma(ta.tr(true)" in atr_src)

st_src = emitted.get("supertrend", "")
ok("REGRESSION: supertrend does NOT use ta.supertrend, which is a different rule",
   "ta.supertrend(" not in st_src)
ok("it keeps the same trailing-stop state the Python keeps",
   "var float stopLevel" in st_src and "math.max" in st_src)
ok("and its description says so, so nobody assumes the built-in",
   "NOT Pine's ta.supertrend" in st_src)

# --- the conditions match the Python ---------------------------------------
# Not a re-implementation check - a correspondence check. Each entry names
# the comparison the Python makes and the Pine that has to make it too.
EXPECTED = {
    "sma_cross": ("f > s", "ta.sma"),
    "ema_cross": ("f > s", "ta.ema"),
    "macd": ("macdLine > signalLine", "ta.macd"),
    "rsi_reversion": ("r < oversold", "ta.rsi"),
    "momentum": ("mom > threshold", "close[lookback]"),
    "price_vs_sma": ("close > m * (1 + buffer_pct)", "ta.sma"),
    "bollinger_breakout": ("close > upper", "ta.stdev"),
    "bollinger_reversion": ("close < lower", "ta.stdev"),
    "donchian_breakout": ("close > hh", "ta.highest"),
    "buy_and_hold": ("true", ""),
}
for name, (cond, indicator) in EXPECTED.items():
    src = emitted.get(name, "")
    ok(f"{name}: entry condition is the Python's condition", f"longCond = {cond}" in src)
    if indicator:
        ok(f"{name}: uses {indicator}", indicator in src)

# Donchian excludes the CURRENT bar on both sides in Python
# (highs[i-period:i]); [1] is what does that in Pine. Without it the rule
# compares the close to a high that includes itself, and never triggers the
# same way.
d = emitted.get("donchian_breakout", "")
ok("donchian excludes the current bar from its own high, as the Python does",
   "ta.highest(high, period)[1]" in d)
ok("and from its own exit low", "ta.lowest(low, exit_period)[1]" in d)

# --- the measured figures travel with the script ---------------------------
withm = PX.to_pine("sma_cross", {"fast": 20, "slow": 50}, FEE, coin="BTC-USD",
                   timeframe="1d", oos_split_label="2023-11-18",
                   measured={"total_return_pct": 52.2, "trades": 61, "sharpe": 0.44,
                             "final_balance": 15220.0, "max_drawdown_pct": 21.4,
                             "win_rate": 43.0})
ok("the lab's own result is printed in the script it exports",
   "THIS LAB MEASURED" in withm and "52.2" in withm)
ok("including the out-of-sample split date, so both sides test one period",
   "2023-11-18" in withm)
ok("and the drawdown, not just the return - risk travels with the number",
   "21.4" in withm)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
