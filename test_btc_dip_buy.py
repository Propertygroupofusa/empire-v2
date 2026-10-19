"""GRID_BTC_DIP_BUY: $15 of BTC at or under $81,000, each buy paid for by $15 of profit.

The owner, 2026-10-19: "Do $15 per buy. Only make a $15 BTC buy when we have
made $15 from all our sales ... I say 81 or 82."

What these tests hold:
  * off unless the switch reads exactly "true", and off touches nothing;
  * the rule: at or under the buy price, and only with $15 of profit banked
    since the rule switched on that earlier dip buys have not used;
  * the cash floor, the $15 minimum, the 20% ceiling and USD-only all refuse
    before any order, and USDC is never converted;
  * a fill uses its profit once, and a failed record cannot spend it twice;
  * the module has no sell path, and the fleet loop calls it after the branches.
"""
import asyncio
import inspect

import pytest

import btc_dip_buy as dip
import concentration_gate
import crypto_btc_compound_bot as engine
import crypto_grid_bot as grid

NOW = 2_000_000_000.0


def run(c):
    return asyncio.run(c)


class _Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"touched ({name}) with the switch off")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(dip, "_unsaved_spent", 0.0)
    monkeypatch.setattr(dip, "_next_pass_at", 0.0)
    monkeypatch.setattr(dip, "_said", {})
    monkeypatch.setattr(dip, "BUY_USD", 15.0)
    monkeypatch.setattr(dip, "BUY_AT_PRICE", 81000.0)


@pytest.fixture
def world(monkeypatch):
    """A switched-on pass with every read faked; nothing reaches the venue."""
    monkeypatch.setenv("GRID_BTC_DIP_BUY", "true")
    w = {"price": 80_500.0, "made": 16.0, "spent": 0.0, "usd": 3000.0, "usdc": 0.0,
         "conc": (True, "ok"), "fill": (0.000186, 80_500.0), "buys": [], "feed": [],
         "write_fails": False, "state_fails": False}

    async def read_state(now):
        if w["state_fails"]:
            raise RuntimeError("db down")
        return NOW - 86400, w["spent"]

    async def profit_since(start):
        return w["made"]

    async def add_spent(usd):
        if w["write_fails"]:
            raise RuntimeError("db down")
        w["spent"] += usd

    async def price_now(session):
        return w["price"]

    async def wallet_cash(session):
        return w["usd"], w["usdc"], None

    async def reserve():
        return 0.0, "none"

    async def book(*a, **k):
        return {"book": True}

    async def maker_buy(session, usd, pid, wait):
        w["buys"].append((pid, usd))
        return w["fill"]

    async def wait():
        return 1

    async def feed(name, reason, throttle=True):
        w["feed"].append((name, reason))

    async def no_convert(*a, **k):
        raise AssertionError("USDC was converted, or a market order sent, for the BTC dip buy")

    monkeypatch.setattr(dip, "read_state", read_state)
    monkeypatch.setattr(dip, "profit_since", profit_since)
    monkeypatch.setattr(dip, "add_spent", add_spent)
    monkeypatch.setattr(dip, "price_now", price_now)
    monkeypatch.setattr(dip, "_feed", feed)
    monkeypatch.setattr(grid, "wallet_cash", wallet_cash)
    monkeypatch.setattr(grid, "unfunded_deployment_reserve", reserve)
    monkeypatch.setattr(grid, "account_market_book", book)
    monkeypatch.setattr(grid, "maker_wait_seconds", wait)
    monkeypatch.setattr(grid, "GRID_CASH_RESERVE_USD", 1820.0)
    monkeypatch.setattr(concentration_gate, "concentration_verdict", lambda *a, **k: w["conc"])
    monkeypatch.setattr(engine, "place_maker_buy", maker_buy)
    monkeypatch.setattr(engine, "place_market_buy", no_convert)
    if hasattr(grid, "ensure_usd_for_buy"):
        monkeypatch.setattr(grid, "ensure_usd_for_buy", no_convert)
    if hasattr(engine, "convert_usdc_to_usd"):
        monkeypatch.setattr(engine, "convert_usdc_to_usd", no_convert)
    return w


def _pass(now=NOW):
    dip._next_pass_at = 0.0
    return run(dip.maybe_buy(object(), now=now))


# ------------------------------------------------------------------- switch

@pytest.mark.parametrize("value,expected", [
    (None, False), ("", False), ("1", False), ("yes", False), ("false", False),
    ("true", True), (" TRUE ", True)])
def test_only_an_explicit_true_turns_it_on(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("GRID_BTC_DIP_BUY", raising=False)
    else:
        monkeypatch.setenv("GRID_BTC_DIP_BUY", value)
    assert dip.enabled() is expected


def test_off_touches_nothing(monkeypatch):
    monkeypatch.delenv("GRID_BTC_DIP_BUY", raising=False)
    assert run(dip.maybe_buy(_Untouchable(), now=NOW))[0] is False


def test_defaults_are_the_owners_numbers():
    src = inspect.getsource(dip)
    assert 'env_float("GRID_BTC_DIP_BUY_USD", 15.0)' in src
    assert 'env_float("GRID_BTC_DIP_BUY_PRICE", 81000.0)' in src


# --------------------------------------------------------------------- rule

def test_above_the_buy_price_waits():
    ok, why = dip.verdict(81_000.01, 100.0, 0.0)
    assert not ok and "above the $81,000 buy price" in why


@pytest.mark.parametrize("price", [81_000.0, 70_000.0])
def test_at_or_under_the_price_with_profit_buys(price):
    assert dip.verdict(price, 15.0, 0.0)[0] is True


def test_needs_fifteen_dollars_of_unused_profit():
    assert dip.verdict(80_000.0, 14.99, 0.0)[0] is False
    assert dip.verdict(80_000.0, 29.0, 15.0)[0] is False      # $14 left after one buy
    assert dip.verdict(80_000.0, 30.0, 15.0)[0] is True       # $15 left


@pytest.mark.parametrize("p,made,spent", [(None, 20.0, 0.0), (0, 20.0, 0.0), ("x", 20.0, 0.0),
                                          (80_000.0, None, 0.0), (80_000.0, 20.0, None)])
def test_unreadable_is_no_buy(p, made, spent):
    assert dip.verdict(p, made, spent)[0] is False


def test_under_fifteen_dollar_size_refuses():
    assert dip.verdict(80_000.0, 100.0, 0.0, buy_usd=10.0)[0] is False


# --------------------------------------------------------------------- pass

def test_a_dip_with_profit_buys_fifteen_dollars_once(world):
    bought, msg = _pass()
    assert bought and world["buys"] == [("BTC-USD", 15.0)]
    assert world["spent"] == pytest.approx(0.000186 * 80_500.0)
    assert world["feed"][-1][0] == "BUY" and "no-sell list" in msg
    assert _pass(NOW + 120)[0] is False and len(world["buys"]) == 1    # $1 of profit left


def test_profit_pays_for_one_buy_each(world):
    world["made"] = 45.0
    world["fill"] = (0.000186, 80_645.16)             # $15.00
    for i in range(4):
        _pass(NOW + 120 * i)
    assert len(world["buys"]) == 3


def test_a_failed_record_cannot_spend_the_profit_twice(world):
    world["write_fails"] = True
    assert _pass()[0] is True
    assert _pass(NOW + 120)[0] is False and len(world["buys"]) == 1


def test_unreadable_record_is_no_buy(world):
    world["state_fails"] = True
    assert _pass()[0] is False and world["buys"] == []


def test_cash_floor_refuses(world):
    world["usd"] = 1830.0                    # $10 over the $1,820 floor
    assert _pass()[0] is False and world["buys"] == []
    assert world["feed"][-1][0] == "CASH_RESERVE"


def test_usd_short_refuses_and_never_converts_usdc(world):
    world["usd"], world["usdc"] = 10.0, 5000.0
    bought, why = _pass()
    assert not bought and world["buys"] == [] and "USDC is not converted" in why


def test_twenty_percent_ceiling_refuses(world):
    world["conc"] = (False, "BTC would be 20.4% of the account")
    assert _pass()[0] is False and world["buys"] == []
    assert world["feed"][-1][0] == "CONCENTRATION"


def test_no_fill_uses_no_profit(world):
    world["fill"] = None
    assert _pass()[0] is False and world["spent"] == 0.0
    world["fill"] = (0.000186, 80_500.0)
    assert _pass(NOW + 120)[0] is True


def test_market_read_once_a_minute(world):
    _pass()
    assert run(dip.maybe_buy(object(), now=NOW + 30))[1] == "read this minute already"


# ------------------------------------------------------------------ shape

def test_the_module_cannot_sell_or_convert():
    code = inspect.getsource(dip).split('"""', 2)[2]      # past the module docstring
    for word in ("place_market_sell", "place_maker_sell", "grid_sell", '"SELL"',
                 "place_market_buy", "ensure_usd_for_buy", "convert_usdc"):
        assert word not in code, word


def test_the_fleet_loop_runs_it_after_the_branches():
    src = inspect.getsource(grid)
    loop = src.index("await run_grid_branch_cycle(session, branch, cycle_id=_cycle_id)")
    call = src.index("await btc_dip_buy.maybe_buy(session)")
    assert loop < call < src.index("await _resolve_maker_expiries(session)")


def test_status_reports_the_switch():
    import routers.trading_dashboard as td
    assert '"grid_btc_dip_buy": (os.getenv("GRID_BTC_DIP_BUY") or "").strip().lower() == "true"' \
        in inspect.getsource(td)
