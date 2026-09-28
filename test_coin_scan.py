"""The scan must reject the traps it would otherwise have funded.

Every case below is a REAL coin from the 2026-09-28 scan of all 402 online
Coinbase USD products, with its measured 60-day numbers. The two that matter
most are COTI and HFT: they had the highest trip counts in the entire liquid
universe, and ranking on trips - the obvious thing to do - would have put the
fleet's money straight into them.
"""
import pytest

import coin_scan as CS

# product: trips@3%/60d, candles, 24h notional, 60d change %, max drawdown %
MEASURED = {
    # the trap: most oscillation in the universe, and bleeding
    "COTI-USD":    dict(trips=18, candles=1440, notional_usd=1_000_000, change_pct=-21.4, drawdown_pct=-50),
    "HFT-USD":     dict(trips=18, candles=1440, notional_usd=1_700_000, change_pct=-26.1, drawdown_pct=-85),
    # the other trap: a runner
    "USELESS-USD": dict(trips=9,  candles=1440, notional_usd=6_300_000, change_pct=434.2, drawdown_pct=-42),
    "PYTH-USD":    dict(trips=6,  candles=1440, notional_usd=2_400_000, change_pct=109.7, drawdown_pct=-18),
    # too thin to fund, however well it oscillates
    "ACH-USD":     dict(trips=16, candles=1440, notional_usd=52_080,    change_pct=5.0,   drawdown_pct=-20),
    # the real candidates
    "PRIME-USD":   dict(trips=10, candles=1440, notional_usd=900_000,   change_pct=18.6,  drawdown_pct=-15),
    "TON-USD":     dict(trips=7,  candles=1440, notional_usd=3_100_000, change_pct=15.3,  drawdown_pct=-16),
    "APE-USD":     dict(trips=7,  candles=1440, notional_usd=1_300_000, change_pct=17.3,  drawdown_pct=-24),
    # already held, for comparison
    "JASMY-USD":   dict(trips=6,  candles=1440, notional_usd=3_400_000, change_pct=23.5,  drawdown_pct=-32),
    "ZEC-USD":     dict(trips=1,  candles=1440, notional_usd=105_100_000, change_pct=-5.0, drawdown_pct=-25),
}


@pytest.fixture(scope="module")
def ranked():
    return CS.rank(MEASURED)


# ------------------------------------------- the traps are rejected by name
@pytest.mark.parametrize("pid,needle", [
    ("COTI-USD", "drawdown"),
    ("HFT-USD", "drawdown"),
    ("USELESS-USD", "runner"),
    ("PYTH-USD", "runner"),
    ("ACH-USD", "depth floor"),
])
def test_trap_is_rejected_with_its_reason(ranked, pid, needle):
    _, rejected = ranked
    hit = next((r for r in rejected if r["product_id"] == pid), None)
    assert hit is not None, f"{pid} was NOT rejected - this is the bug"
    assert needle in hit["reason"], hit["reason"]


def test_the_two_highest_trip_coins_in_the_universe_are_both_rejected():
    """The whole point. Sorting by trips puts COTI and HFT first; both are
    losers. If this ever passes by accident, the scan is ranking on trips."""
    fundable, _ = CS.rank(MEASURED)
    names = [f["product_id"] for f in fundable]
    assert "COTI-USD" not in names and "HFT-USD" not in names
    assert max(MEASURED["COTI-USD"]["trips"], MEASURED["HFT-USD"]["trips"]) == max(
        m["trips"] for m in MEASURED.values()), "the premise moved - recheck this test"


# --------------------------------------------------- the candidates survive
def test_the_real_candidates_are_fundable(ranked):
    fundable, _ = ranked
    names = [f["product_id"] for f in fundable]
    for pid in ("PRIME-USD", "TON-USD", "APE-USD"):
        assert pid in names


def test_the_best_candidate_beats_the_best_coin_already_held(ranked):
    fundable, _ = ranked
    best = fundable[0]
    held = next(f for f in fundable if f["product_id"] == "JASMY-USD")
    assert best["score"] > held["score"]


def test_a_quiet_coin_holding_most_of_the_money_ranks_last(ranked):
    """ZEC: one round trip in sixty days, on the fleet's largest allocation."""
    fundable, _ = ranked
    assert fundable[-1]["product_id"] == "ZEC-USD"


# ------------------------------------------------- nothing is silently lost
def test_every_coin_lands_in_exactly_one_list(ranked):
    fundable, rejected = ranked
    seen = [f["product_id"] for f in fundable] + [r["product_id"] for r in rejected]
    assert sorted(seen) == sorted(MEASURED)
    assert len(seen) == len(set(seen))


def test_every_rejection_carries_a_reason(ranked):
    _, rejected = ranked
    assert all(r.get("reason") for r in rejected)


def test_a_short_history_is_unknown_not_zero():
    why = CS.reject_reason(trips=20, candles=200, notional_usd=5_000_000,
                           change_pct=1.0, drawdown_pct=-5)
    assert "unknown, not zero" in why


def test_unreadable_depth_refuses_rather_than_assuming():
    why = CS.reject_reason(20, 1440, None, 1.0, -5)
    assert "unreadable" in why


def test_an_unreadable_price_path_is_not_judged_on_trips_alone():
    why = CS.reject_reason(20, 1440, 5_000_000, None, None)
    assert "trips alone are not enough" in why


# --------------------------------------------------- funding is its own gate
def test_negative_free_cash_funds_nothing():
    """Measured live: free cash -$246.87 while the scan had good coins."""
    usd, why = CS.fundable_usd(-246.87, 88.0, 3)
    assert usd == 0.0
    assert "already claim every dollar" in why


def test_unreadable_free_cash_funds_nothing():
    usd, why = CS.fundable_usd(None, 88.0, 3)
    assert usd == 0.0 and "gap is not a zero" in why


def test_real_free_cash_splits_evenly_and_floors_to_the_cent():
    usd, _ = CS.fundable_usd(301.00, 88.0, 3)
    assert usd == pytest.approx(100.33)
    assert usd * 3 <= 301.00 + 1e-9


# ------------------------------------------------------------- mutation
def test_ranking_on_raw_trips_would_fail_these_tests():
    """Prove the suitability score is doing the work, not the trip count."""
    by_trips = sorted(MEASURED, key=lambda p: -MEASURED[p]["trips"])
    assert by_trips[0] in ("COTI-USD", "HFT-USD")
    fundable, _ = CS.rank(MEASURED)
    assert fundable[0]["product_id"] not in ("COTI-USD", "HFT-USD")


def test_the_bleeders_are_caught_by_two_independent_gates():
    """Written expecting one gate to be load-bearing; the test said otherwise.

    HFT fell 26.1% AND drew down 85%, so the decline gate and the drawdown
    gate each catch it alone. Relaxing either one still keeps it out, and
    only relaxing BOTH readmits it. That is defence in depth rather than a
    single threshold doing all the work - worth asserting so a later tidy-up
    that removes "the redundant one" fails here instead of in the book.
    """
    dd, dec = CS.MAX_DRAWDOWN_PCT, CS.MAX_DECLINE_PCT
    try:
        CS.MAX_DRAWDOWN_PCT = -99.0          # drawdown gate off, decline gate on
        assert "HFT-USD" not in [f["product_id"] for f in CS.rank(MEASURED)[0]]
        CS.MAX_DRAWDOWN_PCT = dd
        CS.MAX_DECLINE_PCT = -99.0           # decline gate off, drawdown gate on
        assert "HFT-USD" not in [f["product_id"] for f in CS.rank(MEASURED)[0]]
        CS.MAX_DRAWDOWN_PCT = -99.0          # both off - now it gets through
        assert "HFT-USD" in [f["product_id"] for f in CS.rank(MEASURED)[0]], \
            "neither gate is what keeps HFT out - find what does"
    finally:
        CS.MAX_DRAWDOWN_PCT, CS.MAX_DECLINE_PCT = dd, dec
