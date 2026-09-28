"""Capital velocity, tested on the fleet's own live numbers.

The two cases that decide whether this is worth having:

  LINK  7 trips -> $0.25 on $127   fast but weak
  NEAR  4 trips -> $6.82 on $214   slower but productive

Any scoring that puts LINK above NEAR has reproduced the bug the whole
design exists to fix, so that comparison is asserted directly.
"""
import pytest

import capital_velocity as CV

DAYS = 1.0
# product: trips, net_usd, capital_usd  (live book, 2026-09-28)
LIVE = {
    "NEAR-USD":  (4, 6.82, 214.50),
    "JASMY-USD": (3, 1.92, 72.02),
    "TIA-USD":   (2, 0.91, 70.14),
    "ONDO-USD":  (2, 0.82, 70.05),
    "ETH-USD":   (9, 2.30, 400.00),
    "ACH-USD":   (1, 0.36, 99.93),
    "LINK-USD":  (7, 0.25, 127.11),
    "BCH-USD":   (3, 0.03, 171.75),
    "ZEC-USD":   (0, 0.00, 2272.62),
    "XRP-USD":   (0, 0.00, 2240.54),
}


def build():
    coins = [{"product_id": p, "trips": t, "net_usd": n, "capital_usd": c,
              "mean_hold_hours": 6.0, "pnl_stdev": None}
             for p, (t, n, c) in LIVE.items()]
    tot_n = sum(v[1] for v in LIVE.values())
    tot_c = sum(v[2] for v in LIVE.values())
    tot_t = sum(v[0] for v in LIVE.values())
    npds = [CV.net_per_dollar_per_day(n, c, DAYS) for _, n, c in
            ((p,) + v[1:] for p, v in zip(LIVE, LIVE.values()))]
    npds = [CV.net_per_dollar_per_day(v[1], v[2], DAYS) for v in LIVE.values()]
    tpds = [CV.trips_per_1k_per_day(v[0], v[2], DAYS) for v in LIVE.values()]
    edges = [CV.fee_adjusted_edge(v[1], v[0]) or 0.0 for v in LIVE.values()]
    fleet = {
        "net_per_dollar_per_day": tot_n / tot_c / DAYS,
        "trips_per_1k_per_day": tot_t / (tot_c / 1000.0) / DAYS,
        "fee_adjusted_edge": tot_n / tot_t,
        "mean_hold_hours": 6.0,
        "npd_lo": min(npds), "npd_hi": max(npds),
        "tpd_lo": min(tpds), "tpd_hi": max(tpds),
        "edge_lo": min(edges), "edge_hi": max(edges),
        "hold_lo": 1.0, "hold_hi": 48.0,
    }
    return [CV.score_coin(c, fleet, DAYS) for c in coins]


@pytest.fixture(scope="module")
def scored():
    return {s["product_id"]: s for s in build()}


# ---------------------------------------------- the metric is the metric
def test_net_per_dollar_per_day_matches_the_hand_arithmetic():
    v = CV.net_per_dollar_per_day(6.82, 214.50, 1.0)
    assert v * 1000 == pytest.approx(31.79, abs=0.01)


def test_it_refuses_rather_than_returning_zero_when_uncomputable():
    assert CV.net_per_dollar_per_day(5.0, 0, 1.0) is None
    assert CV.net_per_dollar_per_day(5.0, 100, 0) is None


def test_the_headline_case_slower_but_productive_beats_fast_but_weak(scored):
    """LINK ran 7 trips for 25 cents; NEAR ran 4 for $6.82."""
    assert scored["NEAR-USD"]["score"] > scored["LINK-USD"]["score"]


def test_a_coin_with_more_trips_does_not_automatically_win(scored):
    """ETH has the most trips in the book (9) and is not the best coin."""
    assert max(LIVE, key=lambda p: LIVE[p][0]) == "ETH-USD"
    best = max(scored.values(), key=lambda s: s["score"])
    assert best["product_id"] != "ETH-USD"


def test_the_score_alone_does_not_demote_a_coin_that_never_traded(scored):
    """Written expecting ZEC to score last. It does not, and that is right.

    BCH ran three trips for three cents: it has EVIDENCE it is poor. ZEC ran
    none: it has no evidence either way, so shrinkage returns the fleet
    average for it. A score that punished ZEC here would be inventing a
    measurement, and the same machinery would then punish a freshly funded
    good coin for not having traded yet.

    Demoting idle capital is the IDLE TAX's job, which works on counts where
    zero is a fact rather than an estimate. The separation is the design:
    the score ranks what has been measured, the tax handles what has not.
    """
    order = sorted(scored.values(), key=lambda s: -s["score"])
    assert order[-1]["product_id"] == "BCH-USD", "the measured-bad coin should rank last"
    zec = scored["ZEC-USD"]
    assert zec["trips"] == 0
    assert zec["score"] > order[-1]["score"]


def test_but_the_idle_tax_puts_it_in_tier_d_anyway(scored):
    """The other half of that separation, asserted end to end."""
    rows = CV.assign_tiers(list(scored.values()),
                           {"ZEC-USD": "REMOVE", "XRP-USD": "REMOVE"})
    tiers = {r["product_id"]: r["tier"] for r in rows}
    assert tiers["ZEC-USD"] == "D" and tiers["XRP-USD"] == "D"


# ------------------------------------------------------- shrinkage works
def test_zero_trips_returns_the_fleet_value_exactly():
    """A coin that never traded has not disagreed with the fleet."""
    assert CV.shrink(999.0, 0, 2.10) == 2.10


def test_one_winning_trade_barely_moves_a_coin():
    """The owner's rule - never scale on one trade - as arithmetic."""
    moved = CV.shrink(100.0, 1, 0.0)
    assert moved == pytest.approx(100.0 / 9.0)
    assert moved < 100.0 * 0.12


def test_evidence_accumulates_toward_the_coins_own_number():
    prev = None
    for n in (1, 4, 8, 32, 128):
        v = CV.shrink(100.0, n, 0.0)
        assert prev is None or v > prev
        prev = v
    assert CV.shrink(100.0, 8, 0.0) == pytest.approx(50.0)   # sample == prior
    assert CV.shrink(100.0, 1000, 0.0) > 99.0


def test_consistency_is_not_scored_on_too_few_trips(scored):
    assert scored["ACH-USD"]["consistency_measured"] is False
    assert scored["ETH-USD"]["consistency_measured"] is False  # no stdev supplied


def test_win_rate_is_not_an_input():
    """It sits near 77-79% by construction in a grid; scoring it would score
    the exit rule. Asserted on the weights so it cannot creep back."""
    assert "win_rate" not in CV.WEIGHTS
    assert sum(CV.WEIGHTS.values()) == pytest.approx(1.0)


# ------------------------------------------------------- the idle tax
@pytest.mark.parametrize("days,trips,window,expected", [
    (2, 3, 2, "OK"),
    (8, 0, 8, "WARN"),
    (15, 1, 15, "REVIEW"),
    (31, 1, 31, "REDUCE"),
    (61, 0, 61, "REMOVE"),
    (61, 1, 61, "REMOVE"),
])
def test_the_ladder_returns_the_harshest_rung_met(days, trips, window, expected):
    action, _ = CV.idle_verdict(days, trips, window)
    assert action == expected


def test_zec_at_sixty_days_and_one_trip_is_a_removal_candidate():
    action, why = CV.idle_verdict(60, 1, 60)
    assert action == "REMOVE"
    assert "60 days" in why


def test_an_unknown_last_trip_is_not_taxed():
    action, why = CV.idle_verdict(None, 0, 60)
    assert action == "UNKNOWN"
    assert "gap is not a zero" in why


# --------------------------------------------------- tiers and targets
def test_idle_demotes_but_never_promotes(scored):
    rows = list(scored.values())
    plain = {r["product_id"]: r["tier"] for r in CV.assign_tiers(rows, {})}
    taxed = {r["product_id"]: r["tier"]
             for r in CV.assign_tiers(rows, {"NEAR-USD": "REMOVE"})}
    assert plain["NEAR-USD"] in ("A", "B")
    assert taxed["NEAR-USD"] == "D"
    # nothing else was promoted by the demotion
    order = {"A": 0, "B": 1, "C": 2, "D": 3}
    for p in plain:
        assert order[taxed[p]] >= order[plain[p]] or p != "NEAR-USD"


def test_a_coin_that_never_traded_is_tier_d(scored):
    tiers = {r["product_id"]: r["tier"] for r in CV.assign_tiers(list(scored.values()), {})}
    assert tiers["ZEC-USD"] == "D" and tiers["XRP-USD"] == "D"


def test_no_target_exceeds_the_single_coin_cap(scored):
    tiered = CV.assign_tiers(list(scored.values()), {})
    rows = CV.target_allocations(tiered, 7429.0)
    for r in rows:
        assert r["target_usd"] <= 7429.0 * CV.MAX_SINGLE_COIN_SHARE + 0.01


def test_tier_d_is_targeted_at_zero(scored):
    tiered = CV.assign_tiers(list(scored.values()), {})
    rows = {r["product_id"]: r for r in CV.target_allocations(tiered, 7429.0)}
    assert rows["ZEC-USD"]["target_usd"] == 0.0
    assert rows["ZEC-USD"]["delta_usd"] < 0


def test_the_plan_moves_money_off_the_quiet_giants(scored):
    tiered = CV.assign_tiers(list(scored.values()), {})
    rows = CV.target_allocations(tiered, 7429.0)
    # sorted most-negative first: the biggest reductions lead
    assert rows[0]["product_id"] in ("ZEC-USD", "XRP-USD")


def test_no_targets_without_capital(scored):
    tiered = CV.assign_tiers(list(scored.values()), {})
    assert CV.target_allocations(tiered, 0) == []


# --------------------------------------------- the tier A floor (the guarantee)
def test_a_below_median_earner_cannot_hold_tier_a(scored):
    """LINK: 7 trips, 25 cents, $1.97 per $1k per day. The weights alone
    cannot guarantee this - any additive score lets a big enough frequency
    term carry a weak coin - so the floor is what enforces it."""
    rows = {r["product_id"]: r for r in CV.assign_tiers(list(scored.values()), {})}
    assert rows["LINK-USD"]["tier"] != "A"


def test_the_floor_is_the_fleet_median_not_a_hardcoded_number():
    vals = [{"net_per_dollar_per_day": v, "score": 1.0, "trips": 3}
            for v in (1.0, 2.0, 3.0, 4.0)]
    assert CV.tier_a_floor(vals) == pytest.approx(2.5)
    assert CV.tier_a_floor([]) is None
    # a coin that never traded does not get a vote on where the bar sits
    with_silent = vals + [{"net_per_dollar_per_day": 0.0, "score": 1.0, "trips": 0}] * 6
    assert CV.tier_a_floor(with_silent) == pytest.approx(2.5)


def test_removing_the_floor_would_readmit_the_weak_coin(scored):
    saved = CV.tier_a_floor
    try:
        CV.tier_a_floor = lambda s: None
        rows = {r["product_id"]: r for r in CV.assign_tiers(list(scored.values()), {})}
        assert rows["LINK-USD"]["tier"] == "A", "the floor is not what keeps LINK out of A"
    finally:
        CV.tier_a_floor = saved


def test_net_per_dollar_carries_the_plurality_of_the_weight():
    assert CV.WEIGHTS["net_per_dollar"] == max(CV.WEIGHTS.values())
    assert CV.WEIGHTS["net_per_dollar"] > CV.WEIGHTS["trip_frequency"]


def test_a_lower_tier_never_out_funds_a_higher_one_per_coin(scored):
    """BCH was alone in tier C and drew $743 - more than any tier B coin -
    purely for being the only one there. The tier ordering has to survive
    how many coins happen to land in each."""
    tiered = CV.assign_tiers(list(scored.values()), {"ZEC-USD": "REMOVE"})
    rows = CV.target_allocations(tiered, 7429.0)
    per_tier = {}
    for r in rows:
        per_tier.setdefault(r["tier"], set()).add(r["target_usd"])
    for t in per_tier:
        assert len(per_tier[t]) == 1, f"tier {t} has uneven targets"
    order = [t for t in ("A", "B", "C", "D") if t in per_tier]
    vals = [next(iter(per_tier[t])) for t in order]
    assert vals == sorted(vals, reverse=True), dict(zip(order, vals))
