"""The one-time removal of the duplicated ONDO/TIA budget is narrow.

Live 2026-10-14: ONDO-USD $1,666.25 and TIA-USD $1,649.43 against $117.29
and $100.47 before the rotation bug - exactly 16 x $96.81 each. The module
may take back exactly that and nothing else; every other state is left
alone. Fakes only: no database, no venue.
"""
import asyncio
import types
from datetime import datetime, timezone

import pytest

import budget_correction as bc


def run(c):
    return asyncio.run(c)


def B(pid, alloc, locked=False, bot=None):
    return types.SimpleNamespace(product_id=pid, bot_name=bot or pid[:3], allocated_usd=alloc, locked=locked)


def grid_with(branches, slices=None, withdraw_error=None):
    calls, feed = [], []

    async def get_grid_branches():
        return branches

    async def get_grid_slices(bot):
        return (slices or {}).get(bot, [])

    async def withdraw_from_grid_branch(bot, amount, caller=None):
        if withdraw_error:
            raise withdraw_error
        calls.append((bot, amount, caller))
        b = next(x for x in branches if x.bot_name == bot)
        b.allocated_usd = round(b.allocated_usd - amount, 2)
        return {"remaining_allocated_usd": b.allocated_usd}

    async def _record_gate_decision(bot, pid, verdict, reason):
        feed.append((pid, verdict))

    g = types.SimpleNamespace(get_grid_branches=get_grid_branches, get_grid_slices=get_grid_slices,
                              withdraw_from_grid_branch=withdraw_from_grid_branch,
                              _record_gate_decision=_record_gate_decision)
    return g, calls, feed


@pytest.fixture
def marker(monkeypatch):
    state = {"written": False}

    async def exists():
        return state["written"]

    async def write():
        state["written"] = True

    monkeypatch.setattr(bc, "_marker_exists", exists)
    monkeypatch.setattr(bc, "_write_marker", write)
    return state


NOW = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)


def live():
    return [B("ONDO-USD", 1666.25, bot="crypto_grid_4"), B("TIA-USD", 1649.43, bot="crypto_grid_7"),
            B("APE-USD", 96.81, bot="crypto_grid_8")]


def test_the_two_figures_are_the_same_duplicated_amount():
    a = round(1666.25 - 117.29, 2)
    b = round(1649.43 - 100.47, 2)
    assert a == b == 1548.96 == round(16 * 96.81, 2)


def test_it_takes_back_exactly_the_duplicate_from_both(marker):
    g, calls, feed = grid_with(live())
    r = run(bc.run_once(g, NOW))
    assert r["reason"] == "CORRECTED" and r["taken_back_usd"] == 3097.92
    assert [(c[0], c[1]) for c in calls] == [("crypto_grid_4", 1548.96), ("crypto_grid_7", 1548.96)]
    assert all("budget_correction" in c[2] for c in calls)
    assert marker["written"] is True
    assert {f[1] for f in feed} == {"BUDGET_CORRECTED"}


def test_apes_budget_is_never_touched(marker):
    g, calls, _ = grid_with(live())
    run(bc.run_once(g, NOW))
    assert all(c[0] != "crypto_grid_8" for c in calls)


def test_a_second_run_does_nothing(marker):
    g, calls, _ = grid_with(live())
    run(bc.run_once(g, NOW))
    n = len(calls)
    r = run(bc.run_once(g, NOW))
    assert r["reason"] == "ALREADY_DONE" and len(calls) == n


def test_if_the_owner_already_withdrew_it_leaves_that_branch_alone(marker):
    bs = [B("ONDO-USD", 117.29, bot="crypto_grid_4"), B("TIA-USD", 1649.43, bot="crypto_grid_7")]
    g, calls, _ = grid_with(bs)
    r = run(bc.run_once(g, NOW))
    assert [c[0] for c in calls] == ["crypto_grid_7"] and r["taken_back_usd"] == 1548.96
    assert "left alone" in r["left_alone"][0]["why"]


def test_any_other_allocation_is_left_alone(marker):
    for alloc in (1666.30, 1700.00, 1000.00, 117.29):
        bs = [B("ONDO-USD", alloc, bot="crypto_grid_4"), B("TIA-USD", alloc, bot="crypto_grid_7")]
        g, calls, _ = grid_with(bs)
        run(bc.run_once(g, NOW))
        assert calls == [], alloc


def test_a_locked_branch_is_never_unlocked_or_touched(marker):
    bs = [B("ONDO-USD", 1666.25, locked=True, bot="crypto_grid_4"), B("TIA-USD", 1649.43, bot="crypto_grid_7")]
    g, calls, _ = grid_with(bs)
    run(bc.run_once(g, NOW))
    assert [c[0] for c in calls] == ["crypto_grid_7"]


def test_a_branch_holding_a_slice_is_left_for_the_next_boot(marker):
    g, calls, _ = grid_with(live(), slices={"crypto_grid_4": [object()]})
    r = run(bc.run_once(g, NOW))
    assert [c[0] for c in calls] == ["crypto_grid_7"]
    assert marker["written"] is False                 # ONDO still to do
    assert "open slice" in r["left_alone"][0]["why"]


def test_a_refused_withdraw_is_not_marked_done(marker):
    g, calls, _ = grid_with(live(), withdraw_error=ValueError("keep-alive floor"))
    r = run(bc.run_once(g, NOW))
    assert calls == [] and marker["written"] is False and r["reason"] == "NOTHING_TO_CORRECT"


def test_an_unreadable_book_touches_nothing(marker):
    g, calls, _ = grid_with(live())

    async def boom():
        raise RuntimeError("db down")
    g.get_grid_branches = boom
    r = run(bc.run_once(g, NOW))
    assert r["reason"] == "READ_FAILED" and calls == [] and marker["written"] is False


def test_it_expires(marker):
    g, calls, _ = grid_with(live())
    r = run(bc.run_once(g, datetime(2026, 10, 21, 0, 1, tzinfo=timezone.utc)))
    assert r["reason"] == "EXPIRED" and calls == []
