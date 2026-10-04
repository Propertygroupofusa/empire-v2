"""Cash no branch claims, into branches that can actually spend it.

Encodes a correction: I told the account owner the fleet was UNDER-cashed,
computing ammunition as if all 21 branches could buy 3 rungs each (~$2,474
against ~$1,021 in the wallet). Thirteen of them are parked and cannot buy
at all. Only eight can, needing $591.10 against $1,162.11. There is a
surplus, not a shortfall.
"""
import ast

import pytest

import idle_cash as ic


def br(pid, alloc, open_slices=0, levels=3, bot=None, active=True, locked=False):
    return {"product_id": pid, "bot_name": bot or pid, "allocated_usd": alloc,
            "open_slices": open_slices, "num_levels": levels,
            "active": active, "locked": locked}


def tr(pid, n=1):
    return [{"product_id": pid} for _ in range(n)]


def pad(n=12, alloc=400):
    """A fleet big enough that the 20% rule is not saturated by default.

    Third time this has bitten in this session's tests: in a one- or
    two-branch fleet EVERY branch already exceeds 20%, so nothing can ever
    receive and the rule looks broken when it is working. Padding branches
    are parked, so they are refused rather than becoming recipients.
    """
    return [br(f"P{i}", alloc, open_slices=3, levels=3) for i in range(n)]


# ------------------------------------------------- who may receive it

def test_a_parked_branch_is_skipped_it_cannot_spend_it():
    """Allocation on a branch that cannot buy is idle money with a name on it."""
    p = ic.plan([br("A", 100, open_slices=3, levels=3)], tr("A", 5),
                unclaimed_usd=1000)
    assert p["ok"] is False
    assert p["refusals"][0]["reason"] == "PARKED_CANNOT_SPEND_IT"


def test_a_branch_with_no_completed_trip_is_skipped():
    p = ic.plan([br("A", 100, open_slices=0)], [], unclaimed_usd=1000)
    assert p["refusals"][0]["reason"] == "NO_COMPLETED_ROUND_TRIP_YET"


def test_a_buyable_branch_with_a_trip_receives():
    p = ic.plan([br("A", 100, open_slices=0)] + pad(), tr("A"), unclaimed_usd=1000)
    assert p["ok"] and p["adds"][0]["product_id"] == "A"


def test_inactive_and_locked_are_skipped():
    for kw, reason in ((dict(active=False), "BRANCH_IS_INACTIVE"),
                       (dict(locked=True), "BRANCH_IS_LOCKED")):
        p = ic.plan([br("A", 100, open_slices=0, **kw)], tr("A"), unclaimed_usd=1000)
        assert p["refusals"][0]["reason"] == reason


# --------------------------------------------------- equal weight

def test_it_splits_evenly_rather_than_backing_the_leader():
    """NEAR has earned more than everything else combined and coin_evidence
    still calls it HOLD - four trips cannot separate a coin from the fleet.
    Funding a four-trade sample like a proven one is the same error as
    talking yourself out of a winner, pointed the other way."""
    bs = [br("WIN", 200, open_slices=0), br("OK", 50, open_slices=0)] + pad()
    p = ic.plan(bs, tr("WIN", 9) + tr("OK", 1), unclaimed_usd=500 + ic.RESERVE_USD)
    amounts = {a["product_id"]: a["usd"] for a in p["adds"]}
    assert amounts["WIN"] == amounts["OK"]
    assert p["equal_weight"] is True


def test_the_split_sums_to_the_deployable_figure():
    bs = [br(c, 100, open_slices=0) for c in "ABC"] + pad()
    p = ic.plan(bs, tr("A") + tr("B") + tr("C"), unclaimed_usd=400)
    assert abs(p["total_usd"] - p["deployable_usd"]) < 0.05


# ------------------------------------------------------- the reserve

def test_a_reserve_is_held_back():
    """A wallet at zero turns an ordinary rebuy into a failed order."""
    p = ic.plan([br("A", 100, open_slices=0)], tr("A"), unclaimed_usd=300)
    assert p["deployable_usd"] == 300 - ic.RESERVE_USD
    assert p["total_usd"] <= p["deployable_usd"]


def test_cash_below_the_reserve_deploys_nothing():
    p = ic.plan([br("A", 100, open_slices=0)], tr("A"),
                unclaimed_usd=ic.RESERVE_USD - 1)
    assert p["ok"] is False and p["total_usd"] == 0.0


def test_the_deployable_figure_is_unclaimed_cash_not_the_wallet():
    """allocated_usd already claims the cash behind every unfilled rung."""
    p = ic.plan([br("A", 100, open_slices=0)], tr("A"), unclaimed_usd=500)
    assert p["unclaimed_usd"] == 500


# --------------------------------------------------- the 20% rule

def test_no_branch_may_be_pushed_past_the_share_rule():
    bs = [br("BIG", 900, open_slices=0), br("S", 50, open_slices=0)]
    p = ic.plan(bs, tr("BIG") + tr("S"), unclaimed_usd=1100,
                max_share_pct=20)
    got = {a["product_id"] for a in p["adds"]}
    assert "BIG" not in got
    assert any(r["reason"] == "WOULD_BREACH_THE_SHARE_RULE" for r in p["refusals"])


def test_every_add_reports_its_resulting_share():
    p = ic.plan([br("A", 100, open_slices=0)] + pad(), tr("A"), unclaimed_usd=400)
    assert p["adds"][0]["share_pct_after"] is not None


# ------------------------------------------------------- safety

def test_dust_is_not_worth_a_bookkeeping_entry():
    bs = [br(c, 10, open_slices=0) for c in "ABCDEFGHIJ"]
    trades = sum((tr(c) for c in "ABCDEFGHIJ"), [])
    p = ic.plan(bs, trades, unclaimed_usd=ic.RESERVE_USD + 30)
    assert all(a["usd"] >= ic.MIN_ADD_USD for a in p["adds"])


def test_it_changes_nothing_by_itself():
    p = ic.plan([br("A", 100, open_slices=0)], tr("A"), unclaimed_usd=400)
    assert p["is_a_plan_not_a_change"] is True


def test_it_cannot_reach_the_venue_or_the_database():
    mods = set()
    for n in ast.walk(ast.parse(open("idle_cash.py").read())):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert not (mods & {"aiohttp", "sqlalchemy", "models", "requests", "crypto_grid_bot"})


def test_junk_rows_do_not_crash_it():
    p = ic.plan([br("A", 100, open_slices=0), "junk", {}] + pad(), tr("A"), unclaimed_usd=400)
    assert p["ok"]


def test_negative_unclaimed_deploys_nothing():
    p = ic.plan([br("A", 100, open_slices=0)], tr("A"), unclaimed_usd=-500)
    assert p["ok"] is False and p["total_usd"] == 0.0


def test_the_live_shape_reproduces():
    """8 buyable, 2 of them without a trip -> 6 receive; 12 parked refused."""
    bs = ([br(c, 200, open_slices=0) for c in ("NEAR", "TIA", "ONDO")]
          + [br(c, 200, open_slices=2) for c in ("ALGO", "QNT", "JASMY")]
          + [br(c, 200, open_slices=1) for c in ("FLOKI", "BTC")]
          + [br(c, 400, open_slices=3) for c in ("ZEC", "XRP", "XLM", "SHIB")])
    trades = sum((tr(c) for c in ("NEAR", "TIA", "ONDO", "ALGO", "QNT", "JASMY")), [])
    p = ic.plan(bs, trades, unclaimed_usd=458.61)
    assert len(p["adds"]) == 6
    assert sum(1 for r in p["refusals"] if r["reason"] == "PARKED_CANNOT_SPEND_IT") == 4
    assert sum(1 for r in p["refusals"] if r["reason"] == "NO_COMPLETED_ROUND_TRIP_YET") == 2


def test_a_fleet_too_small_for_the_share_rule_refuses_deliberately():
    """Stated so it is not mistaken for the rule failing. With one branch
    holding everything, adding anything breaches 20% - and refusing is
    correct, because concentrating the whole account into one coin to stop
    cash sitting idle is the worse trade."""
    p = ic.plan([br("A", 100, open_slices=0)], tr("A"), unclaimed_usd=400)
    assert p["adds"] == []
    assert any(r["reason"] == "WOULD_BREACH_THE_SHARE_RULE" for r in p["refusals"])


# ------------------------------------- a branch that only ever lost money

def trp(pid, pnls):
    """Round trips WITH their P&L. tr() above omits pnl, which reads as 0.0
    and stays eligible - that is deliberate, so every pre-existing test
    keeps its meaning."""
    return [{"product_id": pid, "pnl": p} for p in pnls]


def test_a_branch_whose_trips_net_negative_is_refused():
    """ONDO-USD: 5 completed round trips, -$4.57 net. It cleared the
    one-trip bar and would have been handed an equal share."""
    out = ic.plan([br("ONDO-USD", 65.16, open_slices=1)] + pad(),
                  trp("ONDO-USD", [0.5, -1.2, 0.3, -2.0, -2.17]),
                  unclaimed_usd=1000.0)
    assert [a["product_id"] for a in out["adds"]] == []
    r = [x for x in out["refusals"] if x["product_id"] == "ONDO-USD"]
    assert r and r[0]["reason"] == "NET_NEGATIVE_SO_FAR"
    assert r[0]["net_realised_usd"] == -4.57
    assert r[0]["round_trips"] == 5


def test_a_profitable_branch_is_still_funded():
    out = ic.plan([br("NEAR-USD", 183.89, open_slices=2)] + pad(),
                  trp("NEAR-USD", [1.2, 0.8, -0.3, 2.0]),
                  unclaimed_usd=1000.0)
    assert "NEAR-USD" in [a["product_id"] for a in out["adds"]]


def test_a_single_loss_does_not_condemn_a_net_winner():
    """The test is the NET of the trips, not whether any one lost."""
    out = ic.plan([br("A-USD", 200.0, open_slices=1)] + pad(),
                  trp("A-USD", [-5.0, 6.0]), unclaimed_usd=1000.0)
    assert "A-USD" in [a["product_id"] for a in out["adds"]]


def test_breakeven_is_not_refused():
    """Only strictly negative is withheld; exactly zero stays eligible, so
    this changes as little of the existing behaviour as possible."""
    out = ic.plan([br("A-USD", 200.0, open_slices=1)] + pad(),
                  trp("A-USD", [-2.0, 2.0]), unclaimed_usd=1000.0)
    assert "A-USD" in [a["product_id"] for a in out["adds"]]


def test_the_refusal_never_changes_what_a_winner_receives():
    """REFUSE-ONLY: withholding from a loser must not re-weight anyone.
    The winners' shares are identical with and without the loser present."""
    winners = [br("A-USD", 200.0, open_slices=1),
               br("B-USD", 200.0, open_slices=1)]
    good = trp("A-USD", [3.0]) + trp("B-USD", [3.0])
    without = ic.plan(winners + pad(), good, unclaimed_usd=1000.0)
    with_loser = ic.plan(winners + [br("C-USD", 200.0, open_slices=1)] + pad(),
                         good + trp("C-USD", [-9.0]), unclaimed_usd=1000.0)
    shares = lambda o: {a["product_id"]: a["usd"] for a in o["adds"]}
    assert shares(without) == shares(with_loser)
    assert "C-USD" not in shares(with_loser)


def test_it_is_not_a_performance_ranking():
    """Two winners of very different quality still split EQUALLY. The
    module's refusal to rank is intact - measured Spearman +0.459 on 14
    coins is under the ~0.544 that sample needs, so ranking stays out."""
    out = ic.plan([br("A-USD", 200.0, open_slices=1),
                   br("B-USD", 200.0, open_slices=1)] + pad(),
                  trp("A-USD", [50.0]) + trp("B-USD", [0.05]),
                  unclaimed_usd=1000.0)
    usd = {a["product_id"]: a["usd"] for a in out["adds"]}
    assert usd["A-USD"] == usd["B-USD"]
