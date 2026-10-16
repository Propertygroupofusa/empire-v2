"""GRID_SELL_ONLY: the grid never buys, and sells a slice only at a profit.

The owner, 2026-10-15, with STOP_TRADING on: build the sell-only mode, so
profit is banked as prices recover without buying anything or selling
anything below cost. STOP_TRADING stays on - turning it off would restart
every other job too - and this is the one thing allowed to run under it.

What these tests hold:
  * every grid buy is refused at grid_buy(), before anything reaches the venue;
  * STOP_TRADING alone still stops the cycle before it reads a price;
  * with the switch on, the cycle runs under STOP_TRADING;
  * in the cycle, the buy branch is unreachable and the stop-loss sale is off,
    so the only exits left are the two that require a profit;
  * the dashboard says SELL_ONLY instead of HALTED or QUIET.
"""
import ast
import asyncio
import inspect
import types

import pytest

import crypto_grid_bot as grid
import order_outcome
import trading_silence


def run(c):
    return asyncio.run(c)


class _Untouchable:
    """Any attribute read fails the test: nothing may reach the venue."""
    def __getattr__(self, name):
        raise AssertionError(f"the venue was touched ({name}) while GRID_SELL_ONLY was on")


@pytest.fixture
def sell_only(monkeypatch):
    monkeypatch.setenv("GRID_SELL_ONLY", "true")


# ------------------------------------------------------------------ switch

@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("false", False), ("1", False), ("yes", False),
    ("true", True), ("TRUE", True), (" true ", True)])
def test_only_an_explicit_true_turns_it_on(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("GRID_SELL_ONLY", raising=False)
    else:
        monkeypatch.setenv("GRID_SELL_ONLY", value)
    assert grid.grid_sell_only() is expected


# ------------------------------------------------------------------- buying

def test_grid_buy_refuses_and_sends_nothing(sell_only, monkeypatch):
    monkeypatch.setattr(grid, "engine", _Untouchable())

    async def boom():
        raise AssertionError("grid_buy went past the sell-only refusal")
    monkeypatch.setattr(grid, "is_maker_orders_active", boom)
    why = {}
    assert run(grid.grid_buy(_Untouchable(), 50.0, "BTC-USD", "t", outcome_out=why)) is None
    assert why["cause"] == order_outcome.NO_ORDER_CREATED
    assert "GRID_SELL_ONLY" in why["detail"]


def test_with_the_switch_off_grid_buy_goes_on_as_before(monkeypatch):
    monkeypatch.delenv("GRID_SELL_ONLY", raising=False)

    class Reached(Exception):
        pass

    async def reached():
        raise Reached()
    monkeypatch.setattr(grid, "is_maker_orders_active", reached)
    with pytest.raises(Reached):
        run(grid.grid_buy(None, 50.0, "BTC-USD", "t"))


def test_every_grid_buy_goes_through_grid_buy():
    """The refusal lives in grid_buy, so it only covers every path if no
    grid code places a buy any other way."""
    tree = ast.parse(inspect.getsource(grid))
    outside = []

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr in ("place_maker_buy", "place_market_buy")
                    and name != "grid_buy"):
                outside.append((name, child.func.attr))
            walk(child, name)
    walk(tree, None)
    assert outside == [], f"buys placed outside grid_buy: {outside}"


# ---------------------------------------------------------------- the cycle

def _branch():
    return types.SimpleNamespace(product_id="BTC-USD", bot_name="crypto_grid_test")


def test_stop_trading_alone_still_stops_the_cycle_before_any_read(monkeypatch):
    monkeypatch.setenv("STOP_TRADING", "true")
    monkeypatch.delenv("GRID_SELL_ONLY", raising=False)
    monkeypatch.setattr(grid, "engine", _Untouchable())
    assert run(grid.run_grid_branch_cycle(_Untouchable(), _branch())) is None


def test_sell_only_lets_the_cycle_run_under_stop_trading(sell_only, monkeypatch):
    monkeypatch.setenv("STOP_TRADING", "true")
    seen = []

    class _Engine:
        async def get_price_and_volatility(self, session, product_id):
            seen.append(product_id)
            return None, None            # no price: the cycle ends right here
    monkeypatch.setattr(grid, "engine", _Engine())
    assert run(grid.run_grid_branch_cycle(object(), _branch())) is None
    assert seen == ["BTC-USD"]


def _cycle_tree():
    src = inspect.getsource(grid.run_grid_branch_cycle)
    return src, ast.parse(src)


def test_the_buy_branch_is_unreachable_in_sell_only():
    src, tree = _cycle_tree()
    assert '_sell_only = bool(getattr(branch, "buys_paused", False)) or _owner_sell_only' in src
    found = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp)
                and isinstance(node.test.op, ast.Or)
                and {getattr(v, "id", None) for v in node.test.values} == {"drawdown_breached", "_sell_only"}):
            assert node.orelse and isinstance(node.orelse[0], ast.If)
            buy_test = ast.unparse(node.orelse[0].test)
            assert "branch.reference_price * (1 - grid_pct)" in buy_test
            found = True
    assert found, "the dip-buy is no longer the elif of the sell-only check"


def test_the_stop_loss_sale_is_off_in_sell_only():
    src, tree = _cycle_tree()
    override = "    if _owner_sell_only or _owner_bounce:\n        _stop_slice = None\n"
    assert src.count(override) == 1, "the sell-only override of the stop sale is gone"
    pick = src.index("_stop_slice = _stop_candidate")
    at = src.index(override)
    # after every place a stop slice can be chosen, before anything reads it
    assert pick < at < src.index("_parked = branch_is_parked(")
    assert at < src.index("if _stop_slice is not None or _parked_sell or _rise_hit:")
    assert src.count("_stop_slice = _stop_candidate") == 1


def test_the_cycle_has_one_sale_and_it_is_profit_or_stop():
    src, _ = _cycle_tree()
    assert src.count("await grid_sell(") == 1
    assert 'exit_reason = ("stop_loss" if _stop_slice is not None' in src


def test_the_switch_is_read_once_per_cycle_before_anything_else():
    src, _ = _cycle_tree()
    gate = src.index('if os.getenv("STOP_TRADING", "false").lower() == "true" and not _owner_mode:')
    assert src.index("_owner_mode = grid_owner_mode()") < gate < src.index("engine.get_price_and_volatility(")
    assert src.index('_owner_sell_only = _owner_mode == "sell_only"') < gate


# ---------------------------------------------------------------- reporting

def test_the_dashboard_says_sell_only_not_halted_or_quiet():
    out = trading_silence.assess(hours_since_last_fill=6.0, branches=[], sell_only=True)
    assert out["verdict"] == "SELL_ONLY" and out["alarm"] is False
    assert "never at a loss" in out["reason"]
    assert trading_silence.assess(hours_since_last_fill=6.0, branches=[], halted=True)["verdict"] == "HALTED"


def test_status_and_grid_status_read_the_switch():
    import pathlib
    src = pathlib.Path(grid.__file__).with_name("routers").joinpath("trading_dashboard.py").read_text()
    assert '"grid_sell_only": (os.getenv("GRID_SELL_ONLY") or "").strip().lower() == "true"' in src
    assert 'sell_only=(os.getenv("GRID_SELL_ONLY") or "").strip().lower() == "true",' in src
