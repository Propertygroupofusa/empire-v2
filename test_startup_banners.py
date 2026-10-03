"""Startup banners must describe the rules that actually run.

prop_bot's banner once claimed "long entry < 30 | short entry > 70 (trades
both directions)" while the bot bought strength (RSI > 55) and never
shorted. The swing bot claimed "Account: $980 | Daily Target: $225".
"""
import ast
import inspect

import alpaca_swing_bot
import bot_mandates
import prop_bot


def _run_source(mod):
    """Only the code of run(): log strings and calls, never its comments."""
    return ast.unparse(ast.parse(inspect.getsource(mod.run)))


def test_prop_banner_drops_the_dead_rsi_and_shorting_claims():
    src = _run_source(prop_bot)
    assert "trades both directions" not in src
    assert "RSI_BUY_BELOW" not in src and "RSI_SELL_ABOVE" not in src
    assert "Profitable days" not in src
    assert "describe_live_rules(family)" in src
    assert "get_live_strategy_family()" in src


def test_the_cycle_line_and_the_startup_line_share_one_description():
    tree = ast.parse(inspect.getsource(prop_bot))
    callers = {fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
               for node in ast.walk(fn)
               if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "describe_live_rules"}
    assert {"run", "run_prop_cycle"} <= callers, callers


def test_momentum_reads_as_buying_strength():
    prev = prop_bot.APEX_MANDATE["entry"]
    try:
        prop_bot.APEX_MANDATE["entry"] = bot_mandates.MOMENTUM_ENTRY
        entry, exit_ = prop_bot.describe_live_rules("momentum")
        assert entry.startswith("RSI > 55") and "SMA20" in entry
        assert "trailing stop" in exit_
        prop_bot.APEX_MANDATE["entry"] = bot_mandates.MEAN_REVERSION_ENTRY
        entry, exit_ = prop_bot.describe_live_rules("mean_reversion")
        assert entry.startswith("RSI < 40") and "profit target" in exit_
    finally:
        prop_bot.APEX_MANDATE["entry"] = prev


def test_swing_banner_does_not_pass_a_constant_off_as_the_balance():
    src = _run_source(alpaca_swing_bot)
    assert 'log.info(f"Account: ${ACCOUNT_SIZE' not in src
    assert "Daily Target" not in src
    assert "not the live balance" in src
