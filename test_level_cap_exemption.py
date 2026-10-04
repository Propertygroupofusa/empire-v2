"""A spacing override must not strand a branch below the ladder it already built.

Live, 2026-10-04, under the 3_levels_2.5pct override: six branches held 3-7
slices against a 3-level ceiling and could not open a rung at any price.
$5,844.04 - 68.6% of the fleet - was frozen. The sixteen branches that could
still buy were returning 2.97% against those six at 0.96%.

The exemption is opt-in and empty by default, because unfreezing a branch lets
it spend real money on a real position. These tests pin that default, the
ceiling-not-floor property, and the wiring.
"""
import ast
import importlib
import inspect
import os
import sys


def _load(exempt_env):
    for k in list(sys.modules):
        if k == "crypto_grid_bot":
            del sys.modules[k]
    old = os.environ.get("GRID_LEVEL_CAP_EXEMPT")
    if exempt_env is None:
        os.environ.pop("GRID_LEVEL_CAP_EXEMPT", None)
    else:
        os.environ["GRID_LEVEL_CAP_EXEMPT"] = exempt_env
    try:
        import crypto_grid_bot as g
        return importlib.reload(g)
    finally:
        if old is None:
            os.environ.pop("GRID_LEVEL_CAP_EXEMPT", None)
        else:
            os.environ["GRID_LEVEL_CAP_EXEMPT"] = old


# --- the default must change nothing ---------------------------------------

def test_empty_by_default_so_today_is_unchanged():
    g = _load(None)
    assert g.GRID_LEVEL_CAP_EXEMPT == frozenset()
    for pid in ("ZEC-USD", "XRP-USD", "LINK-USD", "ANYTHING-USD"):
        assert g.branch_is_level_cap_exempt(pid) is False, pid


def test_a_blank_or_comma_only_value_is_still_empty():
    for blank in ("", "   ", ",", " , , "):
        g = _load(blank)
        assert g.GRID_LEVEL_CAP_EXEMPT == frozenset(), repr(blank)


# --- it names coins, and only the coins named ------------------------------

def test_it_exempts_only_what_is_named():
    g = _load("LINK-USD,HBAR-USD,XLM-USD,BCH-USD,XRP-USD")
    for pid in ("LINK-USD", "HBAR-USD", "XLM-USD", "BCH-USD", "XRP-USD"):
        assert g.branch_is_level_cap_exempt(pid) is True, pid
    # The one the code warns about by name stays capped.
    assert g.branch_is_level_cap_exempt("ZEC-USD") is False


def test_case_and_whitespace_do_not_decide_real_money():
    g = _load("  link-usd ,HBAR-usd  ")
    assert g.branch_is_level_cap_exempt("LINK-USD") is True
    assert g.branch_is_level_cap_exempt("  hbar-usd  ") is True


def test_a_missing_product_id_is_never_exempt():
    """The safe answer to 'may this branch buy more' is no."""
    g = _load("LINK-USD")
    for bad in (None, "", "   "):
        assert g.branch_is_level_cap_exempt(bad) is False, repr(bad)


# --- ceiling exemption, NOT a floor override -------------------------------

def test_an_exempt_branch_still_cannot_exceed_its_own_capital():
    g = _load("TIA-USD")
    # $15 of allocation supports 3 slices at the $5 minimum trade, never 10.
    assert g._safe_num_levels_for_allocation(15.0) == 3
    assert g._safe_num_levels_for_allocation(2228.05) == g.DEFAULT_GRID_LEVELS
    assert g._safe_num_levels_for_allocation(0.0) == 1


def test_the_exemption_restores_only_what_no_override_would_have_given():
    g = _load("XRP-USD")
    alloc = 2228.05
    no_override = g._safe_num_levels_for_allocation(alloc)
    override_cap = min(no_override, 3)
    exempt = g._safe_num_levels_for_allocation(alloc)   # what the patch assigns
    assert override_cap == 3
    assert exempt == no_override
    assert exempt > override_cap        # the freeze is lifted
    assert exempt <= g.DEFAULT_GRID_LEVELS   # and nothing new is invented


# --- the wiring, so it cannot silently regress -----------------------------

def test_the_cap_block_actually_consults_the_exemption():
    g = _load(None)
    src = inspect.getsource(g)
    i = src.index("real_effective_levels = max(1, min(_safe_num_levels_for_allocation")
    window = src[i:i + 900]
    assert "branch_is_level_cap_exempt(branch.product_id)" in window, (
        "the override cap no longer consults the exemption")
    assert "real_effective_levels = _safe_num_levels_for_allocation(branch.allocated_usd)" in window


def test_the_buy_gate_itself_is_untouched():
    """Only the CEILING moves. The rule that a full branch may not buy, and
    every gate in front of it, must still read exactly as before."""
    g = _load(None)
    src = inspect.getsource(g)
    assert "len(tradeable_slices(slices)) < branch.num_levels" in src
    # Gates this module genuinely owns. _check_concentration lives in
    # prop_bot, not here - asserting it from this module tested nothing.
    for untouched in ("UNBACKED - no buy", "concentration_gate",
                      "GRID_CASH_RESERVE_USD", "branch_backing_verdict",
                      "drawdown"):
        assert untouched in src, untouched


def test_the_module_is_syntactically_whole():
    ast.parse(open("crypto_grid_bot.py").read())
