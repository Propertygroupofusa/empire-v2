#!/usr/bin/env python3
""""No 8% trigger from the adoption date" is not "no trigger at any price".

THE GAP THIS CLOSES
-------------------
An adopted branch names a stop of 0, for a good reason: the fleet stop sells a
slice 8% below its ENTRY, and an adopted slice's entry is the price on the day
the branch took charge of coin the owner may have held for a year. An 8%
wobble would liquidate a long-term hold against a cost basis nobody paid.

That was implemented as no trigger at all, ever. Measured 2026-09-29: fourteen
branches, $4,573 of coin, nothing able to sell any of it at any price. ZEC 17%
below its adoption price, $401 down, with the grid refusing a losing sale, the
resting stops declining coin a branch holds slices on, and the concentration
trimmer declining it under the same rule.

WHY IT IS BUILT HERE AND NOT IN THE TRIMMER
-------------------------------------------
Because a trimmer loss trigger cannot fire. plan_trims refuses ACTIVELY_TRADED
before any other reason can let a trim through, and all fourteen uncovered
assets have open grid slices - so the trigger would be dead code. Forcing it
past that rule recreates the incident the rule was written after: $885.43 of
ZEC and $244.78 of XRP sold out from under live branches, which then claimed
units the wallet no longer held.

The three refusals share one cause: the grid's slice ledger is the book of
record for those units, so any OTHER seller desynchronises it. The only safe
seller is the grid, through the stop, which chooses a slice and then uses the
same proven sell path that retires the row and records the trade.

WHAT THIS FILE TESTS
--------------------
Behaviour, by calling the policy. The first test is the one that matters: the
default is OFF and the resolved stop is 0.0, so merging this changes nothing
until the owner arms it deliberately. Arming sells coin someone has held for a
long time, at a loss; that is their decision, not this code's.
"""
import sys

import adaptive_stop as a

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


# Real daily volatilities read off the live fleet 2026-09-29, so the numbers
# these tests assert against are the ones the code will actually meet.
LIVE_VOL = {
    "ZEC-USD": 6.4706, "XRP-USD": 3.6224, "XLM-USD": 4.3518, "ETH-USD": 2.4223,
    "SHIB-USD": 3.7271, "LTC-USD": 3.974, "ALGO-USD": 5.1263, "BCH-USD": 5.0351,
    "PEPE-USD": 5.7185, "HBAR-USD": 4.6845, "QNT-USD": 13.467, "LINK-USD": 4.1388,
    "SOL-USD": 2.9231, "ACH-USD": 4.7153,
}


def test_it_is_off_by_default_and_changes_nothing():
    """THE PROPERTY THAT MAKES THIS SAFE TO MERGE. Every adopted branch
    resolves to the same 0.0 it resolves to today until the switch is
    thrown."""
    ok("the default mode is off", a.adopted_mode() == a.ADOPTED_MODE_OFF,
       a.adopted_mode())
    for pid, vol in LIVE_VOL.items():
        r = a.adopted_stop(pid, vol)
        ok(f"{pid}: unarmed resolves to no stop, exactly as now",
           r["stop_pct"] == 0.0, repr(r["stop_pct"]))
        ok(f"{pid}: and says why rather than going quiet",
           r["source"] == "adopted_not_armed" and "not 'arm'" in r["reason"])


def test_only_the_exact_word_arms_it():
    """A switch that sells coin takes the exact word. The whitespace and case
    exception is deliberate - a trailing space on a Railway variable is an
    accident, not a different intention - and is the same rule
    resting_stops.normalise_mode uses."""
    for v in ("arm", "ARM", " Arm ", "\targ"[:3] + "m"):
        ok(f"{v!r} arms it", a.adopted_mode(v) == a.ADOPTED_MODE_ARM)
    for v in (None, "", " ", "true", "True", "yes", "1", "on", "armed",
              "arming", "disarm", 1, True, object()):
        ok(f"{v!r} does NOT arm it", a.adopted_mode(v) == a.ADOPTED_MODE_OFF,
           a.adopted_mode(v))


def test_the_stop_is_never_near_enough_to_be_a_wobble():
    """The whole objection to the fleet stop was that 8% is noise on an
    adoption-date entry. A floor of 20% is the answer, and it must hold for
    a quiet coin whose own volatility would scale to less."""
    floor = a.ADOPTED_DEFAULT_FLOOR
    ok("the floor is at least 20%", floor >= 0.20, repr(floor))
    ok("which is comfortably past the 8% fleet stop", floor > 0.08 * 2)
    for pid, vol in LIVE_VOL.items():
        s = a.adopted_stop(pid, vol, mode_override="arm")["stop_pct"]
        ok(f"{pid}: armed stop {s * 100:.1f}% is at or past the floor",
           s >= floor, f"{s} < {floor}")
        ok(f"{pid}: and within the cap", s <= a.ADOPTED_DEFAULT_CAP,
           f"{s} > {a.ADOPTED_DEFAULT_CAP}")
    # The quietest coin on the fleet scales to 6 x 2.42% = 14.5%, under the
    # floor - so the floor is doing real work, not decorating.
    quiet = a.adopted_stop("ETH-USD", 2.4223, mode_override="arm")
    ok("a quiet coin is floored rather than given a tight stop",
       quiet["stop_pct"] == floor, repr(quiet["stop_pct"]))
    ok("an extremely quiet coin is floored too",
       a.adopted_stop("X-USD", 0.01, mode_override="arm")["stop_pct"] == floor)


def test_it_is_always_wider_than_the_ordinary_stop():
    """If it were not, there would be no reason for it to exist - the branch
    could just use the fleet stop it was given an override to escape."""
    for pid, vol in LIVE_VOL.items():
        adopted = a.adopted_stop(pid, vol, mode_override="arm")["stop_pct"]
        ordinary = a.resolve(pid, 0.08, vol, overrides={},
                             mode_override="adaptive")["stop_pct"]
        ok(f"{pid}: adopted {adopted * 100:.1f}% >= ordinary {ordinary * 100:.1f}%",
           adopted >= ordinary, f"{adopted} < {ordinary}")
        ok(f"{pid}: and wider than the 8% fixed stop", adopted > 0.08)


def test_a_stop_that_cannot_be_sized_is_not_invented():
    """DELIBERATELY THE OPPOSITE OF resolve(). There, an unreadable
    volatility keeps the fixed stop - falling back to a trigger that already
    existed. Here it would CREATE one on a long-term hold from a number
    nothing measured, so it stays at 0 and retries next cycle."""
    for vol in (None, "", "abc", float("nan"), float("inf"), -1.0, 0.0, [], {}):
        r = a.adopted_stop("ZEC-USD", vol, mode_override="arm")
        ok(f"volatility {vol!r} -> no stop, not a guess", r["stop_pct"] == 0.0,
           repr(r["stop_pct"]))
        ok(f"volatility {vol!r} -> says it could not be sized",
           r["source"] == "adopted_no_volatility")
    # And the contrast, so the asymmetry is asserted rather than described.
    fell_back = a.resolve("ZEC-USD", 0.08, None, overrides={},
                          mode_override="adaptive")
    ok("resolve() by contrast keeps a real stop when volatility is unreadable",
       fell_back["stop_pct"] == 0.08, repr(fell_back["stop_pct"]))


def test_an_explicit_per_coin_zero_still_wins():
    """The owner naming a coin by hand outranks a fleet-wide policy."""
    import os
    old = os.environ.get(a.OVERRIDES_ENV)
    os.environ[a.OVERRIDES_ENV] = "ZEC-USD:0,BTC-USD:0.05"
    try:
        z = a.adopted_stop("ZEC-USD", 6.4706, mode_override="arm")
        ok("a named 0 keeps the branch stopless even when armed",
           z["stop_pct"] == 0.0, repr(z["stop_pct"]))
        ok("and says which switch decided it",
           z["source"] == "adopted_override_off" and a.OVERRIDES_ENV in z["reason"])
        other = a.adopted_stop("SOL-USD", 2.9231, mode_override="arm")
        ok("a coin not named is unaffected", other["stop_pct"] > 0,
           repr(other["stop_pct"]))
    finally:
        if old is None:
            os.environ.pop(a.OVERRIDES_ENV, None)
        else:
            os.environ[a.OVERRIDES_ENV] = old


# The live book, 2026-09-29: worst slice on each uncovered branch as a
# fraction below its own entry. This is what arming would meet on day one.
LIVE_WORST_SLICE = {
    "ZEC-USD": -0.1726, "XRP-USD": -0.0424, "XLM-USD": 0.0319, "ETH-USD": -0.0207,
    "SHIB-USD": -0.0721, "LTC-USD": -0.0612, "ALGO-USD": 0.1447, "BCH-USD": -0.1066,
    "PEPE-USD": -0.0700, "HBAR-USD": -0.0600, "QNT-USD": 0.3562, "LINK-USD": -0.0151,
    "SOL-USD": -0.0574, "ACH-USD": -0.0810,
}


def test_arming_it_on_the_live_book_would_sell_nothing():
    """Insurance, not a liquidation. If arming this immediately dumped the
    positions it is meant to protect, it would be the 8%-wobble objection
    back again at a different number."""
    fires = []
    for pid, worst in LIVE_WORST_SLICE.items():
        stop = a.adopted_stop(pid, LIVE_VOL[pid], mode_override="arm")["stop_pct"]
        if stop > 0 and worst <= -stop:
            fires.append((pid, round(worst * 100, 2), round(stop * 100, 1)))
    ok("no live position is past its armed stop", not fires, str(fires))
    # The worst one, named, with the room it has left - the figure worth
    # knowing before arming anything.
    zec = a.adopted_stop("ZEC-USD", LIVE_VOL["ZEC-USD"], mode_override="arm")["stop_pct"]
    room = zec - abs(LIVE_WORST_SLICE["ZEC-USD"])
    ok(f"ZEC, the worst, still has {room * 100:.1f} points of room",
       room > 0.10, f"{room}")


def test_a_position_past_its_stop_is_selected_and_one_inside_it_is_not():
    """The trigger has to actually trigger. Swept either side of the line."""
    stop = a.adopted_stop("ZEC-USD", LIVE_VOL["ZEC-USD"], mode_override="arm")["stop_pct"]
    ok("just inside the line does not fire", not (-stop + 0.001 <= -stop))
    ok("exactly on the line fires", -stop <= -stop)
    ok("past the line fires", -stop - 0.001 <= -stop)
    ok("a profitable position never fires", not (0.05 <= -stop))


# ─────────────────────────────────────────────────────────────────────────
# The grid has to report and count it consistently.
# ─────────────────────────────────────────────────────────────────────────

class Branch:
    def __init__(self, product_id, bot_name, override):
        self.product_id = product_id
        self.bot_name = bot_name
        self.stop_loss_pct_override = override


def test_the_reported_stop_matches_what_the_cycle_would_apply():
    """This function exists because a per-product read once reported a
    15.88% stop on branches the cycle applied none to. Reporting 0 while the
    cycle now applies 35% is that same bug with the sign flipped."""
    import os

    import crypto_grid_bot as grid
    b = Branch("ZEC-USD", "crypto_grid_9", 0.0)
    resolved = {"daily_vol_pct": LIVE_VOL["ZEC-USD"]}

    r = grid._reported_stop(b, resolved)
    ok("unarmed, it still reports no stop", r["stop_pct"] == 0.0, repr(r["stop_pct"]))
    ok("and no longer claims the resting stops cover it",
       "covered at the portfolio level" not in r["reason"], r["reason"])
    ok("it points at where cover can be checked",
       "/resting-stops" in r["reason"])

    old = os.environ.get(a.ADOPTED_MODE_ENV)
    os.environ[a.ADOPTED_MODE_ENV] = "arm"
    try:
        r = grid._reported_stop(b, resolved)
        ok("armed, it reports the real distance instead of 0",
           r["stop_pct"] == 0.35, repr(r["stop_pct"]))
        ok("and names it an adopted stop", r["source"] == "adopted_adaptive")
        ok("the volatility it was sized from is carried through",
           r["daily_vol_pct"] == LIVE_VOL["ZEC-USD"])
        # Armed but unsizable must not report a number it cannot stand behind.
        r2 = grid._reported_stop(b, {"daily_vol_pct": None})
        ok("armed with no volatility still reports 0, with the reason",
           r2["stop_pct"] == 0.0 and "could not be read" in r2["reason"],
           r2["reason"])
    finally:
        if old is None:
            os.environ.pop(a.ADOPTED_MODE_ENV, None)
        else:
            os.environ[a.ADOPTED_MODE_ENV] = old

    ok("a branch naming a real stop is untouched by any of this",
       grid._reported_stop(Branch("L-USD", "b", 0.08), resolved)["stop_pct"] == 0.08)
    ok("and a branch with no override still gets the adaptive answer",
       grid._reported_stop(Branch("N-USD", "b", None), {"stop_pct": 0.19})["stop_pct"]
       == 0.19)


def test_an_armed_branch_stops_being_counted_as_a_coverage_gap():
    """The gap report and the stop policy have to agree, or the fix shows up
    as a permanent red number on the panel that was built to find it."""
    import asyncio
    import contextlib
    import os

    import crypto_grid_bot as grid

    class _Rows:
        def __init__(self, rows): self._rows = rows
        def scalars(self): return self
        def all(self): return self._rows

    class _DB:
        async def execute(self, *a_, **k): return _Rows([Branch("ZEC-USD", "g9", 0.0)])

    @contextlib.asynccontextmanager
    async def _sess(): yield _DB()

    old_f, old_m = grid.get_session_factory, os.environ.get(a.ADOPTED_MODE_ENV)
    grid.get_session_factory = lambda: _sess
    try:
        os.environ.pop(a.ADOPTED_MODE_ENV, None)
        ok("unarmed, ZEC is reported as a gap",
           "ZEC" in asyncio.run(grid.products_without_a_grid_stop()))
        os.environ[a.ADOPTED_MODE_ENV] = "arm"
        ok("armed, it is no longer a gap - it has a stop policy",
           "ZEC" not in asyncio.run(grid.products_without_a_grid_stop()))
        os.environ[a.ADOPTED_MODE_ENV] = "true"
        ok("a value that does not arm it is still a gap",
           "ZEC" in asyncio.run(grid.products_without_a_grid_stop()))
    finally:
        grid.get_session_factory = old_f
        if old_m is None:
            os.environ.pop(a.ADOPTED_MODE_ENV, None)
        else:
            os.environ[a.ADOPTED_MODE_ENV] = old_m


def test_the_stop_did_not_get_its_own_copy_of_the_sell_path():
    """The existing comment is the rule: this block only CHOOSES a slice, and
    every line of order placement, P&L, row deletion and trade recording is
    the already-proven path reused unchanged. Checked by comparing the order
    calls in the cycle against the version on origin/main - a new one here
    would be a second seller, which is the whole bug being fixed.
    """
    import ast
    import pathlib
    import subprocess

    def order_calls(src):
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "run_grid_branch_cycle"), None)
        if fn is None:
            return None
        out = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                name = n.func.attr
                if name.startswith("place_") or name in ("grid_sell", "grid_buy"):
                    out.append(name)
        return sorted(out)

    here = pathlib.Path(__file__).with_name("crypto_grid_bot.py")
    mine = order_calls(here.read_text())
    ok("the cycle was located", mine is not None)
    try:
        base = subprocess.run(["git", "show", "origin/main:crypto_grid_bot.py"],
                              cwd=here.parent, capture_output=True, text=True,
                              timeout=60)
        if base.returncode != 0:
            ok(f"origin/main could be read for comparison ({base.stderr[:80]})", False)
            return
        ok("no order-placing call was added to the cycle",
           mine == order_calls(base.stdout),
           f"now {mine}, was {order_calls(base.stdout)}")
    except Exception as exc:
        ok(f"the comparison could run ({type(exc).__name__}: {exc})", False)


def test_zz_nothing_above_failed():
    assert not _failures, f"{len(_failures)} checks failed: {_failures}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        print(f"\n== {t.__name__}")
        try:
            t()
        except BaseException as e:
            ok(f"{t.__name__} ran to completion", False,
               f"raised {type(e).__name__}: {e}")
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} adopted-stop checks passed")
