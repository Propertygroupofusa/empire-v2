"""Tests for putting idle coin under a branch that already exists.

The dangerous half of this module is not the coin it deploys. It is the
four invariants it must not break while doing it - the backing ledger, the
full-branch rule that stops a branch buying with cash nobody gave it, the
stop that must not be measured from a price nobody paid, and sizing
against what the venue will release rather than what is owned.
"""
import ast

import pytest

import coin_adoption
import coin_topup


SRC = open("coin_topup.py").read()


def holding(asset, usd, price, available_units=None, units=None):
    u = units if units is not None else usd / price
    return {"asset": asset, "usd": usd, "price": price, "units": u,
            "available_units": available_units}


def branch(pid, allocated, *, override=0.0, levels=3, active=True, locked=False,
           bot="crypto_grid_1"):
    return {"product_id": pid, "allocated_usd": allocated, "num_levels": levels,
            "stop_loss_pct_override": override, "active": active, "locked": locked,
            "bot_name": bot}


# ---------------------------------------------------------------- eligibility

def test_an_original_branch_is_refused_because_its_stop_means_something():
    """Its entries are prices someone really paid.

    Adding coin at today's price would put the fleet stop 8% below an entry
    nobody paid - the hazard stop_loss_pct_override exists to prevent. This
    is why the live BTC-USD branch is left alone.
    """
    assert coin_topup.eligible(branch("BTC-USD", 69.23, override=None)) == "NOT_AN_ADOPTED_BRANCH"


def test_an_adopted_branch_is_eligible():
    assert coin_topup.eligible(branch("ZEC-USD", 400.0, override=0.0)) is None


def test_a_branch_with_a_real_override_is_still_eligible():
    """0.0 is the common case, but any non-NULL override marks adoption."""
    assert coin_topup.eligible(branch("ZEC-USD", 400.0, override=0.12)) is None


def test_zero_is_tested_with_is_none_never_truthiness():
    """`if branch.get(...)` would refuse every adopted branch, since the
    override they all carry is 0.0. Asserted on the parsed function so the
    docstring cannot satisfy it."""
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == "eligible")
    compares = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)]
    assert any(isinstance(c.ops[0], ast.Is) and isinstance(c.comparators[0], ast.Constant)
               and c.comparators[0].value is None for c in compares)


def test_inactive_and_locked_branches_are_refused():
    assert coin_topup.eligible(branch("ZEC-USD", 1.0, active=False)) == "BRANCH_IS_INACTIVE"
    assert coin_topup.eligible(branch("ZEC-USD", 1.0, locked=True)) == "BRANCH_IS_LOCKED"


# ------------------------------------------------------------ the invariants

def _one(**kw):
    hs = [holding("ZEC", 2284.44, 1659.20, available_units=2284.44 / 1659.20)]
    bs = [branch("ZEC-USD", 400.0)]
    return coin_topup.plan(hs, bs, account_total_usd=11529.08, **kw)


def test_the_claim_grows_by_exactly_the_coin_put_behind_it():
    t = _one()["topups"][0]
    backed = round(sum(s["qty"] * s["entry_price"] for s in t["slices"]), 2)
    assert t["add_usd"] == backed
    assert t["allocated_usd_after"] == round(t["allocated_usd_before"] + backed, 2)


def test_the_branch_cannot_buy_after_a_topup():
    """run_grid_branch_cycle buys only when len(slices) < num_levels.

    The planner proposes num_levels = open + added, which makes the branch
    exactly full. What it must NOT depend on is that figure surviving: the
    live fleet runs a global spacing override (3_levels_2.5pct), and
    run_grid_branch_cycle re-applies it EVERY cycle, forcing num_levels
    back to 3 on every branch. Measured after the live top-ups: ZEC 6
    open / 3 levels, XRP 9/3, SHIB 6/3.

    So the property that actually protects the account is open >= levels,
    and it has to hold under BOTH numbers - the one the writer sets and the
    one the override reclaims. Asserting equality alone passed while
    describing something the running system overwrites within a cycle.
    """
    t = _one()["topups"][0]
    assert t["add_levels"] == len(t["slices"])

    proposed = t["num_levels_after"]
    open_after = t["num_levels_before"] + t["add_levels"]
    assert proposed == open_after, "the planner still proposes exactly full"

    # ... and under the level count the override will actually force, which
    # is computed here rather than assumed. A branch left with FEWER open
    # slices than levels is free to buy on the next cycle.
    import crypto_grid_bot as grid
    alloc = t["allocated_usd_after"]
    safe = grid._safe_num_levels_for_allocation(alloc)
    for name, cfg in grid.GRID_LEVEL_SPACING_CANDIDATES.items():
        forced = max(1, min(safe, cfg["num_levels"]))
        assert open_after >= forced, (
            f"under {name} this top-up would leave {open_after} open slices "
            f"against {forced} levels - the branch could buy immediately")


def test_it_buys_nothing_and_sells_nothing():
    p = _one()
    assert p["buys_nothing"] and p["sells_nothing"] and p["is_a_plan_not_a_change"]


def test_the_slices_sum_to_the_units_taken():
    t = _one()["topups"][0]
    assert round(sum(s["qty"] for s in t["slices"]), 12) == t["add_units"]


def test_every_slice_is_entered_at_todays_price():
    """Pretending otherwise would put cost bases in the ledger nobody paid."""
    t = _one()["topups"][0]
    assert {s["entry_price"] for s in t["slices"]} == {1659.20}


# ------------------------------------------- size against what is releasable

def test_staked_units_are_not_deployed():
    """The whole reason the census now reports available separately.

    900 ADA owned, none of it available: the branch must be offered
    nothing, not 900 units it cannot move.
    """
    hs = [holding("ADA", 430.98, 0.4788, available_units=0.0)]
    p = coin_topup.plan(hs, [branch("ADA-USD", 0.0)], account_total_usd=11529.08)
    assert p["ok"] is False
    assert any(r["reason"] == "ALL_UNITS_LOCKED_OR_STAKED" for r in p["refusals"])


def test_a_partly_staked_coin_deploys_only_the_free_part():
    # $936 held, only $128 of it free.
    price = 123.79
    hs = [holding("SOL", 936.33, price, available_units=128.07 / price)]
    p = coin_topup.plan(hs, [branch("SOL-USD", 0.0)], account_total_usd=11529.08)
    assert p["ok"]
    assert p["topups"][0]["add_usd"] <= 128.07 + 0.01


def test_units_already_under_the_branch_are_not_offered_twice():
    price = 1659.20
    hs = [holding("ZEC", 2284.44, price, available_units=2284.44 / price)]
    p = coin_topup.plan(hs, [branch("ZEC-USD", 400.0)], account_total_usd=11529.08,
                        max_total_usd=99999.0)
    t = p["topups"][0]
    assert t["allocated_usd_after"] <= 2284.44 + 0.01
    assert t["idle_usd_after"] == 0.0


def test_a_missing_available_read_falls_back_to_units_and_says_nothing_silently():
    """The fallback exists, but it must not invent an available figure."""
    hs = [holding("ZEC", 2284.44, 1659.20, available_units=None)]
    p = coin_topup.plan(hs, [branch("ZEC-USD", 400.0)], account_total_usd=11529.08)
    assert p["ok"]          # it still works
    assert "available_units" in SRC and "locked" not in p["topups"][0]


# ------------------------------------------------------------------ the ramp

def test_the_ramp_bounds_one_pass():
    hs = [holding("ZEC", 5000.0, 100.0, available_units=50.0),
          holding("XRP", 5000.0, 1.5, available_units=3333.0)]
    bs = [branch("ZEC-USD", 0.0, bot="a"), branch("XRP-USD", 0.0, bot="b")]
    p = coin_topup.plan(hs, bs, account_total_usd=50000.0, max_total_usd=2000.0)
    assert p["total_usd"] <= 2000.0
    assert any(r["reason"] == "RAMP_EXHAUSTED_THIS_PASS" for r in p["refusals"])


def test_the_most_idle_branch_goes_first():
    hs = [holding("AAA", 300.0, 1.0, available_units=300.0),
          holding("BBB", 5000.0, 1.0, available_units=5000.0)]
    bs = [branch("AAA-USD", 0.0, bot="a"), branch("BBB-USD", 0.0, bot="b")]
    p = coin_topup.plan(hs, bs, account_total_usd=50000.0, max_total_usd=99999.0)
    assert p["topups"][0]["asset"] == "BBB"


def test_idle_is_floored_because_profit_can_outgrow_the_coin():
    """allocated_usd grows with realized profit, so it can exceed the
    holding. That is a branch with nothing idle, not negative idle."""
    hs = [holding("NEAR", 147.26, 5.19, available_units=147.26 / 5.19)]
    p = coin_topup.plan(hs, [branch("NEAR-USD", 212.59)], account_total_usd=11529.08)
    assert p["ok"] is False
    r = next(r for r in p["refusals"] if r["asset"] == "NEAR")
    assert r["reason"] == "NO_IDLE_COIN" and r["idle_usd"] == 0.0


def test_dust_below_the_minimum_is_refused():
    hs = [holding("XYO", 55.52, 0.00366, available_units=55.52 / 0.00366)]
    p = coin_topup.plan(hs, [branch("XYO-USD", 0.0)], account_total_usd=11529.08)
    assert p["ok"] is False


# ------------------------------------------------------------- the 20% rule

def test_a_position_over_the_limit_is_topped_up_sell_only():
    """More coin under a sell-only branch is MORE of the position walking
    down through strength, which is the point - it is not a reason to
    refuse, it is a reason to mark it."""
    hs = [holding("BIG", 3000.0, 10.0, available_units=300.0)]
    p = coin_topup.plan(hs, [branch("BIG-USD", 100.0)], account_total_usd=11529.08,
                        max_total_usd=99999.0)
    t = p["topups"][0]
    assert t["position_share_pct"] > coin_topup.MAX_POSITION_SHARE_PCT
    assert t["sell_only"] is True and t["why_sell_only"]


def test_a_position_under_the_limit_is_not_marked():
    hs = [holding("ZEC", 2284.44, 1659.20, available_units=2284.44 / 1659.20)]
    p = coin_topup.plan(hs, [branch("ZEC-USD", 400.0)], account_total_usd=11529.08)
    assert p["topups"][0]["sell_only"] is False


# -------------------------------------------------------- refusals are kept

def test_every_refusal_carries_a_reason():
    hs = [holding("ZEC", 2284.44, 1659.20, available_units=2284.44 / 1659.20)]
    bs = [branch("ZEC-USD", 400.0), branch("BTC-USD", 69.23, override=None, bot="b"),
          branch("NOPE-USD", 0.0, bot="c")]
    p = coin_topup.plan(hs, bs, account_total_usd=11529.08)
    assert all(r.get("reason") for r in p["refusals"])
    reasons = {r["reason"] for r in p["refusals"]}
    assert "NOT_AN_ADOPTED_BRANCH" in reasons
    assert "COIN_NOT_IN_THE_CENSUS" in reasons


def test_nothing_at_all_still_returns_a_readable_plan():
    p = coin_topup.plan([], [], account_total_usd=11529.08)
    assert p["ok"] is False and p["topups"] == [] and p["detail"]


def test_the_module_cannot_reach_the_network_or_the_database():
    tree = ast.parse(SRC)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert not (imported & {"aiohttp", "requests", "sqlalchemy", "models", "httpx"})


def test_the_caps_are_owner_settable(monkeypatch):
    import importlib
    monkeypatch.setenv("COIN_TOPUP_MAX_TOTAL_USD", "500")
    m = importlib.reload(coin_topup)
    try:
        assert m.MAX_TOTAL_TOPUP_USD == 500.0
    finally:
        monkeypatch.delenv("COIN_TOPUP_MAX_TOTAL_USD")
        importlib.reload(coin_topup)


def test_the_minimum_matches_adoptions_so_the_two_agree():
    assert coin_topup.MIN_TOPUP_USD == coin_adoption.MIN_ADOPT_USD
    assert coin_topup.MAX_POSITION_SHARE_PCT == coin_adoption.MAX_POSITION_SHARE_PCT


def test_zero_available_is_not_confused_with_a_missing_read():
    """0.0 available means fully staked; None means the census did not say.

    A positive-only helper collapses the two, and the collapse deploys
    every staked unit. Kept as its own test because that is exactly the
    bug this file caught on the first run.
    """
    price = 0.4788
    staked = coin_topup.plan([holding("ADA", 430.98, price, available_units=0.0)],
                             [branch("ADA-USD", 0.0)], account_total_usd=11529.08)
    unknown = coin_topup.plan([holding("ADA", 430.98, price, available_units=None)],
                              [branch("ADA-USD", 0.0)], account_total_usd=11529.08)
    assert staked["ok"] is False
    assert unknown["ok"] is True


# ---------------------------------------------------------------------------
# THE WRITER. Asserted structurally - the DB path runs on the live fleet,
# but the four invariants and the re-read are what must not drift.
# ---------------------------------------------------------------------------
import coin_adoption_worker as _w

_WSRC = open("coin_adoption_worker.py").read()


def _topup_src():
    fn = next(n for n in ast.walk(ast.parse(_WSRC))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "topup_once")
    return "\n".join(_WSRC.splitlines()[fn.lineno - 1:fn.end_lineno])


def test_the_writer_is_armed_twice_like_every_other_money_path():
    src = _topup_src()
    assert src.count("is_armed()") >= 2
    assert "disarmed mid-pass" in src


def test_the_claim_grows_by_the_slices_actually_written():
    src = _topup_src()
    assert 'added_usd = round(sum(s["qty"] * s["entry_price"] for s in t["slices"]), 2)' in src
    assert "row.allocated_usd = round((row.allocated_usd or 0.0) + added_usd, 2)" in src


def test_num_levels_comes_from_the_live_open_count_not_the_plan():
    """A sale between sizing and writing would otherwise leave the branch
    short of full, and a branch below full is free to buy.

    This asserts the WRITE is computed correctly. It deliberately does not
    assert the value persists - see
    test_the_topup_num_levels_write_does_not_survive_the_spacing_override.
    """
    src = _topup_src()
    assert "row.num_levels = len(open_now) + len(t[\"slices\"])" in src
    assert "num_levels_after" not in src          # never the planned figure


def test_the_topup_num_levels_write_does_not_survive_the_spacing_override():
    """Recorded because the opposite was claimed, out loud, and was wrong.

    run_grid_branch_cycle re-applies the live grid spacing override on
    EVERY cycle: num_levels = max(1, min(safe_for_allocation,
    override["num_levels"])). With 3_levels_2.5pct live that is 3, so the
    top-up's write is reclaimed within a cycle and the branch runs 3 coarse
    rungs rather than the 6 fine ones the plan describes.

    Not dangerous - open(6) >= levels(3) still means it cannot buy, and
    allocated / levels keeps each rung fully backed - but a test asserting
    a value the running system overwrites is worse than no test.
    """
    bot = open("crypto_grid_bot.py").read()
    tree = ast.parse(bot)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "run_grid_branch_cycle")
    cycle = "\n".join(bot.splitlines()[fn.lineno - 1:fn.end_lineno])

    assert "row.num_levels = real_effective_levels" in cycle, (
        "the override no longer writes num_levels - if that is deliberate, "
        "this test and the top-up's own comment both need revisiting")
    assert "get_live_grid_spacing_override()" in cycle

    import crypto_grid_bot as grid
    cfg = grid.GRID_LEVEL_SPACING_CANDIDATES["3_levels_2.5pct"]
    assert cfg["num_levels"] == 3

    # The live shape, recomputed rather than asserted as a pair of literals:
    # for each topped-up branch, the level count the override forces must
    # not EXCEED its open slices. That direction is the one that lets a
    # branch buy, and it is what a future override change would break.
    live = [("ZEC-USD", 2272.62, 6), ("XRP-USD", 2240.54, 9), ("SHIB-USD", 267.37, 6)]
    forced = max(1, min(grid._safe_num_levels_for_allocation(2272.62), cfg["num_levels"]))
    assert forced == 3, forced
    for pid, alloc, open_slices in live:
        f = max(1, min(grid._safe_num_levels_for_allocation(alloc), cfg["num_levels"]))
        assert open_slices >= f, f"{pid}: {open_slices} open vs {f} forced levels"

    # THE GUARANTEE HAS A BOUNDARY, and it is worth knowing where.
    # "Cannot buy" holds only while the top-up leaves at least as many open
    # slices as the override forces. A SMALL top-up does not get it: one new
    # slice on a branch holding one leaves 2 open against 3 forced levels,
    # and that branch is free to buy on its next cycle. Backed - allocated
    # already counts the rung - but not the "starts full" the docstring
    # describes, so it is asserted here rather than left to be discovered.
    small_forced = max(1, min(grid._safe_num_levels_for_allocation(140.0),
                              cfg["num_levels"]))
    assert small_forced > 2, (
        "a 2-slice branch is no longer free to buy under this override - the "
        "boundary this test documents has moved and the docstring should say so")


def test_the_peak_moves_with_the_allocation():
    src = _topup_src()
    assert "row.peak_equity" in src and "+ added_usd" in src


def test_the_override_is_rechecked_against_the_live_row():
    src = _topup_src()
    assert "row.stop_loss_pct_override is None" in src
    assert "no longer an adopted branch" in src


def test_it_never_places_an_order():
    src = _topup_src()
    for forbidden in ("place_order", "market_order", "create_order", "sell(", "buy("):
        assert forbidden not in src


def test_the_loop_runs_adoption_first_then_the_topup():
    fn = next(n for n in ast.walk(ast.parse(_WSRC))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "run_periodically")
    body = "\n".join(_WSRC.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert body.index("check_once(") < body.index("topup_once(")
    # and a failure in one must not take the other down with it
    assert "topup" in body and "pass failed" in body


def test_the_heartbeat_reports_the_topup_separately():
    assert "topped_up" in _w.HEARTBEAT and "last_topup_result" in _w.HEARTBEAT
