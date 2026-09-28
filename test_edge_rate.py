"""A rate needs a denominator that held still, or a stated average of one.

This exists because of a mistake made at 10:23Z on 2026-09-28. The fleet
had realised $50.04 over 26.17 days. Divided by the working capital of
that instant, $1,516.21, it came to 0.1183%/day and was reported as
"inside the professional band". Twenty-two minutes later the same
arithmetic gave 0.0509%/day - XRP's $2,240.54 crossed back over the -1%
line and the parked share went 81.3% to 53.6% on price alone, with
nothing traded in between.

Neither figure was a rate. Both were a long measurement divided by one
instant's denominator, and the instant supplied the answer.

Nothing in the system recorded working capital over time, so the mistake
was not detectable from stored data - only by noticing that the same sum
gave a different answer twenty minutes later. This module records the
denominator alongside the numerator and computes the rate against the
denominator's TIME-WEIGHTED AVERAGE over the same window.
"""
from datetime import datetime, timedelta, timezone

import pytest

import edge_rate as er

T0 = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)


def snap(hours, realized, account=10000.0, working=2000.0, allocated=8000.0):
    return {"at": T0 + timedelta(hours=hours), "realized_usd": realized,
            "account_usd": account, "working_usd": working,
            "allocated_usd": allocated}


# ── the rule the mistake broke ───────────────────────────────────────────

def test_the_denominator_is_averaged_across_the_window():
    """The instant's capital must not decide the answer."""
    a = snap(0, 100.0, working=1500.0)
    b = snap(24, 110.0, working=3500.0)
    r = er.rate_between(a, b, basis="working")
    # $10 over one day against the mean of 1500 and 3500, not either end.
    assert r["basis_usd"] == pytest.approx(2500.0)
    assert r["pct_per_day"] == pytest.approx(10.0 / 2500.0 * 100)


def test_the_same_pair_read_from_either_end_gives_one_answer():
    a, b = snap(0, 100.0, working=1500.0), snap(24, 110.0, working=3500.0)
    at_start = 10.0 / 1500.0 * 100
    at_end = 10.0 / 3500.0 * 100
    r = er.rate_between(a, b, basis="working")["pct_per_day"]
    assert at_end < r < at_start, "the answer still depends on one endpoint"


def test_a_longer_interval_weighs_more_than_a_short_one():
    """A simple mean of endpoints over-weights a five-minute reading."""
    s = [snap(0, 0.0, working=1000.0), snap(23, 10.0, working=1000.0),
         snap(24, 11.0, working=9000.0)]
    r = er.series_rate(s, basis="working")
    assert r["basis_usd"] < 2000.0, (
        f"basis {r['basis_usd']} - the one-hour reading at $9,000 dominated")


# ── unknown is a third verdict ───────────────────────────────────────────

def test_one_snapshot_is_not_a_rate():
    r = er.series_rate([snap(0, 100.0)], basis="account")
    assert r["status"] == er.UNKNOWN
    assert r["pct_per_day"] is None
    assert "one snapshot" in r["detail"].lower() or "two" in r["detail"].lower()


def test_no_snapshots_is_not_a_rate_either():
    assert er.series_rate([], basis="account")["status"] == er.UNKNOWN
    assert er.series_rate(None, basis="account")["status"] == er.UNKNOWN


def test_two_readings_at_the_same_instant_are_not_a_rate():
    r = er.rate_between(snap(0, 100.0), snap(0, 110.0), basis="account")
    assert r["status"] == er.UNKNOWN
    assert r["pct_per_day"] is None


def test_an_unreadable_basis_is_unknown_never_zero():
    a = dict(snap(0, 100.0)); a["working_usd"] = None
    r = er.rate_between(a, snap(24, 110.0), basis="working")
    assert r["status"] == er.UNKNOWN
    assert r["pct_per_day"] is None


def test_a_zero_basis_is_unknown_not_an_infinite_rate():
    a, b = snap(0, 100.0, working=0.0), snap(24, 110.0, working=0.0)
    r = er.rate_between(a, b, basis="working")
    assert r["status"] == er.UNKNOWN
    assert r["pct_per_day"] is None


# ── it reports which book it used ────────────────────────────────────────

def test_the_basis_is_named_in_the_answer():
    for basis in ("account", "working", "allocated"):
        r = er.rate_between(snap(0, 100.0), snap(24, 110.0), basis=basis)
        assert r["basis"] == basis
        assert basis in r["detail"]


def test_the_same_window_on_two_books_gives_two_clearly_labelled_rates():
    s = [snap(0, 100.0, account=10000.0, working=2000.0),
         snap(24, 110.0, account=10000.0, working=2000.0)]
    acct = er.series_rate(s, basis="account")
    work = er.series_rate(s, basis="working")
    assert acct["pct_per_day"] == pytest.approx(0.1)
    assert work["pct_per_day"] == pytest.approx(0.5)
    assert acct["basis"] != work["basis"]


def test_an_unknown_basis_name_is_refused_rather_than_guessed():
    r = er.rate_between(snap(0, 100.0), snap(24, 110.0), basis="vibes")
    assert r["status"] == er.UNKNOWN


# ── realised can only go up; a drop means the books were reset ───────────

def test_realised_going_backwards_is_unknown_not_a_negative_rate():
    """Cumulative realised cannot fall. If it does, the series spans a
    reset and the difference is not a loss."""
    r = er.rate_between(snap(0, 100.0), snap(24, 90.0), basis="account")
    assert r["status"] == er.UNKNOWN
    assert "reset" in r["detail"].lower() or "backwards" in r["detail"].lower()


# ── the live shape ───────────────────────────────────────────────────────

def test_a_flat_window_is_a_real_zero_not_an_unknown():
    """Nothing traded is a measurement, not a gap."""
    s = [snap(0, 52.40), snap(24, 52.40)]
    r = er.series_rate(s, basis="account")
    assert r["status"] == er.OK
    assert r["pct_per_day"] == 0.0


def test_the_series_spans_first_to_last_not_just_the_newest_pair():
    s = [snap(0, 0.0), snap(24, 5.0), snap(48, 10.0)]
    r = er.series_rate(s, basis="account")
    assert r["days"] == pytest.approx(2.0)
    assert r["realized_delta_usd"] == pytest.approx(10.0)


def test_snapshots_out_of_order_are_sorted_not_trusted():
    s = [snap(48, 10.0), snap(0, 0.0), snap(24, 5.0)]
    r = er.series_rate(s, basis="account")
    assert r["days"] == pytest.approx(2.0)
    assert r["realized_delta_usd"] == pytest.approx(10.0)


# ── the recorder and the endpoint ────────────────────────────────────────

def _src(path, name):
    import ast
    import os
    s = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), path),
             encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(s))
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)
    return ast.get_source_segment(s, fn), fn


def test_the_recorder_takes_the_union_of_stuck_and_locked_never_the_sum():
    """Three branches sit in both lists. Adding them double-counts."""
    body, _ = _src("crypto_grid_bot.py", "record_capital_snapshot")
    assert "stuck_set | locked_set" in body
    assert "stuck_usd + locked_usd" not in body


def test_the_recorder_writes_nothing_rather_than_a_partial_row():
    """A snapshot missing its denominator is a hole in the one series
    that exists to have none."""
    body, _ = _src("crypto_grid_bot.py", "record_capital_snapshot")
    assert 'if realized is None or account is None:' in body
    assert body.index("realized is None or account is None") < body.index("db.add(")


def test_the_recorder_cannot_stop_the_loop_it_rides_on():
    body, _ = _src("crypto_grid_bot.py", "record_capital_snapshot")
    assert "except Exception" in body
    assert "non-fatal" in body


def test_the_recorder_throttles_on_stored_state_not_a_process_timer():
    """A restart must not reset the clock and flood the table."""
    body, _ = _src("crypto_grid_bot.py", "record_capital_snapshot")
    assert "SNAPSHOT_INTERVAL_SECONDS" in body
    assert "newest" in body and "order_by" in body


def test_the_endpoint_is_read_only_and_records_nothing():
    body, _ = _src("routers/trading_dashboard.py", "edge_rate_endpoint")
    for w in ("db.add(", "commit(", "record_capital_snapshot"):
        assert w not in body, f"{w} in a read-only endpoint"


def test_the_endpoint_reports_every_book_not_just_the_chosen_one():
    body, _ = _src("routers/trading_dashboard.py", "edge_rate_endpoint")
    assert "all_bases" in body
    assert "edge_rate.BASES" in body


def test_the_endpoint_says_how_long_until_a_rate_exists():
    body, _ = _src("routers/trading_dashboard.py", "edge_rate_endpoint")
    assert "if len(snaps) < 2:" in body
    assert "hour(s) from now" in body
