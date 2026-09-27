"""Tests for the loop that writes slices against real holdings.

It spends nothing, which makes it feel safe and is exactly why it needs
this file: it hands the owner's coin to a trading loop, and from that
moment the coin is traded rather than held. That change is one-way.
"""
import ast

import pytest

import coin_adoption_worker as w

SRC = open("coin_adoption_worker.py").read()
TREE = ast.parse(SRC)


def fn(name):
    return next(n for n in ast.walk(TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


# ------------------------------------------------- it ships disarmed

def test_it_is_disarmed_unless_the_setting_says_exactly_arm(monkeypatch):
    monkeypatch.delenv(w.MODE_ENV, raising=False)
    assert w.is_armed() is False
    for junk in ("", "observe", "true", "yes", "1", "on", "ARMED", "disarm"):
        monkeypatch.setenv(w.MODE_ENV, junk)
        assert w.is_armed() is False, junk


def test_whitespace_around_arm_still_arms_it(monkeypatch):
    """Stated in the docstring because the docstring once claimed the
    opposite and the owner was told the wrong thing twice."""
    for v in ("arm", "ARM", " arm ", "Arm\n"):
        monkeypatch.setenv(w.MODE_ENV, v)
        assert w.is_armed() is True, repr(v)
    assert "ARM " in w.current_mode.__doc__


def test_an_observing_pass_fetches_nothing_at_all(monkeypatch):
    """A disarmed deployment must do no work, so it cannot fail into
    adopting through some half-completed path.

    Called with session_factory=None on purpose: if the observe path ever
    starts touching the database, this raises instead of passing quietly.
    """
    import asyncio
    monkeypatch.setenv(w.MODE_ENV, "observe")
    r = asyncio.run(w.check_once(None))
    assert r["armed"] is False and r["adopted"] == 0
    assert "observing, nothing fetched" in r["detail"]


def test_the_permission_is_checked_twice():
    src = "\n".join(SRC.splitlines()[fn("check_once").lineno - 1:fn("check_once").end_lineno])
    assert src.count("is_armed()") >= 2


def test_it_stops_writing_the_moment_it_is_disarmed_mid_pass():
    src = "\n".join(SRC.splitlines()[fn("check_once").lineno - 1:fn("check_once").end_lineno])
    i = src.index("for a in plan")
    assert "if not is_armed():" in src[i:]
    assert "break" in src[i:]


# --------------------------------------- the three load-bearing rules

def test_the_branch_starts_full_so_it_cannot_buy_before_it_sells():
    """num_levels must equal the slice count. Higher, and the branch
    immediately tries to buy rungs with cash nobody earmarked to it."""
    assert 'num_levels=a["num_levels"]' in SRC
    assert '"num_levels": len(slices)' in open("coin_adoption.py").read()


def test_the_branch_claims_exactly_what_its_coin_is_worth():
    assert 'allocated_usd=a["allocated_usd"]' in SRC


def test_an_adopted_branch_never_runs_the_eight_percent_entry_stop():
    """An adopted entry is the price on the day it was adopted. An 8%
    wobble would liquidate a hold of any age against a cost basis that
    never existed."""
    assert "stop_loss_pct_override=ADOPTED_STOP_PCT" in SRC
    assert w.ADOPTED_STOP_PCT == 0.0


def test_every_written_slice_is_marked_adopted():
    """Without the flag the ledger computes lifetime returns from an
    entry price nobody paid."""
    assert "adopted=True," in SRC


# --------------------------------------------------- the refusals

def test_an_unreadable_claim_list_refuses_rather_than_adopting():
    """Adopting a coin a branch already holds puts two systems on one
    pooled balance - the structural gap behind this repo's phantom
    positions."""
    src = "\n".join(SRC.splitlines()[fn("check_once").lineno - 1:fn("check_once").end_lineno])
    assert "refusing" in src and "unknown claim list" in src


def test_a_coin_that_gained_a_branch_since_the_plan_is_skipped():
    """The census and the plan take time. A branch created in between
    must not be doubled up on."""
    assert "gained a branch since the plan" in SRC


def test_a_coin_already_adopted_is_never_adopted_twice():
    assert "_already_adopted" in SRC
    assert "CryptoGridSlice.adopted == True" in SRC


def test_one_coin_failing_does_not_abandon_the_rest():
    src = "\n".join(SRC.splitlines()[fn("check_once").lineno - 1:fn("check_once").end_lineno])
    i = src.index("for a in plan")
    assert "except Exception as exc:" in src[i:]


# ------------------------------------------------ it cannot trade

def test_the_worker_never_places_an_order():
    """Adoption is bookkeeping. If this file can reach a venue's order
    path, it is no longer adoption."""
    called = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call):
            f = node.func
            called.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    for banned in ("grid_buy", "grid_sell", "place_market_buy", "place_market_sell",
                   "place_order", "create_grid_branch"):
        assert banned not in called, f"the adoption worker CALLS {banned}"


def test_the_loop_never_dies():
    src = "\n".join(SRC.splitlines()[fn("run_periodically").lineno - 1:
                                     fn("run_periodically").end_lineno])
    assert "while True:" in src and "except Exception" in src
    assert "HEARTBEAT[\"last_pass_at\"]" in src


def test_the_heartbeat_ticks_whether_the_pass_worked_or_threw():
    """A heartbeat that only ticks on success cannot tell 'not running'
    from 'running and failing', which is the distinction it exists for."""
    src = "\n".join(SRC.splitlines()[fn("run_periodically").lineno - 1:
                                     fn("run_periodically").end_lineno])
    i = src.index("except Exception")
    assert 'HEARTBEAT["last_pass_at"]' in src[i:]
