"""The headline promised a sale nothing was going to place.

Live at 2026-09-28T09:24Z, AUTO_TRIM_MODE=arm, /auto-trim returned:

    "headline": "4 holding(s) over the limit. $143.38 will be sold on
                 the next pass."
    "consolidation_is_preview_only": true
    "consolidation_note": "The worker trims the concentration ceiling
                 only. Tail consolidation is sized and shown here but
                 nothing places it."
    worker.last_result: "Nothing is over the limit."

Of the $143.38, $81.76 was three CONSOLIDATE rows (JASMY $29.78,
ETC $28.53, PUMP $23.45) which no code path places, and $61.62 was the
USD row, which is not a product. Nothing at all was going to be sold.

The same response carried both the claim and its refutation: summarise()
totals every row with act=True, while the endpoint separately states
that the consolidate rows are preview-only. Two numbers that must agree,
computed in two places from two rules, with nothing forcing them to
match - and the one a reader sees first was the wrong one.

The fix splits the count by what actually places an order. The worker
calls plan_trims, which emits no CONSOLIDATE rows, so its summary is
unchanged; plan_actions adds them, and they are now named as a preview.
"""
from datetime import datetime, timezone

import auto_trim as at

NOW = datetime(2026, 9, 28, 9, 24, tzinfo=timezone.utc)


def row(asset, kind, usd, act=True):
    r = {"asset": asset, "usd": usd, "share_pct": 1.0, "act": act,
         "trim_usd": usd if act else 0.0, "reason": "TAIL" if kind == "CONSOLIDATE" else "OVER_LIMIT"}
    if act:
        r["kind"] = kind
    return r


# The live shape: one ceiling trim, three preview-only consolidations.
LIVE = [row("ZEC", "TRIM", 61.62),
        row("JASMY", "CONSOLIDATE", 29.78),
        row("ETC", "CONSOLIDATE", 28.53),
        row("PUMP", "CONSOLIDATE", 23.45)]


def test_the_sold_figure_counts_only_what_places_an_order():
    s = at.summarise(LIVE, "arm")
    assert s["would_trim_usd"] == 61.62, (
        f"would_trim_usd is {s['would_trim_usd']} - it is counting consolidations "
        f"that no code path places")
    assert s["would_trim_count"] == 1


def test_the_preview_only_total_is_reported_separately_not_hidden():
    s = at.summarise(LIVE, "arm")
    assert s["would_consolidate_usd"] == 81.76
    assert s["would_consolidate_count"] == 3


def test_the_headline_does_not_claim_a_preview_will_be_sold():
    s = at.summarise(LIVE, "arm")
    assert "$143.38" not in s["headline"], (
        "the headline still states the blended total as a sale")
    assert "$61.62" in s["headline"]


def test_the_headline_names_the_preview_rather_than_dropping_it():
    s = at.summarise(LIVE, "arm")
    h = s["headline"].lower()
    assert "81.76" in s["headline"] and "preview" in h, (
        "the consolidations must still be visible, just not counted as orders")


def test_consolidations_alone_never_read_as_a_pending_sale():
    s = at.summarise([r for r in LIVE if r["asset"] != "ZEC"], "arm")
    assert s["would_trim_usd"] == 0.0
    assert "will be sold" not in s["headline"], s["headline"]
    assert "81.76" in s["headline"]


def test_the_worker_summary_is_unchanged_by_this():
    """plan_trims emits no CONSOLIDATE rows, so its headline must read
    exactly as it did before - the worker is the path that really sells."""
    plans = at.plan_trims([{"asset": "ZEC", "usd": 2864.01, "units": 1.8, "price": 1591.1}],
                          11243.82, now=NOW)
    s = at.summarise(plans, "arm")
    assert s["would_trim_usd"] > 0
    assert "will be sold on the next pass" in s["headline"]
    assert s["would_consolidate_usd"] == 0.0


def test_nothing_acting_still_says_nothing():
    s = at.summarise([row("ZEC", "TRIM", 0.0, act=False)], "arm")
    assert s["would_trim_usd"] == 0.0 and s["would_consolidate_usd"] == 0.0
    assert "Nothing is over the limit." in s["headline"]


def test_observing_still_says_nothing_will_be_placed():
    s = at.summarise(LIVE, "observe")
    assert s["armed"] is False
    assert "nothing will be placed" in s["headline"].lower()
    assert "$61.62" in s["headline"]
