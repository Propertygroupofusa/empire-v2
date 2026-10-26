"""The patient stock bot (alpaca_patient.py): practice only, the tested rule, no orders.

Run as written: python3 test_alpaca_patient.py

The owner, 2026-10-26: "build the patient version". What these hold:
  * off unless ALPACA_PATIENT reads exactly "paper" - there is no live mode;
  * a piece sells only at its target, the price that nets 5% after the 0.12%
    round trip, and only on a LATER hour - never under it, never by a stop or
    a clock;
  * a buy needs a 14-hour RSI under 40 on regular-session hours, at most 4
    pieces and one per symbol, and only on the hour that just closed;
  * hours are built from 30-minute bars, 09:30-16:00 Eastern, in summer and
    winter time, and an hour still forming - or missing its second half - is
    never decided on;
  * the module has no order path and imports nothing from the Coinbase side.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
import alpaca_patient as ap  # noqa: E402

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def day_bars(y, m, d, start=(8, 0), end=(17, 0), base=100.0, step=0.0):
    """30-minute bars across one Eastern day, pre- and post-market included."""
    out, t = [], datetime(y, m, d, *start, tzinfo=ET)
    stop = datetime(y, m, d, *end, tzinfo=ET)
    px = base
    while t < stop:
        out.append((int(t.timestamp()), px, px + 1.0, px - 1.0, px + 0.5))
        px += step
        t += timedelta(minutes=30)
    return out


def buckets(closes, t0=0, highs=None):
    return [(t0 + i * 3600, c, (highs[i] if highs else c), c, c) for i, c in enumerate(closes)]


def falling(n=30, start=100.0):
    return [start - i for i in range(n)]


def rising(n=30, start=100.0):
    return [start + i for i in range(n)]


class Switch(unittest.TestCase):
    def tearDown(self):
        os.environ.pop(ap.SWITCH_ENV, None)

    def test_only_paper_is_on_and_there_is_no_live_mode(self):
        for v, want in ((None, "off"), ("", "off"), ("paper", "paper"), (" PAPER ", "paper"),
                        ("live", "off"), ("true", "off"), ("1", "off"), ("yes", "off")):
            if v is None:
                os.environ.pop(ap.SWITCH_ENV, None)
            else:
                os.environ[ap.SWITCH_ENV] = v
            self.assertEqual(ap.mode(), want, v)

    def test_off_reads_nothing(self):
        async def boom(*a, **k):
            raise AssertionError("read with the switch off")
        real = ap._state
        ap._state = boom
        try:
            out = asyncio.run(ap.run_pass(object(), now=et(2026, 10, 27, 12, 0)))
        finally:
            ap._state = real
        self.assertEqual(out["skipped"], "off")


class Rules(unittest.TestCase):
    def test_the_owners_numbers(self):
        self.assertEqual((ap.PIECE_USD, ap.MAX_OPEN, ap.NET, ap.RSI_BUY_BELOW), (120.0, 4, 0.05, 40.0))
        self.assertEqual(len(ap.SYMBOLS), 13)
        for retired in ("SH", "PSQ", "DOG", "RWM"):
            self.assertNotIn(retired, ap.SYMBOLS)

    def test_the_target_nets_five_percent_after_the_round_trip(self):
        for e in (1.0, 48.72, 524.51):
            p = ap.target_price(e)
            self.assertAlmostEqual(ap.net_after_costs(1.0, e, p), 0.05 * e, places=9)
            self.assertGreater(ap.net_after_costs(1.0, e, p * 0.999), 0.0)
            self.assertLess(ap.net_after_costs(1.0, e, e), 0.0)        # flat is a loss after costs

    def test_rsi_is_wilders(self):
        self.assertEqual(ap.rsi(rising()), 100.0)
        self.assertEqual(ap.rsi(falling()), 0.0)
        self.assertIsNone(ap.rsi([1.0] * 14))
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
                  45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
        # Wilder's classic worked example: RSI 70.53 at the 15th close, 66.32 at the
        # 16th. The published table rounds its averages to four decimals, which
        # moves the first figure by ~0.07; exact arithmetic lands within 0.1.
        self.assertAlmostEqual(ap.rsi(closes[:15]), 70.53, delta=0.1)
        self.assertAlmostEqual(ap.rsi(closes[:16]), 66.32, delta=0.1)


class Hours(unittest.TestCase):
    def test_a_day_becomes_seven_regular_session_hours(self):
        bars = day_bars(2026, 10, 27, step=0.1)
        hb = ap.hourly_buckets(bars, now=et(2026, 10, 27, 20, 0))
        starts = [datetime.fromtimestamp(b[0], ET).strftime("%H:%M") for b in hb]
        self.assertEqual(starts, ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"])
        first = [b for b in bars if b[0] in (et(2026, 10, 27, 9, 30), et(2026, 10, 27, 10, 0))]
        self.assertEqual(hb[0][1], first[0][1])                          # open of the first half
        self.assertEqual(hb[0][4], first[1][4])                          # close of the second half
        self.assertEqual(hb[0][2], max(first[0][2], first[1][2]))
        self.assertEqual(hb[0][3], min(first[0][3], first[1][3]))

    def test_summer_and_winter_both_open_at_half_past_nine_eastern(self):
        for y, m, d in ((2026, 7, 14), (2026, 12, 15)):
            hb = ap.hourly_buckets(day_bars(y, m, d), now=et(y, m, d, 20, 0))
            self.assertEqual(datetime.fromtimestamp(hb[0][0], ET).strftime("%H:%M"), "09:30")
            self.assertEqual(len(hb), 7)

    def test_an_hour_still_forming_is_left_out(self):
        hb = ap.hourly_buckets(day_bars(2026, 10, 27), now=et(2026, 10, 27, 11, 10))
        self.assertEqual([datetime.fromtimestamp(b[0], ET).strftime("%H:%M") for b in hb], ["09:30"])

    def test_an_hour_missing_its_second_half_is_left_out(self):
        bars = [b for b in day_bars(2026, 10, 27) if b[0] < et(2026, 10, 27, 11, 0)]
        hb = ap.hourly_buckets(bars, now=et(2026, 10, 27, 11, 40))
        self.assertEqual([datetime.fromtimestamp(b[0], ET).strftime("%H:%M") for b in hb], ["09:30"])

    def test_the_newest_closed_hour(self):
        f = lambda t: datetime.fromtimestamp(t, ET).strftime("%a %H:%M")
        self.assertEqual(f(ap.newest_closed_bucket(et(2026, 10, 27, 12, 0))), "Tue 10:30")
        self.assertEqual(f(ap.newest_closed_bucket(et(2026, 10, 27, 10, 0))), "Mon 15:30")
        self.assertEqual(f(ap.newest_closed_bucket(et(2026, 10, 31, 12, 0))), "Fri 15:30")


def piece(sym, entry, at, id_=1):
    return {"id": id_, "symbol": sym, "qty": ap.PIECE_USD / entry, "entry_price": entry,
            "target_price": ap.target_price(entry), "opened_at": at}


class Plan(unittest.TestCase):
    def test_a_later_hour_at_the_target_sells_at_the_target(self):
        p = piece("SPY", 100.0, 3600)
        tgt = p["target_price"]
        sales, _ = ap.plan([p], {"SPY": buckets([100, 101, 102], highs=[100, 101, tgt + 0.01])})
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0][2], tgt)

    def test_nothing_under_the_target_ever_sells(self):
        p = piece("SPY", 100.0, 0)
        sales, _ = ap.plan([p], {"SPY": buckets([90, 80, 99], highs=[90, 80, p["target_price"] - 0.01])})
        self.assertEqual(sales, [])

    def test_the_buying_hour_itself_cannot_sell(self):
        p = piece("SPY", 100.0, 7200)
        sales, _ = ap.plan([p], {"SPY": buckets([100, 100, 100], highs=[200, 200, 200])})
        self.assertEqual(sales, [])

    def test_a_dip_buys_at_the_newest_close(self):
        _, buys = ap.plan([], {"QQQ": buckets(falling())})
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0][0], "QQQ")
        self.assertEqual(buys[0][2], falling()[-1])
        self.assertLess(buys[0][3], 40)

    def test_no_dip_no_buy(self):
        _, buys = ap.plan([], {"QQQ": buckets(rising())})
        self.assertEqual(buys, [])

    def test_at_most_four_and_one_per_symbol(self):
        held = [piece(s, 100.0, 0, i) for i, s in enumerate(["SPY", "QQQ", "DIA"])]
        data = {s: buckets(falling()) for s in ap.SYMBOLS}
        _, buys = ap.plan(held, data)
        self.assertEqual([b[0] for b in buys], ["IWM"])         # the next symbol in order, and only one

    def test_a_sale_frees_its_slot_in_the_same_pass(self):
        held = [piece(s, 100.0, 0, i) for i, s in enumerate(["SPY", "QQQ", "DIA", "IWM"])]
        tgt = held[0]["target_price"]
        data = {s: buckets(falling()) for s in ap.SYMBOLS}
        data["SPY"] = buckets(falling(), highs=[tgt + 1] * 30)
        sales, buys = ap.plan(held, data)
        self.assertEqual([s[0]["symbol"] for s in sales], ["SPY"])
        self.assertEqual(len(buys), 1)

    def test_a_stale_hour_is_never_bought_on(self):
        data = {"QQQ": buckets(falling())}
        _, buys = ap.plan([], data, bucket=data["QQQ"][-1][0] + 3600)
        self.assertEqual(buys, [])
        _, buys = ap.plan([], data, bucket=data["QQQ"][-1][0])
        self.assertEqual(len(buys), 1)


class Pass(unittest.TestCase):
    """run_pass end to end, with the record and the price feed faked."""

    def setUp(self):
        os.environ[ap.SWITCH_ENV] = "paper"
        self.state, self.bought, self.sold, self.pieces = {}, [], [], []
        self.real = {n: getattr(ap, n) for n in ("_state", "_set_state", "open_pieces", "record_buy",
                                                  "record_sale", "bars_30m")}
        self.now = et(2026, 10, 27, 14, 0)            # 12:30 hour closed at 13:30, settled
        days = [(2026, 10, d) for d in (19, 20, 21, 22, 23, 26, 27)]
        self.feed = {s: sum((day_bars(*d, base=200.0 - i * 3, step=-0.2) for i, d in enumerate(days)), [])
                     for s in ap.SYMBOLS}
        test = self

        class Row:
            def __init__(self, b=0.0, s=0.0):
                self.base_capital, self.starting_capital = b, s

        async def _state(key):
            v = test.state.get(key)
            return Row(*v) if v else None

        async def _set_state(key, base=None, start=None, add_base=0.0, add_start=0.0):
            b, s = test.state.get(key, (0.0, 0.0))
            b = base if base is not None else b
            s = start if start is not None else s
            test.state[key] = (b + add_base, s + add_start)

        async def open_pieces(paper=True):
            return list(test.pieces)

        async def record_buy(sym, t, px, r, paper=True):
            test.bought.append((sym, t, px, r))
            return ap.PIECE_USD / px

        async def record_sale(pid, t, px):
            test.sold.append((pid, t, px))
            return 1.0

        async def bars_30m(session, sym, now):
            return test.feed.get(sym)

        for n, f in (("_state", _state), ("_set_state", _set_state), ("open_pieces", open_pieces),
                     ("record_buy", record_buy), ("record_sale", record_sale), ("bars_30m", bars_30m)):
            setattr(ap, n, f)

    def tearDown(self):
        os.environ.pop(ap.SWITCH_ENV, None)
        for n, f in self.real.items():
            setattr(ap, n, f)

    def run_pass(self, now=None):
        return asyncio.run(ap.run_pass(object(), now=now or self.now))

    def test_a_falling_market_buys_four_pieces_on_the_hour_that_closed(self):
        out = self.run_pass()
        self.assertEqual([b[0] for b in self.bought], ["SPY", "QQQ", "DIA", "IWM"])
        self.assertEqual({b[1] for b in self.bought}, {et(2026, 10, 27, 12, 30)})
        self.assertEqual(out["skipped"], None)
        self.assertEqual(self.state[ap.PASSES_KEY], (1.0, 0.0))

    def test_the_same_hour_is_never_decided_twice(self):
        self.run_pass()
        out = self.run_pass(self.now + 300)
        self.assertEqual(out["skipped"], "no new closed hour")
        self.assertEqual(len(self.bought), 4)

    def test_unreadable_prices_decide_nothing_and_count_a_failure(self):
        self.feed["GLD"] = None
        out = self.run_pass()
        self.assertEqual(self.bought, [])
        self.assertIn("unreadable", out["skipped"])
        self.assertEqual(self.state[ap.PASSES_KEY], (0.0, 1.0))
        self.assertNotIn(ap.LAST_BUCKET_KEY, self.state)      # the hour is retried, not skipped


class Source(unittest.TestCase):
    with open("alpaca_patient.py") as _f:
        src = _f.read()

    def test_it_has_no_order_path(self):
        for bad in ("session.post", "/v2/orders", "/orders", "submit_order", "place_", "market_order"):
            self.assertNotIn(bad, self.src, bad)

    def test_it_imports_nothing_from_the_coinbase_side(self):
        imports = [l for l in self.src.splitlines() if l.strip().startswith(("import ", "from "))]
        for l in imports:
            self.assertNotIn("crypto", l.lower(), l)
            self.assertNotIn("coinbase", l.lower(), l)
            self.assertNotIn("grid", l.lower(), l)

    def test_the_boot_hook_is_gated_on_paper(self):
        with open("main.py") as f:
            main = f.read()
        i = main.index("import alpaca_patient")
        self.assertIn('if alpaca_patient.mode() == "paper":', main[i:i + 300])

    def test_the_status_page_is_a_get(self):
        with open("routers/trading_dashboard.py") as f:
            self.assertIn('@router.get("/alpaca-patient")', f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
