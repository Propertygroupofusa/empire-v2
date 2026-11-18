"""The Coinbase money check (coinbase_money_check.py): read-only, nothing hidden.

Run as written: python3 test_coinbase_money_check.py

The owner, 2026-10-31: "make sure it's like not no anonymous person that's
stealing out of my account". What these hold:
  * a send or a withdrawal is reported as money that LEFT Coinbase, with its
    amount, time and a masked destination - and the verdict says so;
  * moves between the owner's own Coinbase balances (predictions/futures,
    staking) are inside_coinbase, never counted as having left;
  * trades are skipped (the fills feed already has them); an unknown type is
    shown as "other", never dropped;
  * the predictions (futures) cash balance is read next to the main cash;
  * a Coinbase refusal is reported, never read as "nothing happened";
  * the file only ever sends GETs.

2026-11-18, after two weeks in which the owner's bet winnings had not come back
and the report could only say "nothing left":
  * every USD and USDC wallet is listed by name with its balance;
  * every portfolio is listed with Coinbase's own totals and cash positions,
    and one unreadable portfolio does not hide the others;
  * bets_cash says what went to the predictions balance and what came back -
    and money sent to fund a bet is inside_coinbase, never "other";
  * interest is came_in; a came-in type that took money out is shown as other;
  * a failed side read (futures summary, portfolios) with the wallets read is
    a problem on the list, never "Coinbase did not answer".
"""
import asyncio
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, ".")
import coinbase_money_check as mc  # noqa: E402

NOW = datetime(2026, 10, 31, 22, 0, tzinfo=timezone.utc)


def tx(t, amount, cur="USDC", at="2026-10-31T06:20:00Z", usd=None, to=None, title=None):
    d = {"type": t, "status": "completed", "created_at": at,
         "amount": {"amount": str(amount), "currency": cur},
         "native_amount": {"amount": str(usd if usd is not None else amount), "currency": "USD"},
         "details": {"title": title or t, "subtitle": ""}}
    if to:
        d["to"] = to
    return d


class FakeCoinbase:
    def __init__(self, txs=None, cfm=None, refuse=None, ports=None, breakdowns=None):
        self.ports = ports or []
        self.breakdowns = breakdowns or {}
        self.txs = txs or {}
        self.cfm = cfm
        self.refuse = refuse or set()
        self.calls = []

    async def get(self, session, path):
        self.calls.append(path)
        for r in self.refuse:
            if r in path:
                return 403, {"error": "forbidden"}
        if path.startswith(mc.CFM_PATH):
            return 200, {"balance_summary": self.cfm or {}}
        if path.startswith(mc.V2_ACCOUNTS + "?"):
            return 200, {"data": [{"id": "acc-usdc", "currency": {"code": "USDC"}, "name": "USDC Wallet",
                                   "type": "wallet", "balance": {"amount": "3317.00", "currency": "USDC"}},
                                  {"id": "acc-usd", "currency": {"code": "USD"}, "name": "Cash (USD)",
                                   "type": "fiat", "balance": {"amount": "0.00", "currency": "USD"}},
                                  {"id": "acc-btc", "currency": {"code": "BTC"}, "name": "BTC Wallet",
                                   "type": "wallet", "balance": {"amount": "0.018", "currency": "BTC"}}],
                         "pagination": {"next_uri": None}}
        if path == mc.PORTFOLIOS_PATH:
            return 200, {"portfolios": [{"uuid": u, "name": n, "type": t} for u, n, t in self.ports]}
        if path.startswith(mc.PORTFOLIOS_PATH + "/"):
            u = path.rsplit("/", 1)[1]
            if u not in self.breakdowns:
                return 404, {"error": "not found"}
            return 200, {"breakdown": self.breakdowns[u]}
        for aid, rows in self.txs.items():
            if path.startswith(f"{mc.V2_ACCOUNTS}/{aid}/transactions"):
                return 200, {"data": rows, "pagination": {"next_uri": None}}
        return 200, {"data": [], "pagination": {"next_uri": None}}


def run(fake, days=3):
    with mock.patch.object(mc, "_get", fake.get):
        return asyncio.run(mc.check(object(), days=days, now=NOW))


class Kinds(unittest.TestCase):
    def test_sends_and_withdrawals_left_coinbase(self):
        self.assertEqual(mc.kind("send", -630.0), "left_coinbase")
        self.assertEqual(mc.kind("fiat_withdrawal", -100.0), "left_coinbase")
        self.assertEqual(mc.kind("send", 25.0), "came_in")        # a send TO the owner

    def test_own_balances_are_inside(self):
        for t in ("transfer", "staking_transfer", "derivatives_settlement", "fcm_futures_usdc_sell",
                  "fcm_anything_new", "cfm_sweep"):
            self.assertEqual(mc.kind(t, -50.0), "inside_coinbase", t)

    def test_trades_are_skipped_and_unknowns_shown(self):
        for t in ("advanced_trade_fill", "buy", "sell", "trade"):
            self.assertIsNone(mc.kind(t, -10.0), t)
        self.assertEqual(mc.kind("brand_new_type", -10.0), "other")

    def test_destinations_are_masked(self):
        self.assertEqual(mc._mask("delfarrell591@gmail.com"), "d***@gmail.com")
        self.assertEqual(mc._mask("0x1234567890abcdef1234"), "0x12...ef1234")


class Report(unittest.TestCase):
    def test_a_send_out_is_named_with_amount_time_and_masked_place(self):
        fake = FakeCoinbase(txs={"acc-usdc": [
            tx("send", -630.0, to={"resource": "ethereum_address", "address": "0xabcdef0123456789aa"}),
            tx("advanced_trade_fill", -20.0)]})
        out = run(fake)
        [m] = out["movements"]
        self.assertEqual((m["kind"], m["amount"], m["at"], m["to"]),
                         ("left_coinbase", -630.0, "2026-10-31T06:20:00Z", "0xab...6789aa"))
        self.assertIn("$630.00 LEFT Coinbase", out["verdict"])
        self.assertEqual(out["trades_skipped"], 1)

    def test_money_moved_to_predictions_is_not_called_stolen(self):
        fake = FakeCoinbase(
            txs={"acc-usdc": [tx("transfer", -514.80, title="Transferred to Predictions")]},
            cfm={"cfm_usd_balance": {"value": "630.12", "currency": "USD"},
                 "cbi_usd_balance": {"value": "2540.75", "currency": "USD"},
                 "total_usd_balance": {"value": "3170.87", "currency": "USD"}})
        out = run(fake)
        self.assertTrue(out["verdict"].startswith("No USD or USDC left Coinbase"))
        self.assertEqual(out["totals"], {"inside_coinbase": -514.8})
        self.assertEqual(out["predictions"]["predictions_cash_usd"], 630.12)
        self.assertEqual(out["predictions"]["both_usd"], 3170.87)

    def test_only_the_window_is_read(self):
        fake = FakeCoinbase(txs={"acc-usdc": [tx("send", -5.0, at="2026-10-31T10:00:00Z"),
                                              tx("send", -999.0, at="2026-10-20T10:00:00Z")]})
        out = run(fake, days=3)
        self.assertEqual([m["amount"] for m in out["movements"]], [-5.0])

    def test_usd_and_usdc_wallets_are_both_read_and_nothing_else(self):
        fake = FakeCoinbase()
        run(fake)
        read = [c for c in fake.calls if "/transactions" in c]
        self.assertEqual(sorted(c.split("/")[3] for c in read), ["acc-usd", "acc-usdc"])

    def test_a_refusal_is_reported_never_read_as_nothing_happened(self):
        out = run(FakeCoinbase(refuse={"/transactions", mc.CFM_PATH}))
        self.assertTrue(out["problems"])
        self.assertIn("did not answer", out["verdict"])
        self.assertIsNone(out["predictions"])

    def test_one_unreadable_wallet_is_never_an_all_clear(self):
        fake = FakeCoinbase(txs={"acc-usdc": [tx("transfer", -5.0)]}, refuse={"acc-usd/transactions"})
        out = run(fake)
        self.assertTrue(out["verdict"].startswith("Not all movements could be read (USD: transactions HTTP 403)"))
        self.assertEqual(len(out["movements"]), 1)

    def test_it_never_raises(self):
        async def boom(session, path):
            raise RuntimeError("x")
        with mock.patch.object(mc, "_get", boom):
            out = asyncio.run(mc.check(object(), now=NOW))
        self.assertIn("failed", out["verdict"])


def money(v):
    return {"value": str(v), "currency": "USD"}


class WhereTheCashSits(unittest.TestCase):
    def test_every_cash_wallet_is_listed_by_name_with_its_balance(self):
        out = run(FakeCoinbase())
        self.assertEqual(out["cash_wallets"],
                         [{"currency": "USDC", "name": "USDC Wallet", "type": "wallet", "balance": 3317.0},
                          {"currency": "USD", "name": "Cash (USD)", "type": "fiat", "balance": 0.0}])

    def test_every_portfolio_is_listed_with_coinbases_own_totals(self):
        fake = FakeCoinbase(
            ports=[("p-1", "Default", "DEFAULT"), ("p-2", "Predictions", "CONSUMER")],
            breakdowns={
                "p-1": {"portfolio_balances": {"total_balance": money(7800), "total_crypto_balance": money(4483),
                                               "total_cash_equivalent_balance": money(3317),
                                               "total_futures_balance": money(0)},
                        "spot_positions": [{"asset": "USDC", "is_cash": True, "total_balance_fiat": 3317.0},
                                           {"asset": "XRP", "is_cash": False, "total_balance_fiat": 1000.0}]},
                "p-2": {"portfolio_balances": {"total_balance": money(1507.65),
                                               "total_cash_equivalent_balance": money(1507.65)},
                        "spot_positions": [{"asset": "USD", "is_cash": True, "total_balance_fiat": 1507.65}],
                        "futures_positions": [{}, {}]}})
        out = run(fake)
        d, p = out["portfolios"]
        self.assertEqual((d["name"], d["type"], d["total_usd"], d["cash_usd"], d["coin_usd"], d["futures_usd"]),
                         ("Default", "DEFAULT", 7800.0, 3317.0, 4483.0, 0.0))
        self.assertEqual(d["cash_positions"], [{"asset": "USDC", "usd": 3317.0}])
        self.assertEqual((p["name"], p["cash_usd"], p["futures_positions"]), ("Predictions", 1507.65, 2))
        self.assertEqual(p["cash_positions"], [{"asset": "USD", "usd": 1507.65}])
        self.assertEqual(out["problems"], [])

    def test_one_unreadable_portfolio_does_not_hide_the_others(self):
        fake = FakeCoinbase(ports=[("p-1", "Default", "DEFAULT"), ("p-9", "Other", "CONSUMER")],
                            breakdowns={"p-1": {"portfolio_balances": {"total_balance": money(10)}}})
        a, b = run(fake)["portfolios"]
        self.assertEqual(a["total_usd"], 10.0)
        self.assertEqual(b, {"name": "Other", "type": "CONSUMER", "unreadable": "breakdown HTTP 404"})

    def test_a_failed_side_read_is_a_problem_and_never_coinbase_did_not_answer(self):
        # the wallets were read and nothing moved: the answer stands, with the gaps listed
        out = run(FakeCoinbase(refuse={mc.PORTFOLIOS_PATH, mc.CFM_PATH}))
        self.assertTrue(out["verdict"].startswith("No USD or USDC left Coinbase"), out["verdict"])
        self.assertEqual(sorted(out["problems"]), ["futures balance HTTP 403", "portfolios HTTP 403"])
        self.assertEqual(out["portfolios"], [])

    def test_no_wallet_listed_is_never_an_all_clear(self):
        class NoWallets(FakeCoinbase):
            async def get(self, session, path):
                if path.startswith(mc.V2_ACCOUNTS + "?"):
                    return 200, {"data": [], "pagination": {"next_uri": None}}
                return await super().get(session, path)
        out = run(NoWallets())
        self.assertIn("did not answer", out["verdict"])
        self.assertIn("no USD or USDC wallet was listed", out["verdict"])

    def test_bet_cash_says_what_went_and_what_came_back(self):
        fake = FakeCoinbase(txs={
            "acc-usdc": [tx("prediction_market_usdc_sell", -54.98),
                         tx("prediction_market_usdc_sell_additional_encumberment", -0.01),
                         tx("interest", 2.34)],
            "acc-usd": [tx("derivatives_settlement", -24.99, cur="USD"),
                        tx("derivatives_settlement", 20.02, cur="USD"),
                        tx("send", -5.0, cur="USD")]})
        out = run(fake)
        self.assertEqual({k: out["bets_cash"][k] for k in ("sent_usd", "came_back_usd", "net_usd")},
                         {"sent_usd": 79.98, "came_back_usd": 20.02, "net_usd": -59.96})
        self.assertIn("not profit or loss", out["bets_cash"]["is"])
        self.assertEqual(out["totals"], {"inside_coinbase": -59.96, "came_in": 2.34, "left_coinbase": -5.0})

    def test_bet_funding_is_inside_and_interest_came_in(self):
        self.assertEqual(mc.kind("prediction_market_usdc_sell", -54.98), "inside_coinbase")
        self.assertEqual(mc.kind("prediction_market_anything_new", -1.0), "inside_coinbase")
        self.assertEqual(mc.kind("interest", 2.34), "came_in")
        self.assertEqual(mc.kind("retail_defi_lend_reward_payout", 0.0), "came_in")
        self.assertEqual(mc.kind("interest", -2.34), "other")       # took money out: show it, do not call it income


class Source(unittest.TestCase):
    with open("coinbase_money_check.py") as _f:
        src = _f.read()

    def test_get_only(self):
        for bad in ("session.post", "session.put", "session.delete", "session.request", '"POST"', '"DELETE"',
                    '"PUT"', "brokerage/orders", "convert", "move_funds", "sweeps"):
            self.assertFalse(bad in self.src, bad)

    def test_coinbase_only(self):
        for l in self.src.splitlines():
            if l.strip().startswith(("import ", "from ")):
                self.assertNotIn("alpaca", l.lower(), l)

    def test_the_page_is_a_get(self):
        with open("routers/trading_dashboard.py") as f:
            self.assertIn('@router.get("/coinbase-money-check")', f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
