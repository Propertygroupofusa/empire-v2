"""The coin keeper (coin_keeper.py): practice only, leftovers only, never under the target.

Run as written: python3 test_coin_keeper.py

The owner, 2026-10-31: "Do the practice mode for the leftover coins." What these hold:
  * off unless COIN_KEEPER reads exactly "paper";
  * a leftover is the units no grid slice and no coin picker piece holds -
    staked units are never counted, BTC (the no-sell list) is never taken,
    and anything under the $15 floor or without a Coinbase cost is left out
    and said so;
  * each leftover gets ONE target, netting 5% of Coinbase's cost after the
    worst-case 1.2% fee; a practice sale is recorded only when the price
    reaches it, and AT it;
  * an asset sold in practice is never taken in again; a leftover that
    shrinks under its piece is closed as gone, with no profit or loss;
  * an unreadable Coinbase or claims read decides nothing;
  * the file has no order path and imports nothing from Alpaca.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, ".")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import coin_keeper as ck  # noqa: E402
import database  # noqa: E402
from models import CoinKeeperPiece, CoinPickerPiece, CryptoGridSlice  # noqa: E402

T0 = 1793500000.0


def pos(asset, units, price, cost, available=None):
    return {"asset": asset, "units": units, "available": units if available is None else available,
            "cost_per_unit": cost, "price": price}


class Rules(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(ck.SWITCH_ENV, None)

    def test_only_paper_is_on(self):
        for v, want in ((None, "off"), ("", "off"), ("paper", "paper"), (" PAPER ", "paper"),
                        ("live", "off"), ("true", "off"), ("on", "off")):
            if v is None:
                os.environ.pop(ck.SWITCH_ENV, None)
            else:
                os.environ[ck.SWITCH_ENV] = v
            self.assertEqual(ck.mode(), want, v)

    def test_the_target_nets_five_percent_after_the_worst_case_fee(self):
        t = ck.target_price(10.0)
        self.assertAlmostEqual(ck.practice_pnl(3.0, 10.0, t), 0.05 * 30.0)
        self.assertAlmostEqual(t, 10.0 * 1.05 / (1 - 0.012))

    def test_leftovers_are_the_units_no_bot_holds(self):
        positions = [pos("NEAR", 22.4, 4.43, 5.0), pos("BTC", 0.018, 77000.0, 70000.0),
                     pos("RNDR", 9.88, 3.09, 4.0), pos("FLR", 563.0, 0.006, 0.01),
                     pos("ODD", 100.0, 1.0, 0.0), pos("XRP", 813.8, 1.25, 1.4),
                     pos("SOL", 10.96, 95.0, 120.0, available=2.13), pos("USDC", 2540.0, 1.0, 1.0)]
        claims = {"NEAR": 1.936, "XRP": 813.8, "SOL": 1.96}
        keep, skipped = ck.leftovers(positions, claims, set(), {"BTC"})
        got = {k["asset"]: round(k["qty"], 6) for k in keep}
        self.assertEqual(got, {"NEAR": round(22.4 - 1.936, 6), "RNDR": 9.88, "SOL": round(2.13 - 1.96, 6)})
        reasons = dict(skipped)
        self.assertIn("never sold", reasons["BTC"])
        self.assertIn("under the $15 floor", reasons["FLR"])
        self.assertIn("no cost", reasons["ODD"])
        self.assertIn("holds every unit", reasons["XRP"])
        self.assertNotIn("USDC", reasons)

    def test_a_tree_claim_keeps_the_coin_out(self):
        keep, skipped = ck.leftovers([pos("LINK", 10.0, 11.8, 10.0)], {}, {"LINK"}, set())
        self.assertEqual(keep, [])
        self.assertIn("tree", dict(skipped)["LINK"])


class Pass(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = os.environ.get(ck.SWITCH_ENV)
        os.environ[ck.SWITCH_ENV] = "paper"
        os.environ.pop("NO_SELL_COINS", None)
        self.dir = tempfile.mkdtemp()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{os.path.join(self.dir, 'k.db')}")
        self.sf = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            for t in (CoinKeeperPiece.__table__, CoinPickerPiece.__table__, CryptoGridSlice.__table__):
                await conn.run_sync(t.create)
        async with self.sf() as db:
            db.add(CryptoGridSlice(bot_name="crypto_grid_2", product_id="NEAR-USD", qty=1.936, entry_price=6.0))
            db.add(CoinPickerPiece(product_id="RAY-USD", status="open", paper=False, qty=7.81, entry_price=2.0,
                                   cost_usd=15.6, target_price=2.1, round_trip_fee_rate=0.007))
            await db.commit()
        self.positions = [pos("NEAR", 22.4, 4.43, 5.0), pos("RNDR", 9.88, 3.09, 4.0),
                          pos("RAY", 7.81, 1.63, 2.0), pos("BTC", 0.018, 77000.0, 70000.0)]
        self.prices = {"NEAR": 4.43, "RNDR": 3.09}
        test = self

        async def positions(session):
            return test.positions

        async def price_now(session, asset):
            return test.prices.get(asset)

        async def tree_claimed():
            return set()

        self.patches = [mock.patch.object(database, "get_session_factory", lambda: self.sf),
                        mock.patch.object(ck, "positions", positions),
                        mock.patch.object(ck, "price_now", price_now),
                        mock.patch.object(ck, "tree_claimed", tree_claimed)]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        await self.engine.dispose()
        shutil.rmtree(self.dir, ignore_errors=True)
        if self.saved is None:
            os.environ.pop(ck.SWITCH_ENV, None)
        else:
            os.environ[ck.SWITCH_ENV] = self.saved

    async def rows(self):
        async with self.sf() as db:
            return {r.asset: r for r in (await db.execute(select(CoinKeeperPiece))).scalars().all()}

    async def test_the_first_pass_takes_in_the_leftovers_with_one_target_each(self):
        out = await ck.run_pass(object(), now=T0)
        rows = await self.rows()
        self.assertEqual(sorted(rows), ["NEAR", "RNDR"])               # RAY: the picker holds it; BTC: never
        self.assertAlmostEqual(rows["NEAR"].qty, 22.4 - 1.936)
        self.assertAlmostEqual(rows["NEAR"].target_price, ck.target_price(5.0))
        self.assertTrue(all(r.paper and r.status == "open" for r in rows.values()))
        self.assertEqual(sorted(a for a, _, _ in out["taken_in"]), ["NEAR", "RNDR"])
        self.assertIn("BTC", dict(out["skipped"]))

    async def test_a_practice_sale_happens_only_at_the_target_and_at_it(self):
        await ck.run_pass(object(), now=T0)
        t = ck.target_price(5.0)
        self.prices["NEAR"] = t - 0.0001
        out = await ck.run_pass(object(), now=T0 + 900)
        self.assertEqual(out["sold"], [])
        self.prices["NEAR"] = t + 1.0
        out = await ck.run_pass(object(), now=T0 + 1800)
        near = (await self.rows())["NEAR"]
        self.assertEqual((near.status, near.exit_price), ("sold", t))
        self.assertAlmostEqual(near.pnl, 0.05 * near.cost_usd)
        self.assertEqual([s[0] for s in out["sold"]], ["NEAR"])

    async def test_an_asset_sold_in_practice_is_never_taken_in_again(self):
        await ck.run_pass(object(), now=T0)
        self.prices["NEAR"] = 100.0
        await ck.run_pass(object(), now=T0 + 900)
        out = await ck.run_pass(object(), now=T0 + 1800)
        self.assertNotIn("NEAR", [a for a, _, _ in out["taken_in"]])
        async with self.sf() as db:
            n = len([r for r in (await db.execute(select(CoinKeeperPiece))).scalars().all() if r.asset == "NEAR"])
        self.assertEqual(n, 1)

    async def test_a_leftover_that_shrinks_is_gone_without_profit_or_loss(self):
        await ck.run_pass(object(), now=T0)
        async with self.sf() as db:                                     # the grid's adoption took most of it
            db.add(CryptoGridSlice(bot_name="crypto_grid_2", product_id="NEAR-USD", qty=15.0, entry_price=5.0))
            await db.commit()
        out = await ck.run_pass(object(), now=T0 + 900)
        near = (await self.rows())["NEAR"]
        self.assertEqual((near.status, near.pnl, near.exit_price), ("gone", None, None))
        self.assertEqual(out["gone"], ["NEAR"])

    async def test_an_unreadable_coinbase_decides_nothing(self):
        async def refused(session):
            return "portfolio breakdown HTTP 403"
        with mock.patch.object(ck, "positions", refused):
            out = await ck.run_pass(object(), now=T0)
        self.assertEqual(out["problems"], ["portfolio breakdown HTTP 403"])
        self.assertEqual(await self.rows(), {})

    async def test_unreadable_claims_decide_nothing(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(CryptoGridSlice.__table__.drop)
        out = await ck.run_pass(object(), now=T0)
        self.assertTrue(out["problems"] and out["problems"][0].startswith("failed"))
        self.assertEqual(await self.rows(), {})

    async def test_off_reads_nothing(self):
        os.environ[ck.SWITCH_ENV] = "live"

        async def boom(session):
            raise AssertionError("read with the switch off")
        with mock.patch.object(ck, "positions", boom):
            out = await ck.run_pass(object(), now=T0)
        self.assertEqual(out["problems"], ["off"])

    async def test_the_status_page_shows_the_book_and_what_was_left_out(self):
        import routers.trading_dashboard as td
        await ck.run_pass(object(), now=T0)
        with mock.patch.object(td, "get_session_factory", lambda: self.sf):
            page = await td.coin_keeper_status()
        self.assertEqual((page["mode_here"], page["is_practice"], page["places_orders"]), ("paper", True, False))
        self.assertEqual(sorted(r["asset"] for r in page["open"]), ["NEAR", "RNDR"])
        self.assertIn("BTC", dict(page["left_out"]))


class Source(unittest.TestCase):
    with open("coin_keeper.py") as _f:
        src = _f.read()

    def test_it_has_no_order_path(self):
        for bad in ("session.post", "session.delete", "session.request", '"POST"', "brokerage/orders",
                    "convert", "place_", "market_order", "limit_order"):
            self.assertFalse(bad in self.src, bad)

    def test_it_imports_nothing_from_alpaca(self):
        for l in self.src.splitlines():
            if l.strip().startswith(("import ", "from ")):
                self.assertNotIn("alpaca", l.lower(), l)

    def test_the_boot_hook_is_gated_on_paper(self):
        with open("main.py") as f:
            main = f.read()
        i = main.index("import coin_keeper")
        self.assertIn('if coin_keeper.mode() == "paper":', main[i:i + 200])

    def test_the_status_page_is_a_get(self):
        with open("routers/trading_dashboard.py") as f:
            self.assertIn('@router.get("/coin-keeper")', f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
