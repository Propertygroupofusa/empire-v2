"""The Alpaca wheel - real money, so every gate is pinned here.

Owner's rules, verbatim: "never sell a put unless I have enough cash to buy
the shares if assigned, and never sell a call below my cost basis".
"""
import asyncio
import os
import tempfile
from datetime import date, timedelta
from unittest import mock

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import alpaca_wheel_bot as w
import models

TODAY = date(2026, 10, 6)
EXP = (TODAY + timedelta(days=21)).isoformat()


def _c(sym, typ, strike, bid, ask, oi=500, exp=EXP):
    return {"symbol": sym, "type": typ, "strike": strike, "expiration": exp,
            "open_interest": oi, "bid": bid, "ask": ask}


# ── pure rules ───────────────────────────────────────────────────────────

def test_collateral_gate_refuses_when_cash_short():
    ok, why = w.collateral_gate(6.0, 1, cash=599.99, options_bp=10_000, already_reserved=0)
    assert not ok and "600.00" in why


def test_collateral_gate_counts_existing_reserve():
    ok, _ = w.collateral_gate(5.0, 1, cash=611.07, options_bp=611.07, already_reserved=200)
    assert not ok


def test_collateral_gate_needs_options_buying_power_too():
    ok, why = w.collateral_gate(5.0, 1, cash=611.07, options_bp=300, already_reserved=0)
    assert not ok and "options buying power" in why


def test_unknown_balance_never_orders():
    assert not w.collateral_gate(5.0, 1, None, 1000, 0)[0]
    assert not w.collateral_gate(5.0, 1, 1000, None, 0)[0]


def test_tsla_is_unaffordable_on_this_account():
    ok, _ = w.collateral_gate(360.0, 1, cash=611.07, options_bp=611.07, already_reserved=0)
    assert not ok


def test_risk_cap_counts_open_stock_and_collateral():
    ok, why = w.risk_cap_gate(976.83, 366.0, 0.0, 500.0, 0.50)
    assert not ok and "risk cap" in why
    assert w.risk_cap_gate(976.83, 0.0, 0.0, 400.0, 0.50)[0]


def test_pick_put_targets_ten_pct_below_and_skips_bad_quotes():
    contracts = [
        _c("X_P9", "put", 9.5, 0.30, 0.34),       # above 90% of 10 -> ineligible
        _c("X_P8W", "put", 8.5, 0.05, 0.40),      # spread too wide
        _c("X_P8", "put", 8.0, 0.10, 0.12),       # good
        _c("X_P7", "put", 7.0, 0.05, 0.06),
    ]
    c, mid, why = w.pick_put(contracts, 10.0, TODAY)
    assert c["symbol"] == "X_P8" and abs(mid - 0.11) < 1e-9


def test_pick_put_respects_expiry_window():
    near = (TODAY + timedelta(days=5)).isoformat()
    far = (TODAY + timedelta(days=60)).isoformat()
    contracts = [_c("A", "put", 8.0, 0.1, 0.12, exp=near), _c("B", "put", 8.0, 0.1, 0.12, exp=far)]
    c, _, why = w.pick_put(contracts, 10.0, TODAY)
    assert c is None and "14-28" in why


def test_pick_put_rejects_thin_open_interest_and_tiny_premium():
    assert w.pick_put([_c("A", "put", 8.0, 0.10, 0.12, oi=5)], 10.0, TODAY)[0] is None
    assert w.pick_put([_c("A", "put", 8.0, 0.01, 0.02)], 10.0, TODAY)[0] is None


def test_call_never_below_cost_basis():
    # Price fell to 5; cost basis 8. A 10%-above call would be 5.50 - forbidden.
    contracts = [_c("C55", "call", 5.5, 0.30, 0.32), _c("C75", "call", 7.5, 0.10, 0.11),
                 _c("C80", "call", 8.0, 0.08, 0.09)]
    c, _, _ = w.pick_call(contracts, 5.0, 8.0, TODAY)
    assert c["symbol"] == "C80"
    only_low = [_c("C55", "call", 5.5, 0.30, 0.32)]
    assert w.pick_call(only_low, 5.0, 8.0, TODAY)[0] is None


def test_call_targets_ten_pct_above_when_above_basis():
    contracts = [_c("C10", "call", 10.0, 0.5, 0.52), _c("C11", "call", 11.0, 0.2, 0.22),
                 _c("C12", "call", 12.0, 0.1, 0.11)]
    c, _, _ = w.pick_call(contracts, 10.0, 9.0, TODAY)
    assert c["symbol"] == "C11"


def test_call_refused_without_cost_basis():
    assert w.pick_call([_c("C", "call", 12, 0.1, 0.11)], 10, None, TODAY)[0] is None


def test_take_profit_at_half_the_premium():
    assert w.take_profit_due(open_credit=20.0, contracts=1, ask=0.10)
    assert not w.take_profit_due(open_credit=20.0, contracts=1, ask=0.11)
    assert not w.take_profit_due(open_credit=20.0, contracts=1, ask=None)


def test_assigned_cost_basis_subtracts_premium():
    assert abs(w.assigned_cost_basis(8.0, 11.0, 1) - 7.89) < 1e-9


def test_option_tick_rounding():
    assert w.round_option_price(0.113) == 0.11
    assert w.round_option_price(3.12) == 3.10


# ── full cycle, broker mocked ────────────────────────────────────────────

class FakeAlpaca:
    def __init__(self, cash=611.07, obp=611.07, equity=976.83, is_open=True, level=1,
                 positions=None, contracts=None, price=10.0, orders=None):
        self.account = {"cash": str(cash), "options_buying_power": str(obp),
                        "equity": str(equity), "options_trading_level": level}
        self.is_open = is_open
        self.positions = positions or []
        self.contracts = contracts or []
        self.price = price
        self.orders = orders or {}
        self.placed = []
        self.prices = {}

    async def get(self, session, url, params=None):
        if url.endswith("/v2/clock"):
            return {"is_open": self.is_open}
        if url.endswith("/v2/account"):
            return self.account
        if url.endswith("/v2/positions"):
            return self.positions
        if "/v2/orders/" in url:
            return self.orders[url.rsplit("/", 1)[1]]
        if url.endswith("/v2/stocks/trades/latest"):
            return {"trades": {sym: {"p": self.prices.get(sym, self.price)}
                               for sym in params["symbols"].split(",")}}
        if url.endswith("/trades/latest"):
            return {"trade": {"p": self.price}}
        if url.endswith("/v2/options/contracts"):
            t = params["type"]
            return {"option_contracts": [
                {"symbol": c["symbol"], "type": c["type"], "strike_price": str(c["strike"]),
                 "underlying_symbol": c.get("underlying", "X"),
                 "expiration_date": c["expiration"], "open_interest": str(c["open_interest"])}
                for c in self.contracts if c["type"] == t]}
        if url.endswith("/options/snapshots"):
            syms = params["symbols"].split(",")
            return {"snapshots": {c["symbol"]: {"latestQuote": {"bp": c["bid"], "ap": c["ask"]}}
                                  for c in self.contracts if c["symbol"] in syms}}
        raise AssertionError(url)

    async def place(self, session, symbol, qty, side, intent, limit, tag):
        self.placed.append((symbol, qty, side, intent, limit))
        return f"ord{len(self.placed)}"


def _run(fake, setup=None, active=True, today=TODAY, cycles=1):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class FakeDT:
        @staticmethod
        def now(tz=None):
            from datetime import datetime
            return datetime(today.year, today.month, today.day, 15, 0, tzinfo=tz)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        # PATCH THE ACCESSOR, NOT A BOUND NAME. alpaca_wheel_bot now calls
        # get_session_factory()() at call time rather than holding an
        # import-time copy of database.AsyncSessionLocal - which was None
        # at import and only worked by import-order luck. The test follows
        # the module: it replaces the accessor so every call site gets this
        # test's factory, exactly as the real one gets the real factory.
        with mock.patch.object(w, "get_session_factory", lambda: factory), \
             mock.patch.object(w, "_get", fake.get), \
             mock.patch.object(w, "place_option_order", fake.place), \
             mock.patch.object(w, "datetime", FakeDT), \
             mock.patch.object(w, "_max_risk_pct", lambda: 0.50), \
             mock.patch.dict(os.environ, {"STOP_TRADING": "false"}):
            if setup:
                await setup()
            await w.set_wheel_active(active)
            out = None
            for _ in range(cycles):
                out = await w.run_wheel_cycle()
            return out, [s.to_dict() for s in await w.list_states()]

    try:
        return asyncio.run(go())
    finally:
        os.unlink(path)


def _passive_off():
    import prop_bot
    async def no():
        return False
    return mock.patch.object(prop_bot, "is_alpaca_passive_mode", no)


def test_switch_off_places_nothing():
    fake = FakeAlpaca(contracts=[_c("X_P8", "put", 5.0, 0.10, 0.12)], price=5.5)
    with _passive_off():
        out, _ = _run(fake, setup=lambda: w.set_approved("X", True), active=False)
    assert fake.placed == [] and "master switch is OFF" in out["blockers"]


def test_no_approved_ticker_places_nothing():
    fake = FakeAlpaca(contracts=[_c("X_P5", "put", 5.0, 0.10, 0.12)], price=6.0)
    with _passive_off():
        out, _ = _run(fake)
    assert fake.placed == [] and any("no approved" in b for b in out["blockers"])


def test_market_closed_places_nothing():
    fake = FakeAlpaca(is_open=False, contracts=[_c("X_P5", "put", 5.0, 0.10, 0.12)], price=6.0)
    with _passive_off():
        out, _ = _run(fake, setup=lambda: w.set_approved("X", True))
    assert fake.placed == [] and "market closed" in out["blockers"]


def test_options_not_approved_places_nothing():
    fake = FakeAlpaca(level=0, contracts=[_c("X_P5", "put", 5.0, 0.10, 0.12)], price=6.0)
    with _passive_off():
        _run(fake, setup=lambda: w.set_approved("X", True))
    assert fake.placed == []


def test_unaffordable_put_waits_with_reason():
    fake = FakeAlpaca(contracts=[_c("X_P9", "put", 9.0, 0.20, 0.22)], price=10.0)
    with _passive_off():
        _, states = _run(fake, setup=lambda: w.set_approved("X", True))
    assert fake.placed == [] and "waiting" in states[0]["last_note"]


def test_affordable_put_is_sold_and_reserves_collateral():
    fake = FakeAlpaca(cash=2000, obp=2000, equity=5000,
                      contracts=[_c("X_P5", "put", 5.0, 0.10, 0.12)], price=6.0)
    with _passive_off():
        out, states = _run(fake, setup=lambda: w.set_approved("X", True))
    assert fake.placed == [("X_P5", 1, "sell", "sell_to_open", 0.11)]
    assert states[0]["stage"] == "PUT" and out["reserved_collateral_usd"] == 500.0
    assert w.reserved_collateral_usd == 500.0


def test_risk_cap_blocks_put_even_with_cash():
    fake = FakeAlpaca(cash=2000, obp=2000, equity=1000,
                      positions=[{"symbol": "AAPL", "market_value": "400", "qty": "1"}],
                      contracts=[_c("X_P5", "put", 5.0, 0.10, 0.12)], price=6.0)
    with _passive_off():
        _, states = _run(fake, setup=lambda: w.set_approved("X", True))
    assert fake.placed == [] and "risk cap" in states[0]["last_note"]


def test_assignment_then_covered_call_above_basis():
    async def setup():
        await w.set_approved("X", True)
        async with w.get_session_factory()() as db:
            from sqlalchemy import select
            st = (await db.execute(select(models.AlpacaWheelState))).scalar_one()
            st.stage, st.contracts = "PUT", 1
            st.option_symbol, st.option_strike, st.open_credit = "X_P5", 5.0, 11.0
            await db.commit()
    fake = FakeAlpaca(cash=2000, obp=2000, equity=5000, price=4.5,
                      positions=[{"symbol": "X", "qty": "100", "market_value": "450"}],
                      contracts=[_c("X_C49", "call", 4.95, 0.20, 0.22),
                                 _c("X_C50", "call", 5.0, 0.15, 0.16)])
    with _passive_off():
        _, states = _run(fake, setup=setup)
    s = states[0]
    assert s["stage"] == "CALL" and abs(s["cost_basis"] - 4.89) < 1e-9
    # floor = max(cost basis 4.89, 4.50 * 1.10 = 4.95) -> the 4.95 strike
    assert fake.placed[0][0] == "X_C49"
    assert fake.placed[0][2:4] == ("sell", "sell_to_open")
    assert s["premium_total"] == 11.0


def test_expired_put_keeps_premium_and_returns_idle():
    async def setup():
        await w.set_approved("X", True)
        async with w.get_session_factory()() as db:
            from sqlalchemy import select
            st = (await db.execute(select(models.AlpacaWheelState))).scalar_one()
            st.stage, st.contracts = "PUT", 1
            st.option_symbol, st.option_strike, st.open_credit = "X_P5", 5.0, 11.0
            await db.commit()
    fake = FakeAlpaca(price=10.0)  # cannot afford another put on 611 cash at ~9
    with _passive_off():
        _, states = _run(fake, setup=setup)
    assert states[0]["stage"] == "IDLE" and states[0]["premium_total"] == 11.0


def test_take_profit_buys_to_close():
    async def setup():
        await w.set_approved("X", True)
        async with w.get_session_factory()() as db:
            from sqlalchemy import select
            st = (await db.execute(select(models.AlpacaWheelState))).scalar_one()
            st.stage, st.contracts = "PUT", 1
            st.option_symbol, st.option_strike, st.open_credit = "X_P5", 5.0, 20.0
            await db.commit()
    fake = FakeAlpaca(price=10.0, positions=[{"symbol": "X_P5", "qty": "-1", "market_value": "-9",
                                              "asset_class": "us_option"}],
                      contracts=[_c("X_P5", "put", 5.0, 0.08, 0.09)])
    with _passive_off():
        _run(fake, setup=setup)
    assert fake.placed == [("X_P5", 1, "buy", "buy_to_close", 0.09)]


def test_unknown_account_fields_place_nothing():
    fake = FakeAlpaca(contracts=[_c("X_P5", "put", 5.0, 0.10, 0.12)], price=6.0)
    fake.account = {"equity": "1000"}
    with _passive_off():
        _run(fake, setup=lambda: w.set_approved("X", True))
    assert fake.placed == []


def test_prop_bot_counts_wheel_collateral_against_its_cap():
    import prop_bot
    with mock.patch.object(w, "reserved_collateral_usd", 450.0), \
         mock.patch.object(prop_bot, "open_prop_positions", {}), \
         mock.patch.object(prop_bot, "MAX_RISK_PERCENT", 0.50):
        ok, why = prop_bot.check_margin_safety(5000, 800, 0, extra_open_notional=0.0)
    assert not ok and "Risk limit" in why


def test_capacity_names_the_binding_limit():
    # Today's real account: cash 611.07, ~366 of stock, 50% cap of 976.83.
    cap = w.collateral_capacity(611.07, 611.07, 976.83, 366.0, 0.0, 0.50)
    assert cap["binding"] == "risk cap"
    assert abs(cap["max_collateral"] - 122.42) < 0.01 and abs(cap["max_strike"] - 1.22) < 0.01


def test_capacity_unknown_balance_is_zero():
    assert w.collateral_capacity(None, 1, 1, 0, 0, 0.5)["max_collateral"] == 0.0


def test_candidate_fails_with_dollar_gap_when_unaffordable():
    g = w.evaluate_candidate("X", 10.0, [_c("X_P8", "put", 8.0, 0.10, 0.12)], TODAY, max_strike=1.22)
    assert not g["pass"] and "800.00" in g["reason"]
    assert g["annualized_pct"] > 0 and g["breakeven"] == 7.89


def test_candidate_passes_when_affordable():
    g = w.evaluate_candidate("X", 2.0, [_c("X_P1", "put", 1.5, 0.05, 0.06)], TODAY, max_strike=2.0)
    assert g["pass"] and g["collateral_usd"] == 150.0


def test_one_wheel_at_a_time():
    async def setup():
        await w.set_approved("X", True)
        await w.set_approved("Y", True)
        async with w.get_session_factory()() as db:
            from sqlalchemy import select
            st = (await db.execute(select(models.AlpacaWheelState).where(
                models.AlpacaWheelState.underlying == "X"))).scalar_one()
            st.stage, st.contracts = "PUT", 1
            st.option_symbol, st.option_strike, st.open_credit = "X_P5", 5.0, 11.0
            await db.commit()
    fake = FakeAlpaca(cash=5000, obp=5000, equity=20000, price=6.0,
                      positions=[{"symbol": "X_P5", "qty": "-1", "market_value": "-11",
                                  "asset_class": "us_option"}],
                      contracts=[_c("X_P5", "put", 5.0, 0.30, 0.31), _c("Y_P5", "put", 5.0, 0.10, 0.12)])
    with _passive_off():
        _, states = _run(fake, setup=setup)
    y = next(s for s in states if s["underlying"] == "Y")
    assert fake.placed == [] and "single-contract" in y["last_note"]


def test_reject_is_recorded_and_never_trades():
    fake = FakeAlpaca(cash=2000, obp=2000, equity=5000,
                      contracts=[_c("X_P5", "put", 5.0, 0.10, 0.12)], price=6.0)
    with _passive_off():
        _, states = _run(fake, setup=lambda: w.set_approved("X", False))
    assert states[0]["approved"] is False and fake.placed == []


def test_scan_grades_and_hides_rejected():
    fake = FakeAlpaca(cash=2000, obp=2000, equity=5000, price=6.0,
                      contracts=[dict(_c("A_P5", "put", 5.0, 0.10, 0.12), underlying="A"),
                                 dict(_c("B_P5", "put", 5.0, 0.10, 0.12), underlying="B")])
    out = {}

    async def setup():
        await w.set_approved("B", False)
        out.update(await w.scan_candidates())
    with _passive_off():
        _run(fake, setup=setup, active=False)
    names = [c["underlying"] for c in out["candidates"]]
    assert names == ["A"] and out["candidates"][0]["pass"]
    assert out["capacity"]["binding"] in ("cash", "options buying power", "risk cap")
    assert fake.placed == []


def test_panel_capacity_matches_the_order_gate_exactly():
    """The capacity the panel shows and the gates that block a real order
    are two functions. They must never disagree: a strike passes both
    order gates if and only if its collateral fits the shown capacity."""
    import random
    rng = random.Random(7)
    for _ in range(5000):
        cash = rng.uniform(0, 3000)
        obp = rng.uniform(0, 3000)
        equity = rng.uniform(100, 6000)
        open_n = rng.uniform(0, 2000)
        reserved = rng.choice([0.0, rng.uniform(0, 800)])
        strike = round(rng.uniform(0.5, 30), 2)
        cap = w.collateral_capacity(cash, obp, equity, open_n, reserved, 0.50)
        gates = (w.collateral_gate(strike, 1, cash, obp, reserved)[0]
                 and w.risk_cap_gate(equity, open_n, reserved, strike * 100, 0.50)[0])
        fits = strike * 100 <= cap["max_collateral"] + 0.005
        if abs(strike * 100 - cap["max_collateral"]) > 0.01:   # ignore rounding at the exact edge
            assert gates == fits, (cash, obp, equity, open_n, reserved, strike, cap)
    # Today's account: the panel's ~$122 is the gate's real ceiling.
    assert w.risk_cap_gate(976.83, 366.0, 0.0, 122.0, 0.50)[0]
    assert not w.risk_cap_gate(976.83, 366.0, 0.0, 123.0, 0.50)[0]


def test_bad_ticker_rejected():
    import pytest
    with pytest.raises(ValueError):
        asyncio.run(w.set_approved("TS LA;", True))


if __name__ == "__main__":
    import inspect, sys
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn(); print("PASS", name)
            except Exception as e:
                fails += 1; print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
