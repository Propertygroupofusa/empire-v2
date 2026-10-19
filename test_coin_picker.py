"""GRID_COIN_PICKER: ten coins, $15 pieces, each sold only at a profit after fees.

The owner, 2026-10-19: "buy and sell at a profit ... keep searching until you find
the one ... bounce around ... where don't mess up no other coins flow." Then:
"Build it", "$15".

What these tests hold:
  * off unless the switch reads exactly "true", and off touches nothing;
  * the target nets exactly 1% of cost after both fee legs, and the ranking
    replay never sells under it;
  * the picks are the coins that turned over most profitably and quickly, and
    never a coin the grid or the tree claims, or one on the no-sell list;
  * a piece sells only at or above its target and at a profit; one buy a pass,
    $15, on a 5% dip, inside every gate; unreadable claims buy nothing;
  * the picker's held coins are claimed against the grid, the tree and adoption,
    and its sales count toward the BTC dip buy's profit;
  * nothing in it can place a market order or convert USDC.
"""
import asyncio
import inspect
import types

import pytest

import coin_picker as cp
import concentration_gate
import crypto_btc_compound_bot as engine
import crypto_coin_claims as claims
import crypto_grid_bot as grid
import profit_only

NOW = 2_000_000_000.0


def run(c):
    return asyncio.run(c)


class _Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"touched ({name}) with the switch off")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(cp, "_picks", [])
    monkeypatch.setattr(cp, "_picked_at", 0.0)
    monkeypatch.setattr(cp, "_rank_task", None)
    monkeypatch.setattr(cp, "_next_pass_at", 0.0)
    monkeypatch.setattr(cp, "_said", {})
    monkeypatch.setattr(cp, "PIECE_USD", 15.0)


# ------------------------------------------------------------------- switch

@pytest.mark.parametrize("value,expected", [(None, "off"), ("", "off"), ("1", "off"),
                                            ("yes", "off"), ("live", "off"), ("true", "live"),
                                            (" TRUE ", "live"), ("paper", "paper"), (" Paper ", "paper")])
def test_only_true_is_real_money_and_paper_is_practice(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("GRID_COIN_PICKER", raising=False)
    else:
        monkeypatch.setenv("GRID_COIN_PICKER", value)
    assert cp.mode() == expected and cp.enabled() is (expected != "off")


def test_off_touches_nothing(monkeypatch):
    monkeypatch.delenv("GRID_COIN_PICKER", raising=False)
    assert run(cp.maybe_trade(_Untouchable(), now=NOW)) == {"on": False}


def test_the_owners_numbers():
    src = inspect.getsource(cp)
    assert 'env_float("GRID_COIN_PICKER_USD", 15.0)' in src
    assert (cp.DIP, cp.NET, cp.TOP_K, cp.MAX_OPEN) == (0.05, 0.01, 10, 20)


# ------------------------------------------------------------------- rules

@pytest.mark.parametrize("rate", [0.007, 0.012, 0.024])
def test_the_target_nets_one_percent_after_both_fees(rate):
    t = cp.target_price(100.0, rate)
    assert profit_only.net_after_fees(1.0, 100.0, t, rate) == pytest.approx(1.0)
    assert profit_only.verdict(1.0, 100.0, t, rate)[0] is True


def _rows(prices, start=1_700_000_000):
    """Hourly candles from a list of (open, high, low, close)."""
    return [(start + i * 3600, o, h, l, c) for i, (o, h, l, c) in enumerate(prices)]


def test_the_replay_buys_the_dip_and_sells_only_at_the_target():
    flat = [(100, 100, 100, 100)] * 30
    dip = [(100, 100, 94, 95)]                  # 6% under the 24h high: buys at 95
    stuck = [(95, 95.5, 94, 95)] * 5            # under the target: holds
    up = [(95, 97, 95, 96.5)]                   # reaches 95 x 1.0171 = 96.62? sells
    n, banked, w24 = cp.simulate(_rows(flat + dip + stuck + up))
    assert n == 1 and banked == pytest.approx(0.15, abs=0.01) and w24 == 1.0


def test_the_replay_never_sells_under_the_target():
    flat = [(100, 100, 100, 100)] * 30
    dip = [(100, 100, 94, 95)]
    fall = [(95, 95.5, 80, 81)] * 40
    assert cp.simulate(_rows(flat + dip + fall))[0] == 0


def test_choose_takes_fast_profitable_coins_only():
    scores = {"A-USD": (10, 5.0, 0.9), "B-USD": (10, 9.0, 0.5),   # slow
              "C-USD": (3, 20.0, 1.0),                            # too few
              "D-USD": (8, 7.0, 0.8), "E-USD": (6, 1.0, 0.7)}
    assert cp.choose(scores, k=2) == ["D-USD", "A-USD"]


def test_ranking_never_picks_a_claimed_or_no_sell_coin(monkeypatch):
    async def universe(session):
        return ["BTC-USD", "XRP-USD", "AAA-USD", "BBB-USD"]

    async def claimed():
        return {"XRP-USD"}

    seen = []

    async def hourly(session, pid, hours):
        seen.append(pid)
        flat = [(100, 100, 100, 100)] * 30
        return _rows((flat + [(100, 100, 94, 95), (95, 97, 95, 96.5)]) * 40)
    monkeypatch.setattr(cp, "universe", universe)
    monkeypatch.setattr(cp, "_claimed", claimed)
    monkeypatch.setattr(cp, "hourly", hourly)
    picks, scores = run(cp.rank(object()))
    assert seen == ["AAA-USD", "BBB-USD"] and set(picks) == {"AAA-USD", "BBB-USD"}


# --------------------------------------------------------------------- pass

@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("GRID_COIN_PICKER", "true")
    w = {"pieces": [], "prices": {}, "highs": {}, "claimed": set(), "usd": 3000.0, "usdc": 0.0,
         "conc": (True, "ok"), "buy_fill": (1.0, 9.5), "sell_fill": "full",
         "buys": [], "sells": [], "sales": [], "recorded": [], "feed": [], "counted": [],
         "asked_paper": []}

    async def open_pieces(paper=False):
        w["asked_paper"].append(paper)
        return list(w["pieces"])

    async def price_now(session, pid):
        return w["prices"].get(pid)

    async def high_24h(session, pid):
        return w["highs"].get(pid)

    async def claimed():
        return w["claimed"]

    async def record_buy(pid, q, px, rate, paper=False):
        w["recorded"].append((pid, q, px, rate, paper))

    async def record_sale(pid, q, px, pnl):
        w["sales"].append((pid, q, px, pnl))

    async def rate():
        return 0.007

    async def wait():
        return 1

    async def wallet_cash(session):
        return w["usd"], w["usdc"], None

    async def reserve():
        return 0.0, "none"

    async def book(*a, **k):
        return {}

    async def maker_buy(session, usd, pid, wait):
        w["buys"].append((pid, usd))
        return w["buy_fill"]

    async def maker_sell(session, qty, pid, wait):
        w["sells"].append((pid, qty))
        return (qty, w["prices"][pid]) if w["sell_fill"] == "full" else None

    async def feed(name, pid, reason, throttle=True):
        w["feed"].append((name, pid))

    async def count(ok, which):
        w["counted"].append((ok, which))

    async def forbidden(*a, **k):
        raise AssertionError("a market order or a USDC conversion")

    for name, fn in (("open_pieces", open_pieces), ("price_now", price_now), ("high_24h", high_24h),
                     ("_claimed", claimed), ("record_buy", record_buy), ("record_sale", record_sale),
                     ("_feed", feed), ("_count", count)):
        monkeypatch.setattr(cp, name, fn)
    monkeypatch.setattr(cp, "_rank_task", types.SimpleNamespace(done=lambda: False))
    monkeypatch.setattr(grid, "get_effective_round_trip_fee_rate", rate)
    monkeypatch.setattr(grid, "maker_wait_seconds", wait)
    monkeypatch.setattr(grid, "wallet_cash", wallet_cash)
    monkeypatch.setattr(grid, "unfunded_deployment_reserve", reserve)
    monkeypatch.setattr(grid, "account_market_book", book)
    monkeypatch.setattr(grid, "GRID_CASH_RESERVE_USD", 1820.0)
    monkeypatch.setattr(concentration_gate, "concentration_verdict", lambda *a, **k: w["conc"])
    monkeypatch.setattr(engine, "place_maker_buy", maker_buy)
    monkeypatch.setattr(engine, "place_maker_sell", maker_sell)
    monkeypatch.setattr(engine, "place_market_buy", forbidden)
    monkeypatch.setattr(engine, "place_market_sell", forbidden)
    if hasattr(grid, "ensure_usd_for_buy"):
        monkeypatch.setattr(grid, "ensure_usd_for_buy", forbidden)
    return w


def _piece(pid, entry, qty=1.0, rate=0.007, id_=1):
    return types.SimpleNamespace(id=id_, product_id=pid, qty=qty, entry_price=entry,
                                 target_price=cp.target_price(entry, rate))


def _pass(now=NOW):
    cp._next_pass_at = 0.0
    return run(cp.maybe_trade(object(), now=now))


def test_a_piece_sells_at_its_target_for_a_profit(world):
    world["pieces"] = [_piece("AAA-USD", 10.0)]
    world["prices"]["AAA-USD"] = cp.target_price(10.0, 0.007) + 0.01
    out = _pass()
    assert world["sells"] == [("AAA-USD", 1.0)]
    assert world["sales"][0][3] > 0 and out["sold"][0][1] > 0


def test_a_piece_under_its_target_is_held(world):
    world["pieces"] = [_piece("AAA-USD", 10.0)]
    world["prices"]["AAA-USD"] = 10.15                    # a gain, but under 10.17
    _pass()
    assert world["sells"] == [] and world["sales"] == []


def test_one_fifteen_dollar_buy_on_a_dip(world, monkeypatch):
    monkeypatch.setattr(cp, "_picks", ["AAA-USD", "BBB-USD", "CCC-USD"])
    world["pieces"] = [_piece("AAA-USD", 10.0)]           # already holds AAA
    world["prices"].update({"AAA-USD": 9.0, "BBB-USD": 9.5, "CCC-USD": 9.4})
    world["highs"].update({"AAA-USD": 10.0, "BBB-USD": 10.0, "CCC-USD": 10.0})
    out = _pass()
    assert world["buys"] == [("BBB-USD", 15.0)]           # one, and not the held coin
    assert world["recorded"][0][0] == "BBB-USD" and out["bought"][0] == "BBB-USD"


def test_no_buy_without_a_five_percent_dip(world, monkeypatch):
    monkeypatch.setattr(cp, "_picks", ["BBB-USD"])
    world["prices"]["BBB-USD"], world["highs"]["BBB-USD"] = 9.51, 10.0
    _pass()
    assert world["buys"] == []


@pytest.mark.parametrize("pid", ["XRP-USD", "BTC-USD"])
def test_never_buys_a_claimed_or_no_sell_coin(world, monkeypatch, pid):
    monkeypatch.setattr(cp, "_picks", [pid])
    world["claimed"] = {"XRP-USD"}
    world["prices"][pid], world["highs"][pid] = 9.0, 10.0
    _pass()
    assert world["buys"] == []


def test_unreadable_claims_buy_nothing(world, monkeypatch):
    async def none():
        return None
    monkeypatch.setattr(cp, "_claimed", none)
    monkeypatch.setattr(cp, "_picks", ["BBB-USD"])
    world["prices"]["BBB-USD"], world["highs"]["BBB-USD"] = 9.0, 10.0
    _pass()
    assert world["buys"] == []


def test_the_cash_floor_refuses(world, monkeypatch):
    monkeypatch.setattr(cp, "_picks", ["BBB-USD"])
    world["prices"]["BBB-USD"], world["highs"]["BBB-USD"] = 9.0, 10.0
    world["usd"] = 1830.0
    _pass()
    assert world["buys"] == [] and world["feed"][-1][0] == "CASH_RESERVE"


def test_usd_short_refuses_and_never_converts(world, monkeypatch):
    monkeypatch.setattr(cp, "_picks", ["BBB-USD"])
    world["prices"]["BBB-USD"], world["highs"]["BBB-USD"] = 9.0, 10.0
    world["usd"], world["usdc"] = 10.0, 5000.0
    _pass()
    assert world["buys"] == []


def test_the_twenty_percent_ceiling_refuses(world, monkeypatch):
    monkeypatch.setattr(cp, "_picks", ["BBB-USD"])
    world["prices"]["BBB-USD"], world["highs"]["BBB-USD"] = 9.0, 10.0
    world["conc"] = (False, "over 20%")
    _pass()
    assert world["buys"] == []


def test_at_most_twenty_coins_held(world, monkeypatch):
    monkeypatch.setattr(cp, "_picks", ["NEW-USD"])
    world["pieces"] = [_piece(f"C{i}-USD", 10.0, id_=i) for i in range(20)]
    for i in range(20):
        world["prices"][f"C{i}-USD"] = 9.0
    world["prices"]["NEW-USD"], world["highs"]["NEW-USD"] = 9.0, 10.0
    _pass()
    assert world["buys"] == []


def test_under_fifteen_dollars_buys_nothing(world, monkeypatch):
    monkeypatch.setattr(cp, "PIECE_USD", 10.0)
    monkeypatch.setattr(cp, "_picks", ["BBB-USD"])
    world["prices"]["BBB-USD"], world["highs"]["BBB-USD"] = 9.0, 10.0
    _pass()
    assert world["buys"] == []


# ------------------------------------------------------------------ practice

@pytest.fixture
def practice(world, monkeypatch):
    monkeypatch.setenv("GRID_COIN_PICKER", "paper")

    async def no_order(*a, **k):
        raise AssertionError("practice mode placed a real order")
    monkeypatch.setattr(engine, "place_maker_buy", no_order)
    monkeypatch.setattr(engine, "place_maker_sell", no_order)
    return world


def test_practice_buys_without_an_order_and_marks_it_paper(practice, monkeypatch):
    monkeypatch.setattr(cp, "_picks", ["BBB-USD"])
    practice["prices"]["BBB-USD"], practice["highs"]["BBB-USD"] = 9.0, 10.0
    out = _pass()
    pid, q, px, rate, paper = practice["recorded"][0]
    assert (pid, px, paper) == ("BBB-USD", 9.0) + (True,) and q * px == pytest.approx(15.0)
    assert practice["feed"][-1][0] == "PAPER_BUY" and out["mode"] == "paper"
    assert practice["asked_paper"] == [True]                # reads only the practice book


def test_practice_sells_at_the_target_without_an_order(practice):
    practice["pieces"] = [_piece("AAA-USD", 10.0)]
    practice["prices"]["AAA-USD"] = 12.0
    _pass()
    pid, q, px, pnl = practice["sales"][0]
    assert px == pytest.approx(cp.target_price(10.0, 0.007)) and pnl == pytest.approx(0.10)
    assert practice["feed"][-1][0] == "PAPER_SELL"


def test_every_pass_is_counted_and_a_failure_is_counted_as_one(practice, monkeypatch):
    _pass()

    async def fails(*a, **k):
        raise ValueError("crash inside the pass")
    monkeypatch.setattr(grid, "get_effective_round_trip_fee_rate", fails)
    _pass(NOW + 120)
    assert practice["counted"] == [(True, "paper"), (False, "paper")]
    assert practice["feed"][-1][0] == "ERROR"


def test_a_live_pass_reads_only_the_real_book(world):
    _pass()
    assert world["asked_paper"] == [False] and world["counted"] == [(True, "live")]


# ---------------------------------------------------------- staying out of the way

def test_held_coins_are_claimed_against_the_grid_tree_and_adoption(monkeypatch):
    async def tree():
        return {"SOL-USD"}

    async def grid_c(include_inactive=False):
        return {"XRP-USD"}

    async def picker():
        return {"LIGHTER-USD"}
    monkeypatch.setattr(claims, "tree_claimed_products", tree)
    monkeypatch.setattr(claims, "grid_claimed_products", grid_c)
    monkeypatch.setattr(claims, "picker_claimed_products", picker)
    assert run(claims.claimed_by_other(claims.GRID)) == {"SOL-USD", "LIGHTER-USD"}
    assert run(claims.claimed_by_other(claims.TREE)) == {"XRP-USD", "LIGHTER-USD"}
    assert run(claims.claimed_by_other(claims.PICKER)) == {"XRP-USD", "SOL-USD"}


def test_an_unreadable_picker_table_keeps_the_trees_claims(monkeypatch):
    async def tree():
        return {"SOL-USD"}

    async def broken():
        raise RuntimeError("no table yet")
    monkeypatch.setattr(claims, "tree_claimed_products", tree)
    monkeypatch.setattr(claims, "picker_claimed_products", broken)
    assert run(claims.claimed_by_other(claims.GRID)) == {"SOL-USD"}


def test_the_fleet_loop_runs_it_after_the_btc_dip_buy():
    src = inspect.getsource(grid)
    dip = src.index("await btc_dip_buy.maybe_buy(session)")
    pick = src.index("await coin_picker.maybe_trade(session)")
    assert dip < pick < src.index("await _resolve_maker_expiries(session)")


def test_its_sales_count_toward_the_btc_dip_buy():
    import btc_dip_buy
    assert "coin_picker.banked_since(" in inspect.getsource(btc_dip_buy.profit_since)


def test_status_and_the_picker_page_show_the_mode_and_both_books():
    import routers.trading_dashboard as td
    src = inspect.getsource(td)
    assert '"grid_coin_picker_mode"' in src and '"grid_btc_dip_buy_mode"' in src
    page = inspect.getsource(td.coin_picker_status)
    assert '"practice": book(True)' in page and '"real": book(False)' in page
    assert '"failed_passes"' in page


def test_the_module_cannot_market_trade_or_convert():
    code = inspect.getsource(cp).split('"""', 2)[2]
    for word in ("place_market_sell", "place_market_buy", "ensure_usd_for_buy", "convert_usdc"):
        assert word not in code, word


def test_the_records_round_trip_through_a_database():
    aiosqlite = pytest.importorskip("aiosqlite")  # noqa: F841
    from datetime import datetime, timedelta
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    import database
    import models

    async def go():
        eng = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with eng.begin() as c:
            await c.run_sync(models.CoinPickerPiece.__table__.create)
            await c.run_sync(models.CoinPickerPick.__table__.create)
        S = async_sessionmaker(eng, expire_on_commit=False)
        real, real_claims = database.get_session_factory, claims.get_session_factory
        database.get_session_factory = claims.get_session_factory = lambda: S
        try:
            await cp.record_buy("AAA-USD", 2.0, 10.0, 0.007)
            await cp.record_buy("PPP-USD", 1.0, 5.0, 0.007, paper=True)
            assert [x.product_id for x in await cp.open_pieces(paper=True)] == ["PPP-USD"]
            (pp,) = await cp.open_pieces(paper=True)
            await cp.record_sale(pp.id, 1.0, 5.2, 0.15)         # a practice sale
            (p,) = await cp.open_pieces()
            assert p.target_price == pytest.approx(cp.target_price(10.0, 0.007))
            await cp.record_sale(p.id, 1.5, 10.2, 0.2)          # partial: 0.5 stays open
            left = await cp.open_pieces()
            assert len(left) == 1 and left[0].qty == pytest.approx(0.5)
            assert await cp.banked_since(datetime.utcnow() - timedelta(hours=1)) == pytest.approx(0.2)
            await cp.save_picks(["AAA-USD", "BBB-USD"], {"AAA-USD": (8, 3.0, 0.9)}, NOW)
            picks, at = await cp.load_latest_picks()
            assert picks == ["AAA-USD", "BBB-USD"] and at == pytest.approx(NOW)
            assert await claims.picker_claimed_products() == {"AAA-USD"}
        finally:
            database.get_session_factory, claims.get_session_factory = real, real_claims
    run(go())
