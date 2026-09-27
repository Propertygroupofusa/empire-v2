"""Tests for the per-branch stop, and the reason it had to exist.

The fleet stop sells any slice whose price falls 8% below its ENTRY. An
adopted slice's entry is the price on the day a branch took charge of
coin the owner may have held for a year - so an 8% wobble would
liquidate a long-term hold and book it as a stop_loss against a cost
basis nobody ever paid.

The dangerous half of this change is not the new behaviour. It is
whether the OLD behaviour survived it untouched, because every existing
branch runs through the same block.
"""
import ast

import pytest


SRC = open("crypto_grid_bot.py").read()
TREE = ast.parse(SRC)


def cycle_src():
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "run_grid_branch_cycle")
    return "\n".join(SRC.splitlines()[fn.lineno - 1:fn.end_lineno])


# ---------------------------------------- the old path is untouched

def test_a_branch_with_no_override_still_runs_the_adaptive_resolver():
    """Every branch that exists today has a NULL override. If this stops
    calling adaptive_stop.resolve for them, the change silently removed
    per-coin stops from the whole live fleet."""
    src = cycle_src()
    assert "adaptive_stop.resolve(branch.product_id, GRID_STOP_LOSS_PCT, _stop_vol)" in src
    assert "_override is not None" in src


def test_the_fleet_default_is_still_the_fallback_on_every_error_path():
    src = cycle_src()
    #  once in the else-branch's opening, once in the except handler,
    #  and once when an unreadable override falls back to it
    assert src.count("_stop_pct = GRID_STOP_LOSS_PCT") >= 3


def test_an_unreadable_override_falls_back_to_the_fleet_stop_not_to_none():
    """A stop must never be removed by a path that merely failed to read
    a number. Garbage in the column has to land on the fleet default AND
    re-enter the adaptive path, not leave the slice uncovered."""
    src = cycle_src()
    i = src.index("_override = getattr(branch")
    j = src.index("_stop_slice = None", i)
    block = src[i:j]
    assert "except (TypeError, ValueError)" in block
    assert "_stop_pct = GRID_STOP_LOSS_PCT" in block
    assert "_override = None" in block          # so the adaptive path still runs


def test_the_stop_still_bypasses_the_profitable_slice_check():
    """The stop's whole job is to sell at a loss on purpose. If this
    change routed it through _pick_profitable_slice_to_sell it would
    quietly never fire."""
    src = cycle_src()
    assert "oldest = _stop_slice" in src


def test_a_zero_stop_is_still_logged_loudly():
    src = cycle_src()
    i = src.index("_override = getattr(branch")
    j = src.index("_stop_slice = None", i)
    assert "NO GRID STOP" in src[i:j]


# ------------------------------------------------ the new behaviour

def test_the_override_wins_over_the_adaptive_resolver():
    """An adopted branch's stop must not be re-derived from the coin's
    volatility - it was chosen for what the entry price MEANS, not for
    how the coin moves."""
    src = cycle_src()
    i = src.index("_override = getattr(branch")
    j = src.index("_stop_slice = None", i)
    block = src[i:j]
    # the resolver call sits inside the else, after the override branch
    assert block.index("_override is not None") < block.index("adaptive_stop.resolve")


def test_the_column_exists_and_is_nullable():
    import models
    col = models.CryptoGridBranch.__table__.columns["stop_loss_pct_override"]
    assert col.nullable is True
    assert col.default is None


def test_an_adopted_slice_can_be_told_from_a_bought_one():
    """Without the flag the ledger computes lifetime returns from an
    entry price nobody paid."""
    import models
    col = models.CryptoGridSlice.__table__.columns["adopted"]
    assert col.nullable is True


def test_the_trade_history_was_not_given_the_flag_by_accident():
    import models
    assert "adopted" not in [c.name for c in models.CryptoGridTradeHistory.__table__.columns]


# -------------------------------------- the behaviour it protects

def test_an_eight_percent_stop_on_an_adoption_price_is_the_risk_named():
    """The reason for the whole change, kept in the source so it cannot
    be removed as an unexplained special case."""
    src = cycle_src()
    i = src.index("A BRANCH MAY NAME ITS OWN STOP")
    j = src.index("_stop_slice = None", i)
    block = src[i:j]
    assert "held for a year" in block and "cost basis nobody ever paid" in block


def test_the_stop_trigger_itself_is_unchanged():
    src = cycle_src()
    assert "if _entry and price <= _entry * (1 - _stop_pct):" in src


# ------------------------------- the off switch must mean off

def test_the_post_sale_rotation_path_honours_the_fleet_switch():
    """THE HOLE. run_grid_branch_cycle calls _maybe_rotate_one_grid_branch
    with after_sale=True whenever a sale empties a branch to flat, and
    that path reached only the env var - which DEFAULTS TO ON. The
    dashboard could report auto_rotate_active=False while a branch that
    sold its last slice was still re-pointed onto another coin."""
    fn = next(n for n in ast.walk(TREE)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_maybe_rotate_one_grid_branch")
    body = "\n".join(SRC.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert "is_grid_auto_rotate_active" in body, \
        "the post-sale path does not check the fleet switch"
    # and it must be checked BEFORE the env var, which defaults to on
    assert body.index("is_grid_auto_rotate_active") < body.index("auto_rotate_enabled()")


def test_every_caller_of_the_rotation_helper_is_now_gated():
    """Three callers: the sweep, the flat-rebalance, and the main cycle.
    The gate lives inside the helper now, so all three inherit it."""
    callers = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call):
            f = node.func
            if (f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")) \
                    == "_maybe_rotate_one_grid_branch":
                for p in ast.walk(TREE):
                    if (isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and p.lineno <= node.lineno <= (p.end_lineno or 0)):
                        callers.add(p.name)
                        break
    assert "run_grid_branch_cycle" in callers, "the main-cycle caller vanished"
    assert len(callers) >= 3


def test_both_rotation_gates_are_reported_not_just_one():
    """Reporting only the DB switch is what made 'off' misleading."""
    assert '"auto_rotate_env_enabled"' in SRC
    assert '"auto_rotate_fully_off"' in SRC


def test_the_branch_stop_override_is_visible_from_outside():
    """A safety setting that cannot be inspected is one nobody can trust."""
    assert '"stop_loss_pct_override": getattr(b, "stop_loss_pct_override", None)' in SRC
