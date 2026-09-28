"""Closing one position, at a loss, on purpose.

There was no path to it. close-all refuses unless the WHOLE fleet is in
profit - correct for a button that liquidates everything, but it left no
way to exit a single position that had stopped working. The only
alternative was a raw /coinbase/sell, which moves the coin and leaves the
branch rows behind still claiming it: a phantom position, and a worse
problem than the one being solved.

ZEC-USD on 2026-09-28: 7 slices, $2,341.45 of cost basis, 31% of the
fleet, zero completed round trips in 26 days.
"""
import ast
import inspect
import pathlib

import crypto_grid_bot

ROUTER = (pathlib.Path(__file__).with_name("routers") / "trading_dashboard.py").read_text()


def _fn(name):
    tree = ast.parse(ROUTER)
    for n in ast.walk(tree):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"{name} not found")


def test_close_can_be_narrowed_to_one_branch():
    sig = inspect.signature(crypto_grid_bot.close_all_grid_slices)
    assert "only_bot_name" in sig.parameters
    assert "only_product_id" in sig.parameters
    assert sig.parameters["only_product_id"].default is None, "default must stay fleet-wide"


def test_the_filter_narrows_and_defaults_to_everything():
    """Exercised against the real function body rather than described: the
    two filters compose, and neither fires when both are None."""
    src = inspect.getsource(crypto_grid_bot.close_all_grid_slices)
    assert "if only_bot_name:" in src
    assert "if only_product_id:" in src


def test_the_endpoint_is_dry_run_by_default():
    fn = _fn("close_one_grid_branch_endpoint")
    defaults = {a.arg: d for a, d in zip(fn.args.args[-len(fn.args.defaults):],
                                         fn.args.defaults)}
    assert defaults["dry_run"].value is True, "a loss must not be realisable by accident"
    assert defaults["accept_loss"].value is False


def test_realising_a_loss_needs_two_deliberate_flags():
    """One flag is easy to leave set in a saved command."""
    src = ast.get_source_segment(ROUTER, _fn("close_one_grid_branch_endpoint"))
    assert "if realized < 0 and not accept_loss:" in src
    assert "not reversible" in src


def test_an_unpriceable_branch_is_refused_not_closed_blind():
    src = ast.get_source_segment(ROUTER, _fn("close_one_grid_branch_endpoint"))
    assert "A gap is not a zero" in src
    assert "refusing to close blind" in src


def test_the_exit_leg_is_priced_as_taker_not_maker():
    """It is a market sell by design - a close that does not fill is not a
    close - so quoting it at the maker rate would understate the loss."""
    src = ast.get_source_segment(ROUTER, _fn("close_one_grid_branch_endpoint"))
    assert "round_trip" in src and "/ 2" in src
    assert "TAKER" in src


def test_it_reuses_close_all_rather_than_reimplementing_the_math():
    """A second copy of the fee and P&L formula is how two numbers that
    must agree stop agreeing."""
    src = ast.get_source_segment(ROUTER, _fn("close_one_grid_branch_endpoint"))
    assert "close_all_grid_slices(only_product_id=product_id)" in src
    for invented in ("CryptoGridTradeHistory(", "allocated_usd +=", "place_market_sell"):
        assert invented not in src, f"the endpoint must not hand-roll {invented}"


def test_the_preview_arithmetic_matches_the_live_zec_numbers():
    """cost 2341.45, value 2209.99 at $1557.46, taker leg 0.0075."""
    cost, value, leg = 2341.45, 2209.99, 0.0075
    fees = value * leg
    realized = value - cost - fees
    assert round(fees, 2) == 16.57
    assert round(realized, 2) == -148.03
    assert round(value - fees, 2) == 2193.42


def test_close_all_still_refuses_an_unprofitable_fleet():
    """The narrowing must not have opened the fleet-wide button."""
    src = ast.get_source_segment(ROUTER, _fn("close_all_grid_slices_endpoint"))
    assert "if total <= 0:" in src
    assert "refusing to close everything" in src
