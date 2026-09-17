import os
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from alpaca_iron_condor_bot import (
    AlpacaREST,
    IronCondorBot,
    IronCondorCandidate,
    OptionQuote,
)


class IronCondorBotTests(unittest.TestCase):
    def quote(
        self,
        symbol: str,
        option_type: str,
        strike: str,
        delta: str,
        bid: str,
        ask: str,
    ) -> OptionQuote:
        return OptionQuote(
            symbol=symbol,
            underlying="SPY",
            expiration=date(2026, 10, 16),
            option_type=option_type,
            strike=Decimal(strike),
            delta=Decimal(delta),
            bid=Decimal(bid),
            ask=Decimal(ask),
        )

    def candidate(self) -> IronCondorCandidate:
        return IronCondorCandidate(
            underlying="SPY",
            expiration=date(2026, 10, 16),
            short_call=self.quote("SPY261016C00600000", "call", "600", "0.25", "0.50", "0.55"),
            long_call=self.quote("SPY261016C00605000", "call", "605", "0.15", "0.25", "0.30"),
            short_put=self.quote("SPY261016P00550000", "put", "550", "-0.25", "0.45", "0.50"),
            long_put=self.quote("SPY261016P00545000", "put", "545", "-0.15", "0.20", "0.25"),
            natural_credit=Decimal("0.40"),
            limit_credit=Decimal("0.35"),
        )

    def test_parses_occ_snapshot(self) -> None:
        snapshot = {
            "latestQuote": {"bp": 1.25, "ap": 1.30},
            "greeks": {"delta": -0.24},
        }

        quote = IronCondorBot.parse_option_snapshot("SPY261016P00550000", snapshot)

        self.assertEqual(quote.underlying, "SPY")
        self.assertEqual(quote.expiration, date(2026, 10, 16))
        self.assertEqual(quote.option_type, "put")
        self.assertEqual(quote.strike, Decimal("550"))
        self.assertEqual(quote.delta, Decimal("-0.24"))

    def test_selects_rule_compliant_five_point_condor(self) -> None:
        candidate = self.candidate()
        chain = [
            candidate.short_call,
            candidate.long_call,
            candidate.short_put,
            candidate.long_put,
        ]

        selected = IronCondorBot.select_candidate("SPY", chain)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.symbols, candidate.symbols)
        self.assertEqual(selected.natural_credit, Decimal("0.40"))
        self.assertEqual(selected.limit_credit, Decimal("0.35"))

    def test_entry_credit_is_negative_for_mleg_order(self) -> None:
        payload = IronCondorBot._entry_payload(self.candidate(), 1)

        self.assertEqual(payload["order_class"], "mleg")
        self.assertEqual(payload["limit_price"], "-0.35")
        self.assertEqual(len(payload["legs"]), 4)
        self.assertEqual(payload["legs"][0]["position_intent"], "sell_to_open")
        self.assertEqual(payload["legs"][1]["position_intent"], "buy_to_open")

    def test_close_debit_is_positive_and_reverses_intents(self) -> None:
        candidate = self.candidate()
        trade = {
            "underlying": "SPY",
            "symbols": candidate.symbols,
            "contracts": 1,
        }

        payload = IronCondorBot._close_payload(trade, Decimal("0.18"))

        self.assertEqual(payload["limit_price"], "0.18")
        self.assertEqual(payload["legs"][0]["position_intent"], "buy_to_close")
        self.assertEqual(payload["legs"][1]["position_intent"], "sell_to_close")

    def test_rejects_live_trading_url(self) -> None:
        environment = {
            "ALPACA_API_KEY": "test-key",
            "ALPACA_SECRET_KEY": "test-secret",
            "ALPACA_BASE_URL": "https://api.alpaca.markets",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "paper-only"):
                AlpacaREST()


if __name__ == "__main__":
    unittest.main()
