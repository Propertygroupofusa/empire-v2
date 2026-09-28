"""The spread limit was a share of the move all along - it just said 0.15%.

WHAT HAPPENED. DEFAULT_MAX_SPREAD_PCT was a flat 0.0015, and its own comment
gave the reason: a wide spread "is not recoverable INSIDE A GRID STEP". The
step was 1.00% when that was written, where 0.15% is 15% of the move. The
fleet step is now 3.00% and the constant never moved with it, so the gate
went on refusing at 15% of a step that no longer exists.

Measured on the live book 2026-09-28, at the real 0.70% maker round trip:

    coin    spread   net @ 3.00% step   net @ 1.00% step
    JASMY   0.378%       +1.61%              -0.39%
    ACH     0.336%       +1.70%              -0.30%
    PEPE    0.235%       +1.72%              -0.28%
    SHIB    0.172%       +1.89%              -0.11%

Every one LOSES at the step the limit was written for - the gate was right -
and every one CLEARS at the step actually in force. JASMY and ACH are the
fleet's two best earners by net profit per dollar per day, refused on every
cycle.

THIS IS NOT A LOOSENING, and the first test is the proof: at a 1.00% target
the derived limit is 0.150%, identical to the constant it replaces. Nothing
that passed before changes. The rule is the same rule, stated in the units
its own rationale always used, so it cannot go stale again.

AND THE GATE GOT STRICTER IN THE PLACE THAT MATTERED. The spread was gated
but never PRICED - refused above the limit, charged at ZERO below it - so
total_cost_pct's docstring ("everything a completed round trip pays") was
false for the whole range it admitted. It is now charged into the edge, so a
coin whose spread eats its edge is refused by arithmetic rather than passing
a threshold it happened to sit under.
"""
import pytest

import crypto_nine_coin_scanner as S

# live 2026-09-28: spread, average hourly swing
LIVE = {"JASMY-USD": (0.00378, 0.0155), "ACH-USD": (0.00336, 0.0131),
        "PEPE-USD": (0.00235, 0.0171), "SHIB-USD": (0.00172, 0.0119),
        "NEAR-USD": (0.00045, 0.0282)}
FEE = 0.0070


# ------------------------------------------- it is the same rule, restated
def test_at_the_step_it_was_written_for_the_limit_is_unchanged():
    """The whole claim that this is not a loosening."""
    assert S.max_spread_for(0.010) == pytest.approx(S.DEFAULT_MAX_SPREAD_PCT)


def test_it_scales_with_the_move_being_attempted():
    assert S.max_spread_for(0.030) == pytest.approx(0.0045)
    assert S.max_spread_for(0.020) == pytest.approx(0.0030)


def test_a_tiny_target_cannot_collapse_the_gate():
    """Without the floor, a 0.10% target would admit only a 0.015% spread
    and refuse every real book."""
    assert S.max_spread_for(0.001) == pytest.approx(S.DEFAULT_MAX_SPREAD_PCT)
    assert S.max_spread_for(0.0) == pytest.approx(S.DEFAULT_MAX_SPREAD_PCT)


def test_a_huge_target_cannot_licence_a_broken_book():
    assert S.max_spread_for(0.50) == pytest.approx(S.DEFAULT_MAX_SPREAD_ABSOLUTE_PCT)
    assert S.max_spread_for(0.20) <= S.DEFAULT_MAX_SPREAD_ABSOLUTE_PCT


# --------------------------------------------- the live coins, both steps
@pytest.mark.parametrize("coin", sorted(LIVE))
def test_the_blocked_coins_pass_at_the_step_in_force(coin):
    spread, _ = LIVE[coin]
    assert spread <= S.max_spread_for(0.030), coin


@pytest.mark.parametrize("coin", sorted(LIVE))
def test_and_are_still_refused_at_the_step_the_old_limit_assumed(coin):
    """The gate was RIGHT at 1.00%. If this ever passes, the change has gone
    from restating the rule to abandoning it."""
    spread, swing = LIVE[coin]
    if spread <= S.max_spread_for(0.010):
        pytest.skip(f"{coin} was never blocked - nothing to prove")
    assert S.net_edge_pct(0.010, swing, FEE, spread_pct=spread) < 0, coin


@pytest.mark.parametrize("coin", sorted(LIVE))
def test_every_admitted_coin_genuinely_clears_its_costs(coin):
    spread, swing = LIVE[coin]
    assert S.net_edge_pct(0.030, swing, FEE, spread_pct=spread) > 0, coin


# ------------------------------------------- the spread is now a real cost
def test_the_spread_is_charged_into_the_cost():
    swing = 0.0155
    without = S.total_cost_pct(swing, FEE)
    with_spread = S.total_cost_pct(swing, FEE, spread_pct=0.00378)
    assert with_spread - without == pytest.approx(0.00378)


def test_charging_it_makes_the_gate_stricter_not_looser():
    """A coin inside the limit used to have its spread ignored entirely."""
    swing = 0.0119
    assert S.net_edge_pct(0.030, swing, FEE, spread_pct=0.0014) < \
           S.net_edge_pct(0.030, swing, FEE, spread_pct=0.0)


def test_a_spread_that_eats_the_edge_is_refused_by_arithmetic():
    swing = 0.0119
    assert S.net_edge_pct(0.012, swing, FEE, spread_pct=0.0045) < 0


def test_a_negative_or_missing_spread_is_not_a_credit():
    swing = 0.0119
    base = S.total_cost_pct(swing, FEE)
    assert S.total_cost_pct(swing, FEE, spread_pct=None) == pytest.approx(base)
    assert S.total_cost_pct(swing, FEE, spread_pct=-0.01) == pytest.approx(base)


def test_it_does_not_double_charge_adverse_selection():
    """Adverse selection subsumes per-side SLIPPAGE - price moving against
    the fill - which is a different cost from the bid-ask gap."""
    swing = 0.0155
    assert S.total_cost_pct(swing, FEE, spread_pct=0.003) == pytest.approx(
        FEE + S.adverse_selection_pct(swing) + 0.003)


# ---------------------------------------- an explicit caller still governs
def test_a_caller_that_sets_the_limit_still_gets_exactly_that():
    row = S.evaluate_coin("X-USD", target_pct=0.030, stop_pct=0.02,
                          hourly_swing_pct=0.0155, best_bid=100.0, best_ask=100.4,
                          bid_depth_usd=1e6, ask_depth_usd=1e6,
                          max_spread_pct=0.0015)
    assert row["max_spread_pct"] == pytest.approx(0.0015)
    assert "is over the" in (row.get("reason") or "")


def test_the_derived_limit_is_reported_so_the_number_can_be_traced():
    row = S.evaluate_coin("X-USD", target_pct=0.030, stop_pct=0.02,
                          hourly_swing_pct=0.0155, best_bid=100.0, best_ask=100.4,
                          bid_depth_usd=1e6, ask_depth_usd=1e6)
    assert row["max_spread_pct"] == pytest.approx(0.0045)


def test_jasmy_end_to_end_passes_the_gate_it_was_failing():
    spread, swing = LIVE["JASMY-USD"]
    bid = 100.0
    row = S.evaluate_coin("JASMY-USD", target_pct=0.030, stop_pct=0.02,
                          hourly_swing_pct=swing, best_bid=bid,
                          best_ask=bid * (1 + spread),
                          bid_depth_usd=1e6, ask_depth_usd=1e6)
    assert "spread" not in (row.get("reason") or ""), row.get("reason")
    assert row["net_edge_pct"] > 0


# ------------------------------------------------------------- mutation
def test_reverting_to_a_flat_limit_would_reblock_the_earners():
    for coin in ("JASMY-USD", "ACH-USD", "PEPE-USD", "SHIB-USD"):
        assert LIVE[coin][0] > S.DEFAULT_MAX_SPREAD_PCT, (
            f"{coin} was not actually blocked by the flat limit - premise gone")


def test_removing_the_absolute_backstop_would_admit_a_broken_book():
    assert S.max_spread_for(0.50, absolute=1.0) > S.DEFAULT_MAX_SPREAD_ABSOLUTE_PCT
