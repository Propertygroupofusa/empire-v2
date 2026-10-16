"""GRID_BOUNCE_BUYING: the grid buys only after a 2% bounce off the 24h low.

The owner, 2026-10-16: "when you see the market is not bullish and is going
down, that's when I want you to start buying. And when you can tell that the
market is bullish, that's when I want you to start selling." Selling already
works that way (a slice sells when the price rises past its profit line). The
buy side is the rule measured before this was built: wait until a coin is 2%
above its lowest price of the last 24 hours.

What these tests hold:
  * only an explicit "true" turns it on, and it wins over GRID_SELL_ONLY so
    switching it off falls back to sell-only;
  * the 2% check, and that anything unreadable means no buy (fails closed);
  * grid_buy refuses before anything reaches the venue until the bounce is
    there, and goes on as before once it is;
  * the cycle runs under STOP_TRADING, the buy branch is reachable, the
    stop-loss sale is off, and the bounce check is the last gate before any
    USDC is converted;
  * the dashboard says BOUNCE_BUYING, and the page names the wait in words.
"""
import ast
import asyncio
import inspect
import pathlib
import textwrap
import types

import pytest

import crypto_grid_bot as grid
import order_outcome
import trading_silence


def run(c):
    return asyncio.run(c)


class _Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"the venue was touched ({name}) before the bounce")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("GRID_SELL_ONLY", raising=False)
    monkeypatch.delenv("GRID_BOUNCE_BUYING", raising=False)
    grid._bounce_cache.clear()
    yield
    grid._bounce_cache.clear()


# ------------------------------------------------------------------ switch

@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("false", False), ("1", False), ("yes", False),
    ("true", True), ("TRUE", True), (" true ", True)])
def test_only_an_explicit_true_turns_it_on(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("GRID_BOUNCE_BUYING", value)
    assert grid.grid_bounce_buying() is expected


def test_bounce_wins_over_sell_only_and_falls_back_to_it(monkeypatch):
    assert grid.grid_owner_mode() == ""
    monkeypatch.setenv("GRID_SELL_ONLY", "true")
    assert grid.grid_owner_mode() == "sell_only"
    monkeypatch.setenv("GRID_BOUNCE_BUYING", "true")
    assert grid.grid_owner_mode() == "bounce"
    monkeypatch.setenv("GRID_BOUNCE_BUYING", "false")
    assert grid.grid_owner_mode() == "sell_only"


# ------------------------------------------------------------- the 2% check

def test_two_percent_off_the_low_is_a_yes_and_less_is_a_no():
    assert grid.bounce_verdict(102.0, 100.0)[0] is True
    assert grid.bounce_verdict(105.0, 100.0)[0] is True
    ok, why = grid.bounce_verdict(101.9, 100.0)
    assert ok is False and "waiting for a bounce" in why and "needs 2%" in why


@pytest.mark.parametrize("last,low", [(None, 100), (100, None), ("x", 100), (0, 100), (100, 0), (-1, 5)])
def test_anything_unreadable_is_a_no(last, low):
    ok, why = grid.bounce_verdict(last, low)
    assert ok is False and "no buy" in why


class _Resp:
    def __init__(self, status, body=None, boom=None):
        self.status, self.body, self.boom = status, body, boom

    async def __aenter__(self):
        if self.boom:
            raise self.boom
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self.body


class _Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.resp


def test_the_signal_reads_coinbase_public_24h_stats():
    s = _Session(_Resp(200, {"low": "100", "last": "103", "high": "110"}))
    ok, why = run(grid.bounce_signal(s, "XRP-USD", now=1000.0))
    assert ok is True and "3.00% above its 24h low" in why
    assert s.calls == ["https://api.exchange.coinbase.com/products/XRP-USD/stats"]


@pytest.mark.parametrize("resp", [_Resp(500), _Resp(200, {"low": "100"}),
                                  _Resp(0, boom=TimeoutError())])
def test_the_signal_fails_closed(resp):
    ok, why = run(grid.bounce_signal(_Session(resp), "XRP-USD", now=1000.0))
    assert ok is False and "no buy" in why


def test_the_signal_is_read_at_most_once_a_minute_per_coin():
    s = _Session(_Resp(200, {"low": "100", "last": "103"}))
    run(grid.bounce_signal(s, "XRP-USD", now=1000.0))
    run(grid.bounce_signal(s, "XRP-USD", now=1059.0))
    assert len(s.calls) == 1
    run(grid.bounce_signal(s, "XRP-USD", now=1061.0))
    assert len(s.calls) == 2


# ------------------------------------------------------------------ buying

def _signal(monkeypatch, ok):
    async def fake(session, product_id, now=None):
        return ok, ("bounced" if ok else "waiting for a bounce - test")
    monkeypatch.setattr(grid, "bounce_signal", fake)


def test_grid_buy_refuses_before_the_bounce_and_sends_nothing(monkeypatch):
    monkeypatch.setenv("GRID_BOUNCE_BUYING", "true")
    _signal(monkeypatch, False)
    monkeypatch.setattr(grid, "engine", _Untouchable())

    async def boom():
        raise AssertionError("grid_buy went past the bounce check")
    monkeypatch.setattr(grid, "is_maker_orders_active", boom)
    why = {}
    assert run(grid.grid_buy(_Untouchable(), 50.0, "XRP-USD", "t", outcome_out=why)) is None
    assert why["cause"] == order_outcome.NO_ORDER_CREATED
    assert "GRID_BOUNCE_BUYING" in why["detail"]


@pytest.mark.parametrize("sell_only_too", [False, True])
def test_after_the_bounce_grid_buy_goes_on_as_before(monkeypatch, sell_only_too):
    monkeypatch.setenv("GRID_BOUNCE_BUYING", "true")
    if sell_only_too:
        monkeypatch.setenv("GRID_SELL_ONLY", "true")
    _signal(monkeypatch, True)

    class Reached(Exception):
        pass

    async def reached():
        raise Reached()
    monkeypatch.setattr(grid, "is_maker_orders_active", reached)
    with pytest.raises(Reached):
        run(grid.grid_buy(None, 50.0, "XRP-USD", "t"))


# ---------------------------------------------------------------- the cycle

def _cycle_src():
    return inspect.getsource(grid.run_grid_branch_cycle)


def test_bounce_buying_lets_the_cycle_run_under_stop_trading(monkeypatch):
    monkeypatch.setenv("STOP_TRADING", "true")
    monkeypatch.setenv("GRID_BOUNCE_BUYING", "true")
    seen = []

    class _Engine:
        async def get_price_and_volatility(self, session, product_id):
            seen.append(product_id)
            return None, None
    monkeypatch.setattr(grid, "engine", _Engine())
    branch = types.SimpleNamespace(product_id="XRP-USD", bot_name="crypto_grid_test")
    assert run(grid.run_grid_branch_cycle(object(), branch)) is None
    assert seen == ["XRP-USD"]


def _cycle_tree():
    return ast.parse(textwrap.dedent(_cycle_src()))


def test_the_buy_branch_is_reachable_and_the_stop_sale_is_off():
    tree = _cycle_tree()
    # bounce does NOT make the branch sell-only - the dip buy stays reachable
    sell_only = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 and [ast.unparse(t) for t in n.targets] == ["_sell_only"]]
    assert len(sell_only) == 1
    assert ast.unparse(sell_only[0].value) == "bool(getattr(branch, 'buys_paused', False)) or _owner_sell_only"
    # and the stop-loss sale is cleared under either owner switch
    overrides = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "_owner_sell_only or _owner_bounce"]
    assert len(overrides) == 1
    assert [ast.unparse(s) for s in overrides[0].body] == ["_stop_slice = None"]


def test_the_bounce_is_the_last_gate_before_any_usdc_is_converted():
    src = _cycle_src()
    gate = src.index("        if _owner_bounce:\n            _b_ok, _b_why = await bounce_signal(")
    assert src.index("inflated_budget_hold(branch.product_id") < gate
    assert gate < src.index("_usd_ok, _usd_note = await ensure_usd_for_buy(session, spend)")
    assert gate < src.index("fill = await grid_buy(session, spend")
    assert src[gate:src.index("_usd_ok, _usd_note", gate)].count("return") == 1


# ---------------------------------------------------------------- reporting

def test_the_dashboard_says_bounce_buying():
    out = trading_silence.assess(hours_since_last_fill=3.0, branches=[], bounce=True, sell_only=True)
    assert out["verdict"] == "BOUNCE_BUYING" and out["alarm"] is False
    assert "2% above its 24-hour low" in out["reason"] and "never at a loss" in out["reason"]
    assert trading_silence.assess(hours_since_last_fill=3.0, branches=[], halted=True,
                                  bounce=True)["verdict"] == "HALTED"


def _router_tree():
    path = pathlib.Path(grid.__file__).with_name("routers").joinpath("trading_dashboard.py")
    return ast.parse(path.read_text())


def test_status_reports_the_switch():
    want = "(os.getenv('GRID_BOUNCE_BUYING') or '').strip().lower() == 'true'"
    found = [ast.unparse(v) for n in ast.walk(_router_tree()) if isinstance(n, ast.Dict)
             for k, v in zip(n.keys, n.values)
             if isinstance(k, ast.Constant) and k.value == "grid_bounce_buying"]
    assert found == [want]


def test_grid_status_is_not_halted_and_says_bounce_when_it_is_on():
    calls = [n for n in ast.walk(_router_tree()) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "trading_silence.assess"]
    assert len(calls) == 1
    kw = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kw["bounce"] == "(os.getenv('GRID_BOUNCE_BUYING') or '').strip().lower() == 'true'"
    halted = ast.parse(kw["halted"], mode="eval").body
    assert isinstance(halted, ast.BoolOp) and isinstance(halted.op, ast.And)
    assert [ast.unparse(v) for v in halted.values] == [
        "os.getenv('STOP_TRADING', 'false').lower() == 'true'",
        "(os.getenv('GRID_SELL_ONLY') or '').strip().lower() != 'true'",
        "(os.getenv('GRID_BOUNCE_BUYING') or '').strip().lower() != 'true'"]


def test_the_page_names_the_wait_in_words():
    page = pathlib.Path(grid.__file__).with_name("family_tree_dashboard.html").read_text()
    assert "WAITING_FOR_BOUNCE: 'it is waiting for the price to come back 2% off its 24-hour low" in page
