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
    def __init__(self, txs=None, cfm=None, refuse=None):
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
            return 200, {"data": [{"id": "acc-usdc", "currency": {"code": "USDC"}},
                                  {"id": "acc-usd", "currency": {"code": "USD"}},
                                  {"id": "acc-btc", "currency": {"code": "BTC"}}],
                         "pagination": {"next_uri": None}}
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
