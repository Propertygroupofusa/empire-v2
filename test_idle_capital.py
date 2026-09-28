"""Flat is not idle, and a rotator that cannot tell them apart churns.

Measured on the live fleet, three branches were flat at once: ONDO (traded
4h ago), TIA (5h ago), BONK (18 DAYS ago). The first two are grids between
fills; the third is capital in the wrong coin. crypto_grid_bot's rotation
keys on branch.created_at and flatness, so it would treat all three alike.
"""
from datetime import datetime, timedelta, timezone

import pytest

import idle_capital as ic

NOW = datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc)


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat().replace("+00:00", "Z")


# Which coin occupies which slot, for the test being run. branch() fills
# it and trade() reads it, so a trade row carries the same bot_name AND
# product_id the live feed does.
_SLOT_COIN = {}


@pytest.fixture(autouse=True)
def _fresh_slots():
    """Slots are recycled in production; they must not leak between tests."""
    _SLOT_COIN.clear()
    yield
    _SLOT_COIN.clear()


def branch(pid, usd=69.0, open_slices=0, bot=None, created=None):
    name = bot or f"grid_{pid}"
    _SLOT_COIN[name] = pid
    return {"product_id": pid, "bot_name": name, "allocated_usd": usd,
            "open_slices": open_slices, "created_at": created}


def trade(bot, when, product=None):
    """One closed trade, shaped like the live feed.

    THIS HELPER USED TO EMIT {bot_name, closed_at} AND NOTHING ELSE, and
    that made 13 tests in this file assert nothing for as long as it took
    somebody to run them.

    last_trade_by_branch keys on (bot_name, product_id) on purpose - slot
    names are recycled, and PRIME-USD inherited a previous occupant's
    record, was called "stale for 26 days" and had its whole $240.38
    moved out seven minutes after it was created. When the key gained the
    coin, this helper was not updated, so every lookup missed and every
    branch came back UNKNOWN_AGE - the report tests failed against
    correct code.

    /grid-status/trade-history returns bot_name AND product_id on every
    row, which test_the_fixture_matches_the_live_producer pins. A fixture
    that is not the producer's shape tests a system nobody runs.
    """
    return {"bot_name": bot, "closed_at": when,
            "product_id": product if product is not None else _SLOT_COIN.get(bot)}


# ---------------------------------------------------------------- the split

def test_a_branch_holding_slices_is_working_however_long_ago_it_traded():
    r = ic.classify(branch("ETH", open_slices=3), None, now=NOW)
    assert r["state"] == ic.WORKING
    assert "in the market" in r["why"]


def test_flat_but_traded_hours_ago_is_waiting_not_idle():
    r = ic.classify(branch("ONDO"), NOW - timedelta(hours=4.6), now=NOW)
    assert r["state"] == ic.WAITING
    assert "between" in r["why"]


def test_flat_and_silent_for_weeks_is_stale():
    r = ic.classify(branch("BONK"), NOW - timedelta(days=18.4), now=NOW)
    assert r["state"] == ic.STALE
    assert r["idle_hours"] == pytest.approx(18.4 * 24, abs=1)


def test_the_boundary_is_the_horizon_window():
    """72h is horizon_study's own 92.5% figure, not a round number."""
    assert ic.classify(branch("X"), NOW - timedelta(hours=71.9), now=NOW)["state"] == ic.WAITING
    assert ic.classify(branch("X"), NOW - timedelta(hours=72.1), now=NOW)["state"] == ic.STALE


def test_the_live_three_are_classified_the_way_the_fleet_showed_them():
    bs = [branch("ONDO", 69.58, bot="g4"), branch("TIA", 69.67, bot="g7"),
          branch("BONK", 69.23, bot="g3")]
    ts = [trade("g4", ago(hours=4.6)), trade("g7", ago(hours=5.8)),
          trade("g3", ago(days=18.4))]
    r = ic.report(bs, ts, now=NOW)
    got = {x["product_id"]: x["state"] for x in r["branches"]}
    assert got == {"ONDO": ic.WAITING, "TIA": ic.WAITING, "BONK": ic.STALE}
    assert r["stale_usd"] == 69.23


def test_a_naive_flat_check_would_have_moved_all_three():
    """The thing this module exists to prevent, stated as a test."""
    bs = [branch("ONDO", bot="g4"), branch("TIA", bot="g7"), branch("BONK", bot="g3")]
    ts = [trade("g4", ago(hours=4)), trade("g7", ago(hours=5)), trade("g3", ago(days=18))]
    flat_only = [b for b in bs if b["open_slices"] == 0]
    r = ic.report(bs, ts, now=NOW)
    assert len(flat_only) == 3
    assert len(r["stale"]) == 1


# ------------------------------------------------- a gap is not a zero

def test_a_branch_missing_from_a_TRUNCATED_window_is_not_called_dead():
    """The trade feed is capped. Absence from it is not proof of silence -
    the same mistake as an unread census drawing a crash out of a gap."""
    bs = [branch("OLD", bot="gX")]
    ts = [trade("other", ago(days=3))]
    r = ic.report(bs, ts, now=NOW, total_trade_count=500)
    assert r["window_truncated"] is True
    row = r["branches"][0]
    assert row["state"] == ic.UNKNOWN_AGE
    assert row["at_least"] is True
    assert "floor, not a measurement" in row["why"]


def test_an_untruncated_window_can_judge_absence_against_creation():
    bs = [branch("OLD", bot="gX", created=ago(days=20))]
    ts = [trade("other", ago(days=3))]
    r = ic.report(bs, ts, now=NOW, total_trade_count=1)
    assert r["window_truncated"] is False
    assert r["branches"][0]["state"] == ic.STALE


def test_a_brand_new_branch_is_never_stale():
    r = ic.classify(branch("NEW", created=ago(hours=2)), None, now=NOW)
    assert r["state"] == ic.TOO_NEW


def test_a_new_branch_inside_grace_is_not_judged_even_with_no_creation_window():
    r = ic.classify(branch("NEW", created=ago(hours=1)), None, now=NOW, window_truncated=True)
    assert r["state"] == ic.TOO_NEW


# ------------------------------------------------------------- the report

def test_money_is_totalled_per_state():
    bs = [branch("A", 100.0, open_slices=3, bot="a"), branch("B", 50.0, bot="b"),
          branch("C", 25.0, bot="c")]
    ts = [trade("b", ago(hours=1)), trade("c", ago(days=9))]
    r = ic.report(bs, ts, now=NOW)
    assert r["by_state"][ic.WORKING] == {"count": 1, "usd": 100.0}
    assert r["by_state"][ic.WAITING] == {"count": 1, "usd": 50.0}
    assert r["by_state"][ic.STALE] == {"count": 1, "usd": 25.0}


def test_stale_branches_sort_first():
    bs = [branch("A", open_slices=3, bot="a"), branch("B", bot="b")]
    ts = [trade("b", ago(days=10))]
    assert ic.report(bs, ts, now=NOW)["branches"][0]["product_id"] == "B"


def test_nothing_stale_says_so_plainly():
    bs = [branch("A", bot="a")]
    r = ic.report(bs, [trade("a", ago(hours=2))], now=NOW)
    assert r["stale"] == [] and "No branch is past" in r["detail"]


def test_it_rotates_nothing():
    r = ic.report([branch("A", bot="a")], [], now=NOW)
    assert r["rotates_nothing"] is True


def test_it_cannot_reach_the_network_or_the_database():
    import ast
    tree = ast.parse(open("idle_capital.py").read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert not (mods & {"aiohttp", "requests", "sqlalchemy", "models", "httpx"})


def test_it_never_calls_a_rotation_helper():
    """Asserted on CALLS in the parsed tree, not on the source text.

    The docstring of this module explains what rotation is and why it is
    not done here, so any substring search for "rotate" matches the
    explanation and reads the warning as the offence - the same trap
    test_status_strip already documents for its own brace matcher.
    """
    import ast
    tree = ast.parse(open("idle_capital.py").read())
    called = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                called.add(f.id)
            elif isinstance(f, ast.Attribute):
                called.add(f.attr)
    forbidden = {"withdraw_from_grid_branch", "move_cash_between_grid_branches",
                 "place_order", "place_market_sell", "run_grid_auto_rotate_sweep",
                 "_maybe_rotate_one_grid_branch", "add_cash_to_grid_branch"}
    assert not (called & forbidden), sorted(called & forbidden)


def test_bad_timestamps_do_not_crash_it():
    bs = [branch("A", bot="a")]
    ts = [{"bot_name": "a", "closed_at": "not-a-date"}, {"bot_name": None}, "junk"]
    r = ic.report(bs, ts, now=NOW)
    assert r["branches"][0]["state"] in (ic.UNKNOWN_AGE, ic.STALE, ic.TOO_NEW)


def test_a_missing_open_slices_key_is_treated_as_flat_not_as_working():
    """Absent must not read as 'holding something' - that would hide a
    stale branch behind a missing field."""
    r = ic.classify({"bot_name": "a", "allocated_usd": 10.0},
                    NOW - timedelta(days=9), now=NOW)
    assert r["state"] == ic.STALE


def test_the_window_is_owner_settable():
    assert ic.classify(branch("X"), NOW - timedelta(hours=10), now=NOW,
                       stale_after_hours=6)["state"] == ic.STALE


# ---------------------------------------------------------------------------
# WHERE STALE CASH GOES.
#
# "Keep it flipping" has one honest destination: a branch that is
# demonstrably flipping - the one that most recently CLOSED a round trip on
# this account with this config, not a backtest or a ranking.
# ---------------------------------------------------------------------------

def _rep(branches, trades, now=NOW, total=None):
    return ic.report(branches, trades, now=now, total_trade_count=total)


def _fleet(*extra):
    """A realistically sized fleet.

    The 20% share rule is measured against TOTAL allocation, so in a
    three-branch fleet every branch already exceeds it and nothing can ever
    receive. That is the rule working, not a bug - concentrating half the
    account into one coin to avoid $69 sitting idle is the worse trade - but
    it means a destination test has to be run against a fleet the cap does
    not already saturate. The live fleet is 21 branches / $7,422.
    """
    base = [branch(f"F{i}", 400.0, open_slices=3, bot=f"f{i}") for i in range(16)]
    base_trades = [trade(f"f{i}", ago(hours=20 + i)) for i in range(16)]
    return list(extra) + base, base_trades


def test_it_moves_the_stale_branch_into_the_one_trading_most_recently():
    """Destinations are WAITING, so XLM and ETH are flat here.

    They used to carry open_slices=3, from before rotation_plan was
    restricted to WAITING candidates - see the note on that restriction
    and the $240.38 it was written for.
    """
    bs, bt = _fleet(branch("BONK", 69.23, bot="g3"),
                    branch("XLM", 400.0, bot="g10"),
                    branch("ETH", 400.0, bot="g11"))
    ts = bt + [trade("g3", ago(days=18)), trade("g10", ago(minutes=5)),
               trade("g11", ago(hours=9))]
    p = ic.rotation_plan(_rep(bs, ts))
    assert p["ok"] and len(p["moves"]) == 1
    m = p["moves"][0]
    assert m["from_product_id"] == "BONK" and m["to_product_id"] == "XLM"
    assert m["usd"] == 69.23


def test_it_never_sends_idle_money_to_another_idle_branch():
    """Churn that pays a fee to move silence somewhere else."""
    bs, bt = _fleet(branch("BONK", 69.23, bot="g3"), branch("DEAD", 80.0, bot="g9"),
                    branch("XLM", 400.0, bot="g10"))
    ts = bt + [trade("g3", ago(days=18)), trade("g9", ago(days=12)),
               trade("g10", ago(hours=1))]
    p = ic.rotation_plan(_rep(bs, ts))
    assert {m["to_product_id"] for m in p["moves"]} == {"XLM"}


def test_a_waiting_branch_is_a_valid_destination_but_never_a_source():
    """It traded inside the window - it is proving the thing BONK has not.

    WAITING is the ONLY destination state. Its reason literally reads
    "it closed a trade Nh ago", which is the evidence the worker's
    contract asks for; WORKING means "holding open slices", a statement
    about the present that says nothing about ever completing a trip.
    """
    # Padding is TOO_NEW - it dilutes the share denominator without being
    # either a source or a candidate, so ONDO is the only destination.
    pad = [branch(f"S{i}", 120.0, bot=f"s{i}", created=ago(hours=1)) for i in range(8)]
    bs = [branch("BONK", 69.23, bot="g3"), branch("ONDO", 69.58, bot="g4")] + pad
    ts = [trade("g3", ago(days=18)), trade("g4", ago(hours=4))]
    p = ic.rotation_plan(_rep(bs, ts))
    assert {r["state"] for r in _rep(bs, ts)["branches"] if r["product_id"].startswith("S")} == {ic.TOO_NEW}
    assert [m["from_product_id"] for m in p["moves"]] == ["BONK"]
    assert p["moves"][0]["to_product_id"] == "ONDO"


def test_a_branch_holding_slices_is_not_a_destination_at_all():
    """REVERSED 2026-09-28, and the reversal is the point.

    This asserted that a WORKING branch outranks a WAITING one -
    "holding open rungs is stronger evidence of flipping than a past
    fill" - which is what rotation_plan used to do and what it was
    changed to stop doing.

    Holding slices is a statement about the present. It does not say the
    coin has ever completed a round trip, and every WORKING branch
    reports idle_hours 0.0, so ranking the two together put all of them
    ahead of every branch that had actually proved itself and the
    allocation tiebreak then picked the LARGEST. Live that sent
    PRIME-USD's $240.38 into XLM-USD, a branch with zero completed round
    trips in the fleet's entire history, taking it to $691.51 and third
    largest in the fleet.

    So the F-branches here hold slices and are NOT candidates; ONDO,
    flat and traded four hours ago, is.
    """
    bs, bt = _fleet(branch("BONK", 69.23, bot="g3"), branch("ONDO", 69.58, bot="g4"))
    ts = bt + [trade("g3", ago(days=18)), trade("g4", ago(hours=4))]
    rep = _rep(bs, ts)
    working = {r["product_id"] for r in rep["branches"] if r["state"] == ic.WORKING}
    assert working and all(p.startswith("F") for p in working)
    p = ic.rotation_plan(rep)
    assert p["moves"][0]["to_product_id"] == "ONDO", (
        "a slice-holding branch was chosen over one that proved a round trip")


def test_no_branch_is_allowed_to_swallow_the_fleet():
    """The owner's 20% rule, measured on allocation - which is what moves."""
    bs = [branch("BONK", 300.0, bot="g3"), branch("BIG", 600.0, open_slices=3, bot="g10")]
    ts = [trade("g3", ago(days=18)), trade("g10", ago(hours=1))]
    p = ic.rotation_plan(_rep(bs, ts))
    assert p["moves"] == []
    assert p["refusals"][0]["reason"] == "NO_DESTINATION_UNDER_THE_SHARE_RULE"


def test_two_moves_in_one_pass_cannot_both_overfill_the_same_branch():
    bs = [branch("A", 60.0, bot="a"), branch("B", 60.0, bot="b"),
          branch("C", 400.0, open_slices=3, bot="c"), branch("D", 400.0, open_slices=3, bot="d"),
          branch("E", 400.0, open_slices=3, bot="e")]
    ts = [trade("a", ago(days=9)), trade("b", ago(days=9)),
          trade("c", ago(hours=1)), trade("d", ago(hours=2)), trade("e", ago(hours=3))]
    p = ic.rotation_plan(_rep(bs, ts), max_dest_share_pct=40)
    dests = [m["to_product_id"] for m in p["moves"]]
    assert len(dests) == len(set(dests)) or all(
        m["dest_share_pct_after"] <= 40 for m in p["moves"])


def test_dust_below_the_floor_is_not_worth_a_fee():
    bs = [branch("TINY", 9.0, bot="t"), branch("XLM", 400.0, open_slices=3, bot="g10")]
    ts = [trade("t", ago(days=30)), trade("g10", ago(hours=1))]
    p = ic.rotation_plan(_rep(bs, ts))
    assert p["moves"] == [] and p["refusals"][0]["reason"] == "BELOW_MIN_MOVE"


def test_it_never_retires_cash_to_unallocated_usd():
    """crypto_grid_bot's own rotation does this when nothing clears the ROI
    floor, turning idle money into MORE idle money - the opposite of the ask."""
    bs = [branch("BONK", 69.23, bot="g3")]
    p = ic.rotation_plan(_rep(bs, [trade("g3", ago(days=18))]))
    assert p["retires_nothing_to_cash"] is True
    assert p["moves"] == []
    assert p["refusals"][0]["reason"] == "NO_DESTINATION_UNDER_THE_SHARE_RULE"


def test_nothing_stale_plans_nothing():
    bs = [branch("A", 400.0, open_slices=3, bot="a")]
    p = ic.rotation_plan(_rep(bs, [trade("a", ago(hours=1))]))
    assert p["ok"] is False and p["moves"] == []


def test_the_plan_changes_nothing_by_itself():
    bs = [branch("BONK", 69.23, bot="g3"), branch("XLM", 400.0, open_slices=3, bot="g10")]
    p = ic.rotation_plan(_rep(bs, [trade("g3", ago(days=18)), trade("g10", ago(hours=1))]))
    assert p["is_a_plan_not_a_change"] is True


def test_the_live_case_plans_the_move_the_fleet_actually_needs():
    """BONK $69.23 idle 18 days, into a branch that closed a trip today."""
    bs, bt = _fleet(branch("BONK", 69.23, bot="g3"), branch("ONDO", 69.58, bot="g4"),
                    branch("TIA", 69.67, bot="g7"),
                    branch("XLM", 400.0, open_slices=3, bot="g10"))
    ts = bt + [trade("g3", ago(days=18.4)), trade("g4", ago(hours=4.6)),
               trade("g7", ago(hours=5.8)), trade("g10", ago(minutes=2))]
    p = ic.rotation_plan(_rep(bs, ts))
    assert len(p["moves"]) == 1
    assert p["moves"][0]["from_product_id"] == "BONK"
    assert p["total_usd"] == 69.23


def test_a_fleet_too_small_for_the_share_rule_refuses_rather_than_concentrating():
    """Deliberate, and stated so it is not read as the rule failing.

    With three branches every one already exceeds 20%, so no destination
    qualifies and the cash stays put. Concentrating half the account into a
    single coin to stop $69 sitting idle is the worse trade, and the owner's
    20% rule is the one that wins.
    """
    bs = [branch("BONK", 69.23, bot="g3"), branch("XLM", 400.0, open_slices=3, bot="g10")]
    p = ic.rotation_plan(_rep(bs, [trade("g3", ago(days=18)), trade("g10", ago(hours=1))]))
    assert p["moves"] == []
    assert p["refusals"][0]["reason"] == "NO_DESTINATION_UNDER_THE_SHARE_RULE"



# ------------------------------------- the fixture must be the real shape

def test_the_fixture_matches_the_live_producer():
    """The keys idle_capital reads off a trade row are the keys
    get_grid_trade_history actually emits.

    Read off the producer rather than remembered: this file spent hours
    red because its own trade rows were missing product_id, which the
    live feed has always carried.
    """
    import ast
    import inspect

    from models import CryptoGridTradeHistory

    # get_grid_trade_history builds recent_trades as
    # [row.to_dict() for row in ...], so the MODEL is the producer - the
    # function itself has no literal dict to read, which a first draft of
    # this test learned the expensive way by asserting against the
    # aggregate rows beside it.
    src = inspect.getsource(CryptoGridTradeHistory.to_dict)
    tree = ast.parse(src.lstrip())
    emitted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    emitted.add(k.value)
    for key in ("bot_name", "product_id", "closed_at"):
        assert key in emitted, (
            f"CryptoGridTradeHistory.to_dict no longer emits {key!r} - this "
            f"file's trade() fixture is now lying about the producer's shape")

    row = trade("g1", "2026-09-28T00:00:00Z", product="ZEC")
    assert set(row) <= emitted, f"fixture emits keys the producer does not: {set(row) - emitted}"


def test_a_trade_row_carries_the_coin_not_just_the_slot():
    b = branch("ONDO", bot="g4")
    t = trade("g4", ago(hours=1))
    assert t["product_id"] == b["product_id"] == "ONDO"
    assert ic.last_trade_by_branch([t]) != {}
    assert ("g4", "ONDO") in ic.last_trade_by_branch([t])


def test_a_recycled_slot_does_not_inherit_the_previous_coins_record():
    """The bug the (bot_name, product_id) key exists for. PRIME-USD took
    over slot crypto_grid_3, was called stale for 26 days, and had its
    whole $240.38 moved out seven minutes after it was created.

    Seven minutes old is inside the 6h grace, so TOO_NEW alone saves it
    and the KEY is not what is under test - a first version of this
    asserted only that case and passed with the coin stripped out of the
    key. The second branch here is well past grace, where nothing but
    the key stands between it and the previous occupant's record.
    """
    old_trade = trade("crypto_grid_3", ago(days=26), product="BONK")

    fresh = branch("PRIME", bot="crypto_grid_3", created=ago(minutes=7))
    r = ic.report([fresh], [old_trade], now=NOW)
    assert r["branches"][0]["state"] == ic.TOO_NEW
    assert r["stale"] == []

    # Past the grace period, where the AGE CLAMP takes over: idle hours
    # are capped at how long the branch has existed, so a 30h-old branch
    # cannot be 26 days idle however the key resolves.
    settled = branch("PRIME", bot="crypto_grid_3", created=ago(hours=30))
    r = ic.report([settled], [old_trade], now=NOW, total_trade_count=500)
    assert r["branches"][0]["state"] != ic.STALE, (
        "it inherited the previous occupant's 26-day-old trade")
    assert r["stale_usd"] == 0.0

    # HONESTLY: neither case above isolates the key. Stripping the coin
    # out of last_trade_by_branch leaves both of these passing, because
    # grace and the age clamp fire first, and past the clamp a branch
    # with no trades of its own is STALE by the never-traded path anyway.
    # The key is defence in depth behind two protections that reach the
    # live incident first; what it uniquely guarantees is the index
    # contract, which test_a_trade_row_carries_the_coin_not_just_the_slot
    # is the test for. Recorded rather than dressed up as coverage this
    # file does not have.
