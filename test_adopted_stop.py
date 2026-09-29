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
    """Was a text match on the cycle body. The decision moved out into
    crypto_grid_bot.stop_report_line - because branching on the slices before
    the stop made an unarmed branch log "ADOPTED STOP ARMED" - so this now
    calls it instead of grepping for the words. Same property, asserted where
    it can no longer pass on a comment or fail on a rewording."""
    import crypto_grid_bot
    unarmed = {"stop_pct": 0.0, "source": "adopted-unarmed",
               "reason": "GRID_ADOPTED_STOP_MODE is not 'arm'"}
    level, msg = crypto_grid_bot.stop_report_line("crypto_grid_9", 0.0,
                                                 unarmed, has_slices=True)
    assert level == "warning", msg
    assert "🚨" in msg, msg
    assert "NO GRID STOP" in msg, msg


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


# ---------------------------- sell-only: the better path for an overweight

def test_a_sell_only_branch_may_sell_but_never_buys():
    """A two-way grid on an overweight position fights the trimmer: sells
    into strength, then buys the dip straight back. Paused, the same
    branch walks the position DOWN through strength at a profit target."""
    src = cycle_src()
    assert '_sell_only = bool(getattr(branch, "buys_paused", False))' in src
    assert "if drawdown_breached or _sell_only:" in src


def test_the_buy_gate_is_the_only_thing_sell_only_touches():
    """It must not reach the SELL path. A branch that cannot sell is not
    sell-only, it is stuck."""
    src = cycle_src()
    i = src.index("_sell_only = bool(")
    j = src.index("elif price <= branch.reference_price", i)
    block = src[i:j]
    assert "grid_sell" not in block and "_pick_profitable_slice_to_sell" not in block


def test_a_sell_only_branch_does_not_get_the_drawdown_alarm():
    """A branch at a fresh peak reading 'equity is down 0% from its peak'
    would be an alarm about nothing."""
    src = cycle_src()
    i = src.index("if drawdown_breached or _sell_only:")
    j = src.index("elif price <= branch.reference_price", i)
    block = src[i:j]
    assert "if drawdown_breached:" in block
    assert block.index("if drawdown_breached:") < block.index("real equity")


def test_the_column_is_nullable_so_nothing_running_changes():
    import models
    col = models.CryptoGridBranch.__table__.columns["buys_paused"]
    assert col.nullable is True and col.default is None


def test_the_worker_sets_it_from_the_plan():
    src = open("coin_adoption_worker.py").read()
    assert 'buys_paused=bool(a.get("sell_only"))' in src


# ---------------------------------------------------------------------------
# THE REPORTED STOP MUST BE THE STOP THE LOOP OBEYS.
#
# _resolve_branch_stop is keyed by PRODUCT; the override that decides the
# stop lives on the BRANCH. So grid-status reported ZEC-USD as
# stop_pct 0.158834 / stop_source "adaptive" while run_grid_branch_cycle
# applied no grid stop at all - a safety figure that disagreed with the code
# enforcing it. Worse than no figure: it says coin held for a year is
# protected by a trigger that will never fire.
# ---------------------------------------------------------------------------
import crypto_grid_bot as grid


class _B:
    def __init__(self, override=None, buys_paused=False):
        self.stop_loss_pct_override = override
        self.buys_paused = buys_paused
        self.bot_name = "crypto_grid_test"
        self.product_id = "ZEC-USD"


ADAPTIVE = {"stop_pct": 0.158834, "source": "adaptive",
            "reason": "15.88% for ZEC-USD, 2.50x its 6.35% daily volatility",
            "daily_vol_pct": 6.3534}


def test_a_null_override_reports_exactly_what_the_resolver_said():
    """Every pre-existing branch is NULL. Its reporting must not move."""
    out = grid._reported_stop(_B(None), ADAPTIVE)
    assert out == ADAPTIVE
    assert out is not ADAPTIVE          # a copy, so callers cannot mutate the cache


def test_a_zero_override_reports_no_stop_not_an_adaptive_one():
    out = grid._reported_stop(_B(0.0), ADAPTIVE)
    assert out["stop_pct"] == 0.0
    assert out["source"] == "branch_override_none"
    # WHAT THIS ASSERTS, AND WHY IT IS NO LONGER A FIXED PHRASE.
    #
    # It was `"NO grid stop" in out["reason"]`, and it broke when the adopted
    # catastrophe stop gave the unarmed case a reason naming the switch that
    # would arm it. The property - report no stop, and never the adaptive
    # figure the cycle is not applying - was untouched; only the wording
    # moved. So the wording is not what is checked any more.
    assert "no stop" in out["reason"].lower()
    assert "15.88" not in out["reason"], "reported an adaptive distance it does not apply"
    assert ADAPTIVE["reason"] not in out["reason"]
    # the volatility it was measured at is still worth seeing
    assert out["daily_vol_pct"] == 6.3534


def test_zero_is_tested_with_is_not_none_never_truthiness():
    """`if override:` would send 0.0 straight down the adaptive path.

    This is the same trap the trading loop already avoids, asserted here so
    the two cannot drift apart.
    """
    src = open("crypto_grid_bot.py").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_reported_stop")
    # Asserted on the parsed test, not on the source text - the docstring
    # above quotes `if override:` as the trap being avoided, and a substring
    # search would read the warning as the bug.
    ifs = [n for n in ast.walk(fn) if isinstance(n, ast.If)]
    assert ifs, "no branch on the override at all"
    guard = ifs[0].test
    assert isinstance(guard, ast.Compare), f"guard is {type(guard).__name__}, not a comparison"
    assert isinstance(guard.ops[0], ast.Is)
    assert isinstance(guard.comparators[0], ast.Constant)
    assert guard.comparators[0].value is None


def test_a_real_override_reports_that_number_and_names_the_branch_as_its_source():
    out = grid._reported_stop(_B(0.12), ADAPTIVE)
    assert out["stop_pct"] == 0.12
    assert out["source"] == "branch_override"


def test_an_unreadable_override_reports_the_fixed_stop_the_loop_falls_back_to():
    out = grid._reported_stop(_B("nonsense"), ADAPTIVE)
    assert out["stop_pct"] == grid.GRID_STOP_LOSS_PCT
    assert out["source"] == "fixed"


def test_a_missing_resolver_read_does_not_crash_the_panel():
    assert grid._reported_stop(_B(0.0), None)["stop_pct"] == 0.0
    assert grid._reported_stop(_B(None), None) == {}


def test_grid_status_serializes_buys_paused():
    """A rule nobody can see from outside is a rule on trust.

    sell-only is set at adoption on a position over the 20% rule; it was
    enforced in the loop but absent from grid-status, so the panel could not
    tell an overweight branch that never buys back from a normal one.
    """
    src = open("crypto_grid_bot.py").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "get_grid_status")
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert '"buys_paused"' in body
    # AND THE PER-BRANCH STOP, NOT THE PER-PRODUCT ONE.
    #
    # This was `"_reported_stop(b, stop_by_product.get(b.product_id))" in body`
    # and it broke when the call gained an adopted_mode keyword - the property
    # was untouched, only the call's text moved. Asserted on the tree now: the
    # call must exist, take the BRANCH as its first argument, and be handed the
    # per-product resolution rather than that resolution being used directly.
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_reported_stop"]
    assert len(calls) == 1, f"expected one _reported_stop call, found {len(calls)}"
    call = calls[0]
    assert call.args, "_reported_stop called with no positional arguments"
    assert isinstance(call.args[0], ast.Name) and call.args[0].id == "b", (
        "the first argument must be the BRANCH - that is what carries the "
        "override the whole function exists to honour")
    passed = ast.unparse(call)
    assert "stop_by_product" in passed, (
        "the per-product resolution must be handed IN, so the branch override "
        "can beat it rather than being bypassed")
