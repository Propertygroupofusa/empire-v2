#!/usr/bin/env python3
"""Two layers each said the other one had the stop. Neither did.

THE GAP, measured live 2026-09-29
---------------------------------
Eight adopted grid branches holding $3,712 had no automatic exit at all, and
every layer involved reported that as normal:

  the grid branch   stop_pct 0.0, reason "Adopted coin is covered at the
                    portfolio level by the resting stops"
  resting stops     refuses it: ACTIVELY_TRADED, "The branch carries its own
                    adaptive stop; this would be a second one the first
                    cannot see"
  and for ZEC       refused one step earlier as TRIMMERS_COIN, deferred to
                    the concentration trimmer, which declined it WITHIN_LIMIT

/resting-stops reported protects_usd 0 the whole time, which is also what a
healthy fleet reports - so the one number that could have shown it could not.

WHAT WAS CHANGED, AND WHAT DELIBERATELY WAS NOT
-----------------------------------------------
NOT the decisions. resting_stops declines an actively-traded asset for three
reasons that a stop elsewhere does not touch: a resting sell holds the units
the grid trades with, it can leave the branch tracking units the wallet no
longer has (that is how ETH came to be short 0.0316 units), and it schedules
a loss on a position the fleet does not sell at a loss. Relaxing that guard
was the obvious-looking fix and it is the wrong one.

What changed is that no layer now claims cover it cannot verify, and the gap
is counted. The first test below is the one that matters: placement must be
byte-for-byte what it was.
"""
import asyncio
import sys

import resting_stops as rs

_failures = []
_passes = 0


def ok(label, condition, detail=""):
    global _passes
    if condition:
        _passes += 1
        print(f"  ok   {label}")
    else:
        _failures.append(f"{label}{(' - ' + detail) if detail else ''}")
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")


def plan(asset="ETH", *, units=1.0, price=2000.0, stop=1800.0,
         share_pct=None, limit_pct=None, traded=(), unstopped=None):
    return rs.plan_stop(asset, units_available=units, price=price,
                        stop_price=stop, share_pct=share_pct,
                        limit_pct=limit_pct, actively_traded=traded,
                        unstopped=unstopped)


# The whole matrix of inputs that reach a decision, so "nothing changed" is a
# measurement over the real branches rather than a claim about two of them.
CASES = [
    ("a clean placement",        dict(asset="ETH", traded=())),
    ("held by the grid",         dict(asset="ETH", traded=("ETH",))),
    ("the trimmer's coin",       dict(asset="ZEC", share_pct=25.0, limit_pct=20.0)),
    ("the trimmer's AND traded", dict(asset="ZEC", share_pct=25.0, limit_pct=20.0,
                                      traded=("ZEC",))),
    ("no units available",       dict(asset="ETH", units=0.0)),
    ("unreadable units",         dict(asset="ETH", units=None)),
    ("unreadable price",         dict(asset="ETH", price=None)),
    ("stop above the market",    dict(asset="ETH", price=1800.0, stop=2000.0)),
    ("a dust position",          dict(asset="ETH", units=0.00001, price=0.01)),
]


def test_the_gap_report_changes_no_placement_decision():
    """THE SAFETY PROPERTY. `unstopped` is reporting only: for every input,
    with the gap map absent, empty, or naming this very asset, the verdict
    and every figure the venue acts on must be identical.

    If this ever fails, the change has started deciding things and the three
    objections in resting_stops.plan_stop are being overruled by a field that
    was added to describe them."""
    acted_on = ("ok", "reason", "units", "stop_price", "limit_price",
                "protects_usd", "worst_case_usd", "side", "order_type")
    for label, kwargs in CASES:
        asset = kwargs.get("asset", "ETH")
        base = plan(**kwargs)
        variants = {
            "unstopped=None (unreadable)": plan(unstopped=None, **kwargs),
            "unstopped={} (nothing uncovered)": plan(unstopped={}, **kwargs),
            "unstopped names this asset": plan(
                unstopped={asset: "branch names its own stop of 0"}, **kwargs),
            "unstopped names another asset": plan(
                unstopped={"SOMETHINGELSE": "x"}, **kwargs),
        }
        for vlabel, got in variants.items():
            same = all(base.get(k) == got.get(k) for k in acted_on)
            ok(f"{label}: {vlabel} decides identically", same,
               f"differs on {[k for k in acted_on if base.get(k) != got.get(k)]}")


def test_a_position_with_no_stop_from_either_layer_is_marked_and_priced():
    p = plan(asset="ETH", units=0.5, price=2000.0, traded=("ETH",),
             unstopped={"ETH": "crypto_grid_4 names its own stop of 0"})
    ok("it is still refused, exactly as before", p.get("ok") is False)
    ok("the refusal reason is unchanged", p.get("reason") == "ACTIVELY_TRADED")
    ok("it is flagged uncovered", p.get("uncovered") is True)
    ok("the position is priced so the gap can be totalled",
       p.get("position_usd") == 1000.0, repr(p.get("position_usd")))
    ok("the detail says no layer covers it",
       "NO STOP FROM EITHER LAYER" in (p.get("uncovered_detail") or ""))
    ok("and names which branch declared it",
       "crypto_grid_4" in (p.get("uncovered_detail") or ""))


def test_the_refusal_no_longer_claims_a_stop_the_branch_does_not_have():
    """The sentence that hid the gap for a week."""
    claim = "carries its own adaptive stop"
    with_stop = plan(asset="ETH", traded=("ETH",), unstopped={})
    ok("a branch that HAS a stop is still described as having one",
       claim in (with_stop.get("detail") or ""))
    no_stop = plan(asset="ETH", traded=("ETH",),
                   unstopped={"ETH": "names its own stop of 0"})
    ok("a branch with NO stop is not described as having one",
       claim not in (no_stop.get("detail") or ""), (no_stop.get("detail") or ""))
    ok("it says so in capitals instead",
       "NO STOP OF ITS OWN" in (no_stop.get("detail") or ""))
    ok("and it does not read as a recommendation to rest one here",
       "not a reason to rest an order here" in (no_stop.get("detail") or ""))
    unknown = plan(asset="ETH", traded=("ETH",), unstopped=None)
    ok("an unreadable stop is not claimed as cover either",
       "could not be read" in (unknown.get("detail") or ""), (unknown.get("detail") or ""))


def test_the_gap_is_caught_on_every_refusal_path_not_just_one():
    """ZEC - the largest of them - is refused as TRIMMERS_COIN and never
    reaches the actively-traded block. A gap only one path can report is a
    gap that hides behind the others."""
    seen = {}
    for label, kwargs in CASES:
        asset = kwargs.get("asset", "ETH")
        p = plan(unstopped={asset: "names its own stop of 0"}, **kwargs)
        if p.get("ok"):
            continue
        seen[p.get("reason")] = p.get("uncovered")
    ok("more than one refusal reason was exercised", len(seen) >= 3, str(seen))
    ok("EVERY refusal on a stopless branch is flagged uncovered",
       all(v is True for v in seen.values()), str(seen))
    z = plan(asset="ZEC", share_pct=25.0, limit_pct=20.0,
             unstopped={"ZEC": "names its own stop of 0"})
    ok("ZEC's trimmer refusal specifically is flagged",
       z.get("reason") == "TRIMMERS_COIN" and z.get("uncovered") is True, str(z))


def test_a_placed_stop_is_never_reported_as_a_gap():
    p = plan(asset="ETH", unstopped={"ETH": "names its own stop of 0"})
    ok("the clean case still places", p.get("ok") is True)
    ok("and is not counted as uncovered - this layer IS its stop",
       p.get("uncovered") is not True, repr(p.get("uncovered")))


def test_an_unreadable_stop_map_is_neither_covered_nor_a_gap():
    """UNKNOWN is a third answer. Counting it as covered restores the bug;
    counting it as a gap cries wolf on a read error."""
    p = plan(asset="ETH", traded=("ETH",), unstopped=None)
    ok("uncovered is None, not True and not absent",
       "uncovered" in p and p["uncovered"] is None, repr(p.get("uncovered")))
    s = rs.summarise([p], "arm")
    ok("it is not in uncovered", s["uncovered"] == [], str(s["uncovered"]))
    ok("it is reported as unknown instead",
       s["stop_coverage_unknown"] == ["ETH"], str(s["stop_coverage_unknown"]))
    ok("and the gap total is None, never 0.00", s["uncovered_usd"] is None,
       repr(s["uncovered_usd"]))


def test_the_summary_makes_the_gap_a_number():
    plans = [
        plan(asset="ETH", units=0.5, price=2000.0, traded=("ETH",),
             unstopped={"ETH": "stop of 0"}),                       # 1000
        plan(asset="ZEC", units=1.0, price=1372.0, share_pct=25.0,
             limit_pct=20.0, unstopped={"ZEC": "stop of 0"}),        # 1372
        plan(asset="LINK", traded=("LINK",), unstopped={}),          # has a stop
        plan(asset="SOL", unstopped={}),                             # placed
    ]
    s = rs.summarise(plans, "arm")
    ok("both uncovered assets are listed", s["uncovered"] == ["ETH", "ZEC"],
       str(s["uncovered"]))
    ok("the count is reported", s["uncovered_count"] == 2)
    ok("the dollars are totalled", s["uncovered_usd"] == 2372.0,
       repr(s["uncovered_usd"]))
    ok("the asset with its own stop is not in the gap",
       "LINK" not in s["uncovered"])
    ok("neither is the one this layer protects", "SOL" not in s["uncovered"])
    ok("the note explains what uncovered means",
       "NO automatic exit from either layer" in s["uncovered_note"])
    ok("and does not read as advice to place them",
       "not a recommendation" in s["uncovered_note"])
    ok("protects_usd still means what it meant", s["would_place"] == 1)


def test_an_unpriced_gap_is_reported_not_silently_zero():
    p = plan(asset="ETH", units=None, traded=("ETH",),
             unstopped={"ETH": "stop of 0"})
    s = rs.summarise([p], "arm")
    ok("the asset is still named as uncovered", s["uncovered"] == ["ETH"])
    ok("the total is None rather than 0.00", s["uncovered_usd"] is None,
       repr(s["uncovered_usd"]))
    ok("and it is named as unpriced", s["uncovered_unpriced"] == ["ETH"],
       str(s["uncovered_unpriced"]))


def test_a_clean_fleet_says_so_rather_than_printing_an_empty_gap():
    s = rs.summarise([plan(asset="SOL", unstopped={})], "arm")
    ok("no gap is reported", s["uncovered"] == [] and s["uncovered_count"] == 0)
    ok("and the note says every declined position has its own stop",
       "has a grid stop of its own" in s["uncovered_note"], s["uncovered_note"])


# ─────────────────────────────────────────────────────────────────────────
# The other half: the grid has to answer the question truthfully.
# ─────────────────────────────────────────────────────────────────────────

def _grid_stops(branches, explode=False):
    """products_without_a_grid_stop() against a fake branch table."""
    import contextlib

    import crypto_grid_bot as grid

    class _Rows:
        def __init__(self, rows): self._rows = rows
        def scalars(self): return self
        def all(self): return self._rows

    class _DB:
        async def execute(self, *a, **k):
            if explode:
                raise RuntimeError("database is gone")
            return _Rows(branches)

    @contextlib.asynccontextmanager
    async def _sess():
        yield _DB()

    old = grid.get_session_factory
    grid.get_session_factory = lambda: _sess
    try:
        return asyncio.run(grid.products_without_a_grid_stop())
    finally:
        grid.get_session_factory = old


class B:
    def __init__(self, product_id, bot_name, override):
        self.product_id = product_id
        self.bot_name = bot_name
        self.stop_loss_pct_override = override


def test_the_grid_reports_exactly_which_branches_have_no_stop():
    got = _grid_stops([
        B("ZEC-USD", "crypto_grid_9", 0.0),       # adopted: no stop
        B("ETH-USD", "crypto_grid_4", 0.0),       # adopted: no stop
        B("NEAR-USD", "crypto_grid_2", None),     # adaptive / fixed: has one
        B("LINK-USD", "crypto_grid_3", 0.08),     # names a real stop
        B("BCH-USD", "crypto_grid_5", "junk"),    # unreadable -> fixed stands
    ])
    ok("a stop of exactly 0 is reported", set(got) == {"ZEC", "ETH"}, str(sorted(got)))
    ok("a branch on the adaptive/fixed stop is NOT reported", "NEAR" not in got)
    ok("a branch naming a real stop is NOT reported", "LINK" not in got)
    ok("an unreadable override is NOT reported - the fixed stop stands",
       "BCH" not in got)
    ok("and each entry says which branch declared it",
       "crypto_grid_9" in got.get("ZEC", ""), str(got.get("ZEC")))


def test_zero_is_not_confused_with_absent():
    """The precedence bug this whole area keeps producing: `if override:` is
    false for 0.0 AND for None, and those mean opposite things."""
    ok("0.0 means NO stop", "ZEC" in _grid_stops([B("ZEC-USD", "b", 0.0)]))
    ok("None means the fleet stop applies",
       "ZEC" not in _grid_stops([B("ZEC-USD", "b", None)]))


def test_an_unreadable_branch_table_reports_unknown_not_covered():
    """Empty must reach the caller as UNKNOWN. plan_stop is passed None on
    the exception path for exactly this reason - an empty dict here would
    read as 'every branch has a stop'."""
    got = _grid_stops([B("ZEC-USD", "b", 0.0)], explode=True)
    ok("it returns empty rather than raising", got == {}, repr(got))
    ok("and the callers convert that to None, never {}",
       _callers_pass_none_on_failure())


def _callers_pass_none_on_failure():
    """Both call sites must leave `unstopped` as None when the read fails.

    Checked on the parsed tree: the value is initialised to None and the
    except handler must not replace it with a dict. An empty dict is the one
    value that would silently restore the original bug.
    """
    import ast
    import pathlib
    good = 0
    for path, in (("resting_stops_worker.py",), ("routers/trading_dashboard.py",)):
        tree = ast.parse(pathlib.Path(path).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not any(n in ("unstopped", "_unstopped") for n in names):
                continue
            # Every assignment is either the None seed or the awaited read.
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                good += 1
            elif isinstance(node.value, ast.Await):
                continue
            else:
                return False
    return good >= 2


def test_every_plan_stop_call_site_passes_the_coverage_map():
    """THE GUARD THAT WAS MISSING, and it cost a live wrong answer.

    `unstopped` defaults to None, which means UNKNOWN. A call site that forgets
    it therefore reports every asset as unknown - and that is indistinguishable
    from a genuine read failure, so nothing looked broken. Live 2026-09-29 the
    dashboard's call was missing the argument (a read-modify-write clobber in
    the edit that added it) and `/resting-stops` reported all 49 assets
    unknown and an empty `uncovered` list, on a fleet where fourteen branches
    had no stop.

    Every test above this one exercises plan_stop directly and so passed with
    the wiring broken. Checked on the parsed tree because the property is
    about the CALL, which no unit test of the function itself can see.
    """
    import ast
    import pathlib
    found = 0
    for name in ("routers/trading_dashboard.py", "resting_stops_worker.py"):
        # .parent / name, not .with_name — with_name rejects a separator, and
        # one of these two lives in a subdirectory.
        path = pathlib.Path(__file__).parent / name
        if not path.exists():
            ok(f"{name} exists to be checked", False)
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "plan_stop"):
                found += 1
                kw = {k.arg for k in node.keywords}
                ok(f"{name}:{node.lineno} passes unstopped", "unstopped" in kw,
                   "without it plan_stop defaults to None = UNKNOWN, and the "
                   "coverage gap silently reports as unreadable instead")
                ok(f"{name}:{node.lineno} passes actively_traded too",
                   "actively_traded" in kw,
                   "without it the grid-held guard does not fire at all")
    ok("both known call sites were found", found >= 2, f"found {found}")


def test_every_asset_unknown_does_not_read_like_one_asset_unknown():
    """The same rule this whole file is about, turned on the field it added.
    All-unknown is the coverage map not arriving; one-unknown is a gap in the
    data. If they render the same, the first hides as the second - which is
    exactly what happened."""
    all_unknown = rs.summarise(
        [plan(asset=a, traded=(a,), unstopped=None) for a in ("ETH", "ZEC", "SOL")],
        "arm")
    ok("all-unknown is flagged", all_unknown.get("stop_coverage_unreadable") is True,
       repr(all_unknown.get("stop_coverage_unreadable")))
    ok("and says what to check",
       "passes `unstopped`" in (all_unknown.get("stop_coverage_unreadable_note") or ""))
    some = rs.summarise(
        [plan(asset="ETH", traded=("ETH",), unstopped=None),
         plan(asset="SOL", unstopped={})], "arm")
    ok("a partial unknown is NOT flagged",
       some.get("stop_coverage_unreadable") is False,
       repr(some.get("stop_coverage_unreadable")))
    ok("and carries no alarm note",
       some.get("stop_coverage_unreadable_note") is None)
    clean = rs.summarise([plan(asset="SOL", unstopped={})], "arm")
    ok("a healthy read is not flagged either",
       clean.get("stop_coverage_unreadable") is False)
    ok("an empty plan list is not flagged",
       rs.summarise([], "arm").get("stop_coverage_unreadable") is False)


def test_zz_nothing_above_failed():
    assert not _failures, f"{len(_failures)} checks failed: {_failures}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        print(f"\n== {t.__name__}")
        try:
            t()
        except BaseException as e:
            # A test that RAISES must not take the rest of the suite with it.
            # Breaking the code under test on purpose made one of these throw
            # a KeyError, which aborted the run and hid twelve other tests -
            # the deliberate regression looked like a single failure.
            ok(f"{t.__name__} ran to completion", False,
               f"raised {type(e).__name__}: {e}")
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} stop-coverage checks passed")
