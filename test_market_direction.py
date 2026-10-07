"""No bot opens a position betting against one already held (2026-10-07:
the account held SH, DOG and RWM - each rises when the market falls -
alongside AAPL, so they cancelled and only the spread was paid)."""
import ast
import inspect

import market_direction as md


def test_today_real_mix_is_caught():
    assert md.opposing_holdings("SH", ["AAPL"]) == ["AAPL"]
    assert md.opposing_holdings("AAPL", ["DOG", "RWM", "SH"]) == ["DOG", "RWM", "SH"]
    assert md.opposing_holdings("QQQ", ["SH", "USO"]) == ["SH"]


def test_same_direction_is_allowed():
    assert md.opposing_holdings("META", ["AAPL", "QQQ"]) == []
    assert md.opposing_holdings("RWM", ["SH", "DOG"]) == []


def test_commodities_are_neutral_both_ways():
    assert md.opposing_holdings("USO", ["SH", "AAPL"]) == []
    assert md.opposing_holdings("GLD", ["DOG"]) == []
    assert md.opposing_holdings("SH", ["USO", "GLD", "SLV"]) == []


def test_unknown_ticker_never_blocks():
    assert md.direction("ZZZZ") is None
    assert md.opposing_holdings("ZZZZ", ["SH"]) == []


def test_case_insensitive():
    assert md.opposing_holdings("sh", ["aapl"]) == ["AAPL"]


def test_every_prop_bot_symbol_is_classified_on_purpose():
    import prop_bot
    tickers = {c["symbol"] for c in prop_bot.FUTURES.values()}
    neutral = {"GLD", "USO", "SLV"}
    for t in tickers - neutral:
        assert md.direction(t) is not None, f"{t} trades but has no market direction"


def test_prop_bot_try_open_checks_it_and_logs_the_refusal():
    import prop_bot
    src = inspect.getsource(prop_bot.run_prop_cycle)
    assert "market_direction.opposing_holdings" in src
    assert '"opposing_position"' in src


def test_swing_bot_checks_it_in_both_entry_loops():
    import alpaca_swing_bot
    for fn in (alpaca_swing_bot.run_swing_check, alpaca_swing_bot.run_intraday_check):
        src = inspect.getsource(fn)
        assert "market_direction.opposing_holdings" in src, fn.__name__
        assert "held_now.append(proxy)" in src, fn.__name__


if __name__ == "__main__":
    import sys
    fails = 0
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            try:
                f(); print("PASS", n)
            except Exception as e:
                fails += 1; print("FAIL", n, repr(e))
    sys.exit(1 if fails else 0)
