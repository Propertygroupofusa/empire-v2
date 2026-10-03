"""The cooldown leaked, and the obvious fix for the leak was a worse bug.

WHAT WAS MEASURED, live, 2026-10-01, over 15.2 hours on a 900s cooldown.
Gaps between REAL venue attempts (the ones that spend three Coinbase calls
before the refusal is even computed):

    QNT-USD   121 attempts, median gap 310s, shortest   8s, 73/120 under 800s
    PEPE-USD  111 attempts, median gap 198s, shortest  12s
    TIA-USD    54 attempts, median gap  55s, shortest  16s

An 8-second gap on a 900-second cooldown is not an expiry. 286 expensive
attempts happened where a working cooldown allows at most 12/hour across
three products; the real rate was 18.8/hour.

TWO WAYS IT COULD LEAK, AND BOTH ARE CLOSED HERE.

1. A GAP DISARMED IT. note_dust() cleared on anything that was not DUST -
   REFUSED included. REFUSED means the planner could not decide: unreadable
   balance, missing rules, no price. So one transient failure wiped a
   cooldown armed on a STRUCTURAL fact (QNT holds 0.00097323 against a venue
   minimum of 0.001) and the next 30s cycle paid to rediscover it. The
   module's own docstring already said a gap must never ARM a cooldown. It is
   just as true that a gap must not DISARM one.

2. THE CHECK AND THE ARM ARE THREE AWAITS APART. skip_reason() runs at the
   top of the sell path; the verdict arms at the bottom, after awaiting the
   balance, the product rules and the order book. A second attempt starting
   inside that window passes the same check and pays again.

THE TRAP IN FIXING #2, and it was written before it was caught: a
provisional hold that inherits the 900s window turns a transient unreadable
balance - which returns EARLY, before any verdict - into fifteen minutes of
silence on a product that may be perfectly sellable. That is the exact
failure this module was written to prevent, reintroduced by its own fix. So
a provisional record expires on a separate short clock.

Run: python3 test_dust_cooldown_leak.py
"""
import os
import sys

import dust_cooldown as dc

checks = []


def ok(label, cond, detail=""):
    checks.append((label, bool(cond), detail))


def section(t):
    checks.append((t, None, ""))


def reset():
    dc._armed.clear()


QNT = "QNT-USD"

# ── 1. a gap must not disarm a structural fact ───────────────────────
section("[1] REFUSED is a gap - it must not clear a cooldown")
reset()
dc.note_dust(QNT, dc.DUST, available_units=0.00097323, reason="BELOW_BASE_INCREMENT", now=1000)
ok("armed by the real DUST verdict", dc.skip_reason(QNT, now=1001) is not None)
dc.note_dust(QNT, dc.REFUSED, reason="BALANCE_UNREADABLE", now=1002)
ok("STILL armed after a REFUSED - the fact did not change",
   dc.skip_reason(QNT, now=1003) is not None,
   "this is the 8-second gap: a transient failure wiped a structural verdict")
dc.note_dust(QNT, "SOMETHING_NEW", reason="unknown verdict", now=1004)
ok("an unrecognised verdict also leaves it alone",
   dc.skip_reason(QNT, now=1005) is not None)
ok("it still expires on its own clock",
   dc.skip_reason(QNT, now=1000 + dc.COOLDOWN_SECONDS + 1) is None)

section("[2] EXECUTE still clears - that one IS news")
reset()
dc.note_dust(QNT, dc.DUST, available_units=0.0009, now=2000)
ok("armed", dc.skip_reason(QNT, now=2001) is not None)
dc.note_dust(QNT, dc.EXECUTE, available_units=5.0, now=2002)
ok("cleared by a real EXECUTE", dc.skip_reason(QNT, now=2003) is None,
   "it is sellable now, so the dust verdict is wrong")

section("[3] a buy still clears it")
reset()
dc.note_dust(QNT, dc.DUST, now=3000)
ok("clear() drops it", dc.clear(QNT) is True)
ok("and it is gone", dc.skip_reason(QNT, now=3001) is None)

# ── 4. the in-flight hold ────────────────────────────────────────────
section("[4] the hold stops the SECOND caller inside the gap")
reset()
ok("nothing armed, first caller proceeds", dc.skip_reason(QNT, now=4000) is None)
dc.hold(QNT, now=4000)
ok("second caller 2s later is skipped", dc.skip_reason(QNT, now=4002) is not None)
ok("the reason says an attempt is in flight",
   "in flight" in str(dc.skip_reason(QNT, now=4002)),
   str(dc.skip_reason(QNT, now=4002)))

section("[5] THE TRAP: a hold must NOT last like a cooldown")
reset()
dc.hold(QNT, now=5000)
ok("the hold is short, not 900s", dc.HOLD_SECONDS < dc.COOLDOWN_SECONDS)
ok("still held inside its own window",
   dc.skip_reason(QNT, now=5000 + dc.HOLD_SECONDS - 1) is not None)
ok("RELEASED once the short window passes - not 15 minutes of blindness",
   dc.skip_reason(QNT, now=5000 + dc.HOLD_SECONDS + 1) is None,
   "an unreadable balance returns early; a 900s hold there is the exact bug "
   "this module exists to prevent")

section("[6] a hold never overwrites a real verdict")
reset()
dc.note_dust(QNT, dc.DUST, available_units=0.00097323,
             reason="BELOW_BASE_INCREMENT", now=6000)
dc.hold(QNT, now=6001)
r = dc._armed[QNT]
ok("the real record survives", not r.get("provisional"), str(r))
ok("its figures are intact", r.get("available_units") == 0.00097323)
ok("and it keeps the FULL window, not the short one",
   dc.skip_reason(QNT, now=6000 + dc.HOLD_SECONDS + 5) is not None)

section("[7] the real verdict overwrites a hold")
reset()
dc.hold(QNT, now=7000)
dc.note_dust(QNT, dc.DUST, available_units=0.00097323, now=7001)
ok("no longer provisional", not dc._armed[QNT].get("provisional"))
ok("and now holds the full window",
   dc.skip_reason(QNT, now=7000 + dc.HOLD_SECONDS + 5) is not None)
reset()
dc.hold(QNT, now=7100)
dc.note_dust(QNT, dc.EXECUTE, now=7101)
ok("an EXECUTE clears a hold too", dc.skip_reason(QNT, now=7102) is None)

section("[8] a gap still cannot ARM anything - the original rule")
reset()
dc.note_dust(QNT, dc.REFUSED, reason="BALANCE_UNREADABLE", now=8000)
ok("REFUSED on a clean product arms nothing",
   dc.skip_reason(QNT, now=8001) is None,
   "a gap is not a fact about inventory, in either direction")

section("[9] the sell path calls hold() AFTER the check, BEFORE the awaits")
import ast                                       # noqa: E402
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "crypto_btc_compound_bot.py"), encoding="utf-8").read()
fn = next(n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "place_maker_sell")
# CODE ONLY. The comments around this discuss skip_reason and hold at length.
body = ast.unparse(ast.Module(
    body=[n for n in fn.body
          if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                  and isinstance(n.value.value, str))], type_ignores=[]))
lines = body.splitlines()
i_skip = next(i for i, l in enumerate(lines) if "skip_reason" in l)
i_hold = next(i for i, l in enumerate(lines) if "_dust.hold(" in l)
i_bal = next(i for i, l in enumerate(lines) if "get_asset_balance" in l)
i_note = next(i for i, l in enumerate(lines) if "note_dust" in l)
ok("hold comes AFTER the skip check", i_hold > i_skip, f"{i_skip} -> {i_hold}")
ok("hold comes BEFORE the balance read", i_hold < i_bal, f"{i_hold} -> {i_bal}")
ok("the real verdict is still recorded last", i_note > i_bal)

print()
failed = 0
for label, res, detail in checks:
    if res is None:
        print(f"\n{label}")
    else:
        print(f"  {'PASS' if res else 'FAIL'}  {label}" + (f"   -> {detail}" if detail and not res else ""))
        failed += 0 if res else 1
total = sum(1 for _, r, _ in checks if r is not None)
print(f"\n{total - failed}/{total} checks passed")
print("ALL PASS" if not failed else f"{failed} FAILED")
sys.exit(1 if failed else 0)
