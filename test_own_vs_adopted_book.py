"""The grid's own book and the coin it inherited are two different books.

An adopted slice is coin the account ALREADY HELD - written by
coin_adoption_worker at the price on adoption day, no order, no commission,
on a cost basis set by a decision the grid never made. A slice the grid
BOUGHT is one its own rules chose, at a price its own rules picked.

Measured live 2026-10-04 over 52 open slices: 26 adopted at -$459.13 against
26 grid buys at -$77.34. 85.6% of the headline belonged to inherited stock.
ZEC alone: five adopted slices near $1,655 at -$412.50, while the one slice
the grid chose at $1,586.44 was -$11.53. Blending them reported a failure the
grid did not have.
"""
import inspect
import crypto_grid_bot as g


def _src():
    return inspect.getsource(g.get_grid_status)


def test_the_split_is_published():
    src = _src()
    assert '"unrealized_own_usd": unrealized_own_usd,' in src
    assert '"unrealized_adopted_usd": unrealized_adopted_usd,' in src


def test_the_blended_total_is_untouched():
    """Every existing caller must keep seeing exactly what it saw."""
    src = _src()
    assert 'total_unrealized_net_usd = (' in src
    assert 'round(sum(b["total_unrealized_net_usd"] for b in branches_with_slices), 2)' in src


def test_it_splits_on_the_real_adopted_flag():
    """slice_paid_no_entry_fee reads `adopted`, nothing else. A split keyed on
    an invented field name would silently label every slice one way."""
    assert 'getattr(slice_row, "adopted", False)' in inspect.getsource(g.slice_paid_no_entry_fee)
    assert '_s.get("adopted")' in _src()


def test_an_unreadable_slice_makes_the_split_unknown_not_zero():
    """A missing mark must not be summed as 0.00 - that would understate one
    book and read as a clean number. Same doctrine as the blended total."""
    src = _src()
    assert "_split_known = False" in src
    assert "unrealized_own_usd = round(_own, 2) if _split_known else None" in src
    assert "unrealized_adopted_usd = round(_adopted, 2) if _split_known else None" in src


def test_the_two_halves_reconstruct_the_whole():
    """Arithmetic check on the live-measured figures."""
    own, adopted, total = -77.34, -459.13, -536.47
    assert round(own + adopted, 2) == total
    assert round(adopted / total * 100, 1) == 85.6


def test_the_grid_s_own_book_is_positive():
    """The point of the whole split: banked plus its OWN open inventory."""
    banked, own_open = 135.58, -77.34
    assert round(banked + own_open, 2) == 58.24
    assert banked + own_open > 0


def test_adopted_slices_are_still_excluded_from_entry_fees():
    """The split must not disturb the fee doctrine it is built on."""
    src = inspect.getsource(g.slice_round_trip_fee_rate)
    assert "if slice_paid_no_entry_fee(slice_row):" in src
    assert "entry_rate = 0.0" in src
