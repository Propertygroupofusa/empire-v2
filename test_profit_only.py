"""The owner's profit-only rule: nothing sells below what it cost plus Coinbase's fees.

The owner, 2026-10-19: "Only buy and sell at a gain and profit. Make sure that is
stamped in all of the bots ... the fees for Coinbase ... need to be put in there."

What these tests hold:
  * the rule itself: above zero after both fees sells, at or under zero is held,
    and anything unreadable is held; it agrees with the grid's own fee formula;
  * the grid: the stop-loss sale is off in every mode, and the last word before
    the grid's one sale is the profit check;
  * a forced close leaves a branch open when any slice would not clear;
  * the family tree, the mean-reversion bot and the legacy bot refuse a sale
    that would not clear, without touching the venue;
  * resting stops place nothing, and nothing already resting is cancelled.
"""
import asyncio
import inspect
import types
from datetime import datetime

import pytest

import auto_trim
import crypto_btc_compound_bot as engine
import crypto_coinbase_bot
import crypto_family_tree_bot as tree
import crypto_grid_bot as grid
import crypto_mean_reversion_bot as mr
import profit_only
import resting_stops_worker


def run(c):
    return asyncio.run(c)


class _Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"the venue was touched ({name}) for a sale the rule refuses")


# ------------------------------------------------------------------ the rule

def test_a_sale_above_cost_and_both_fees_is_allowed():
    ok, why = profit_only.verdict(1.0, 100.0, 103.0, 0.007)
    assert ok and "+$" in why


@pytest.mark.parametrize("price", [100.0, 100.5, 99.0])
def test_a_sale_that_does_not_clear_both_fees_is_held(price):
    ok, why = profit_only.verdict(1.0, 100.0, price, 0.007)
    assert not ok and "held, not sold" in why


def test_breakeven_after_fees_is_held():
    """A cent under breakeven is not a profit."""
    even = 100.0 * (1 + 0.0035) / (1 - 0.0035)
    assert abs(profit_only.net_after_fees(1.0, 100.0, even, 0.007)) < 1e-9
    assert profit_only.verdict(1.0, 100.0, even - 0.01, 0.007)[0] is False
    assert profit_only.verdict(1.0, 100.0, even + 0.01, 0.007)[0] is True


@pytest.mark.parametrize("q,e,p", [(None, 100, 103), (1, None, 103), (1, 100, None),
                                   (0, 100, 103), (1, 0, 103), (1, 100, 0), ("x", 100, 103)])
def test_unreadable_is_held(q, e, p):
    assert profit_only.verdict(q, e, p)[0] is False


def test_the_default_fee_errs_high():
    assert profit_only.DEFAULT_ROUND_TRIP_FEE_RATE >= grid.CONSERVATIVE_ROUND_TRIP_FEE_RATE
    assert profit_only.verdict(1.0, 100.0, 102.0)[0] is False      # +2% gross is not enough at 2.4%


def test_it_agrees_with_the_grids_own_formula():
    for q, e, p, r in [(0.5, 80_000, 81_500, 0.007), (12.0, 0.73, 0.75, 0.012), (3, 10, 9, 0.024)]:
        assert profit_only.net_after_fees(q, e, p, r) == pytest.approx(grid._grid_slice_net_pnl(q, e, p, r))


def test_net_verdict():
    assert profit_only.net_verdict(0.01)[0] is True
    assert profit_only.net_verdict(0.0)[0] is False
    assert profit_only.net_verdict(-1.0)[0] is False
    assert profit_only.net_verdict(None)[0] is False


def test_there_is_no_switch():
    src = inspect.getsource(profit_only)
    assert "os.getenv" not in src and "environ" not in src
    assert profit_only.STOP_ORDERS_ALLOWED is False


# ------------------------------------------------------------------ the grid

def test_the_stop_loss_sale_is_off_in_every_mode():
    src = inspect.getsource(grid.run_grid_branch_cycle)
    pick = src.index("_stop_slice = _stop_candidate")
    at = src.index("\n    _stop_slice = None\n", pick)
    assert at < src.index("if _stop_slice is not None or _parked_sell or _rise_hit:")
    assert "if _owner_sell_only or _owner_bounce:\n        _stop_slice = None" not in src


def test_the_profit_check_is_the_last_word_before_the_grid_sale():
    src = inspect.getsource(grid.run_grid_branch_cycle)
    sale = src.index("fill = await grid_sell(session, oldest.qty")
    check = src.rindex("profit_only.verdict(", 0, sale)
    assert "sell_basis_for_slice(oldest" in src[check:sale]
    between = src[check:sale]
    assert "return" in between and "await " not in between.replace("await _record_gate_decision", "")
    assert src.count("await grid_sell(") == 1


def _slice(qty, entry):
    return types.SimpleNamespace(id=1, qty=qty, entry_price=entry, adopted=False,
                                 entry_fee_rate=0.0035)


def test_a_forced_close_leaves_a_losing_branch_open(monkeypatch):
    b = types.SimpleNamespace(bot_name="crypto_grid_x", product_id="TIA-USD")

    async def branches():
        return [b]

    async def slices(name):
        return [_slice(20.0, 0.70), _slice(20.0, 0.60)]       # the first is under water at 0.66

    async def rate():
        return 0.007

    async def maker():
        return False

    async def bid(session, pid):
        return 0.66, 0.6601

    async def no_sale(*a, **k):
        raise AssertionError("a forced close sold at a loss")

    monkeypatch.setattr(grid, "get_grid_branches", branches)
    monkeypatch.setattr(grid, "get_grid_slices", slices)
    monkeypatch.setattr(grid, "get_effective_round_trip_fee_rate", rate)
    monkeypatch.setattr(grid, "is_maker_orders_active", maker)
    monkeypatch.setattr(engine, "get_best_bid_ask", bid)
    monkeypatch.setattr(engine, "place_market_sell", no_sale)
    out = run(grid.close_all_grid_slices())
    rows = out.get("results") or out.get("branches") or []
    assert any(r.get("sold") is False and "profit-only" in (r.get("reason") or "") for r in rows), out


# ------------------------------------------------------------- the other bots

def test_family_tree_refuses_a_losing_sale_without_selling(monkeypatch):
    async def bid(session, pid):
        return 90.0, 90.1

    async def no_sale(*a, **k):
        raise AssertionError("the family tree sold at a loss")
    monkeypatch.setattr(engine, "get_best_bid_ask", bid)
    monkeypatch.setattr(engine, "place_market_sell", no_sale)
    pos = types.SimpleNamespace(qty=1.0, entry_price=100.0)
    assert run(tree._branch_sell_and_settle(object(), "crypto_tree_x", "SOL-USD", pos, "STOP HIT")) is False


def test_family_tree_retirement_is_refused_whole(monkeypatch):
    src = inspect.getsource(tree)
    check = src.index("retirement refused")
    first_sale = src.index("fill = await engine.place_market_sell(session, pos.qty, b.product_id)")
    assert check < first_sale


def test_mean_reversion_refuses_a_losing_exit(monkeypatch):
    async def no_sale(*a, **k):
        raise AssertionError("the mean-reversion bot sold at a loss")
    monkeypatch.setattr(engine, "place_market_sell", no_sale)
    e = mr.MeanReversionEngine()
    e.positions["ETH-USD"] = mr.MeanReversionPosition("ETH-USD", 4000.0, 0.01, datetime.utcnow(), 25.0, 50.0)
    assert run(e.exit_position(None, "ETH-USD", 3900.0, "stop_loss_hit")) is False


def test_legacy_bot_refuses_a_losing_sell_and_one_with_no_entry():
    s = _Untouchable()
    assert run(crypto_coinbase_bot.place_order(s, "SOL/USD", "sell", 1.0, 99.0, entry_price=100.0)) is False
    assert run(crypto_coinbase_bot.place_order(s, "SOL/USD", "sell", 1.0, 150.0)) is False


def test_legacy_bot_passes_its_entry_on_every_sell():
    src = inspect.getsource(crypto_coinbase_bot)
    sells = [l for l in src.splitlines() if 'place_order(session, symbol, "sell"' in l]
    assert sells and all("entry_price=entry" in l for l in sells)


def test_resting_stops_place_nothing_and_cancel_nothing(monkeypatch):
    monkeypatch.delenv("STOP_TRADING", raising=False)
    monkeypatch.setattr(resting_stops_worker, "is_armed", lambda: True)
    out = run(resting_stops_worker.check_once(None))
    assert out["placed"] == 0 and out["replaced"] == 0 and "profit-only" in out["detail"]
    ok, body = run(resting_stops_worker.place(_Untouchable(), "SOL-USD", {"order_configuration": {}}))
    assert ok is False and "profit-only" in body["error_response"]["message"]


def test_auto_trim_already_requires_a_profit():
    assert auto_trim.REQUIRE_PROFIT is True
