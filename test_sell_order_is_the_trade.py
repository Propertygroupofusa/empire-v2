"""One sell order is one trade. A lot is not.

THE BUG, measured on the live Alpaca book 2026-10-05. The dashboard
reported "294 round trips, 34.0% win rate". The account had completed
181 sell orders at a 47.0% win rate. FIFO splits one sell across every
open lot it consumes, and each slice became a counted "round trip":
eleven split sells alone produced 124 of those 294 rows.

The count was inflated 62% and the win rate understated by 13 points -
on the one figure read to decide whether a strategy is working.

It also manufactured phantom dust. 107 rows came out under $5 of
notional and 87 of them (81%) were slivers of a larger sell. Reading
those as real orders leads to "this bot makes 107 pointless $1 trades",
which is false. That conclusion WAS drawn from this data before the fix,
and had to be withdrawn - which is why these tests pin the distinction
rather than just the arithmetic.

The lot rows are NOT wrong and are not removed: lots have different entry
prices and the P&L arithmetic has to respect that. They are simply not a
trade count.
"""
import unittest

import closed_trades


def order(oid, sym, side, qty, price, at, coid=None):
    return {"id": oid, "symbol": sym, "side": side, "status": "filled",
            "filled_qty": str(qty), "filled_avg_price": str(price),
            "filled_at": at, "client_order_id": coid}


class OneSellIsOneTrade(unittest.TestCase):

    def test_a_sell_consuming_three_lots_is_one_trade_not_three(self):
        orders = [
            order("b1", "DOG", "buy", 1, 20.00, "2026-01-01T10:00:00Z"),
            order("b2", "DOG", "buy", 1, 21.00, "2026-01-01T11:00:00Z"),
            order("b3", "DOG", "buy", 1, 22.00, "2026-01-01T12:00:00Z"),
            order("s1", "DOG", "sell", 3, 21.50, "2026-01-02T10:00:00Z"),
        ]
        t = closed_trades.pair_round_trips(orders)["totals"]
        self.assertEqual(t["round_trips"], 3, "three lots were consumed")
        self.assertEqual(t["completed_sells"], 1, "but only ONE sell happened")
        self.assertEqual(t["sells_split_across_lots"], 1)

    def test_the_win_rate_is_computed_on_the_sell_not_the_lots(self):
        # +1.50, +0.50, -0.50 across the lots -> 2 of 3 lot rows win (66.7%)
        # but the SELL netted +1.50 and is a single winner (100%).
        orders = [
            order("b1", "DOG", "buy", 1, 20.00, "2026-01-01T10:00:00Z"),
            order("b2", "DOG", "buy", 1, 21.00, "2026-01-01T11:00:00Z"),
            order("b3", "DOG", "buy", 1, 22.00, "2026-01-01T12:00:00Z"),
            order("s1", "DOG", "sell", 3, 21.50, "2026-01-02T10:00:00Z"),
        ]
        t = closed_trades.pair_round_trips(orders)["totals"]
        self.assertEqual(t["winners"], 2)          # lot level
        self.assertEqual(t["losers"], 1)
        self.assertEqual(t["sell_winners"], 1)     # order level
        self.assertEqual(t["sell_losers"], 0)
        self.assertEqual(t["sell_win_rate_pct"], 100.0)

    def test_a_sell_that_nets_a_loss_counts_as_one_loss(self):
        orders = [
            order("b1", "X", "buy", 1, 30.00, "2026-01-01T10:00:00Z"),
            order("b2", "X", "buy", 1, 20.00, "2026-01-01T11:00:00Z"),
            order("s1", "X", "sell", 2, 24.00, "2026-01-02T10:00:00Z"),
        ]
        t = closed_trades.pair_round_trips(orders)["totals"]
        self.assertEqual(t["winners"], 1)          # the $20 lot won
        self.assertEqual(t["losers"], 1)           # the $30 lot lost
        self.assertEqual(t["sell_winners"], 0)     # the SELL netted -$2
        self.assertEqual(t["sell_losers"], 1)
        self.assertEqual(t["sell_win_rate_pct"], 0.0)

    def test_regrouping_never_changes_the_money(self):
        """The one invariant that may never break. Money is money."""
        orders = [
            order("b1", "A", "buy", 2, 10.00, "2026-01-01T10:00:00Z"),
            order("b2", "A", "buy", 3, 11.00, "2026-01-01T11:00:00Z"),
            order("s1", "A", "sell", 4, 12.00, "2026-01-02T10:00:00Z"),
            order("b3", "B", "buy", 1, 50.00, "2026-01-01T12:00:00Z"),
            order("s2", "B", "sell", 1, 49.00, "2026-01-02T11:00:00Z"),
        ]
        out = closed_trades.pair_round_trips(orders)
        lot_sum = round(sum(t["pnl"] for t in out["trades"]), 6)
        ord_sum = round(sum(o["pnl"] for o in out["orders"]), 6)
        self.assertAlmostEqual(lot_sum, ord_sum, places=6)
        self.assertAlmostEqual(out["totals"]["realised_pnl"], round(lot_sum, 2), places=2)

    def test_an_unsplit_sell_makes_the_two_counts_agree(self):
        orders = [
            order("b1", "A", "buy", 1, 10.00, "2026-01-01T10:00:00Z"),
            order("s1", "A", "sell", 1, 11.00, "2026-01-02T10:00:00Z"),
            order("b2", "B", "buy", 1, 10.00, "2026-01-01T10:00:00Z"),
            order("s2", "B", "sell", 1, 9.00, "2026-01-02T10:00:00Z"),
        ]
        t = closed_trades.pair_round_trips(orders)["totals"]
        self.assertEqual(t["round_trips"], t["completed_sells"])
        self.assertEqual(t["sell_win_rate_pct"], 50.0)
        self.assertEqual(t["sells_split_across_lots"], 0)

    def test_the_phantom_dust(self):
        """A sliver of a big sell must not read as a small order.

        This is the one that produced a withdrawn finding. A $200 sell
        leaving a 0.07-share remainder creates a row worth about $1.50.
        Counting that as a trade the bot chose to place is wrong.
        """
        orders = [
            order("b1", "DOG", "buy", 9.93, 21.34, "2026-01-01T10:00:00Z"),
            order("b2", "DOG", "buy", 0.07, 21.34, "2026-01-01T11:00:00Z"),
            order("s1", "DOG", "sell", 10.0, 21.20, "2026-01-02T10:00:00Z"),
        ]
        out = closed_trades.pair_round_trips(orders)
        tiny = [t for t in out["trades"] if t["qty"] * t["entry_price"] < 5]
        self.assertTrue(tiny, "the sliver row exists at lot level, as it should")
        self.assertEqual(out["totals"]["completed_sells"], 1,
                         "but the account placed ONE sell, not two")
        self.assertEqual(len(out["orders"]), 1)
        self.assertGreater(out["orders"][0]["entry_notional_usd"], 200)

    def test_two_sells_of_the_same_symbol_are_two_trades(self):
        """Grouping must be by SELL ORDER, not by symbol.

        Every other scenario here uses one sell per symbol, which makes
        "group by exit_order_id" and "group by symbol" indistinguishable -
        and a mutation swapping one for the other walked straight through
        the first draft of this file. Two separate sells of the same
        symbol, on different days, are two trades and must count as two.
        """
        orders = [
            order("b1", "DOG", "buy", 1, 20.00, "2026-01-01T10:00:00Z"),
            order("s1", "DOG", "sell", 1, 21.00, "2026-01-02T10:00:00Z"),
            order("b2", "DOG", "buy", 1, 20.00, "2026-01-03T10:00:00Z"),
            order("s2", "DOG", "sell", 1, 19.00, "2026-01-04T10:00:00Z"),
        ]
        t = closed_trades.pair_round_trips(orders)["totals"]
        self.assertEqual(t["completed_sells"], 2,
                         "two sells of one symbol are two trades, not one")
        self.assertEqual(t["sell_winners"], 1)
        self.assertEqual(t["sell_losers"], 1)
        self.assertEqual(t["sell_win_rate_pct"], 50.0)
        self.assertEqual(t["sells_split_across_lots"], 0)

    def test_grouping_is_keyed_on_the_exit_order_id(self):
        """Same symbol, both sells splitting lots - the counts must differ
        from what grouping by symbol would give (which would be 1)."""
        orders = [
            order("b1", "DOG", "buy", 1, 20.00, "2026-01-01T10:00:00Z"),
            order("b2", "DOG", "buy", 1, 21.00, "2026-01-01T11:00:00Z"),
            order("s1", "DOG", "sell", 2, 22.00, "2026-01-02T10:00:00Z"),
            order("b3", "DOG", "buy", 1, 20.00, "2026-01-03T10:00:00Z"),
            order("b4", "DOG", "buy", 1, 21.00, "2026-01-03T11:00:00Z"),
            order("s2", "DOG", "sell", 2, 19.00, "2026-01-04T10:00:00Z"),
        ]
        out = closed_trades.pair_round_trips(orders)
        t = out["totals"]
        self.assertEqual(t["round_trips"], 4, "four lots consumed")
        self.assertEqual(t["completed_sells"], 2, "by TWO sell orders")
        self.assertEqual(t["sells_split_across_lots"], 2)
        self.assertEqual(len({o["exit_order_id"] for o in out["orders"]}), 2)
        self.assertEqual(t["sell_winners"], 1)
        self.assertEqual(t["sell_losers"], 1)

    def test_every_order_row_names_how_many_lots_it_ate(self):
        orders = [
            order("b1", "A", "buy", 1, 10.00, "2026-01-01T10:00:00Z"),
            order("b2", "A", "buy", 1, 10.00, "2026-01-01T11:00:00Z"),
            order("s1", "A", "sell", 2, 11.00, "2026-01-02T10:00:00Z"),
        ]
        o = closed_trades.pair_round_trips(orders)["orders"][0]
        self.assertEqual(o["lots_consumed"], 2)
        self.assertAlmostEqual(o["qty"], 2.0, places=9)

    def test_the_totals_say_which_count_is_which(self):
        orders = [order("b1", "A", "buy", 1, 10.0, "2026-01-01T10:00:00Z"),
                  order("s1", "A", "sell", 1, 11.0, "2026-01-02T10:00:00Z")]
        t = closed_trades.pair_round_trips(orders)["totals"]
        self.assertIn("completed_sells", t["which_count_is_which"])
        self.assertIn("round_trips", t["which_count_is_which"])


class TheEndpointPublishesTheHonestCount(unittest.TestCase):

    def setUp(self):
        self.src = open("routers/trading_dashboard.py").read()

    def test_win_rate_pct_is_the_sell_level_figure(self):
        i = self.src.index("async def _alpaca_realized_record")
        body = self.src[i:i + 4000]
        self.assertIn('"win_rate_pct": t.get("sell_win_rate_pct")', body,
                      "the headline win rate must be computed on sell orders")

    def test_the_old_lot_level_rate_is_kept_under_its_own_name(self):
        i = self.src.index("async def _alpaca_realized_record")
        body = self.src[i:i + 4000]
        self.assertIn('"lot_win_rate_pct"', body,
                      "keep the old figure visible so an older screenshot "
                      "can be reconciled rather than silently contradicted")

    def test_avg_per_trade_divides_by_sells_when_it_can(self):
        i = self.src.index("async def _alpaca_realized_record")
        body = self.src[i:i + 4000]
        self.assertIn("round(net / sells, 4) if sells", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
