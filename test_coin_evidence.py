"""The bar that cannot separate anything, and what can instead.

The fleet's rule needs 10 closed round trips before a coin gets a verdict.
That bar is on WIN RATE, and a grid holds win rate near 77% by construction
because a NORMAL exit only sells above its own entry. Measured live, 9 of 14
coins sit between 75% and 100%.
"""
import math

import pytest

import coin_evidence as ce

FLEET_RATE = 0.77
FLEET_PCT = 2.3186


def coin(pid, trips, wins, mean_pct):
    return {"product_id": pid, "trips": trips, "wins": wins, "mean_pct": mean_pct}


# ------------------------------------------------- the bar has no power

def test_ten_trips_can_only_catch_a_catastrophic_coin():
    caught = ce.detectable_win_rate(FLEET_RATE, 10)
    assert caught is not None and caught < 0.25, caught


def test_a_merely_mediocre_coin_needs_a_sample_no_fleet_will_produce():
    assert ce.trips_to_separate_win_rate(0.77, 0.70) > 500
    assert ce.trips_to_separate_win_rate(0.77, 0.60) > 100


def test_the_worse_the_coin_the_fewer_trips_needed():
    needs = [ce.trips_to_separate_win_rate(0.77, p) for p in (0.70, 0.60, 0.50, 0.30)]
    assert needs == sorted(needs, reverse=True)


def test_a_coin_matching_the_fleet_is_never_separable():
    """None, not a huge number - 'nothing here' is not 'keep waiting'."""
    assert ce.trips_to_separate_win_rate(0.77, 0.77) is None


def test_the_report_states_plainly_that_the_bar_is_uninformative():
    r = ce.report([coin("A", 3, 3, 1.0)], fleet_win_rate=FLEET_RATE,
                  fleet_mean_pct=FLEET_PCT)
    assert r["bar_is_informative"] is False
    assert r["bar_catches_below_win_rate_pct"] < 25


def test_a_bar_that_WOULD_be_informative_reads_as_such():
    r = ce.report([coin("A", 3, 3, 1.0)], fleet_win_rate=FLEET_RATE,
                  fleet_mean_pct=FLEET_PCT, verdict_bar_trips=400)
    assert r["bar_is_informative"] is True


# ------------------------------------------------------------- shrinkage

def test_one_trip_reads_as_the_fleet_barely_nudged():
    """A coin at 100% over one trip is not a 100% coin."""
    out = ce.assess(coin("A", 1, 1, 9.0), fleet_win_rate=FLEET_RATE,
                    fleet_mean_pct=FLEET_PCT)
    assert FLEET_PCT < out["shrunk_mean_pct"] < 9.0
    assert out["shrunk_mean_pct"] - FLEET_PCT < 1.0


def test_more_trips_move_the_estimate_further_from_the_prior():
    thin = ce.assess(coin("A", 1, 1, 9.0), fleet_win_rate=FLEET_RATE, fleet_mean_pct=FLEET_PCT)
    thick = ce.assess(coin("A", 40, 36, 9.0), fleet_win_rate=FLEET_RATE, fleet_mean_pct=FLEET_PCT)
    assert thick["shrunk_mean_pct"] > thin["shrunk_mean_pct"]


def test_no_trips_at_all_reads_exactly_as_the_fleet():
    assert ce.shrink(99.0, 0, FLEET_PCT) == FLEET_PCT


# ----------------------------------------- the asymmetry replay must obey

def test_replay_may_eliminate_a_coin():
    out = ce.assess(coin("BAD", 2, 2, 1.0), fleet_win_rate=FLEET_RATE,
                    fleet_mean_pct=FLEET_PCT, replay_pct=-0.42,
                    replay_window_rose=True)
    assert out["verdict"] == ce.ELIMINATED
    assert out["decided_by"] == "replay"
    assert "still lost" in out["why"]


def test_replay_may_NEVER_crown_one_over_a_rising_window():
    """The sample was favourable; a positive result has not been tested."""
    out = ce.assess(coin("GOOD", 2, 2, 1.0), fleet_win_rate=FLEET_RATE,
                    fleet_mean_pct=FLEET_PCT, replay_pct=+4.0,
                    replay_window_rose=True)
    assert out["verdict"] != ce.FUND
    assert out["replay_is_not_evidence"] is True
    assert "falling market" in out["replay_note"]


def test_a_negative_replay_outranks_thin_positive_live_trips():
    """Two green trips do not rescue a coin that loses on hundreds."""
    out = ce.assess(coin("X", 2, 2, 5.0), fleet_win_rate=FLEET_RATE,
                    fleet_mean_pct=FLEET_PCT, replay_pct=-1.0)
    assert out["verdict"] == ce.ELIMINATED


# ------------------------------------------------------------- verdicts

def test_a_coin_losing_money_is_starved_without_waiting_for_ten_trips():
    out = ce.assess(coin("LOSER", 5, 1, -12.0), fleet_win_rate=FLEET_RATE,
                    fleet_mean_pct=FLEET_PCT)
    assert out["verdict"] == ce.STARVE
    assert out["decided_by"] == "live return"


def test_a_thin_coin_is_held_not_funded_however_green():
    out = ce.assess(coin("NEW", 1, 1, 6.4), fleet_win_rate=FLEET_RATE,
                    fleet_mean_pct=FLEET_PCT)
    assert out["verdict"] == ce.HOLD


def test_a_coin_with_real_trips_above_the_fleet_is_funded():
    out = ce.assess(coin("DOGE", 11, 10, 3.335), fleet_win_rate=FLEET_RATE,
                    fleet_mean_pct=FLEET_PCT)
    assert out["verdict"] == ce.FUND


def test_hold_explains_that_replay_is_the_faster_question():
    out = ce.assess(coin("NEW", 1, 1, 3.0), fleet_win_rate=FLEET_RATE,
                    fleet_mean_pct=FLEET_PCT)
    assert "replay is the faster question" in out["why"]


def test_verdicts_sort_worst_first():
    r = ce.report([coin("G", 11, 10, 3.3), coin("B", 5, 1, -9.0), coin("N", 1, 1, 2.0)],
                  fleet_win_rate=FLEET_RATE, fleet_mean_pct=FLEET_PCT)
    assert r["coins"][0]["verdict"] == ce.STARVE


# --------------------------------------------------------------- safety

def test_it_decides_nothing_by_itself():
    r = ce.report([coin("A", 1, 1, 1.0)], fleet_win_rate=FLEET_RATE, fleet_mean_pct=FLEET_PCT)
    assert r["decides_nothing"] is True


def test_it_cannot_reach_the_venue_or_the_database():
    import ast
    mods = set()
    for n in ast.walk(ast.parse(open("coin_evidence.py").read())):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert not (mods & {"aiohttp", "sqlalchemy", "models", "requests", "crypto_grid_bot"})


def test_degenerate_inputs_do_not_crash():
    assert ce.trips_to_separate_win_rate(0.0, 0.5) is None
    assert ce.trips_to_separate_win_rate(1.0, 0.5) is None
    r = ce.report([{"product_id": "A"}, "junk"], fleet_win_rate=FLEET_RATE,
                  fleet_mean_pct=FLEET_PCT)
    assert len(r["coins"]) == 1


def test_the_live_numbers_reproduce_the_finding():
    """ETH at 75% over 8 trips against a 77% fleet."""
    out = ce.assess(coin("ETH-USD", 8, 6, 1.008), fleet_win_rate=FLEET_RATE,
                    fleet_mean_pct=FLEET_PCT)
    assert out["trips_to_separate_on_win_rate"] > 5000
    assert out["verdict"] == ce.HOLD
