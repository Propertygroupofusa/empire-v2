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


# ------------------------------------------------------------------ wiring
MAIN = open("main.py", encoding="utf-8").read()


def test_the_correction_is_awaited_before_claim_reconciliation_starts():
    i = MAIN.index("await budget_correction.run_once(")
    j = MAIN.index("import reconcile_worker")
    assert i < j


def test_a_failure_in_the_correction_can_never_stop_startup():
    i = MAIN.index("import budget_correction")
    block = MAIN[max(0, i - 200):i + 900]
    assert "try:" in block and "except Exception as e:" in block


def test_it_is_handed_the_real_grid_module_and_nothing_else():
    assert "import crypto_grid_bot as _bc_grid" in MAIN
    assert "budget_correction.run_once(_bc_grid)" in MAIN


def test_the_hold_and_the_correction_speak_about_the_same_two_figures():
    import crypto_grid_bot as grid
    assert {t["product_id"]: t["inflated_usd"] for t in bc.TARGETS} == grid.KNOWN_INFLATED_BUDGETS


def test_the_corrected_budgets_clear_the_keep_alive_floor_so_neither_branch_is_deleted():
    import crypto_grid_bot as grid
    for t in bc.TARGETS:
        assert t["original_usd"] >= grid.GRID_KEEP_BRANCH_ALIVE_USD


def test_the_buy_hold_lifts_the_moment_the_correction_lands():
    import crypto_grid_bot as grid
    for t in bc.TARGETS:
        assert grid.inflated_budget_hold(t["product_id"], t["inflated_usd"])[0] is True
        assert grid.inflated_budget_hold(t["product_id"], t["original_usd"])[0] is False


def test_the_call_it_makes_matches_the_real_withdraw_signature():
    import inspect
    import crypto_grid_bot as grid
    sig = inspect.signature(grid.withdraw_from_grid_branch)
    assert list(sig.parameters)[:2] == ["bot_name", "amount"]
    assert "caller" in sig.parameters and "allow_delete" in sig.parameters
    assert sig.parameters["allow_delete"].default is False      # it can never close a branch


def test_it_never_asks_to_delete_a_branch():
    src = open("budget_correction.py", encoding="utf-8").read()
    assert "allow_delete" not in src


# -------------------------------------------- end to end, real SQLite, real code
def test_end_to_end_against_a_real_database(monkeypatch):
    """The two inflated branches and APE, written to a throwaway database; the
    real get_grid_branches, get_grid_slices and withdraw_from_grid_branch do the
    work. Only the session factory is pointed at the temporary file."""
    import os
    import tempfile
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import crypto_grid_bot as grid
    import database
    from models import Base, CryptoGridBranch, TradingBotState

    path = tempfile.mktemp(suffix=".db")

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)

        def sf():
            return async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(grid, "get_session_factory", sf)
        monkeypatch.setattr(database, "get_session_factory", sf)
        async with sf()() as db:
            for bot, pid, a in (("g_ondo", "ONDO-USD", 1666.25), ("g_tia", "TIA-USD", 1649.43),
                                ("g_ape", "APE-USD", 96.81)):
                db.add(CryptoGridBranch(bot_name=bot, product_id=pid, allocated_usd=a,
                                        num_levels=3, grid_pct=0.03, reference_price=1.0,
                                        peak_equity=a))
            await db.commit()

        first = await bc.run_once(grid)
        async with sf()() as db:
            rows = {b.product_id: b for b in (await db.execute(select(CryptoGridBranch))).scalars().all()}
            marker = (await db.execute(select(TradingBotState).where(
                TradingBotState.bot_name == bc.MARKER_KEY))).scalar_one_or_none()
        second = await bc.run_once(grid)
        await engine.dispose()
        return first, rows, marker, second

    try:
        first, rows, marker, second = run(go())
    finally:
        if os.path.exists(path):
            os.remove(path)

    assert first["reason"] == "CORRECTED" and first["taken_back_usd"] == 3097.92
    assert round(rows["ONDO-USD"].allocated_usd, 2) == 117.29
    assert round(rows["TIA-USD"].allocated_usd, 2) == 100.47
    assert round(rows["APE-USD"].allocated_usd, 2) == 96.81          # never touched
    assert len(rows) == 3                                            # nothing deleted
    assert marker is not None
    assert second["reason"] == "ALREADY_DONE"
    assert round(rows["ONDO-USD"].allocated_usd, 2) < round(rows["ONDO-USD"].peak_equity + 0.01, 2)
