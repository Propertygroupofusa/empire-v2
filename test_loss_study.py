"""Tests for the loss analysis, and for the thing it must never claim.

"Never take a loss" is one setting away and it is the wrong setting: a
grid that cannot sell at a loss just holds losers forever, which is how
a 0.9% step took this fleet from +65.4% to -71.1%. The module has to
keep saying so, and it has to refuse a stop recommendation it cannot
support.
"""
import ast

import pytest

import loss_study as ls


def tr(pnl, qty=1.0, entry=100.0, reason="profit_target", mae=None, coin="A-USD"):
    return {"pnl": pnl, "qty": qty, "entry_price": entry, "exit_reason": reason,
            "mae_pct": mae, "product_id": coin}


LIVE = ([tr(0.4267) for _ in range(63)]
        + [tr(-0.3826, reason="stop_loss") for _ in range(20)]
        + [tr(0.0)])


# ------------------------------------------------- the shape of it

def test_the_fragile_shape_is_named_not_congratulated():
    """75.9% winning with wins the same size as losses is profitable on
    FREQUENCY, and a win rate is the first thing a regime moves."""
    a = ls.analyse(LIVE)
    assert a["win_loss_size_ratio"] == pytest.approx(1.115, abs=0.01)
    assert "fragile shape" in a["fragility"]
    assert ls.verdict(a)[0] == "PROFITABLE_ON_FREQUENCY_NOT_SIZE"


def test_the_breakeven_win_rate_is_computed_not_asserted():
    a = ls.analyse(LIVE)
    #  wins 1.115x losses -> breakeven at 100/(1+1.115) = 47.3%
    assert a["breakeven_win_rate_pct"] == pytest.approx(47.3, abs=0.2)
    assert a["margin_of_safety_points"] > 0


def test_wins_comfortably_larger_than_losses_is_reported_as_fine():
    """Every loss here is a stop too, but that is descriptive and true of
    a healthy fleet and a fragile one alike - the RATIO is what decides
    which, so it is asked first."""
    rows = [tr(3.0) for _ in range(50)] + [tr(-1.0, reason="stop_loss") for _ in range(20)]
    a = ls.analyse(rows)
    assert a["win_loss_size_ratio"] == pytest.approx(3.0)
    assert ls.verdict(a)[0] == "EVERY_LOSS_IS_THE_STOP"


def test_a_healthy_ratio_with_mixed_exits_reads_as_in_proportion():
    rows = ([tr(3.0) for _ in range(50)]
            + [tr(-1.0, reason="stop_loss") for _ in range(10)]
            + [tr(-1.0, reason="profit_target") for _ in range(10)])
    assert ls.verdict(ls.analyse(rows))[0] == "LOSSES_ARE_IN_PROPORTION"


def test_a_few_big_losses_are_separated_from_many_small_ones():
    """Those are what a tighter stop addresses. Trimming the small ones
    saves nothing and cuts winners short doing it."""
    rows = [tr(1.0) for _ in range(50)] + [tr(-0.2) for _ in range(9)] + \
           [tr(-5.0, reason="stop_loss")]
    a = ls.analyse(rows)
    assert a["big_losses"] == 1
    assert ls.verdict(a)[0] == "A_FEW_LOSSES_DOMINATE"
    assert "trimming the many small losses saves almost nothing" in ls.verdict(a)[1].lower()


def test_losses_are_split_by_how_they_were_taken():
    rows = [tr(1.0) for _ in range(30)] + [tr(-1.0, reason="stop_loss") for _ in range(4)] + \
           [tr(-1.0, reason=None)]
    a = ls.analyse(rows)
    assert a["losses_by_exit_reason"] == {"stop_loss": 4, "unrecorded": 1}


def test_a_loss_that_did_not_come_from_the_stop_is_flagged_separately():
    """The sell path refuses to force a losing sale, so a loss from
    anywhere else needs explaining before the stop is touched."""
    rows = [tr(2.0) for _ in range(30)] + [tr(-0.5, reason="profit_target") for _ in range(5)]
    assert ls.verdict(ls.analyse(rows))[0] == "LOSSES_ARE_NOT_FROM_THE_STOP"


def test_every_loss_being_a_stop_is_reported_as_chosen_not_suffered():
    rows = [tr(3.0) for _ in range(30)] + [tr(-0.9, reason="stop_loss") for _ in range(5)]
    code, why = ls.verdict(ls.analyse(rows))
    assert code == "EVERY_LOSS_IS_THE_STOP"
    assert "chosen, not suffered" in why


# --------------------------------------- the sweep refuses politely

def test_the_stop_sweep_refuses_without_recorded_excursions():
    """mae_pct was added 2026-09-26. Answering now would be arithmetic on
    anecdotes, and a stop chosen that way costs money on every later trade."""
    s = ls.stop_sweep(LIVE)
    assert s["available"] is False
    assert s["usable_trades"] == 0
    assert "arithmetic on anecdotes" in s["reason"]


def test_the_sweep_runs_once_the_excursions_are_there():
    rows = ([tr(1.0, mae=-1.0) for _ in range(30)]
            + [tr(-8.7, mae=-9.0, reason="stop_loss") for _ in range(10)])
    s = ls.stop_sweep(rows)
    assert s["available"] is True
    assert s["usable_trades"] == 40
    assert s["best_stop_pct"] in [o["stop_pct"] for o in s["by_stop"]]


def test_a_tighter_stop_that_cuts_winners_is_shown_losing():
    """The whole trap. A 3% stop that also stops out the trades that went
    on to win must come out WORSE, or the sweep is useless."""
    rows = [tr(2.0, mae=-4.0) for _ in range(40)]      # every winner dipped 4% first
    s = ls.stop_sweep(rows)
    by = {o["stop_pct"]: o["net_usd"] for o in s["by_stop"]}
    assert by[3.0] < by[8.0]
    assert s["best_stop_pct"] >= 5.0


def test_a_trade_with_no_excursion_is_counted_never_assumed():
    rows = [tr(1.0, mae=-1.0) for _ in range(30)] + [tr(-5.0) for _ in range(10)]
    s = ls.stop_sweep(rows)
    assert s["usable_trades"] == 30 and s["total_trades"] == 40


def test_the_sweep_says_it_is_not_judging_the_entries():
    rows = [tr(1.0, mae=-1.0) for _ in range(30)]
    s = ls.stop_sweep(rows)
    assert "does not say those entries were good ones" in s["detail"]


# ------------------------------------- what it must never claim

def test_nothing_here_promises_a_strategy_with_no_losses():
    """A grid that never sells at a loss is called holding. The loss does
    not disappear - it stops being counted and starts being inventory."""
    src = open("loss_study.py").read()
    assert "stops being counted and starts being inventory" in src
    assert "-71.1%" in src


def test_no_losses_yet_is_reported_as_a_thin_sample_not_a_solved_problem():
    code, why = ls.verdict(ls.analyse([tr(1.0) for _ in range(30)]))
    assert code == "NO_LOSSES_YET"
    assert "has not met a bad stretch" in why


@pytest.mark.parametrize("junk", [None, [], [{}], ["x"]])
def test_junk_in_never_produces_a_recommendation(junk):
    a = ls.analyse(junk)
    assert a["trades"] == 0
    assert ls.verdict(a)[0] == "NO_TRADES"


def test_it_touches_no_venue_and_no_database():
    tree = ast.parse(open("loss_study.py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "coinbase", "sqlalchemy",
                                "models", "database")), n


def test_everything_survives_json():
    import json
    a = ls.analyse(LIVE)
    assert json.loads(json.dumps(a)) == a
    s = ls.stop_sweep(LIVE)
    assert json.loads(json.dumps(s)) == s


# ------------------------------------- whose losses are these

EPOCH = "2026-09-26T02:45:00Z"
AFTER, BEFORE = "2026-09-27T00:00:00", "2026-09-20T00:00:00"


def trt(pnl, closed, reason=None, qty=1.0, entry=100.0):
    return {"pnl": pnl, "qty": qty, "entry_price": entry,
            "exit_reason": reason, "closed_at": closed, "mae_pct": None}


def test_losses_from_a_replaced_configuration_are_not_a_live_problem():
    """All 19 of this fleet's losses predate the 2026-09-26 change,
    including the DOGE trades that prompted the sell-path fix. Tightening
    a stop over that history costs money on every future trade."""
    rows = ([trt(1.0, BEFORE) for _ in range(60)]
            + [trt(-0.5, BEFORE) for _ in range(19)]
            + [trt(2.45, AFTER) for _ in range(2)])
    a = ls.analyse(rows, config_epoch=EPOCH)
    assert a["losses"] == 19
    assert a["losses_on_current_config"] == 0
    assert a["trades_on_current_config"] == 2
    code, why = ls.verdict(a)
    assert code == "EVERY_LOSS_PREDATES_THIS_CONFIG"
    assert "already fixed" in why


def test_a_loss_on_the_current_config_is_still_a_live_problem():
    rows = ([trt(1.0, BEFORE) for _ in range(60)]
            + [trt(-0.5, BEFORE) for _ in range(19)]
            + [trt(-3.0, AFTER, reason="stop_loss")])
    a = ls.analyse(rows, config_epoch=EPOCH)
    assert a["losses_on_current_config"] == 1
    assert ls.verdict(a)[0] != "EVERY_LOSS_PREDATES_THIS_CONFIG"


def test_an_untimed_trade_counts_as_predating_the_config():
    rows = [trt(1.0, BEFORE) for _ in range(30)] + [{"pnl": -1.0, "qty": 1.0,
                                                    "entry_price": 100.0}]
    a = ls.analyse(rows, config_epoch=EPOCH)
    assert a["trades_on_current_config"] == 0


def test_without_an_epoch_no_claim_is_made_about_whose_losses_they_are():
    a = ls.analyse([trt(-1.0, BEFORE) for _ in range(5)])
    assert "cannot be told" in a["config_split"]
    assert ls.verdict(a)[0] != "EVERY_LOSS_PREDATES_THIS_CONFIG"
