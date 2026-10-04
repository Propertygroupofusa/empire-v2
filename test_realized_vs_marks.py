"""A mark is not a trade. Counting it as one favours whichever grid
configuration happens to hold fewer slices when the window ends.

_replay_grid_bot closes by appending one ("OPEN_AT_WINDOW_END", gross) per
still-open slice - unrealized, and charged no fee, unlike every completed
cycle. Those entries were folded into num_trades, win_rate and total_pnl with
no way to separate them. Open slices are capped at num_levels, so a 10-level
configuration can end a 35-coin window carrying up to 350 marks where a
3-level candidate carries at most 105 - and 98.5% of open coin on the live
fleet is under water. The side-by-side was therefore structurally tilted.
"""
import crypto_selection_backtest as bt


def test_realized_excludes_the_open_marks():
    trades = [("GRID_CYCLE", 2.0), ("GRID_CYCLE", -0.5),
              ("OPEN_AT_WINDOW_END", -9.0), ("OPEN_AT_WINDOW_END", -6.0)]
    out = bt._summarize_strategy_trades(trades, 150.0)
    assert out["realized_trades"] == 2
    assert out["realized_pnl"] == 1.5
    assert out["open_mark_count"] == 2
    assert out["open_mark_pnl"] == -15.0
    # the two halves still reconstruct the old number exactly
    assert round(out["realized_pnl"] + out["open_mark_pnl"], 9) == round(out["total_pnl"], 9)


def test_the_old_fields_are_untouched():
    """Every existing caller and test must see exactly what it saw before."""
    trades = [("GRID_CYCLE", 2.0), ("GRID_CYCLE", -0.5),
              ("OPEN_AT_WINDOW_END", -9.0)]
    out = bt._summarize_strategy_trades(trades, 150.0)
    assert out["num_trades"] == 3
    assert out["total_pnl"] == -7.5
    assert out["win_rate"] == 1 / 3 * 100
    assert out["spend_used"] == 150.0


def test_the_bias_it_exposes_is_real():
    """Same realized record, different inventory at the bell. The old total
    says the 3-level run won; realized says they tied."""
    ten_levels = ([("GRID_CYCLE", 1.0)] * 10) + ([("OPEN_AT_WINDOW_END", -4.0)] * 10)
    three_levels = ([("GRID_CYCLE", 1.0)] * 10) + ([("OPEN_AT_WINDOW_END", -4.0)] * 3)
    a = bt._summarize_strategy_trades(ten_levels, 150.0)
    b = bt._summarize_strategy_trades(three_levels, 150.0)
    assert b["total_pnl"] > a["total_pnl"]            # the old, tilted comparison
    assert a["realized_pnl"] == b["realized_pnl"]     # the honest one
    assert a["realized_win_rate"] == b["realized_win_rate"] == 100.0


def test_a_run_that_closed_everything_has_no_marks():
    out = bt._summarize_strategy_trades([("GRID_CYCLE", 1.25)] * 4, 150.0)
    assert out["open_mark_count"] == 0
    assert out["open_mark_pnl"] == 0
    assert out["realized_pnl"] == out["total_pnl"]
    assert out["realized_avg_usd"] == 1.25


def test_a_run_that_closed_nothing_reports_none_not_zero():
    """No completed round trip means NO realized win rate - reporting 0%
    would read as 'it lost', which is a different claim from 'it never sold'."""
    out = bt._summarize_strategy_trades([("OPEN_AT_WINDOW_END", -3.0)] * 5, 150.0)
    assert out["realized_trades"] == 0
    assert out["realized_win_rate"] is None
    assert out["realized_avg_usd"] is None
    assert out["open_mark_count"] == 5


def test_empty_still_returns_none():
    assert bt._summarize_strategy_trades([], 150.0) is None


def test_the_replay_still_tags_its_marks():
    """If the tag is ever renamed, the split above silently stops working."""
    import inspect
    src = inspect.getsource(bt._replay_grid_bot)
    assert '"OPEN_AT_WINDOW_END"' in src
    assert '"GRID_CYCLE"' in src
