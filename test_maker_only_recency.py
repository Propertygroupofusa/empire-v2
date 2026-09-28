"""The taker alarm could not tell "still happening" from "stopped hours ago".

maker_only_holds has read FAIL all day with the same sentence:

    "35 of 116 recent fills were TAKER, and the newest was filled 3.2h
     AFTER maker-only was armed."

3.2h is newest_taker minus armed_at. Both are fixed points, so that
number never changes. It said 3.2h at 09:22Z, at 11:00Z and at 11:31Z.

Meanwhile newest_taker_at itself never moved off 2026-09-28T07:49:41,
and the maker count in the same window climbed from 72 to 81. Nothing
has crossed the spread for hours - but the only figure the check offered
was one that cannot show that, so the alarm looked identical whether the
leak was live or long finished.

An alarm that cannot distinguish those two is one people stop reading.

The verdict stays FAIL either way: a taker leg billed after arming is a
real breach and stays in the window until it ages out. What changes is
that the detail carries how long ago it was, so a reader knows whether
to go hunting now or note it and move on.

ALSO REMOVED: the prose naming "a resting stop firing, the trimmer, a
close, or a manual sale" as the likely source. Orders go to Coinbase
with a bare uuid4 as their client_order_id - crypto_coinbase_bot line
1174 - so no fill can be attributed to any path. Listing suspects reads
as a finding when it is a guess; the honest statement is that
attribution is impossible until orders are tagged.
"""
from datetime import datetime, timedelta, timezone

import invariants as inv

NOW = datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


ARMED = NOW - timedelta(hours=7)


def test_a_taker_fill_minutes_old_reads_as_still_happening():
    r = inv.maker_only_holds(35, 116, iso(NOW - timedelta(minutes=4)), iso(ARMED))
    assert r["status"] == inv.FAIL
    assert r["newest_taker_age_hours"] < 0.2
    assert "still" in r["detail"].lower() or "minutes" in r["detail"].lower()


def test_a_taker_fill_hours_old_says_how_long_ago_it_stopped():
    """The live case: nothing has crossed since 07:49."""
    r = inv.maker_only_holds(35, 116, iso(NOW - timedelta(hours=3.7)), iso(ARMED))
    assert r["status"] == inv.FAIL, "a breach that happened is still a breach"
    assert 3.6 < r["newest_taker_age_hours"] < 3.8
    assert "3.7h ago" in r["detail"] or "3.7h" in r["detail"]
    assert "stopped" in r["detail"].lower()


def test_the_age_is_reported_as_well_as_the_offset_from_arming():
    """Both numbers, because they answer different questions: one says a
    breach happened, the other says whether it is still going."""
    r = inv.maker_only_holds(35, 116, iso(NOW - timedelta(hours=3.7)), iso(ARMED))
    assert "newest_taker_age_hours" in r
    assert "hours_after_arming" in r
    assert r["hours_after_arming"] > 3.0


def test_the_offset_from_arming_alone_cannot_show_recency():
    """The defect, stated as a test: two fills the same distance after
    arming, one recent and one old, must not read identically."""
    old_armed = NOW - timedelta(hours=20)
    recent = inv.maker_only_holds(5, 100, iso(NOW - timedelta(minutes=2)),
                                  iso(NOW - timedelta(hours=3.2, minutes=2)))
    stale = inv.maker_only_holds(5, 100, iso(NOW - timedelta(hours=16.8)),
                                 iso(old_armed))
    assert abs(recent["hours_after_arming"] - stale["hours_after_arming"]) < 0.2
    assert recent["detail"] != stale["detail"], (
        "same offset from arming, 16 hours apart in recency, identical text")


def test_it_no_longer_calls_the_list_a_likely_source():
    """Retargeted while writing it. The first draft of this test deleted
    the suspect list outright, and test_the_failure_does_not_blame_the_
    grid_it_cannot_have_been caught that - the list is deliberate, and so
    is the post_only sentence that exonerates the grid.

    The real defect was that the list said "likely source" and named four
    paths when six exist. An incomplete enumeration presented as the
    possibilities is worse than none: a reader checks the four, finds
    nothing, and files the alarm under noise."""
    r = inv.maker_only_holds(35, 116, iso(NOW - timedelta(hours=3.7)), iso(ARMED))
    d = r["detail"]
    assert "likely source" not in d.lower()
    assert "post_only" in d, "the grid must still be exonerated"
    assert "client_order_id" in d, "it must say WHY the path cannot be named"


def test_every_module_that_can_place_a_market_order_is_listed():
    """The list cannot silently go stale again.

    It named a resting stop, the trimmer, a close and a manual sale. Two
    whole bots that place market orders on the same wallet -
    crypto_family_tree_bot and crypto_mean_reversion_bot - were never in
    it, and nothing connected the prose to the code that does the placing.
    """
    import os
    import re
    import subprocess
    root = os.path.dirname(os.path.abspath(__file__))
    out = subprocess.run(
        ["grep", "-rl", "-e", r"await engine\.place_market_",
         "-e", r"await _place_market_sell", "--include=*.py", "."],
        cwd=root, capture_output=True, text=True).stdout
    callers = {os.path.basename(p).replace(".py", "")
               for p in out.split() if p and not os.path.basename(p).startswith("test_")}
    assert callers, "the grep found nothing - it has stopped testing anything"
    listed = " ".join(inv.MARKET_ORDER_PATHS)
    missing = sorted(c for c in callers if c not in listed)
    assert not missing, (
        f"these place market orders and are not in MARKET_ORDER_PATHS: {missing}")


def test_it_still_says_what_a_taker_leg_costs():
    r = inv.maker_only_holds(35, 116, iso(NOW - timedelta(hours=3.7)), iso(ARMED))
    assert "0.75" in r["detail"] and "0.35" in r["detail"]


# ── everything the check already guaranteed must still hold ──────────────

def test_no_taker_fills_is_still_a_pass():
    assert inv.maker_only_holds(0, 116, None, iso(ARMED))["status"] == inv.OK


def test_maker_only_off_is_still_a_pass():
    r = inv.maker_only_holds(35, 116, iso(NOW), iso(ARMED), maker_only_active=False)
    assert r["status"] == inv.OK


def test_takers_that_all_predate_arming_are_still_a_pass():
    r = inv.maker_only_holds(35, 116, iso(NOW - timedelta(hours=9)), iso(ARMED))
    assert r["status"] == inv.OK
    assert "predates" in r["detail"] or "predate" in r["detail"]


def test_no_timestamp_is_still_unknown_never_a_pass():
    r = inv.maker_only_holds(35, 116, None, iso(ARMED))
    assert r["status"] == inv.UNKNOWN


def test_no_arming_time_is_still_unknown_and_still_gives_the_age():
    r = inv.maker_only_holds(35, 116, iso(NOW - timedelta(hours=2)), None)
    assert r["status"] == inv.UNKNOWN
    assert r["newest_taker_age_hours"] == 2.0
