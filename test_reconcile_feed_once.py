"""The reconcile guard tells the feed once, not every 15 minutes.

Run as written: python3 test_reconcile_feed_once.py

From 2026-10-30 the reconcile pass refused every 15 minutes for over a week
(the USD wallet read $0 while the cash sat in USDC). Each refusal wrote a
feed row, so 300 identical rows covered three days and pushed every coin
picker sale and error out of the window the owner and his daily check read.
What these hold:
  * a refusal still reaches the feed - when it starts, when it changes
    between REFUSED and UNKNOWN, and once a day while it lasts;
  * a day of refusals (96 passes) writes ONE row, and the log line is still
    written every pass;
  * the first pass after it ends writes ONE "cleared" row, then silence;
  * a reminder carries that pass's figures, not the first one's;
  * what the pass DOES is untouched - only the feed rows change.
"""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
sys.path.insert(0, ".")
import reconcile_worker as rw  # noqa: E402

DAY = rw.GUARD_REMIND_SECONDS


def refused(n):
    return {"status": "REFUSED", "detail": f"the pass wants to remove ${n} of $393.86"}


class Rows(unittest.TestCase):
    def test_a_refusal_is_written_when_it_starts(self):
        row, st = rw.guard_feed_row(None, refused(342.92), 1000.0)
        self.assertEqual(row[0], "RECONCILE_REFUSED")
        self.assertIn("$342.92", row[1])
        self.assertEqual(st, ("REFUSED", 1000.0))

    def test_the_same_refusal_inside_a_day_is_not_written_again(self):
        _, st = rw.guard_feed_row(None, refused(342.92), 1000.0)
        for k in range(1, 96):
            row, st = rw.guard_feed_row(st, refused(393.86), 1000.0 + k * 900)
            self.assertIsNone(row, k)
        self.assertEqual(st, ("REFUSED", 1000.0))

    def test_a_refusal_that_lasts_a_day_is_written_again_with_todays_figures(self):
        _, st = rw.guard_feed_row(None, refused(342.92), 1000.0)
        row, st = rw.guard_feed_row(st, refused(393.86), 1000.0 + DAY)
        self.assertEqual(row[0], "RECONCILE_REFUSED")
        self.assertIn("$393.86 of", row[1])
        self.assertEqual(st, ("REFUSED", 1000.0 + DAY))

    def test_turning_from_refused_to_unknown_is_written(self):
        _, st = rw.guard_feed_row(None, refused(1), 1000.0)
        row, st = rw.guard_feed_row(st, {"status": "UNKNOWN", "detail": "real cash unreadable"}, 1900.0)
        self.assertEqual(row[0], "RECONCILE_UNKNOWN")
        self.assertEqual(st, ("UNKNOWN", 1900.0))

    def test_the_end_of_a_refusal_is_written_once(self):
        _, st = rw.guard_feed_row(None, refused(1), 1000.0)
        ok = {"status": "OK", "detail": "unspent claims $393.86 fit inside $500.00 of real cash"}
        row, st = rw.guard_feed_row(st, ok, 1900.0)
        self.assertEqual(row[0], "RECONCILE_CLEARED")
        self.assertIn("no longer REFUSED", row[1])
        self.assertIn("fit inside", row[1])
        self.assertIsNone(st)
        row, st = rw.guard_feed_row(st, ok, 2800.0)
        self.assertEqual((row, st), (None, None))

    def test_a_healthy_fleet_writes_nothing(self):
        for status in ("OK", "CORRECT"):
            self.assertEqual(rw.guard_feed_row(None, {"status": status, "detail": "x"}, 1.0), (None, None))


class Loop(unittest.TestCase):
    """The real loop, driven for N passes with the clock moving 15 minutes a pass."""

    def drive(self, reports):
        feed, logs = [], []
        reports = list(reports)
        start = datetime(2026, 11, 7, 12, 0, 0)
        clock = {"n": 0}

        class Clock(datetime):
            @classmethod
            def utcnow(cls):
                return start + timedelta(seconds=clock["n"] * rw.CHECK_SECONDS)

        async def armed_now(_sf):
            return False

        async def cash_reader():
            return 0.0

        async def run_once(*a, **k):
            return reports.pop(0)

        async def announce(bot, product, etype, msg, sink=None):
            feed.append((etype, msg))

        class Done(Exception):
            pass

        async def sleep(_s):
            clock["n"] += 1
            if not reports:
                raise Done()

        with mock.patch.object(rw, "armed_now", armed_now), \
                mock.patch.object(rw, "run_once", run_once), \
                mock.patch.object(rw, "_announce", announce), \
                mock.patch.object(rw, "datetime", Clock), \
                mock.patch.object(rw.asyncio, "sleep", sleep), \
                mock.patch.object(rw.log, "warning", lambda m, *a, **k: logs.append(m)):
            try:
                asyncio.run(rw.loop(None, cash_reader))
            except Done:
                pass
        return feed, logs

    def test_a_day_of_refusals_is_one_feed_row_and_every_log_line(self):
        feed, logs = self.drive([refused(342.92)] * 96)
        self.assertEqual([e for e, _ in feed], ["RECONCILE_REFUSED"])
        self.assertEqual(sum("[RECONCILE] REFUSED:" in m for m in logs), 96)

    def test_a_refusal_into_its_second_day_is_reminded_once(self):
        feed, _ = self.drive([refused(342.92)] * 97)
        self.assertEqual([e for e, _ in feed], ["RECONCILE_REFUSED", "RECONCILE_REFUSED"])

    def test_the_week_that_flooded_the_feed_is_now_a_handful_of_rows(self):
        week = [refused(393.86)] * (7 * 96) + [{"status": "OK", "detail": "fits"}] * 3
        feed, _ = self.drive(week)
        self.assertEqual([e for e, _ in feed], ["RECONCILE_REFUSED"] * 7 + ["RECONCILE_CLEARED"])


class Source(unittest.TestCase):
    with open("reconcile_worker.py") as _f:
        src = _f.read()

    def test_the_feed_row_is_only_written_through_guard_feed_row(self):
        body = self.src[self.src.index("async def loop("):]
        self.assertEqual(body.count("await _announce("), 1)
        self.assertIn("guard_feed_row(guard_state, report", body)

    def test_it_still_never_touches_orders_or_slices(self):
        for bad in ("grid_buy", "grid_sell", "place_order", "CryptoGridSlice(", "db.delete"):
            self.assertFalse(bad in self.src, bad)

    def test_the_change_writes_nothing_but_feed_rows(self):
        fn = self.src[self.src.index("def guard_feed_row("):self.src.index("async def loop(")]
        for bad in ("await ", "db.", "session", "log."):
            self.assertFalse(bad in fn, bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
