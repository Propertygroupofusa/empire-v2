"""Export a lab variant as a TradingView Pine v5 strategy - and say exactly
where it will disagree.

WHY THIS FILE STATES ITS DIFFERENCES INSTEAD OF CLAIMING PARITY

The same strategy run in two engines does not produce the same number, and
the gap is not a bug in either one - it is the two engines making different
assumptions about things the data does not record. Reported in the field on
one 758-trade run: roughly 0.1% per trade, which is nothing per trade and
enormous once compounded.

The dangerous version of that gap is the silent one. If an export prints a
different total and does not say why, the natural conclusion is that the
strategy is wrong, or that the engine lied - when the real cause is a fill
rule neither side ever stated. So every script this module emits carries
its execution contract in the header, and the list of known differences
underneath it. A user comparing the two numbers can then tell "these
engines differ as documented" apart from "this strategy does not work".

THE EXECUTION CONTRACT THIS LAB ACTUALLY USES

  1. A signal is computed from bar i's CLOSE.
  2. It is filled at bar i+1's CLOSE. Never bar i - a signal computed from
     a close cannot be filled at that same close, and pretending otherwise
     is the single most common way a backtest invents an edge.
  3. One position at a time, long only, full size.
  4. The round-trip fee is charged once on exit, as a percentage.
  5. NO intrabar stops or targets exist.

Point 5 is worth being explicit about, because it is where engines usually
diverge most. When a strategy carries a stop and a target that a single
bar's wick touches BOTH, the bar's open/high/low/close cannot say which was
hit first. A pessimistic engine books the stop; TradingView estimates the
path from the bar's shape and can book the target. That single assumption
compounds into large divergences.

This lab is not on either side of that argument, because it never takes an
intrabar stop or target at all: every entry and exit happens at a close.
That removes the ambiguity rather than resolving it, and it is the reason a
script from here uses close-to-close orders instead of stop/limit brackets.
If intrabar exits are ever added, THIS is the decision that has to be made
explicitly and stated here - not inherited by accident.

MAPPING THAT CONTRACT ONTO PINE

Pine's default is to execute an order at the NEXT bar's OPEN, which is a
different fill from ours. `process_orders_on_close=true` moves execution to
the close of the bar the order was placed on, and the entry condition is
then taken from the PREVIOUS bar's signal - so signal at i, fill at close
i+1, which is the rule above.

KNOWN REMAINING DIFFERENCES - stated, not hidden:

  * Candle source. TradingView prices a crypto pair from an exchange of its
    choosing (often Bitstamp); this lab uses Coinbase candles. Different
    venues, different prints, a small per-trade difference that compounds.
  * Fees. This lab charges one round-trip percentage on exit. Pine charges
    a commission per side. The script halves the rate across the two sides
    so the round trip matches; a rounding difference of fractions of a
    basis point per trade remains.
  * Bar alignment. Session and timezone handling can shift which bar a
    signal lands on near boundaries.
  * Warm-up. An indicator's first valid bar can differ by one where Pine
    seeds a series differently from the plain implementations here.

None of these are corrections to apply. They are the reason two honest
engines print two different numbers for one strategy.
"""

import json

# Each entry maps a lab strategy to the Pine that reproduces its LONG
# condition. `cond` is a Pine boolean expression over the declared inputs,
# and it must be the same rule as the Python in strategy_lab.py - if one
# changes, the other is wrong, and test_pine_export.py checks that both
# sides are present for every exportable strategy.
PINE_BODIES = {
    "sma_cross": {
        "inputs": [("fast", "int"), ("slow", "int")],
        "calc": "f = ta.sma(close, fast)\ns = ta.sma(close, slow)",
        "cond": "f > s",
        "desc": "long while the fast SMA is above the slow SMA",
    },
    "ema_cross": {
        "inputs": [("fast", "int"), ("slow", "int")],
        "calc": "f = ta.ema(close, fast)\ns = ta.ema(close, slow)",
        "cond": "f > s",
        "desc": "long while the fast EMA is above the slow EMA",
    },
    "macd": {
        "inputs": [("fast", "int"), ("slow", "int"), ("signal", "int")],
        "calc": ("[macdLine, signalLine, _h] = ta.macd(close, fast, slow, signal)"),
        "cond": "macdLine > signalLine",
        "desc": "long while MACD is above its signal line",
    },
    "rsi_reversion": {
        "inputs": [("period", "int"), ("oversold", "int"), ("overbought", "int")],
        "calc": "r = ta.rsi(close, period)",
        "cond": "r < oversold",
        "exit_cond": "r > overbought",
        "desc": "buy oversold RSI, exit overbought",
    },
    "bollinger_breakout": {
        "inputs": [("period", "int"), ("mult", "float")],
        "calc": ("basis = ta.sma(close, period)\ndev = mult * ta.stdev(close, period)\n"
                 "upper = basis + dev"),
        "cond": "close > upper",
        "exit_cond": "close < basis",
        "desc": "long above the upper Bollinger band, exit back at the basis",
    },
    "bollinger_reversion": {
        "inputs": [("period", "int"), ("mult", "float")],
        "calc": ("basis = ta.sma(close, period)\ndev = mult * ta.stdev(close, period)\n"
                 "lower = basis - dev"),
        "cond": "close < lower",
        "exit_cond": "close > basis",
        "desc": "buy the lower Bollinger band, exit back at the basis",
    },
    "donchian_breakout": {
        "inputs": [("period", "int"), ("exit_period", "int")],
        "calc": ("hh = ta.highest(high, period)[1]\nll = ta.lowest(low, exit_period)[1]"),
        "cond": "close > hh",
        "exit_cond": "close < ll",
        "desc": "buy a new N-bar high, exit on an M-bar low",
    },
    "momentum": {
        "inputs": [("lookback", "int"), ("threshold", "float")],
        "calc": "mom = close / close[lookback] - 1",
        "cond": "mom > threshold",
        "desc": "long while N-bar momentum is above a threshold",
    },
    "price_vs_sma": {
        "inputs": [("period", "int"), ("buffer_pct", "float")],
        "calc": "m = ta.sma(close, period)",
        "cond": "close > m * (1 + buffer_pct)",
        "desc": "long while price is above its SMA by a buffer",
    },
    "atr_breakout": {
        "inputs": [("period", "int"), ("mult", "float")],
        # NOT ta.atr(). Pine's ta.atr is Wilder's RMA smoothing; this lab's
        # atr() is a plain MEAN of true range. Exporting ta.atr would have
        # been a different indicator under the same name - the precise thing
        # this module's docstring says not to do - and on a 14-period ATR the
        # two diverge enough to move trades, not just decimals.
        "calc": ("a = ta.sma(ta.tr(true), period)\nref = close[1]"),
        "cond": "close > ref + mult * a",
        "exit_cond": "close < ref - mult * a",
        "desc": "long on an ATR-sized upward break (ATR = simple mean of true range)",
    },
    "supertrend": {
        "inputs": [("period", "int"), ("mult", "float")],
        # Pine HAS a ta.supertrend, and it is not this. ta.supertrend builds
        # bands off the median price and locks them; this lab's strat_supertrend
        # is a trailing-stop rule: enter when close rises more than one ATR
        # above the prior close, then ratchet a stop at close - mult*ATR and
        # exit when close loses it. Those are different strategies. So this
        # one is written out longhand, with the same state the Python keeps.
        "custom": """a = ta.sma(ta.tr(true), period)
var float stopLevel = na
var bool holding = false
if holding
    stopLevel := math.max(nz(stopLevel, 0.0), close - mult * a)
    if close < stopLevel
        holding := false
        stopLevel := na
else
    if not na(close[1]) and close > close[1] + a
        holding := true
        stopLevel := close - mult * a
longCond = holding
exitCond = not holding""",
        "desc": ("long on a >1 ATR up-move, exit on a ratcheting close - mult x ATR "
                 "stop (this lab's own rule, NOT Pine's ta.supertrend)"),
    },
    "buy_and_hold": {
        "inputs": [],
        "calc": "",
        "cond": "true",
        "desc": "the control: buy once, hold, pay one fee",
    },
}


def _fmt(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def exportable(strategy: str) -> bool:
    return strategy in PINE_BODIES


def to_pine(strategy: str, params: dict, round_trip_fee_rate: float,
            coin: str = "", timeframe: str = "", oos_split_label: str = "",
            measured=None) -> str:
    """One self-contained Pine v5 strategy, with its contract in the header.

    `measured` is this lab's own result for the same variant. It is printed
    in the header so the two numbers sit side by side rather than being
    compared from memory - which is how a documented divergence gets
    mistaken for a broken strategy.
    """
    if strategy not in PINE_BODIES:
        raise ValueError(
            f"{strategy} has no Pine body. Add one to PINE_BODIES beside the "
            f"Python in strategy_lab.py, or the exported script would be a "
            f"different strategy wearing the same name.")
    spec = PINE_BODIES[strategy]
    params = params or {}

    # Pine charges commission per SIDE; this lab charges one round trip on
    # exit. Halving keeps the round trip equal, which is the figure every
    # verdict in the lab is computed against.
    per_side_pct = round(round_trip_fee_rate * 100 / 2, 6)

    lines = []
    lines.append("// " + "=" * 68)
    lines.append(f"// {strategy} {json.dumps(params, sort_keys=True)}")
    if spec["desc"]:
        lines.append(f"// {spec['desc']}")
    if coin or timeframe:
        lines.append(f"// tested on: {coin or 'unknown'} {timeframe or ''}".rstrip())
    lines.append("//")
    lines.append("// EXECUTION CONTRACT (this is what makes the numbers comparable)")
    lines.append("//   signal from bar i's CLOSE, filled at bar i+1's CLOSE")
    lines.append("//   one position at a time, long only, full equity")
    lines.append(f"//   round trip {round_trip_fee_rate * 100:.3f}% "
                 f"= {per_side_pct:.4f}% per side")
    lines.append("//   NO intrabar stop or target - every exit is at a close, so no")
    lines.append("//   bar can hit both and force a guess about which came first")
    if oos_split_label:
        lines.append(f"//   out-of-sample period begins {oos_split_label}")
    if measured:
        lines.append("//")
        lines.append("// THIS LAB MEASURED, on Coinbase candles:")
        for k in ("total_return_pct", "win_rate", "trades", "max_drawdown_pct",
                  "sharpe", "final_balance"):
            if measured.get(k) is not None:
                lines.append(f"//   {k:<20} {measured[k]}")
    lines.append("//")
    lines.append("// EXPECT A DIFFERENT NUMBER HERE, for reasons that are not errors:")
    lines.append("//   * TradingView prices this pair from its own exchange feed;")
    lines.append("//     the figures above came from Coinbase candles")
    lines.append("//   * per-side vs round-trip fee rounding")
    lines.append("//   * bar alignment and indicator warm-up can differ by a bar")
    lines.append("//   Indicators are written to match THIS lab, not to use the")
    lines.append("//   nearest built-in: ATR here is a simple mean of true range, not")
    lines.append("//   ta.atr's Wilder smoothing, and supertrend is this lab's own")
    lines.append("//   trailing-stop rule rather than ta.supertrend.")
    lines.append("// A gap of roughly 0.1% per trade is normal and compounds. Judge the")
    lines.append("// SHAPE of the equity curve, not the final figure.")
    lines.append("// " + "=" * 68)
    lines.append("")
    lines.append("//@version=5")
    # A Pine string literal is double-quoted and this build has no escape for
    # an inner double quote, so json.dumps() here emitted
    # strategy("sma_cross {"fast": 20...") - a script that will not compile.
    # The params go in as k=v, which needs no quoting at all.
    param_txt = " ".join(f"{k}={_fmt(v)}" for k, v in sorted(params.items()))
    title = f"{strategy} {param_txt}".strip() if param_txt else strategy
    assert '"' not in title, f"title would break the Pine string literal: {title!r}"
    lines.append(
        f'strategy("{title}", overlay=true, '
        f'default_qty_type=strategy.percent_of_equity, default_qty_value=100, '
        f'commission_type=strategy.commission.percent, commission_value={per_side_pct}, '
        f'process_orders_on_close=true, initial_capital=10000)')
    lines.append("")
    for name, kind in spec["inputs"]:
        v = params.get(name)
        if v is None:
            continue
        fn = "input.int" if kind == "int" else "input.float"
        lines.append(f'{name} = {fn}({_fmt(v)}, "{name}")')
    if spec.get("custom"):
        # A rule with real state cannot be expressed as one boolean, so the
        # body defines longCond/exitCond itself.
        lines.append("")
        lines.extend(spec["custom"].split("\n"))
    else:
        if spec["calc"]:
            lines.append("")
            lines.extend(spec["calc"].split("\n"))
        lines.append("")
        lines.append(f"longCond = {spec['cond']}")
        exit_cond = spec.get("exit_cond") or f"not ({spec['cond']})"
        lines.append(f"exitCond = {exit_cond}")
    lines.append("")
    lines.append("// [1] is the delay that implements the contract above: the condition")
    lines.append("// is read from the PREVIOUS bar, and process_orders_on_close fills it")
    lines.append("// at THIS bar's close. Signal at i, fill at i+1's close.")
    lines.append("if longCond[1] and strategy.position_size == 0")
    lines.append('    strategy.entry("long", strategy.long)')
    lines.append("if exitCond[1] and strategy.position_size > 0")
    lines.append('    strategy.close("long")')
    return "\n".join(lines) + "\n"
