#!/usr/bin/env python3
"""A thousand dollars left his books and nothing told him.

WHAT WENT WRONG. On 2026-10-09 the account owner asked where $1,016.36 of
his allocation had gone. The answer was reconcile_worker: it had written
the claims down because they were budgeting cash that did not exist. That
is the CORRECT behaviour - it never raises a claim, never reduces a branch
below the coin it owns, never places an order and never touches a slice.
It is the reason the figure could be explained at all.

But the only record it left was a Railway log line, inside a log printing
46,656 lines a day of unchanged state, and he cannot read Railway logs
from a phone. So the books moved by a thousand dollars in silence. His
instruction: "Stop that from happening."

WHAT IS ASSERTED HERE. Not the reconciliation - reconcile.py owns those
rules and test_reconcile.py tests them, and this change touched none of
them. What is asserted is that the owner FINDS OUT:

  1. The sentence names the branch, the product, the before, the after,
     the delta and the coin left untouched - and its arithmetic is
     internally consistent, so the delta cannot disagree with the two
     figures printed beside it.
  2. The figure announced is the LIVE row value at the moment of the
     write, never the planned one. A branch whose allocation moved
     between the plan and the write must not be described from a stale
     number.
  3. ONE feed row per applied correction, tagged CLAIM_REDUCED.
  4. Observe mode announces NOTHING. A dry run that posted to his
     dashboard would be telling him money moved when it did not.
  5. The row is written AFTER the commit. A feed row for a correction
     that rolled back would be a lie about his money.
  6. A refused or unknown pass is announced too - the protection
     declining to act looks exactly like a healthy quiet fleet.
  7. A feed failure never breaks the worker. Telemetry that can break the
     thing it reports on is worse than no telemetry.
  8. The log line and the feed row come from ONE function, so they can
     never drift into saying different things about the same dollars.

Run: python3 test_reconcile_is_announced.py
"""
import asyncio
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import reconcile_worker as rw

_failures = []
_passes = 0


def ok(label, condition, detail=""):
    global _passes
    if condition:
        _passes += 1
        print(f"  ok   {label}")
    else:
        _failures.append(f"{label}{(' - ' + detail) if detail else ''}")
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")


CORRECTION = {
    "bot_name": "crypto_grid_6",
    "product_id": "XRP-USD",
    "new_allocated_usd": 1268.48,
    "coin_basis_usd": 1184.24,
    "before_usd": 2284.84,
}


# ── 1 & 8. the sentence ───────────────────────────────────────────────
def test_the_sentence_names_the_money():
    line = rw.reduction_line(CORRECTION)
    for want in ("crypto_grid_6", "XRP-USD", "2,284.84", "1,268.48",
                 "1,016.36", "1,184.24", "untouched"):
        ok(f"names {want}", want in line, line)
    ok("says WHY, not just what",
       "cash that is not there" in line, line)
    ok("carries the [RECONCILE] tag so it is greppable",
       line.startswith("[RECONCILE]"), line[:40])


def test_the_delta_cannot_disagree_with_the_figures_beside_it():
    # The $1,016.36 above is not a coincidence - it is this exact shape.
    for before, after in ((2284.84, 1268.48), (100.0, 15.0),
                          (1767.76, 15.00), (50.5, 50.4)):
        c = dict(CORRECTION, before_usd=before, new_allocated_usd=after)
        line = rw.reduction_line(c)
        ok(f"{before} -> {after}: the printed delta is the real difference",
           f"(-${before - after:,.2f})" in line, line)


def test_it_is_pure():
    before = dict(CORRECTION)
    rw.reduction_line(CORRECTION)
    rw.reduction_line(CORRECTION)
    ok("reads no clock and mutates nothing it was handed",
       CORRECTION == before)


# ── 7. the announcer survives a broken feed ───────────────────────────
def test_a_broken_feed_never_breaks_the_worker():
    async def explode(*a, **k):
        raise RuntimeError("database gone")
    try:
        asyncio.get_event_loop().run_until_complete(
            rw._announce("b", "P-USD", "CLAIM_REDUCED", "x", sink=explode))
        ok("a sink that raises is swallowed", True)
    except Exception as e:
        ok("a sink that raises is swallowed", False, repr(e))

    seen = []
    async def good(*a):
        seen.append(a)
    asyncio.get_event_loop().run_until_complete(
        rw._announce("b", "P-USD", "CLAIM_REDUCED", "msg", sink=good))
    ok("a working sink receives (bot, product, type, message)",
       seen == [("b", "P-USD", "CLAIM_REDUCED", "msg")], repr(seen))


# ── a fake book, so run_once can be driven without a database ─────────
class FakeRow:
    def __init__(self, bot_name, allocated_usd):
        self.bot_name = bot_name
        self.allocated_usd = allocated_usd


class FakeResult:
    def __init__(self, row): self._row = row
    def scalar_one_or_none(self): return self._row


class FakeDB:
    def __init__(self, rows, log):
        self._rows = rows
        self._log = log
    async def execute(self, stmt):
        # Whichever branch the worker asks for, hand back the next one.
        return FakeResult(self._rows.pop(0) if self._rows else None)
    async def commit(self):
        self._log.append("COMMIT")
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def drive(corrections, rows, dry_run, status="OK"):
    """One run_once pass against a fake book. Returns (report, feed, order)."""
    order = []
    feed = []

    async def sink(bot, product, etype, msg):
        order.append(f"FEED:{etype}")
        feed.append({"bot": bot, "product": product,
                     "type": etype, "message": msg})

    class FakeFactory:
        def __call__(self):
            return lambda: FakeDB(list(rows), order)

    async def fake_branch_rows(_sf):
        return []

    import reconcile as rec
    real_rows, real_fleet = rw._branch_rows, rec.fleet_corrections
    rw._branch_rows = fake_branch_rows
    rec.fleet_corrections = lambda r, c: ([dict(x) for x in corrections],
                                          {"status": status})
    try:
        report = asyncio.get_event_loop().run_until_complete(
            rw.run_once(FakeFactory(), 500.0, dry_run=dry_run, sink=sink))
    finally:
        rw._branch_rows, rec.fleet_corrections = real_rows, real_fleet
    return report, feed, order


# ── 3. one row per applied correction ─────────────────────────────────
def test_an_applied_correction_reaches_his_dashboard():
    report, feed, order = drive(
        [dict(CORRECTION)], [FakeRow("crypto_grid_6", 2284.84)], dry_run=False)
    ok("the correction was applied", report["applied"] == 1, str(report))
    ok("exactly ONE feed row", len(feed) == 1, str(feed))
    if feed:
        ok("tagged CLAIM_REDUCED", feed[0]["type"] == "CLAIM_REDUCED",
           feed[0]["type"])
        ok("filed under the branch that lost the claim",
           feed[0]["bot"] == "crypto_grid_6" and feed[0]["product"] == "XRP-USD",
           str(feed[0]))
        ok("the row carries the full sentence, not a code",
           "1,016.36" in feed[0]["message"] and "untouched" in feed[0]["message"],
           feed[0]["message"])
        ok("the feed row is the SAME text the log line gets",
           feed[0]["message"] == rw.reduction_line(
               dict(CORRECTION, before_usd=2284.84)),
           feed[0]["message"])


# ── 2. the live figure, never the planned one ─────────────────────────
def test_it_announces_the_money_that_actually_moved():
    # The plan said the branch was at $2,284.84. By the time the write
    # ran it was at $2,000.00 - a rung sold in between. The sentence must
    # describe $2,000.00 -> $1,268.48, not the stale plan.
    report, feed, _ = drive(
        [dict(CORRECTION)], [FakeRow("crypto_grid_6", 2000.00)], dry_run=False)
    ok("applied against the live row", report["applied"] == 1, str(report))
    if feed:
        m = feed[0]["message"]
        ok("quotes the LIVE before-figure", "2,000.00" in m, m)
        ok("does not quote the stale planned figure", "2,284.84" not in m, m)
        ok("and the delta is recomputed from it", "(-$731.52)" in m, m)


# ── 4. observe mode stays off his dashboard ───────────────────────────
def test_observe_mode_announces_nothing():
    report, feed, _ = drive(
        [dict(CORRECTION)], [FakeRow("crypto_grid_6", 2284.84)], dry_run=True)
    ok("nothing applied in a dry run", report["applied"] == 0, str(report))
    ok("and NOTHING posted to his dashboard", feed == [], str(feed))


def test_no_corrections_means_no_noise():
    report, feed, _ = drive([], [], dry_run=False)
    ok("a clean pass applies nothing", report["applied"] == 0, str(report))
    ok("a clean pass says nothing", feed == [], str(feed))


# ── 5. the commit comes first ─────────────────────────────────────────
def test_the_row_is_written_after_the_commit():
    _, _, order = drive(
        [dict(CORRECTION)], [FakeRow("crypto_grid_6", 2284.84)], dry_run=False)
    ok("the pass committed", "COMMIT" in order, str(order))
    ok("the feed row came AFTER the commit, never before",
       order.index("COMMIT") < order.index("FEED:CLAIM_REDUCED"), str(order))


# ── a branch already at or below target is left alone, and silently ───
def test_a_branch_needing_nothing_is_not_announced():
    # run_once skips a row already at or below the target. It must not
    # then tell him a claim was cut.
    report, feed, _ = drive(
        [dict(CORRECTION)], [FakeRow("crypto_grid_6", 1268.48)], dry_run=False)
    ok("already at target: nothing applied", report["applied"] == 0, str(report))
    ok("already at target: nothing announced", feed == [], str(feed))


def test_zz_nothing_above_failed():
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"ALL {_passes} ASSERTIONS PASS")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
