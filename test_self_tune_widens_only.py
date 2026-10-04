"""The fleet has been learning into a void. This reconnects it, one way only.

_maybe_self_tune_branch_spacing runs hourly and writes a per-branch
self_tuned_multiplier from that branch's own last five real closed trades. Its
only consumer sits behind `elif is_avg_swing_spacing_active()`, and the live
grid_spacing_override takes the `if` above it - so nothing has ever applied it.

Reconnecting it must never narrow a step. These tests make that structural.
"""
import ast
import importlib
import inspect
import os
import sys


def _load(env=None):
    sys.modules.pop("crypto_grid_bot", None)
    old = os.environ.get("GRID_SELF_TUNE_WIDENS_FIXED_SPACING")
    if env is None:
        os.environ.pop("GRID_SELF_TUNE_WIDENS_FIXED_SPACING", None)
    else:
        os.environ["GRID_SELF_TUNE_WIDENS_FIXED_SPACING"] = env
    try:
        import crypto_grid_bot as g
        return importlib.reload(g)
    finally:
        if old is None:
            os.environ.pop("GRID_SELF_TUNE_WIDENS_FIXED_SPACING", None)
        else:
            os.environ["GRID_SELF_TUNE_WIDENS_FIXED_SPACING"] = old


class _Branch:
    def __init__(self, m):
        self.self_tuned_multiplier = m
        self.product_id = "ONDO-USD"


# --- off by default --------------------------------------------------------

def test_off_by_default():
    g = _load(None)
    assert g.SELF_TUNE_WIDENS_FIXED_SPACING is False


def test_only_explicit_truthy_values_arm_it():
    for on in ("1", "true", "TRUE", "yes", "on"):
        assert _load(on).SELF_TUNE_WIDENS_FIXED_SPACING is True, on
    for off in ("", "0", "false", "no", "off", "maybe"):
        assert _load(off).SELF_TUNE_WIDENS_FIXED_SPACING is False, off


# --- it can only ever widen ------------------------------------------------

def test_a_branch_at_or_below_the_default_is_never_consulted():
    """1.5x IS the validated default. At or under it there is nothing learned
    to apply, and returning a value here is how this would start narrowing."""
    g = _load("true")
    for m in (None, 0.5, 1.0, 1.4, g.AVG_SWING_SPACING_MULTIPLIER):
        assert g.self_tune_widening_multiplier(_Branch(m)) is None, m


def test_only_a_branch_the_tuner_actually_widened_is_consulted():
    g = _load("true")
    for m in (1.6, 2.0, 3.0):
        assert g.self_tune_widening_multiplier(_Branch(m)) == m, m


def test_a_junk_multiplier_is_not_trusted():
    g = _load("true")
    for junk in ("wide", object(), float("nan")):
        out = g.self_tune_widening_multiplier(_Branch(junk))
        assert out is None or out > g.AVG_SWING_SPACING_MULTIPLIER, junk


def test_the_tuner_itself_can_never_go_below_the_validated_default():
    """The source of the multiplier is bounded too, so the floor holds even
    if a caller forgot to check."""
    g = _load("true")
    assert g.SELF_TUNE_MIN_MULTIPLIER == g.AVG_SWING_SPACING_MULTIPLIER
    assert g.SELF_TUNE_MAX_MULTIPLIER > g.SELF_TUNE_MIN_MULTIPLIER
    assert g.SELF_TUNE_POOR_WIN_RATE_PCT < g.SELF_TUNE_GOOD_WIN_RATE_PCT


# --- the wiring ------------------------------------------------------------

def test_the_override_branch_now_consults_the_learned_multiplier():
    g = _load(None)
    src = inspect.getsource(g)
    i = src.index('spacing_log_note = f"real promoted candidate')
    window = src[i:i + 1400]
    assert "self_tune_widening_multiplier(branch)" in window
    assert "SELF_TUNE_WIDENS_FIXED_SPACING" in window


def test_it_is_applied_through_max_so_it_cannot_narrow():
    g = _load(None)
    src = inspect.getsource(g)
    i = src.index('spacing_log_note = f"real promoted candidate')
    window = src[i:i + 1400]
    assert "new_grid_pct = max(new_grid_pct, _swing_pct)" in window, (
        "the widening is no longer applied through max() - it could narrow a step")
    assert "_swing_pct > new_grid_pct" in window, (
        "the guard that only widens is gone")


def test_the_self_tuner_still_runs_every_cycle():
    g = _load(None)
    assert "await _maybe_self_tune_branch_spacing(branch)" in inspect.getsource(g)


def test_nothing_else_about_spacing_moved():
    g = _load(None)
    src = inspect.getsource(g)
    for untouched in ("is_avg_swing_spacing_active", "is_dynamic_spacing_active",
                      "MIN_DYNAMIC_GRID_PCT", "TARGET_NET_MARGIN_PCT"):
        assert untouched in src, untouched


def test_module_is_syntactically_whole():
    ast.parse(open("crypto_grid_bot.py").read())
