"""The patient stock bot's live orders (alpaca_patient_live.py), end to end.

Run as written: python3 test_alpaca_patient_live.py

The owner, 2026-10-27: "Yes build it" - the live switch, built switched off.
Every test runs the real record (a SQLite file with the real tables) against a
fake Alpaca that keeps orders, fills, positions and cash. What these hold:
  * nothing is sent unless ALPACA_PATIENT is exactly "live" AND the old Alpaca
    bots are stopped AND the keys are set - and a blocked pass asks Alpaca
    nothing at all;
  * every order is a DAY LIMIT order: a buy at the decided close +0.2% at
    most, a sell at the target rounded UP to the cent - so no sell can fill
    under the target. No market order, no position close, anywhere;
  * a sell is first placed the trading day after the buy (no day trades);
  * a buy needs Alpaca cash for it and every buy still waiting;
  * a symbol Alpaca holds outside this book, or with an order this bot did
    not send, is never bought - the next dipping symbol takes the slot;
  * fills, part-fills, cancels, refusals and lost replies are all booked
    from Alpaca's own numbers; a buy not filled in 15 minutes is withdrawn;
  * only the process holding the lease sends orders.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import alpaca_patient as ap  # noqa: E402
import alpaca_patient_live as apl  # noqa: E402
import database  # noqa: E402
from models import AlpacaPatientOrder, AlpacaPatientPiece, TradingBotState  # noqa: E402

ET = ZoneInfo("America/New_York")
OPEN = ("new", "accepted", "partially_filled", "pending_new", "pending_cancel")
ENV = ("ALPACA_PATIENT", "STOP_TRADING", "ALPACA_STOP_TRADING", "ALPACA_API_KEY", "ALPACA_SECRET_KEY")


def et(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def day_bars(y, m, d, base=100.0, step=0.0):
    out, t = [], datetime(y, m, d, 8, 0, tzinfo=ET)
    px = base
    while t < datetime(y, m, d, 17, 0, tzinfo=ET):
        out.append((int(t.timestamp()), px, px + 1.0, px - 1.0, px + 0.5))
        px += step
        t += timedelta(minutes=30)
    return out


T0 = et(2026, 10, 27, 14, 0)          # Tuesday: the 12:30 hour is closed and settled
NEXT_DAY = et(2026, 10, 28, 10, 0)    # Wednesday morning


class FakeAlpaca:
    """Orders, fills, positions and cash, answering the paths the module uses."""

    def __init__(self):
        self.is_open = True
        self.cash = 582.26
        self.positions = {}
        self.free_override = {}
        self.orders = {}
        self.foreign_open = []
        self.calls = []
        self.refuse = None
        self.drop_answer = False

    def _free(self, sym):
        if sym in self.free_override:
            return self.free_override[sym]
        held_for_sells = sum(float(o["qty"]) - float(o["filled_qty"]) for o in self.orders.values()
                             if o["symbol"] == sym and o["side"] == "sell" and o["status"] in OPEN)
        return self.positions.get(sym, 0.0) - held_for_sells

    async def api(self, session, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/v2/clock":
            return 200, {"is_open": self.is_open}
        if method == "GET" and path == "/v2/account":
            return 200, {"cash": f"{self.cash:.2f}"}
        if method == "GET" and path == "/v2/positions":
            return 200, [{"symbol": s, "qty": repr(q), "qty_available": repr(self._free(s))}
                         for s, q in self.positions.items() if q]
        if method == "GET" and path.startswith("/v2/orders?status=open"):
            return 200, [dict(o) for o in self.orders.values() if o["status"] in OPEN] + list(self.foreign_open)
        if method == "GET" and path.startswith("/v2/orders:by_client_order_id?client_order_id="):
            o = self.orders.get(path.split("=", 1)[1])
            return (200, dict(o)) if o else (404, {"message": "order not found"})
        if method == "POST" and path == "/v2/orders":
            if self.refuse:
                status, msg = self.refuse
                self.refuse = None
                return status, {"message": msg}
            o = dict(body, id=uuid.uuid4().hex, status="new", filled_qty="0", filled_avg_price=None,
                     filled_at=None)
            self.orders[body["client_order_id"]] = o
            if self.drop_answer:
                self.drop_answer = False
                return None, None
            return 200, dict(o)
        if method == "DELETE" and path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[1]
            for o in self.orders.values():
                if o["id"] == oid and o["status"] in OPEN:
                    o["status"] = "canceled"
                    return 204, None
            return 422, {"message": "not cancelable"}
        raise AssertionError(f"unexpected Alpaca call: {method} {path}")

    def working(self, side=None):
        return [o for o in self.orders.values() if o["status"] in OPEN and (side is None or o["side"] == side)]

    def fill(self, side, symbol, qty=None, price=None, status="filled", at="2026-10-27T18:01:00Z"):
        o = next(o for o in self.working(side) if o["symbol"] == symbol)
        q = float(qty if qty is not None else o["qty"])
        px = float(price if price is not None else o["limit_price"])
        o.update(filled_qty=repr(q), filled_avg_price=repr(px), status=status, filled_at=at)
        sign = 1 if side == "buy" else -1
        self.positions[symbol] = self.positions.get(symbol, 0.0) + sign * q
        self.cash -= sign * q * px
        return o


class LiveCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved_env = {k: os.environ.get(k) for k in ENV}
        os.environ.update(ALPACA_PATIENT="live", STOP_TRADING="true", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")
        os.environ.pop("ALPACA_STOP_TRADING", None)
        self.dir = tempfile.mkdtemp()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{os.path.join(self.dir, 'live.db')}")
        self.sf = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            for t in (AlpacaPatientPiece.__table__, AlpacaPatientOrder.__table__, TradingBotState.__table__):
                await conn.run_sync(t.create)
        self.alpaca = FakeAlpaca()
        days = [(2026, 10, d) for d in (19, 20, 21, 22, 23, 26, 27, 28)]
        self.feed = {s: sum((day_bars(*d, base=200.0 - i * 3, step=-0.2) for i, d in enumerate(days)), [])
                     for s in ap.SYMBOLS}

        async def bars_30m(session, sym, now):
            return self.feed.get(sym)

        self.patches = [mock.patch.object(database, "get_session_factory", lambda: self.sf),
                        mock.patch.object(apl, "_api", self.alpaca.api),
                        mock.patch.object(ap, "bars_30m", bars_30m),
                        mock.patch.object(apl, "PRICE_READ_PAUSE", 0)]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.engine.dispose()
        shutil.rmtree(self.dir, ignore_errors=True)
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    async def pass_at(self, now):
        return await apl.run_pass(object(), now=now)

    async def pieces(self, status=None):
        async with self.sf() as db:
            q = select(AlpacaPatientPiece).where(AlpacaPatientPiece.paper == False)  # noqa: E712
            rows = (await db.execute(q.order_by(AlpacaPatientPiece.id))).scalars().all()
        return [r for r in rows if status is None or r.status == status]

    async def orders(self):
        async with self.sf() as db:
            return (await db.execute(select(AlpacaPatientOrder).order_by(AlpacaPatientOrder.id))).scalars().all()

    async def bought_and_filled(self, symbols=("SPY", "QQQ", "DIA", "IWM")):
        await self.pass_at(T0)
        for s in symbols:
            self.alpaca.fill("buy", s)
        await self.pass_at(T0 + 120)


class Switch(LiveCase):
    async def test_blocked_unless_live_and_the_old_bots_stopped_and_keys_set(self):
        self.assertIsNone(apl.blocker())
        cases = [({"ALPACA_PATIENT": "paper"}, "off"), ({"ALPACA_PATIENT": "lve"}, "off"),
                 ({"ALPACA_STOP_TRADING": "false"}, "old stock bots are running"),
                 ({"STOP_TRADING": "false"}, "old stock bots are running"),
                 ({"ALPACA_API_KEY": ""}, "keys")]
        for env, want in cases:
            with mock.patch.dict(os.environ, env):
                self.assertIn(want, apl.blocker(), env)
        with mock.patch.dict(os.environ, {"STOP_TRADING": "false", "ALPACA_STOP_TRADING": "true"}):
            self.assertIsNone(apl.blocker())        # Alpaca's own switch holds the old bots

    async def test_a_blocked_pass_asks_alpaca_nothing_and_writes_nothing(self):
        for env in ({"ALPACA_STOP_TRADING": "false"}, {"ALPACA_PATIENT": "paper"}, {"ALPACA_SECRET_KEY": ""}):
            with mock.patch.dict(os.environ, env):
                out = await self.pass_at(T0)
            self.assertIsNotNone(out["skipped"])
        self.assertEqual(self.alpaca.calls, [])
        self.assertEqual(await self.orders(), [])
        async with self.sf() as db:
            self.assertEqual((await db.execute(select(TradingBotState))).scalars().all(), [])

    async def test_market_closed_sends_nothing(self):
        self.alpaca.is_open = False
        out = await self.pass_at(T0)
        self.assertEqual(out["skipped"], "market closed")
        self.assertEqual([c for c in self.alpaca.calls if c[0] != "GET"], [])


class Buying(LiveCase):
    async def test_a_falling_market_sends_four_limit_buys_on_the_hour_that_closed(self):
        out = await self.pass_at(T0)
        buys = self.alpaca.working("buy")
        self.assertEqual([o["symbol"] for o in buys], ["SPY", "QQQ", "DIA", "IWM"])
        bucket = et(2026, 10, 27, 12, 30)
        self.assertEqual(out["bucket"], bucket)
        for o in buys:
            self.assertEqual((o["type"], o["time_in_force"], o["side"]), ("limit", "day", "buy"))
            close = next(b[4] for b in ap.hourly_buckets(self.feed[o["symbol"]], T0 - ap.SETTLE_SECONDS)
                         if b[0] == bucket)
            limit = float(o["limit_price"])
            self.assertLessEqual(limit, close * (1 + apl.BUY_LIMIT_SLACK) + 0.005)
            self.assertLessEqual(float(o["qty"]) * limit, ap.PIECE_USD)
            self.assertGreater(float(o["qty"]) * limit, ap.PIECE_USD - 0.01)
            self.assertTrue(o["client_order_id"].startswith("patient_entry-" + o["symbol"] + "-"))
        rows = await self.orders()
        self.assertEqual([(r.side, r.status) for r in rows], [("buy", "sent")] * 4)
        self.assertEqual(await self.pieces(), [])      # nothing is a piece until Alpaca fills it

    async def test_the_same_hour_is_never_decided_twice(self):
        await self.pass_at(T0)
        out = await self.pass_at(T0 + 120)
        self.assertEqual(out["skipped"], "no new closed hour")
        self.assertEqual(len(self.alpaca.orders), 4)

    async def test_a_symbol_held_outside_this_book_is_never_bought(self):
        self.alpaca.positions = {"QQQ": 0.4}          # the old bot's shares
        self.alpaca.foreign_open = [{"symbol": "DIA", "client_order_id": "entry_pass-MYM-abc", "side": "buy"}]
        out = await self.pass_at(T0)
        self.assertEqual([o["symbol"] for o in self.alpaca.working("buy")], ["SPY", "IWM", "GLD", "USO"])
        self.assertEqual(out["not_mine"], ["DIA", "QQQ"])

    async def test_cash_covers_every_buy_or_it_is_not_sent(self):
        self.alpaca.cash = 250.0
        out = await self.pass_at(T0)
        self.assertEqual([o["symbol"] for o in self.alpaca.working("buy")], ["SPY", "QQQ"])
        self.assertTrue(any("cash free" in p for p in out["problems"]))

    async def test_a_fill_becomes_a_real_piece_at_alpacas_price(self):
        await self.pass_at(T0)
        self.alpaca.fill("buy", "SPY", price=170.12)
        out = await self.pass_at(T0 + 120)
        [p] = await self.pieces()
        o = next(o for o in self.alpaca.orders.values() if o["symbol"] == "SPY")
        self.assertEqual((p.symbol, p.status, p.paper), ("SPY", "open", False))
        self.assertAlmostEqual(p.qty, float(o["qty"]))
        self.assertEqual(p.entry_price, 170.12)
        self.assertAlmostEqual(p.target_price, ap.target_price(170.12))
        self.assertAlmostEqual(p.cost_usd, float(o["qty"]) * 170.12)
        self.assertEqual(out["filled"], [("SPY", float(o["qty"]), 170.12)])
        spy = next(r for r in await self.orders() if r.symbol == "SPY")
        self.assertEqual((spy.status, spy.alpaca_status, spy.piece_id), ("done", "filled", p.id))

    async def test_a_buy_not_filled_in_fifteen_minutes_is_withdrawn_and_a_part_fill_kept(self):
        await self.pass_at(T0)
        o = next(o for o in self.alpaca.working("buy") if o["symbol"] == "SPY")
        half = float(o["qty"]) / 2
        self.alpaca.fill("buy", "SPY", qty=half, status="partially_filled")
        await self.pass_at(T0 + 600)
        self.assertEqual(len(self.alpaca.working("buy")), 4)                  # still waiting
        out = await self.pass_at(T0 + apl.BUY_WAIT_SECONDS + 1)
        self.assertEqual(sorted(out["cancelled"]), ["DIA", "IWM", "QQQ", "SPY"])
        self.assertEqual(self.alpaca.working("buy"), [])
        await self.pass_at(T0 + apl.BUY_WAIT_SECONDS + 300)
        [p] = await self.pieces()
        self.assertEqual(p.symbol, "SPY")
        self.assertAlmostEqual(p.qty, half)
        self.assertTrue(all(r.status == "done" for r in await self.orders()))

    async def test_a_refused_buy_is_closed_off_and_named(self):
        self.alpaca.refuse = (403, "insufficient buying power")
        out = await self.pass_at(T0)
        first = (await self.orders())[0]
        self.assertEqual((first.status, first.alpaca_status), ("done", "rejected"))
        self.assertIn("insufficient buying power", first.note)
        self.assertTrue(any("refused" in p for p in out["problems"]))

    async def test_a_lost_reply_is_found_again_by_its_own_id(self):
        self.alpaca.drop_answer = True
        await self.pass_at(T0)
        first = (await self.orders())[0]
        self.assertEqual(first.status, "sending")
        await self.pass_at(T0 + 60)
        first = (await self.orders())[0]
        self.assertEqual((first.status, first.alpaca_status), ("sent", "new"))

    async def test_a_send_that_never_arrived_frees_its_slot(self):
        self.alpaca.drop_answer = True
        await self.pass_at(T0)
        first = (await self.orders())[0]
        del self.alpaca.orders[first.client_order_id]
        await self.pass_at(T0 + 60)
        self.assertEqual((await self.orders())[0].status, "sending")             # too soon to say
        await self.pass_at(T0 + apl.SENDING_GRACE_SECONDS + 1)
        first = (await self.orders())[0]
        self.assertEqual((first.status, first.alpaca_status), ("done", "never_arrived"))


class Selling(LiveCase):
    async def test_never_offered_for_sale_on_the_day_it_was_bought(self):
        await self.bought_and_filled()
        for t in (T0 + 300, T0 + 3600, et(2026, 10, 27, 15, 55)):
            await self.pass_at(t)
        self.assertEqual(self.alpaca.working("sell"), [])

    async def test_the_next_day_each_piece_rests_a_limit_sell_at_its_target_rounded_up(self):
        await self.bought_and_filled()
        await self.pass_at(NEXT_DAY)
        sells = self.alpaca.working("sell")
        pieces = {p.symbol: p for p in await self.pieces()}
        self.assertEqual(sorted(o["symbol"] for o in sells), sorted(pieces))
        for o in sells:
            p = pieces[o["symbol"]]
            limit = float(o["limit_price"])
            self.assertEqual((o["type"], o["time_in_force"]), ("limit", "day"))
            self.assertGreaterEqual(limit, p.target_price)
            self.assertLess(limit, p.target_price + 0.01)
            self.assertEqual(float(o["qty"]), p.qty)
            self.assertTrue(o["client_order_id"].startswith("patient_exit-"))
        await self.pass_at(NEXT_DAY + 300)
        self.assertEqual(len(self.alpaca.working("sell")), 4)                  # one resting sell each, not two

    async def test_a_filled_sell_banks_the_profit_from_alpacas_fills(self):
        await self.bought_and_filled(("SPY",))
        await self.pass_at(NEXT_DAY)
        [p] = await self.pieces()
        o = self.alpaca.fill("sell", "SPY", at="2026-10-28T15:10:00Z")
        out = await self.pass_at(NEXT_DAY + 300)
        [p] = await self.pieces()
        exit_px = float(o["limit_price"])
        self.assertEqual((p.status, p.exit_price), ("closed", exit_px))
        self.assertAlmostEqual(p.pnl, p.qty * (exit_px - p.entry_price))
        self.assertGreater(p.pnl, 0.05 * p.cost_usd)
        self.assertEqual(out["sold"], [("SPY", round(p.pnl, 2))])

    async def test_a_part_sold_piece_splits_and_the_rest_rests_again_next_day(self):
        await self.bought_and_filled(("SPY",))
        await self.pass_at(NEXT_DAY)
        [p] = await self.pieces()
        third = p.qty / 3
        self.alpaca.fill("sell", "SPY", qty=third, status="expired", at="2026-10-28T20:00:00Z")
        await self.pass_at(et(2026, 10, 28, 16, 10))
        closed, = await self.pieces("closed")
        still, = await self.pieces("open")
        self.assertAlmostEqual(closed.qty, third)
        self.assertAlmostEqual(still.qty, p.qty - third)
        self.assertAlmostEqual(closed.qty + still.qty, p.qty)
        self.assertAlmostEqual(still.cost_usd, still.qty * p.entry_price)
        await self.pass_at(et(2026, 10, 29, 10, 0))
        [o] = self.alpaca.working("sell")
        self.assertAlmostEqual(float(o["qty"]), still.qty, places=8)

    async def test_an_expired_sell_is_placed_again_the_next_day(self):
        await self.bought_and_filled(("SPY",))
        await self.pass_at(NEXT_DAY)
        [o] = self.alpaca.working("sell")
        o["status"] = "expired"
        self.alpaca.is_open = False
        await self.pass_at(et(2026, 10, 28, 16, 10))
        self.assertEqual(self.alpaca.working("sell"), [])                      # closed: nothing re-placed
        self.assertEqual((await self.orders())[-1].alpaca_status, "expired")
        self.alpaca.is_open = True
        await self.pass_at(et(2026, 10, 29, 10, 0))
        self.assertEqual(len(self.alpaca.working("sell")), 1)

    async def test_no_sell_when_alpaca_shows_fewer_free_shares_than_the_piece(self):
        await self.bought_and_filled(("SPY",))
        self.alpaca.free_override["SPY"] = 0.01
        out = await self.pass_at(NEXT_DAY)
        self.assertEqual(self.alpaca.working("sell"), [])
        self.assertTrue(any("not sold" in p for p in out["problems"]))


class Prices(unittest.TestCase):
    def test_a_sell_limit_is_never_under_the_target(self):
        for e in (0.97, 1.0, 12.345, 47.58, 99.999, 215.08, 530.58, 701.5, 1234.56):
            t = ap.target_price(e)
            self.assertGreaterEqual(apl.sell_limit(t), t, e)
            self.assertLess(apl.sell_limit(t) - t, 0.01, e)
        self.assertEqual(apl.sell_limit(100.0), 100.0)
        self.assertEqual(apl.sell_limit(100.001), 100.01)

    def test_quantities_round_down_and_never_scientific(self):
        self.assertEqual(apl.qty_str(0.1234567899), "0.123456789")
        self.assertEqual(apl.qty_str(2.0), "2")
        self.assertEqual(apl.qty_str(1e-7), "0.0000001")
        self.assertIsNone(apl.qty_str(0))
        self.assertIsNone(apl.qty_str(1e-10))


class Lease(LiveCase):
    async def test_only_one_process_sends(self):
        now = float(T0)
        self.assertTrue(await apl.hold_lease(now))
        self.assertTrue(await apl.hold_lease(now + 60))                    # renewed by its holder
        with mock.patch.object(apl, "_ME", apl._ME + 1):
            self.assertFalse(await apl.hold_lease(now + 120))
            out = await self.pass_at(T0 + 120)
            self.assertIn("lease", out["skipped"])
            self.assertEqual(self.alpaca.calls, [])
            self.assertTrue(await apl.hold_lease(now + 60 + apl.LEASE_SECONDS + 1))   # the holder went quiet


class StatusPage(LiveCase):
    async def test_two_books_never_added_together(self):
        import routers.trading_dashboard as td
        async with self.sf() as db:
            db.add(AlpacaPatientPiece(symbol="GLD", status="closed", paper=True, qty=1.0, entry_price=100.0,
                                      cost_usd=100.0, target_price=105.2, pnl=5.0))
            db.add(AlpacaPatientPiece(symbol="SPY", status="closed", paper=False, qty=1.0, entry_price=100.0,
                                      cost_usd=100.0, target_price=105.2, pnl=5.25))
            db.add(AlpacaPatientPiece(symbol="QQQ", status="open", paper=False, qty=0.5, entry_price=200.0,
                                      cost_usd=100.0, target_price=210.4, opened_at=datetime(2026, 10, 27, 18)))
            await db.commit()
        with mock.patch.object(td, "get_session_factory", lambda: self.sf):
            page = await td.alpaca_patient_status()
        self.assertEqual((page["mode_here"], page["is_practice"], page["places_orders"]), ("live", False, True))
        self.assertEqual((page["banked_usd"], page["sales"], page["open"]), (5.0, 1, []))
        live = page["live"]
        self.assertEqual((live["banked_usd"], live["sales"], live["open_cost_usd"]), (5.25, 1, 100.0))
        self.assertEqual([p["symbol"] for p in live["open"]], ["QQQ"])
        self.assertIsNone(live["blocked_by"])
        self.assertNotIn("warning", live)
        with mock.patch.dict(os.environ, {"ALPACA_STOP_TRADING": "false"}), \
                mock.patch.object(td, "get_session_factory", lambda: self.sf):
            page = await td.alpaca_patient_status()
        self.assertFalse(page["places_orders"])
        self.assertIn("old stock bots", page["live"]["blocked_by"])
        self.assertIn("not protected", page["live"]["warning"])

    async def test_a_missing_orders_table_still_shows_the_practice_book(self):
        import routers.trading_dashboard as td
        async with self.engine.begin() as conn:
            await conn.run_sync(AlpacaPatientOrder.__table__.drop)
        with mock.patch.object(td, "get_session_factory", lambda: self.sf):
            page = await td.alpaca_patient_status()
        self.assertEqual(page["sales"], 0)
        self.assertIn("orders_unreadable", page["live"])


class Source(unittest.TestCase):
    with open("alpaca_patient_live.py") as _f:
        src = _f.read()

    def test_every_order_is_a_day_limit_order(self):
        self.assertEqual(self.src.count('"/v2/orders", body'), 1)
        self.assertIn('"type": "limit", "time_in_force": "day"', self.src)
        for bad in ('"market"', "'market'", "notional", "stop_price", '"stop"', '"stop_limit"',
                    '"trailing_stop"', "trail_percent", "trail_price", "extended_hours", '"gtc"'):
            self.assertNotIn(bad, self.src, bad)

    def test_it_never_closes_a_position(self):
        self.assertNotIn("/v2/positions/", self.src)
        deletes = [l for l in self.src.splitlines() if '"DELETE"' in l]
        self.assertEqual(len(deletes), 1)
        self.assertIn("/v2/orders/", deletes[0])

    def test_it_imports_nothing_from_the_coinbase_side(self):
        imports = [l for l in self.src.splitlines() if l.strip().startswith(("import ", "from "))]
        for l in imports:
            for bad in ("crypto", "coinbase", "grid", "coin_picker", "btc"):
                self.assertNotIn(bad, l.lower(), l)

    def test_the_practice_file_still_has_no_order_path(self):
        with open("alpaca_patient.py") as f:
            src = f.read()
        for bad in ("session.post", "/v2/orders", "submit_order", "market_order"):
            self.assertNotIn(bad, src, bad)

    def test_the_boot_hook_starts_live_only_on_live(self):
        with open("main.py") as f:
            main = f.read()
        i = main.index("import alpaca_patient")
        hook = main[i:i + 700]
        self.assertIn('elif alpaca_patient.mode() == "live":', hook)
        self.assertIn("alpaca_patient_live.run_forever()", hook)

    def test_the_dashboard_knows_the_patient_orders(self):
        with open("routers/trading_dashboard.py") as f:
            src = f.read()
        self.assertIn('"patient_entry", "patient_exit"', src)
        self.assertIn('@router.get("/alpaca-patient")', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
