"""Shared, pure, dependency-free opening-bar breakout signal logic.

Extracted out of alpaca_selection_backtest.py so the exact same
validated real logic can be reused by BOTH the shadow-mode backtest
tool (which imports `from prop_bot import FUTURES, get_headers`) and
prop_bot.py's own live trading loop (which cannot import anything back
from alpaca_selection_backtest.py without creating a circular import).
This module has zero dependencies of its own on either file, so both
can safely import from it.

Every function here is copied verbatim from the already-validated
alpaca_selection_backtest.py implementation - no behavior change, just
a relocation so it has exactly one real, canonical implementation
instead of two copies that could drift apart.
"""

ELEPHANT_BAR_MIN_SIZE_MULTIPLE = 1.5
ELEPHANT_BAR_LOOKBACK = 10
TAIL_BAR_MIN_WICK_FRACTION = 0.6
OPENING_BAR_ENTRY_BUFFER_USD = 0.01
OPENING_BAR_SPEND_USD = 25000.0
PUSH_MIN_PULLBACK_PCT = 0.003
OPENING_BAR_MAX_ENTRIES_PER_DAY = 6


def _group_bars_by_day(bars: list) -> list:
    """Groups real bars by their own real UTC calendar date - a regular
    US market session (9:30am-4pm ET) always falls entirely within one
    real UTC date regardless of EST/EDT, so this is a safe, simple real
    trading-day boundary with no timezone-conversion library needed.
    Returns an ordered list of (date_str, day_bars) tuples, oldest real
    trading day first."""
    groups = {}
    order = []
    for b in bars:
        date_str = b["t"][:10]  # "2024-01-02T14:30:00Z" -> "2024-01-02"
        if date_str not in groups:
            groups[date_str] = []
            order.append(date_str)
        groups[date_str].append(b)
    return [(d, groups[d]) for d in order]


def _is_elephant_bar(bar: dict, preceding_bars: list) -> bool:
    """A real 'Elephant Bar' - a green (bullish) candle whose own real
    range is meaningfully larger (ELEPHANT_BAR_MIN_SIZE_MULTIPLE, 1.5x
    default) than the AVERAGE real range of the preceding real green
    bars (up to ELEPHANT_BAR_LOOKBACK, 10 default) - "sizable... larger
    and taller than the vast majority of the green bars before it," per
    the account owner's own real description. Requires at least 3 real
    preceding green bars to compare against - never guesses with too
    little real evidence."""
    if bar["c"] <= bar["o"]:
        return False
    green_preceding = [b for b in preceding_bars[-ELEPHANT_BAR_LOOKBACK:] if b["c"] > b["o"]]
    if len(green_preceding) < 3:
        return False
    avg_range = sum(b["h"] - b["l"] for b in green_preceding) / len(green_preceding)
    if avg_range <= 0:
        return False
    return (bar["h"] - bar["l"]) >= ELEPHANT_BAR_MIN_SIZE_MULTIPLE * avg_range


def _is_bottoming_tail_bar(bar: dict) -> bool:
    """A real 'bottoming tail' bar - a candle with a long real lower
    wick (rejection of the downside): at least TAIL_BAR_MIN_WICK_FRACTION
    (60% default) of the bar's own total real range sits BELOW its own
    real body."""
    total_range = bar["h"] - bar["l"]
    if total_range <= 0:
        return False
    body_low = min(bar["o"], bar["c"])
    lower_wick = body_low - bar["l"]
    return (lower_wick / total_range) >= TAIL_BAR_MIN_WICK_FRACTION


def _replay_opening_bar_breakout(session_bars: list, preceding_bars: list, spend: float = OPENING_BAR_SPEND_USD):
    """Real replay of ONE real trading day's opening-bar breakout setup -
    entry trigger = bar 1's high + $0.01, filled the instant ANY LATER
    real bar's high reaches it (scanning forward as far as needed);
    abandoned if price falls to bar 1's own low first; stop = bar 1's
    own low; exit on that STOP or a real confirmed second "push".
    Returns a real trade dict, or None if no real trade fired today."""
    if len(session_bars) < 3:
        return None
    bar1 = session_bars[0]
    is_elephant = _is_elephant_bar(bar1, preceding_bars)
    is_tail = _is_bottoming_tail_bar(bar1)
    if not (is_elephant or is_tail):
        return None

    trigger_price = bar1["h"] + OPENING_BAR_ENTRY_BUFFER_USD
    stop_price = bar1["l"]
    qualifies_as = "elephant" if is_elephant else "tail"

    entry_idx = None
    for i in range(1, len(session_bars)):
        b = session_bars[i]
        if b["l"] <= stop_price:
            return None
        if b["h"] >= trigger_price:
            entry_idx = i
            break
    if entry_idx is None:
        return None

    entry_price = trigger_price
    qty = spend / entry_price

    def _result(exit_price, exit_reason, exit_index):
        pnl_usd = qty * (exit_price - entry_price)
        return {
            "qualifies_as": qualifies_as, "entry_price": entry_price, "entry_index": entry_idx,
            "stop_price": stop_price, "exit_price": exit_price, "exit_reason": exit_reason,
            "exit_index": exit_index, "pnl_usd": round(pnl_usd, 2),
            "pnl_pct": round((exit_price - entry_price) / entry_price, 4),
        }

    peak = entry_price
    pushes = 1
    in_pullback = False
    for i in range(entry_idx + 1, len(session_bars)):
        b = session_bars[i]
        if b["l"] <= stop_price:
            return _result(stop_price, "STOP", i)
        if b["h"] > peak:
            if in_pullback:
                pushes += 1
                in_pullback = False
                if pushes >= 2:
                    return _result(b["h"], f"PUSH_{pushes}", i)
            peak = b["h"]
        elif not in_pullback and peak > 0 and (peak - b["l"]) / peak >= PUSH_MIN_PULLBACK_PCT:
            in_pullback = True

    last = session_bars[-1]
    return _result(last["c"], "SESSION_END", len(session_bars) - 1)


def _replay_one_opening_bar_leg(session_bars: list, anchor_idx: int, spend: float):
    """One real leg (entry-to-exit) of the multi-entry continuation
    replay below. The anchor bar's own high + OPENING_BAR_ENTRY_BUFFER_USD
    is the real entry trigger, its own low is the real stop; exits on
    that STOP, or the FIRST real confirmed push (not the second) - so
    the exit bar can become the next leg's own anchor.

    Returns (trade_dict_or_None, next_anchor_idx). next_anchor_idx is the
    real bar the NEXT leg's own search should start from (this leg's own
    PUSH exit bar) - or None when this leg never found a real entry (the
    level failed before triggering), ended in a real STOP (the trend
    broke down - no further real legs today), or the session ran out."""
    if anchor_idx >= len(session_bars) - 1:
        return None, None
    anchor = session_bars[anchor_idx]
    trigger_price = anchor["h"] + OPENING_BAR_ENTRY_BUFFER_USD
    stop_price = anchor["l"]

    entry_idx = None
    for i in range(anchor_idx + 1, len(session_bars)):
        b = session_bars[i]
        if b["l"] <= stop_price:
            return None, None
        if b["h"] >= trigger_price:
            entry_idx = i
            break
    if entry_idx is None:
        return None, None

    entry_price = trigger_price
    qty = spend / entry_price

    def _leg_result(exit_price, exit_reason, exit_index):
        pnl_usd = qty * (exit_price - entry_price)
        return {
            "entry_price": entry_price, "entry_index": entry_idx, "stop_price": stop_price,
            "exit_price": exit_price, "exit_reason": exit_reason, "exit_index": exit_index,
            "pnl_usd": round(pnl_usd, 2), "pnl_pct": round((exit_price - entry_price) / entry_price, 4),
        }

    peak = entry_price
    in_pullback = False
    for i in range(entry_idx + 1, len(session_bars)):
        b = session_bars[i]
        if b["l"] <= stop_price:
            return _leg_result(stop_price, "STOP", i), None
        if b["h"] > peak:
            if in_pullback:
                return _leg_result(b["h"], "PUSH", i), i
            peak = b["h"]
        elif not in_pullback and peak > 0 and (peak - b["l"]) / peak >= PUSH_MIN_PULLBACK_PCT:
            in_pullback = True

    last = session_bars[-1]
    return _leg_result(last["c"], "SESSION_END", len(session_bars) - 1), None


def _replay_opening_bar_breakout_multi_entry(session_bars: list, preceding_bars: list, spend: float = OPENING_BAR_SPEND_USD, max_entries_per_day: int = OPENING_BAR_MAX_ENTRIES_PER_DAY):
    """Real multi-entry replay of ONE real trading day - the first leg
    still requires bar 1 to genuinely qualify as a real Elephant or Tail
    bar (the "institutional footprint" that sets the day's real
    directional bias); every SUBSEQUENT leg reuses the exact same real
    trigger mechanic off the bar where the PRIOR leg exited via a real
    PUSH, so the system keeps trading the same established real trend
    instead of taking one trade and going quiet. Stops the whole day's
    chain the moment a leg exits via STOP (the real trend broke down) or
    max_entries_per_day is reached. Returns a list of real trade dicts
    (possibly empty) - each carries the shared `qualifies_as`
    (elephant/tail) and its own `leg_number` (1-indexed)."""
    trades = []
    if len(session_bars) < 3:
        return trades
    bar1 = session_bars[0]
    is_elephant = _is_elephant_bar(bar1, preceding_bars)
    is_tail = _is_bottoming_tail_bar(bar1)
    if not (is_elephant or is_tail):
        return trades
    qualifies_as = "elephant" if is_elephant else "tail"

    anchor_idx = 0
    while len(trades) < max_entries_per_day:
        trade, next_anchor_idx = _replay_one_opening_bar_leg(session_bars, anchor_idx, spend)
        if trade is None:
            break
        trade["qualifies_as"] = qualifies_as
        trade["leg_number"] = len(trades) + 1
        trades.append(trade)
        if next_anchor_idx is None:
            break
        anchor_idx = next_anchor_idx
    return trades
