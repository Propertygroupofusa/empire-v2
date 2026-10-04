"""An adopted slice's recorded entry is an adoption-day mark, not a price paid.

coin_adoption writes every slice at the market price on adoption day. For ZEC
that recorded $1,655 against a real average cost of $1,013.80 - a basis 63%
too high, $641.20 a coin. The parked sell route is the ONLY way out of a
branch full on its rungs, and it reads that basis, so ZEC sat at six slices
and zero closed trades waiting on a price it does not need: it is up 31.7%.

The venue's API does not expose an average cost, so this is DECLARED by the
owner, never fetched and never guessed.
"""
import importlib
import inspect
import os
import sys


def _load(raw=None):
    sys.modules.pop("crypto_grid_bot", None)
    old = os.environ.get("GRID_TRUE_COST_BASIS")
    if raw is None:
        os.environ.pop("GRID_TRUE_COST_BASIS", None)
    else:
        os.environ["GRID_TRUE_COST_BASIS"] = raw
    try:
        import crypto_grid_bot as g
        return importlib.reload(g)
    finally:
        if old is None:
            os.environ.pop("GRID_TRUE_COST_BASIS", None)
        else:
            os.environ["GRID_TRUE_COST_BASIS"] = old


class _Slice:
    def __init__(self, entry, adopted, qty=1.0, product_id="ZEC-USD"):
        self.entry_price = entry
        self.adopted = adopted
        self.qty = qty
        self.product_id = product_id


# --- empty by default ------------------------------------------------------

def test_declaring_nothing_changes_nothing():
    g = _load(None)
    assert g.GRID_TRUE_COST_BASIS == {}
    assert g.true_cost_basis_for("ZEC-USD") is None
    s = _Slice(1655.0, adopted=True)
    assert g.sell_basis_for_slice(s) == 1655.0


def test_junk_entries_are_ignored_not_guessed():
    g = _load("ZEC-USD, HBAR:, :1.0, FOO-USD:abc, BAR-USD:-5, BAZ-USD:0")
    assert g.GRID_TRUE_COST_BASIS == {}


def test_it_parses_what_the_owner_declares():
    g = _load("ZEC-USD:1013.80, hbar-usd:0.0987")
    assert g.true_cost_basis_for("ZEC-USD") == 1013.80
    assert g.true_cost_basis_for("  hbar-usd ") == 0.0987
    assert g.true_cost_basis_for("XRP-USD") is None


# --- only adopted slices, never the grid's own -----------------------------

def test_an_adopted_slice_is_judged_on_what_was_paid():
    g = _load("ZEC-USD:1013.80")
    assert g.sell_basis_for_slice(_Slice(1655.0, adopted=True)) == 1013.80


def test_a_slice_the_grid_bought_keeps_its_real_entry():
    """It paid that price. Overriding it would invent a basis."""
    g = _load("ZEC-USD:1013.80")
    assert g.sell_basis_for_slice(_Slice(1586.44, adopted=False)) == 1586.44


def test_an_undeclared_product_keeps_todays_behaviour():
    g = _load("ZEC-USD:1013.80")
    s = _Slice(1.5383, adopted=True, product_id="XRP-USD")
    assert g.sell_basis_for_slice(s) == 1.5383


# --- the record must not absorb an inherited exit --------------------------

def test_the_adopted_exit_reason_exists_and_is_distinct():
    g = _load(None)
    assert g.ADOPTED_EXIT_REASON == "adopted_exit"
    for known in ("stop_loss", "profit_target", "parked_sell"):
        assert g.ADOPTED_EXIT_REASON != known


def test_the_performance_record_excludes_it():
    g = _load(None)
    src = inspect.getsource(g.get_grid_performance_metrics)
    assert "ADOPTED_EXIT_REASON" in src
    assert "CryptoGridTradeHistory.exit_reason" in src
    # an untagged legacy row must still count
    assert "is_(None)" in src


def test_the_sell_path_tags_only_a_declared_adopted_parked_exit():
    g = _load(None)
    src = inspect.getsource(g)
    i = src.index('exit_reason = ADOPTED_EXIT_REASON')
    w = src[i - 700:i + 60]
    assert 'exit_reason == "parked_sell"' in w
    assert "slice_paid_no_entry_fee(oldest)" in w
    assert "true_cost_basis_for(branch.product_id) is not None" in w


def test_the_parked_gate_uses_the_sell_basis():
    g = _load(None)
    src = inspect.getsource(g._pick_parked_slice_to_sell)
    assert "sell_basis_for_slice(s" in src
    assert "pct = net / _basis_usd" in src
    # the dust guard must still size the candidate, not skip it
    assert "GRID_DUST_SLICE_USD" in src


def test_the_floor_itself_is_not_relaxed():
    g = _load("ZEC-USD:1013.80")
    assert g.GRID_PARKED_MIN_NET_PCT == 0.010
    assert "pct >= floor_pct" in inspect.getsource(g._pick_parked_slice_to_sell)
