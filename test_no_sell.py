"""NO_SELL_COINS: coins nothing in this service may sell. BTC by default.

The owner, 2026-10-19, after the grid sold a BTC slice at its profit target
under GRID_SELL_ONLY: "Yes, leave BTC out of selling."

What these tests hold:
  * the list reads BTC when the variable is unset or blank, "none" turns it
    off, and it matches on the coin, so every BTC market counts;
  * the engine's market and maker legs refuse a listed SELL before anything
    reaches the venue, and still send buys and unlisted sells;
  * the grid stops at the list before it picks a slice or names a sale;
  * auto-trim, resting stops, the legacy bot and the manual sell endpoints
    refuse a listed coin without touching the venue;
  * /status reports the list.
"""
import asyncio
import inspect

import pytest
from fastapi import HTTPException

# Imported before routers.*, as test_stop_means_stop does: importing the
# dashboard can put another checkout on sys.path in a development sandbox.
import auto_trim_worker
import crypto_btc_compound_bot as engine
import crypto_coinbase_bot
import crypto_grid_bot as grid
import no_sell
import resting_stops_worker


def run(c):
    return asyncio.run(c)


class _Untouchable:
    """Any attribute read fails the test: nothing may reach the venue."""
    def __getattr__(self, name):
        raise AssertionError(f"the venue was touched ({name}) for a coin on the no-sell list")


class _Reached(Exception):
    pass


class _RecordingSession:
    """Records that an order was posted, then fails it - enough to prove the
    guard let the order through without sending anything real."""
    def __init__(self):
        self.posted = []

    def post(self, url, **kw):
        self.posted.append(kw.get("json"))
        raise _Reached()


@pytest.fixture(autouse=True)
def default_list(monkeypatch):
    monkeypatch.delenv("NO_SELL_COINS", raising=False)


# ------------------------------------------------------------------- the list

@pytest.mark.parametrize("value,expected", [
    (None, ("BTC",)), ("", ("BTC",)), ("   ", ("BTC",)),
    ("none", ()), ("OFF", ()), ("false", ()), ("0", ()), ("no", ()),
    ("BTC", ("BTC",)), ("btc-usd", ("BTC",)), ("BTC, ETH", ("BTC", "ETH")),
    ("BTC-USD;eth/usd;BTC", ("BTC", "ETH")), (",", ("BTC",)), ("-", ("BTC",))])
def test_the_list(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("NO_SELL_COINS", raising=False)
    else:
        monkeypatch.setenv("NO_SELL_COINS", value)
    assert no_sell.coins() == expected


@pytest.mark.parametrize("product,blocked", [
    ("BTC-USD", True), ("btc-usd", True), ("BTC/USD", True), ("BTC-USDC", True), ("BTC", True),
    ("ETH-USD", False), ("WBTC-USD", False), ("", False), (None, False)])
def test_it_matches_the_coin(product, blocked):
    assert no_sell.blocks(product) is blocked


def test_none_lets_btc_sell(monkeypatch):
    monkeypatch.setenv("NO_SELL_COINS", "none")
    assert not no_sell.blocks("BTC-USD")


# ----------------------------------------------------------------- the engine

def _order(side, product="BTC-USD"):
    return {"client_order_id": "t", "product_id": product, "side": side,
            "order_configuration": {"market_market_ioc": {"base_size": "0.0005"}}}


def test_market_leg_refuses_a_btc_sell_and_sends_nothing():
    engine._last_order_error.pop("BTC-USD", None)
    assert run(engine._place_and_confirm(_Untouchable(), "/api/v3/brokerage/orders",
                                         _order("SELL"))) is None
    assert "no-sell list" in engine._last_order_error["BTC-USD"]


def test_maker_leg_refuses_a_btc_sell_and_records_nothing_rested():
    assert run(engine._place_maker_order(_Untouchable(), _order("SELL"), 1)) is None
    assert engine._last_order_rested["BTC-USD"] is False
    assert "no-sell list" in engine._last_order_error["BTC-USD"]


@pytest.mark.parametrize("side,product", [("BUY", "BTC-USD"), ("SELL", "ETH-USD")])
def test_buys_and_other_coins_still_reach_the_venue(monkeypatch, side, product):
    monkeypatch.setattr(engine, "_auth_headers", lambda *a, **k: {})
    s = _RecordingSession()
    run(engine._place_and_confirm(s, "/api/v3/brokerage/orders", _order(side, product)))
    assert len(s.posted) == 1 and s.posted[0]["side"] == side


def test_with_the_list_off_a_btc_sell_reaches_the_venue(monkeypatch):
    monkeypatch.setenv("NO_SELL_COINS", "none")
    monkeypatch.setattr(engine, "_auth_headers", lambda *a, **k: {})
    s = _RecordingSession()
    run(engine._place_and_confirm(s, "/api/v3/brokerage/orders", _order("SELL")))
    assert len(s.posted) == 1


def test_place_market_sell_end_to_end_refuses_btc(monkeypatch):
    async def _bal(session, cur):
        return 1.0, None

    async def _rules(session, pid):
        return {"base_increment": "0.00000001", "base_min_size": "0.00000001"}
    monkeypatch.setattr(engine, "get_asset_balance", _bal)
    monkeypatch.setattr(engine, "get_product_rules", _rules)
    assert run(engine.place_market_sell(_Untouchable(), 0.0005, "BTC-USD",
                                        source="test", allow_unverified_balance=True)) is None


# ------------------------------------------------------------------- the grid

def test_the_grid_stops_at_the_list_before_any_sale_is_picked_or_sent():
    src = inspect.getsource(grid.run_grid_branch_cycle)
    guard = src.index("if no_sell.blocks(branch.product_id):")
    block = src.index("if _stop_slice is not None or _parked_sell or _rise_hit:")
    assert block < guard < src.index("_pick_profitable_slice_to_sell(slices")
    assert guard < src.index("STOP LOSS on")
    assert guard < src.index("await grid_sell(")
    # and it returns without selling: the guard's own block ends in a return
    tail = src[guard:src.index("if _stop_slice is not None:", guard)]
    assert "return" in tail and "grid_sell" not in tail


# --------------------------------------------------------- the other sell paths

def test_auto_trim_refuses_btc_without_the_venue():
    ok, payload = run(auto_trim_worker._place_market_sell(_Untouchable(), "BTC-USD", "0.0005"))
    assert ok is False and "no-sell list" in payload["error_response"]["message"]


def test_auto_trim_pass_skips_btc_before_sizing():
    src = inspect.getsource(auto_trim_worker)
    assert src.index("if no_sell.blocks(product_id):") < src.index("sell_amount.plan_sale(")


def test_resting_stop_is_not_placed_on_btc():
    ok, body = run(resting_stops_worker.place(_Untouchable(), "BTC-USD", {"order_configuration": {}}))
    assert ok is False and "no-sell list" in body["error_response"]["message"]


def test_resting_stops_skip_btc_before_any_cancel():
    src = inspect.getsource(resting_stops_worker)
    loop = src.index('for row in (watch.get("rows") or []):')
    guard = src.index('if no_sell.blocks(f"{asset}-USD"):', loop)
    assert guard < src.index("await cancel(session", loop)
    assert guard < src.index("await place(session", loop)


def test_legacy_bot_refuses_a_btc_sell():
    assert run(crypto_coinbase_bot.place_order(_Untouchable(), "BTC/USD", "sell", 0.0005, 80000.0)) is False


# ------------------------------------------------------------- manual endpoints

def test_dashboard_sell_amount_refuses_btc():
    import routers.trading_dashboard as td
    with pytest.raises(HTTPException) as e:
        run(td.sell_amount_of_holding(td.PartialSellRequest(asset="btc", usd_amount=10, confirm=True)))
    assert e.value.status_code == 403


def test_dashboard_manual_sell_refuses_btc():
    import routers.trading_dashboard as td
    with pytest.raises(HTTPException) as e:
        run(td.manual_sell_coinbase(td.CoinbaseSellRequest(symbol="BTC/USD")))
    assert e.value.status_code == 403


def test_manual_order_endpoint_refuses_a_btc_sell():
    import routers.crypto_trading as ct
    with pytest.raises(HTTPException) as e:
        run(ct.place_order(ct.OrderRequest(product_id="BTC-USD", size="0.0005", side="sell")))
    assert e.value.status_code == 403


def test_withdraw_endpoint_refuses_btc():
    import routers.crypto_trading as ct
    with pytest.raises(HTTPException) as e:
        run(ct.withdraw_all_asset(ct.WithdrawRequest(product_id="BTC-USD")))
    assert e.value.status_code == 403


def test_status_reports_the_list():
    import routers.trading_dashboard as td
    assert '"no_sell_coins": list(no_sell.coins())' in inspect.getsource(td)
