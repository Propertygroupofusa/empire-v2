"""Entries halted must be SAID, once, and must not rotate a position out.

TWO DEFECTS, ONE GUARD, both measured on the live service.

THE LOG LIED BY OMISSION. try_open() has always refused while
entries_halted is set, but the refusal for the market-closed case logged at
DEBUG, which the deployed level never prints. Every candidate still printed
"READY ... attempting entry..." at INFO first. Live 2026-10-09 20:30-20:39Z:
MSFT, AAPL, M2K, MYM and MGC each logged "attempting entry..." every cycle
for nine minutes after the 20:00Z close, with no fill and no refusal after
any of them.

THE ROTATION COULD SELL FOR NOTHING. A candidate at the position cap takes
the rotation branch, which closes the weakest LOSING position to free a slot
and only then calls try_open - which refuses. Under a kill condition or a
manual pause the market is OPEN, so that close executes: a realised loss to
make room for an entry that can never follow.
"""
import re, sys

FAILED = []
def ok(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond: FAILED.append(label)
def section(t): print("\n" + t)

src = open("prop_bot.py").read()

section("[1] the guard exists, and it runs BEFORE the candidate walk")
sort_at  = src.index("candidates.sort(key=lambda c: -c[0])")
guard_at = src.index("if entries_halted and candidates:")
loop_at  = src.index("for _, contract, config, side, price, rsi, trend in candidates:")
ok("the halt guard sits between the sort and the loop",
   sort_at < guard_at < loop_at)
ok("it empties the candidate list rather than relying on a later refusal",
   "candidates = []" in src[guard_at:loop_at])

section("[2] it is ONE line per cycle, not one per symbol")
guard = src[guard_at:loop_at]
ok("exactly one log call in the guard", guard.count("log.info(") == 1)
ok("and it carries the COUNT, so sixteen symbols read as one line",
   "len(candidates)" in guard and "deferred" in guard)
ok("it logs at INFO, the level the service actually prints - the DEBUG "
   "call this replaces is the whole reason nothing was visible",
   "log.info(" in guard and "log.debug(" not in guard)

section("[3] it names WHICH halt, because the two are not the same news")
ok("market closed is told apart from a kill condition",
   "MARKET_CLOSED_PREFIX" in guard and "KILL CONDITION" in guard)
ok("and the halt's own reason text is passed through verbatim",
   "{entries_halted}" in guard)

section("[4] exits are untouched - the guard is entry-side only")
ok("it says so in the line the owner will read",
   "exit" in guard.lower() and "unaffected" in guard.lower())
ok("it does not sit anywhere near close_position",
   "close_position" not in guard)

section("[5] try_open's own refusal is KEPT as the backstop, not replaced")
ok("try_open still refuses on entries_halted",
   re.search(r"if entries_halted:\s*\n", src) is not None)
ok("the one-chokepoint comment is still there",
   "The one chokepoint for NEW risk" in src)
ok("the market-closed branch inside try_open survives for other callers",
   "entry deferred to the open" in src)

section("[6] the rotation path is now unreachable while entries are halted")
rot_at = src.index("🔄 ROTATING:")
ok("the rotation sits AFTER the guard, so an emptied list never reaches it",
   guard_at < rot_at)
ok("rotation still closes only a genuine loss when it does run",
   "weakest_pct < 0" in src)
ok("and it still calls try_open after closing - unchanged behaviour when "
   "entries are live", src.count("await try_open(") >= 2)

section("[7] the shape of the live symptom cannot come back")
walk = src[loop_at:rot_at]
ok("'attempting entry' is still logged on the live path (not deleted, just "
   "no longer reachable while halted)", "attempting entry" in walk)
ok("nothing in the walk re-checks entries_halted - the guard owns it",
   "entries_halted" not in walk)

print("\n" + ("ALL PASS" if not FAILED else f"{len(FAILED)} FAILED: " + "; ".join(FAILED)))
sys.exit(1 if FAILED else 0)
