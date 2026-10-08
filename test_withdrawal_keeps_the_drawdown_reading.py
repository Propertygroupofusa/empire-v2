"""A deliberate withdrawal must not move the drawdown PERCENTAGE.

Run as written: python3 test_withdrawal_keeps_the_drawdown_reading.py

THE SECOND HALF OF THE 2026-10-05 BUG, measured live 2026-10-08 21:15Z.

peak_after_withdrawal was written to subtract what left, so the DOLLAR gap
`peak - equity` - the real market loss - survives the write. It does. But
the breaker does not compare dollars; it compares

    (peak_equity - equity) / peak_equity   against   25%

Lowering the peak leaves the numerator alone and shrinks the denominator,
so after a withdrawal the same unchanged dollar loss reads as a LARGER
percentage - the branch becomes more sensitive to the market by exactly
the fraction of itself that was taken out. The instant breach was fixed
and a hair-trigger was left in its place.

TWO LIVE BRANCHES WERE SITTING IN IT, both frozen out of buying:

  LTC-USD  right-sized 19:54:08Z, $195.04 -> $74.24. Peak $218.53 ->
           $97.73. Its unrealized was -$8.57 - 3.9% of the old peak,
           11.5% of the branch's own $74.24 of coin - and it arrived as a
           32.80% drawdown. The gap was $32.06 before and $32.06 after.
           Only the denominator moved.
  BTC-USD  25.1% against a 25% breaker on -$0.81 of unrealized over
           $36.14 of coin. Frozen on eighty-one cents.

So the invariant a percentage breaker needs is the percentage. Scaling the
peak by the same ratio the equity shrank delivers it exactly, and that is
what these tests pin.

WHAT THEY ALSO PIN, because this touches a circuit breaker:
  * 25% is still 25%. No threshold is read, moved or softened here.
  * the peak only ever moves DOWN. [5]
  * a real crash still breaches, before and after a withdrawal. [4]
  * a branch already past the breaker stays past it. [6]
  * with no price the OLD subtraction runs, byte for byte, so an
    un-updated caller cannot be changed by this. [3]
"""

import sys

sys.path.insert(0, ".")
import crypto_grid_bot as grid

FAILS = []
BREAKER = grid.GRID_DRAWDOWN_BREAKER_PCT


def ok(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def section(t):
    print("\n" + t)


def dd(peak, equity):
    return (peak - equity) / peak if peak > 0 else 0.0


# peak_equity is stored in dollars and cents, so the scaled peak is rounded
# before it is written and the invariant holds to that cent, not to machine
# precision. On the live LTC figures the residual is 2e-5 of the reading -
# five thousand times smaller than the margin to the breaker. Asserting
# 1e-6 here would be asserting that a dollar column has no cents.
SAME = 1e-4


def same_reading(peak_a, eq_a, peak_b, eq_b):
    return abs(dd(peak_a, eq_a) - dd(peak_b, eq_b)) < SAME


section("[1] THE LIVE LTC FREEZE - same dollar loss, same reading")
# Measured: peak $218.53 before the right-size, allocation 195.04 -> 74.24,
# unrealized -$8.57. Equity is allocated + unrealized throughout.
PEAK, BEFORE, AFTER, UNREAL = 218.53, 195.04, 74.24, -8.57
eq_before, eq_after = BEFORE + UNREAL, AFTER + UNREAL
dd_before = dd(PEAK, eq_before)
ok("before the right-size LTC was NOT breached (14.67%)",
   abs(dd_before * 100 - 14.67) < 0.02 and dd_before < BREAKER)

old = grid.peak_after_withdrawal(PEAK, BEFORE, AFTER)          # no price
dd_old = dd(old, eq_after)
ok("the OLD subtraction put it at 32.80% - past the breaker",
   abs(dd_old * 100 - 32.80) < 0.05 and dd_old > BREAKER)
ok("and the dollar gap it preserved was identical either way",
   abs((PEAK - eq_before) - (old - eq_after)) < 0.005)

new = grid.peak_after_withdrawal(PEAK, BEFORE, AFTER, unrealized=UNREAL)
dd_new = dd(new, eq_after)
ok("the fix holds the reading at 14.67% - the percentage is the invariant",
   abs(dd_new - dd_before) < SAME and abs(dd_new * 100 - 14.67) < 0.02)
ok("so LTC is no longer breached", dd_new < BREAKER)
ok("the new peak is $76.96, below the old $97.73 - never above",
   abs(new - 76.96) < 0.02 and new < old)

section("[2] the invariant holds for any withdrawal, not just that one")
for before, after, unreal, peak in [
        # (allocated_before, allocated_after, unrealized, peak)
        (195.04, 74.24, -8.57, 218.53),    # the live LTC right-size
        (400.0, 250.0, -37.50, 500.0),     # the figures the old dollar test used
        (52.81, 20.0, -0.81, 69.42),       # BTC's 81 cents, on a withdrawal
        (80.0, 79.99, 0.0, 100.0),         # a one-cent withdrawal
        (1000.0, 15.0, -120.0, 1400.0),    # drained to the keep-alive floor
        (900.0, 450.0, -55.0, 1200.0)]:    # half the branch, position underwater
    p = grid.peak_after_withdrawal(peak, before, after, unrealized=unreal)
    a, b = dd(peak, before + unreal), dd(p, after + unreal)
    if after + unreal <= 0:
        # The documented degenerate case: no equity left to express a
        # percentage against, so the conservative subtraction is correct
        # and the invariant is not claimed. Pin THAT instead.
        ok(f"{before:.2f}->{after:.2f} with unrealized {unreal:+.2f} leaves no "
           f"equity - takes the conservative subtraction",
           p == round(max(0.0, peak - (before - after)), 2))
        continue
    ok(f"{before:.2f}->{after:.2f} with unrealized {unreal:+.2f}: "
       f"{a*100:.2f}% stays {b*100:.2f}%", abs(a - b) < SAME)

section("[3] NO PRICE MEANS THE OLD BEHAVIOUR, EXACTLY")
for peak, before, after in [(218.53, 195.04, 74.24), (285.40, 285.40, 142.43),
                            (500.0, 400.0, 250.0), (1000.0, 800.0, 600.0)]:
    want = round(max(0.0, peak - (before - after)), 2)
    ok(f"peak_after_withdrawal({peak}, {before}, {after}) is still {want}",
       grid.peak_after_withdrawal(peak, before, after) == want)
ok("an unreadable price falls back too, it does not raise",
   grid.peak_after_withdrawal(218.53, 195.04, 74.24, unrealized="nonsense")
   == 97.73)
ok("a NaN unrealized falls back as well",
   grid.peak_after_withdrawal(218.53, 195.04, 74.24,
                              unrealized=float("nan")) == 97.73)
ok("so does an infinite one",
   grid.peak_after_withdrawal(218.53, 195.04, 74.24,
                              unrealized=float("inf")) == 97.73)

section("[4] A REAL CRASH STILL BREACHES - the safety direction")
# A branch genuinely 40% underwater on its coin, then money taken out.
p = grid.peak_after_withdrawal(1000.0, 800.0, 600.0, unrealized=-400.0)
ok("down 40% on the position reads 60% after the withdrawal, still past 25%",
   dd(p, 600.0 - 400.0) > BREAKER)
ok("and it read 60% BEFORE the withdrawal too - unchanged, as designed",
   same_reading(1000.0, 400.0, p, 200.0))
# And a position that crashes AFTER the withdrawal must still trip.
p2 = grid.peak_after_withdrawal(218.53, 195.04, 74.24, unrealized=-8.57)
ok("a further crash on the shrunk branch still breaches",
   dd(p2, 74.24 - 30.0) > BREAKER)

section("[5] THE PEAK ONLY EVER MOVES DOWN")
import random
random.seed(20261008)
worst = 0.0
for _ in range(4000):
    peak = round(random.uniform(1.0, 5000.0), 2)
    before = round(random.uniform(1.0, 5000.0), 2)
    after = round(random.uniform(0.01, before), 2)
    unreal = round(random.uniform(-before, before), 2)
    p = grid.peak_after_withdrawal(peak, before, after, unrealized=unreal)
    if p is None:
        continue
    if p > peak + 1e-9:
        worst = max(worst, p - peak)
ok("4,000 random withdrawals never raised a peak", worst == 0.0)

section("[6] a branch ALREADY past the breaker stays past it")
# peak 300, equity 200 -> 33.3%, already breached. Take money out.
p = grid.peak_after_withdrawal(300.0, 250.0, 100.0, unrealized=-50.0)
ok("33.33% before stays 33.33% after, still breached",
   same_reading(300.0, 200.0, p, 50.0) and dd(p, 50.0) > BREAKER)

section("[7] the degenerate cases take the conservative answer")
ok("equity already at zero falls back to the subtraction",
   grid.peak_after_withdrawal(100.0, 50.0, 20.0, unrealized=-50.0) == 70.0)
ok("a withdrawal that would leave equity negative falls back too",
   grid.peak_after_withdrawal(100.0, 50.0, 20.0, unrealized=-30.0) == 70.0)
ok("a deposit still returns None, with or without a price",
   grid.peak_after_withdrawal(100.0, 100.0, 250.0, unrealized=-5.0) is None)
ok("a no-op still returns None",
   grid.peak_after_withdrawal(100.0, 100.0, 100.0, unrealized=-5.0) is None)
ok("a NULL peak still stays NULL so the read path self-heals",
   grid.peak_after_withdrawal(None, 400.0, 100.0, unrealized=-5.0) is None)
ok("unreadable allocations still change nothing",
   grid.peak_after_withdrawal(100.0, None, 50.0, unrealized=-5.0) is None
   and grid.peak_after_withdrawal(100.0, 50.0, None, unrealized=-5.0) is None)

section("[8] no threshold was touched")
ok("the breaker is still 25%", abs(BREAKER - 0.25) < 1e-9)
src = open("crypto_grid_bot.py").read()
i = src.index("def peak_after_withdrawal")
body = src[i:src.index("\ndef ", i + 10)]
ok("peak_after_withdrawal does not read the breaker constant at all",
   "GRID_DRAWDOWN_BREAKER_PCT" not in body)
rs = open("branch_rightsize.py").read()
ok("and the right-size does not either",
   "GRID_DRAWDOWN_BREAKER_PCT" not in rs)
ok("the withdrawal is still sized on cost basis, not on the mark",
   "coin_basis(slices)" in rs and "floor = round(max(basis, MIN_BRANCH_USD), 2)" in rs)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
