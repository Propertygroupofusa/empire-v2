"""The breaker's input, on a branch that still holds coin.

Run as written: python3 test_peak_clamp_held.py

Section [3] replays the live fleet of 2026-10-09, where nine branches
carried a stored peak above their entire current claim and two were
breached on it. Those numbers are measured, not invented, so a change
that alters the arithmetic is caught against the real book.
"""
import sys
sys.path.insert(0, ".")
import crypto_grid_bot as g

FAILS = []


def ok(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def section(t):
    print("\n" + t)


S = [{"qty": 1}]          # "this branch holds something"

section("[1] the flag is the safety, so test the flag first")
ok("OFF by default - a deploy changes no behaviour",
   g.peak_ceiling_for_held_branch(97.73, 74.24, S, -8.35) is None)
ok("ON, it clamps", g.peak_ceiling_for_held_branch(97.73, 74.24, S, -8.35,
                                                   enabled=True) == 74.24)
ok("the module default is False", g.GRID_PEAK_CLAMP_HELD is False)

section("[2] what it will and will not touch")
ok("a FLAT branch is left to peak_for_flat_branch, not handled twice",
   g.peak_ceiling_for_held_branch(97.73, 74.24, [], -8.35, enabled=True) is None)
ok("a peak already at or below the ceiling is left alone",
   g.peak_ceiling_for_held_branch(70.0, 74.24, S, -8.35, enabled=True) is None)
ok("a branch AHEAD on its position keeps its real peak - the ceiling "
   "rises with the gain and does not clamp",
   g.peak_ceiling_for_held_branch(80.0, 74.24, S, +12.0, enabled=True) is None)
ok("an unreadable input returns None, never a guessed zero",
   g.peak_ceiling_for_held_branch(97.73, None, S, -8.35, enabled=True) is None
   and g.peak_ceiling_for_held_branch(97.73, 74.24, S, None, enabled=True) is None)
ok("it only ever clamps DOWN",
   g.peak_ceiling_for_held_branch(97.73, 74.24, S, -8.35, enabled=True) < 97.73)

section("[3] the live fleet, 2026-10-09 - the branches that prompted this")
# product, peak, allocated, unrealized, drawdown the breaker read
LIVE = [("LTC",  97.73,   74.24,  -8.35, 32.2),
        ("ALGO", 176.09, 151.23, -19.27, 25.1),
        ("BTC",   69.42,  52.81,  -0.69, 24.9),
        ("XRP", 1516.27, 1268.48, -90.30, 21.8),
        ("ACH",  101.86,  87.33,  -4.76, 18.5)]
for name, peak, alloc, unreal, read in LIVE:
    eq = alloc + unreal
    clamped = g.peak_ceiling_for_held_branch(peak, alloc, S, unreal, enabled=True)
    new_dd = (clamped - eq) / clamped * 100
    pos_dd = -unreal / alloc * 100
    ok(f"{name}: breaker read {read}% -> {new_dd:.1f}%, which IS its position drawdown",
       abs(new_dd - pos_dd) < 0.05)

section("[4] the breach that started it")
peak, alloc, unreal = 97.73, 74.24, -8.35
eq = alloc + unreal
before = (peak - eq) / peak
after_peak = g.peak_ceiling_for_held_branch(peak, alloc, S, unreal, enabled=True)
after = (after_peak - eq) / after_peak
ok("LTC was over the 25% breaker before", before > g.GRID_DRAWDOWN_BREAKER_PCT)
ok("LTC is under it after", after < g.GRID_DRAWDOWN_BREAKER_PCT)
ok("and the deadlock is what made it matter: a breached branch cannot "
   "buy, and the existing repair only fires when FLAT",
   g.peak_for_flat_branch(peak, alloc, S) is None
   and g.peak_for_flat_branch(peak, alloc, []) == 74.24)

section("[5] it must NOT rescue a branch that is genuinely deep")
# same claim, but the position itself is down 40% - no stale peak involved
deep_peak, deep_alloc, deep_unreal = 100.0, 100.0, -40.0
c = g.peak_ceiling_for_held_branch(deep_peak, deep_alloc, S, deep_unreal, enabled=True)
ok("a genuinely deep position is not clamped out of its breach",
   c is None or ((c - (deep_alloc + deep_unreal)) / c) >= 0.40 - 1e-9)

section("[6] end to end through effective_peak_equity")
eq = 74.24 - 8.35
p_off, _ = g.effective_peak_equity(97.73, 74.24, S, eq)
ok("with the flag off the live path is unchanged", p_off == 97.73)
g.GRID_PEAK_CLAMP_HELD = True
try:
    p_on, from_ = g.effective_peak_equity(97.73, 74.24, S, eq)
    ok("with the flag on the peak is clamped", p_on == 74.24)
    ok("and clamped_from carries the old peak so the repair can be logged",
       from_ == 97.73)
    pf, _ = g.effective_peak_equity(97.73, 74.24, [], 74.24)
    ok("the flat path still wins its own case", pf == 74.24)
    ahead, _ = g.effective_peak_equity(50.0, 74.24, S, 80.0)
    ok("rule 3 still ratchets UP to a new high", ahead == 80.0)
finally:
    g.GRID_PEAK_CLAMP_HELD = False
ok("the flag is restored, so this file leaves no global behind",
   g.GRID_PEAK_CLAMP_HELD is False)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
