"""An invariant that tells a human to go and work out the answer is not
an invariant.

maker_only_holds sat at UNKNOWN every time anyone looked at it, reporting
"34 of 85 recent fills were TAKER" and then, in its own detail text,
instructing the reader to "check the newest taker fill's timestamp against
when it was armed". Neither fact was recorded anywhere, so nobody ever did,
and a taker leg costing 0.75% against a floor priced on 0.35% could have
been firing the whole time without the check ever moving off UNKNOWN.
"""
import ast
import datetime as dt
import pathlib

import invariants as inv

NOW = dt.datetime.now(dt.timezone.utc)
ROUTER = pathlib.Path(__file__).with_name("routers") / "trading_dashboard.py"


def ago(hours):
    return NOW - dt.timedelta(hours=hours)


def test_a_taker_fill_after_arming_is_a_real_failure():
    r = inv.maker_only_holds(34, 85, ago(1), ago(5))
    assert r["status"] == inv.FAIL
    assert "AFTER" in r["detail"]
    assert "0.75%" in r["detail"], "the cost of the bug belongs in the verdict"


def test_the_failure_does_not_blame_the_grid_it_cannot_have_been():
    """place_maker_buy/sell set post_only=True, so Coinbase REJECTS a
    crossing maker order rather than filling it as a taker. The first live
    FAIL said "something is still crossing the spread", which points at the
    one subsystem that provably cannot do it. The feed carries no
    originating subsystem, so the verdict must name the possibilities
    rather than pick one."""
    d = inv.maker_only_holds(34, 85, ago(1), ago(5))["detail"]
    assert "post_only" in d
    assert "MARKET order from another path" in d
    for named in ("resting stop", "trimmer", "close", "manual sale"):
        assert named in d, named
    assert "something is still crossing the spread" not in d


def test_taker_fills_that_all_predate_arming_pass():
    r = inv.maker_only_holds(34, 85, ago(9), ago(5))
    assert r["status"] == inv.OK
    assert "predate" in r["detail"]


def test_a_fill_exactly_at_the_arming_moment_is_not_a_failure():
    """Strictly after, not at. A fill stamped the same second the mode was
    armed cannot be shown to have crossed under it."""
    t = ago(5)
    assert inv.maker_only_holds(34, 85, t, t)["status"] == inv.OK


def test_no_taker_fills_is_a_pass():
    assert inv.maker_only_holds(0, 85, None, ago(5))["status"] == inv.OK


def test_maker_only_off_is_a_pass_not_a_failure():
    r = inv.maker_only_holds(34, 85, ago(1), None, maker_only_active=False)
    assert r["status"] == inv.OK


def test_a_missing_arming_time_is_unknown_and_carries_the_useful_fact():
    """UNKNOWN is kept for exactly this case - but it must now report the
    newest taker fill's age, which is what makes it actionable."""
    r = inv.maker_only_holds(34, 85, ago(1.5), None)
    assert r["status"] == inv.UNKNOWN
    assert r["newest_taker_age_hours"] == 1.5
    assert "1.5h old" in r["detail"]


def test_taker_fills_with_no_timestamp_are_unknown_never_a_pass():
    r = inv.maker_only_holds(34, 85, None, ago(5))
    assert r["status"] == inv.UNKNOWN
    assert "Not read as a pass" in r["detail"]


def test_unparseable_timestamps_are_unknown_not_a_crash():
    assert inv.maker_only_holds(34, 85, "not-a-date", ago(5))["status"] == inv.UNKNOWN


def test_naive_and_epoch_timestamps_are_both_understood():
    """The fills feed gives ISO strings; the arming stamp is stored as
    epoch seconds. A mismatch here would raise, not just misjudge."""
    naive = (NOW - dt.timedelta(hours=1)).replace(tzinfo=None)
    r = inv.maker_only_holds(34, 85, naive, (NOW - dt.timedelta(hours=5)).timestamp())
    assert r["status"] == inv.FAIL


def test_the_endpoint_no_longer_hardcodes_the_verdict():
    """Assert on the parsed tree: this file and the router both quote
    'UNKNOWN' while explaining the bug, so a substring search proves
    nothing. The check must be reached through inv.maker_only_holds.
    """
    tree = ast.parse(ROUTER.read_text())
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "maker_only_holds"
    ]
    assert calls, "the endpoint must call inv.maker_only_holds, not build the dict itself"

    # And no literal dict in the router may still name this check.
    literals = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Dict):
            continue
        for k, v in zip(n.keys, n.values):
            if (isinstance(k, ast.Constant) and k.value == "name"
                    and isinstance(v, ast.Constant) and v.value == "maker_only_holds"):
                literals.append(n.lineno)
    # One is allowed: the except-handler that reports the check itself broke.
    assert len(literals) <= 1, f"hand-built maker_only_holds verdicts at lines {literals}"
