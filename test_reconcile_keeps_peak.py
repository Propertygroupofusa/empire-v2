"""A claim cut must not read as a crash.

THE BUG, measured live 2026-10-11 21:16Z. reconcile_worker wrote $350.08 of
unspent claims down across 15 branches after the owner moved his USD into
USDC. It lowered allocated_usd and left peak_equity alone, so the 25%
drawdown breaker - (peak - (allocated + unrealized)) / peak - read the cut
as a loss. BTC-USD: claim $52.84 -> $40.46, market loss $0.14, breaker
41.91%, buys frozen.

These tests drive run_once against a fake session with the live BTC
figures and assert on the row it leaves behind.
"""
import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import reconcile
import reconcile_worker as W
from crypto_grid_bot import GRID_DRAWDOWN_BREAKER_PCT, peak_after_withdrawal


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Result:
    def __init__(self, items):
        self.items = items

    def scalar_one_or_none(self):
        return self.items[0] if self.items else None

    def scalars(self):
        return self

    def all(self):
        return list(self.items)


class FakeDB:
    def __init__(self, branches, slices):
        self.branches, self.slices, self.committed = branches, slices, False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"].__name__
        wanted = next(iter(stmt.compile().params.values()), None)
        pool = self.branches if entity == "CryptoGridBranch" else self.slices
        return Result([r for r in pool if wanted is None or r.bot_name == wanted])

    async def commit(self):
        self.committed = True


# The live BTC branch at 21:16Z on 2026-10-11.
BTC_ENTRY = 84149.61
BTC_QTY = round(18.07 / BTC_ENTRY, 8)
BTC_PRICE = 83510.63
BTC_BEFORE, BTC_AFTER, BTC_PEAK = 52.84, 40.46, 69.42


def drawdown(peak, allocated, unrealized):
    return (peak - (allocated + unrealized)) / peak


def run(branch, slices, correction, prices, monkeypatch, helper=None, dry_run=False):
    db = FakeDB([branch], slices)

    async def no_rows(_sf):
        return []

    async def sink(*a, **k):
        return None

    monkeypatch.setattr(W, "_branch_rows", no_rows)
    monkeypatch.setattr(reconcile, "fleet_corrections",
                        lambda rows, cash: ([dict(correction)], {"status": "OK"}))
    # A private loop, never asyncio.run: asyncio.run clears the thread's
    # current loop on exit, and sibling test files still call
    # asyncio.get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        report = loop.run_until_complete(W.run_once(
            lambda: (lambda: db), 600.0, dry_run=dry_run, sink=sink,
            audit_sink=sink, prices=prices, peak_helper=helper))
    finally:
        loop.close()
    return report, db


def btc():
    branch = Row(bot_name="crypto_grid_1", product_id="BTC-USD",
                 allocated_usd=BTC_BEFORE, peak_equity=BTC_PEAK)
    slices = [Row(bot_name="crypto_grid_1", qty=BTC_QTY, entry_price=BTC_ENTRY)]
    correction = {"bot_name": "crypto_grid_1", "product_id": "BTC-USD",
                  "new_allocated_usd": BTC_AFTER, "coin_basis_usd": 18.07}
    return branch, slices, correction


def test_the_live_btc_cut_no_longer_freezes_the_branch(monkeypatch):
    branch, slices, correction = btc()
    unreal = BTC_QTY * (BTC_PRICE - BTC_ENTRY)
    before = drawdown(BTC_PEAK, BTC_BEFORE, unreal)
    assert before < GRID_DRAWDOWN_BREAKER_PCT, "BTC was not frozen before the cut"

    report, db = run(branch, slices, correction, {"BTC-USD": BTC_PRICE}, monkeypatch)

    assert report["applied"] == 1 and db.committed
    assert branch.allocated_usd == BTC_AFTER, "the claim cut itself must still land"
    after = drawdown(branch.peak_equity, branch.allocated_usd, unreal)
    assert after < GRID_DRAWDOWN_BREAKER_PCT, f"still frozen at {after:.2%}"
    assert after == pytest.approx(before, abs=0.001), "the reading must not move"


def test_without_the_fix_the_same_cut_freezes_it(monkeypatch):
    """The failure this replaces, reproduced: a helper that leaves the peak
    alone is exactly the old behaviour, and it reads 41.9%."""
    branch, slices, correction = btc()
    run(branch, slices, correction, {"BTC-USD": BTC_PRICE}, monkeypatch,
        helper=lambda *a, **k: None)
    unreal = BTC_QTY * (BTC_PRICE - BTC_ENTRY)
    assert branch.peak_equity == BTC_PEAK
    assert drawdown(branch.peak_equity, branch.allocated_usd, unreal) > 0.40


def test_no_price_takes_the_subtraction_path(monkeypatch):
    branch, slices, correction = btc()
    run(branch, slices, correction, None, monkeypatch)
    assert branch.peak_equity == round(BTC_PEAK - (BTC_BEFORE - BTC_AFTER), 2)


def test_a_broken_helper_leaves_the_peak_and_keeps_the_cut(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("helper unreadable")
    branch, slices, correction = btc()
    report, _ = run(branch, slices, correction, {"BTC-USD": BTC_PRICE},
                    monkeypatch, helper=boom)
    assert report["applied"] == 1
    assert branch.allocated_usd == BTC_AFTER
    assert branch.peak_equity == BTC_PEAK


def test_a_null_peak_stays_null(monkeypatch):
    """NULL means 'not yet initialized' and the grid self-heals it to live
    equity. Writing a number here would invent a high-water mark."""
    branch, slices, correction = btc()
    branch.peak_equity = None
    run(branch, slices, correction, {"BTC-USD": BTC_PRICE}, monkeypatch)
    assert branch.peak_equity is None


def test_a_real_crash_stays_breached_through_a_cut(monkeypatch):
    """It cannot blunt the breaker: a branch really down 40% on its coin
    still reads 40% after its claim is cut."""
    branch = Row(bot_name="g", product_id="X-USD", allocated_usd=100.0, peak_equity=100.0)
    slices = [Row(bot_name="g", qty=1.0, entry_price=100.0)]
    correction = {"bot_name": "g", "product_id": "X-USD",
                  "new_allocated_usd": 80.0, "coin_basis_usd": 100.0}
    run(branch, slices, correction, {"X-USD": 60.0}, monkeypatch)
    assert drawdown(branch.peak_equity, branch.allocated_usd, -40.0) >= GRID_DRAWDOWN_BREAKER_PCT


def test_dry_run_touches_neither_number(monkeypatch):
    branch, slices, correction = btc()
    run(branch, slices, correction, {"BTC-USD": BTC_PRICE}, monkeypatch, dry_run=True)
    assert branch.allocated_usd == BTC_BEFORE and branch.peak_equity == BTC_PEAK


def test_the_peak_rule_is_imported_not_reimplemented():
    src = open(os.path.join(HERE, "reconcile_worker.py"), encoding="utf-8").read()
    assert "crypto_grid_bot.peak_after_withdrawal" in src
    assert "equity_after / equity_before" not in src


def test_main_hands_the_worker_live_prices():
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    i = src.index("reconcile_worker.loop(")
    assert "_reconcile_prices" in src[i:i + 120]


def test_unrealized_is_none_not_zero_when_unpriceable():
    s = [Row(qty=1.0, entry_price=10.0)]
    assert W._unrealized(s, None) is None
    assert W._unrealized(s, "nan") is None
    assert W._unrealized(s, 0) is None
    assert W._unrealized(s, 12.0) == 2.0


def test_helper_signature_matches():
    """If the helper's keyword ever changes, the worker's call must too."""
    assert peak_after_withdrawal(100.0, 50.0, 40.0, unrealized=0.0) == 80.0


# ── the one-time repair of cuts written before the fix ─────────────────
from datetime import datetime, timedelta


class RepairDB:
    """Enough of a session for repair_peaks_once: branches, audit rows,
    markers, and the add/commit it writes with."""

    def __init__(self, branches, audits, markers=()):
        self.branches, self.audits = branches, audits
        self.markers, self.added, self.committed = list(markers), [], False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"].__name__
        p = list(stmt.compile().params.values())
        if entity == "TradingBotState":
            return Result([m for m in self.markers if m.bot_name == p[0]])
        if entity == "ClaimReductionAudit":
            src, since, upper = p
            return Result(sorted((a for a in self.audits
                                  if a.source_component == src
                                  and since <= a.created_at < upper),
                                 key=lambda a: a.created_at))
        if entity == "CryptoGridBranch":
            return Result([b for b in self.branches if b.bot_name == p[0]])
        return Result([])  # slices are read through _slices_by_bot

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.committed = True


def cut(bot, before, after, at, src="reconcile_worker"):
    return Row(bot_name=bot, previous_allocated_usd=before, new_allocated_usd=after,
               created_at=at, source_component=src)


def repair(db, prices, started_at, slices, monkeypatch, helper=None):
    async def by_bot(_sf):
        out = {}
        for s in slices:
            out.setdefault(s.bot_name, []).append(s)
        return out

    async def sink(*a, **k):
        return None

    monkeypatch.setattr(W, "_slices_by_bot", by_bot)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(W.repair_peaks_once(
            lambda: (lambda: db), prices, started_at, helper=helper, sink=sink))
    finally:
        loop.close()


T0 = datetime(2026, 10, 11, 21, 0)
START = datetime(2026, 10, 11, 21, 45)


def frozen_btc():
    """BTC as the USDC episode left it: two cuts, peak never moved."""
    branch = Row(bot_name="crypto_grid_1", product_id="BTC-USD",
                 allocated_usd=38.0, peak_equity=BTC_PEAK)
    slices = [Row(bot_name="crypto_grid_1", qty=BTC_QTY, entry_price=BTC_ENTRY)]
    audits = [cut("crypto_grid_1", BTC_BEFORE, BTC_AFTER, T0),
              cut("crypto_grid_1", BTC_AFTER, 38.0, T0 + timedelta(minutes=30))]
    return branch, slices, audits


def test_the_repair_unfreezes_btc_back_to_its_pre_episode_reading(monkeypatch):
    branch, slices, audits = frozen_btc()
    unreal = BTC_QTY * (BTC_PRICE - BTC_ENTRY)
    pre = drawdown(BTC_PEAK, BTC_BEFORE, unreal)
    assert drawdown(branch.peak_equity, branch.allocated_usd, unreal) > 0.40
    db = RepairDB([branch], audits)
    rep = repair(db, {"BTC-USD": BTC_PRICE}, START, slices, monkeypatch)
    assert rep["status"] == "REPAIRED" and len(rep["repaired"]) == 1
    after = drawdown(branch.peak_equity, branch.allocated_usd, unreal)
    assert after == pytest.approx(pre, abs=0.002)
    assert after < GRID_DRAWDOWN_BREAKER_PCT
    assert db.committed and [m.bot_name for m in db.added] == [W.REPAIR_MARKER]


def test_the_repair_runs_once(monkeypatch):
    branch, slices, audits = frozen_btc()
    db = RepairDB([branch], audits, markers=[Row(bot_name=W.REPAIR_MARKER)])
    rep = repair(db, {"BTC-USD": BTC_PRICE}, START, slices, monkeypatch)
    assert rep["status"] == "ALREADY_DONE"
    assert branch.peak_equity == BTC_PEAK and not db.added


def test_cuts_written_by_a_fixed_pass_are_not_replayed(monkeypatch):
    """A cut written after this process started already moved its peak."""
    branch, slices, _ = frozen_btc()
    late = [cut("crypto_grid_1", BTC_BEFORE, BTC_AFTER, START + timedelta(minutes=1))]
    db = RepairDB([branch], late)
    repair(db, {"BTC-USD": BTC_PRICE}, START, slices, monkeypatch)
    assert branch.peak_equity == BTC_PEAK


def test_older_cuts_and_other_writers_are_left_alone(monkeypatch):
    branch, slices, _ = frozen_btc()
    other = [cut("crypto_grid_1", BTC_BEFORE, BTC_AFTER, T0 - timedelta(days=2)),
             cut("crypto_grid_1", BTC_BEFORE, BTC_AFTER, T0, src="branch_rightsize")]
    db = RepairDB([branch], other)
    repair(db, {"BTC-USD": BTC_PRICE}, START, slices, monkeypatch)
    assert branch.peak_equity == BTC_PEAK


def test_a_real_crash_stays_breached_through_the_repair(monkeypatch):
    branch = Row(bot_name="g", product_id="X-USD", allocated_usd=80.0, peak_equity=100.0)
    slices = [Row(bot_name="g", qty=1.0, entry_price=100.0)]
    db = RepairDB([branch], [cut("g", 100.0, 80.0, T0)])
    repair(db, {"X-USD": 60.0}, START, slices, monkeypatch)
    assert drawdown(branch.peak_equity, branch.allocated_usd, -40.0) >= GRID_DRAWDOWN_BREAKER_PCT


def test_a_broken_database_writes_nothing_and_does_not_raise(monkeypatch):
    class Broken(RepairDB):
        async def execute(self, stmt):
            raise RuntimeError("db down")
    branch, slices, audits = frozen_btc()
    db = Broken([branch], audits)
    rep = repair(db, {"BTC-USD": BTC_PRICE}, START, slices, monkeypatch)
    assert rep["status"] == "FAILED"
    assert branch.peak_equity == BTC_PEAK and not db.added and not db.committed


def test_the_loop_repairs_before_its_first_pass():
    src = open(os.path.join(HERE, "reconcile_worker.py"), encoding="utf-8").read()
    body = src[src.index("async def loop("):]
    assert body.index("repair_peaks_once(") < body.index("run_once(")
