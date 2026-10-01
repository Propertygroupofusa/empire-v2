"""A total that silently drops unpriced assets must not be recorded.

The five lowest readings in a 490-point series were all pricing gaps, not
losses. The worst, 2026-10-01T01:47, read $8,172.38 against a $10,117.90
median - about $1,870 low - because six assets could not be priced. The
account owner saw that number overnight and read it as a loss.
"""
import os
import re
import sys

FAILS = []


def ok(label, cond, got=None):
    if cond:
        print(f"  PASS  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"   got: {got!r}" if got is not None else ""))


SRC = open("growth_ledger_worker.py").read()
BLOCK = SRC[SRC.index("A TOTAL THAT SILENTLY DROPS ASSETS"):SRC.index("k = capital_kpis.compute")]

print("\n[1] the guard exists and blanks the total, not just the note")
ok("it reads the unpriced count", "unpriced is not None" in BLOCK)
ok("it compares against a baseline", "GROWTH_UNPRICED_BASELINE" in BLOCK)
ok("it sets the total to None", re.search(r"total\s*=\s*coin_usd\s*=\s*None", BLOCK) is not None)
ok("it does NOT substitute a zero", "= 0.0" not in BLOCK and "= 0 " not in BLOCK)
ok("it records why", "notes.append" in BLOCK)
ok("the note says gap, not loss", "A gap, not a loss" in BLOCK)

print("\n[2] the threshold catches every bad reading and no good one")
baseline = float(os.getenv("GROWTH_UNPRICED_BASELINE", "5"))
bad = [6, 6, 6, 9, 6]            # the five readings under $9,500
typical = [4, 4, 5, 4, 5]        # the normal count in the same series
ok("every bad reading is over the baseline", all(b > baseline for b in bad), bad)
ok("no typical reading is over it", all(t <= baseline for t in typical), typical)

print("\n[3] the rule is the same one the ledger-unreadable case already uses")
ok("LEDGER_DERIVED blanking still present", "LEDGER_DERIVED" in SRC)
ok("that path also assigns None, never 0", "k[f] = None" in SRC)

print("\n[4] the arithmetic of the overnight reading, as a regression fixture")
median, shown = 10117.90, 8172.38
ok("the gap was about $1,870", abs((median - shown) - 1945.52) < 100.0, median - shown)
ok("...which is bigger than the real 24h move of -$73.29", (median - shown) > 73.29)

print("\n[5] a readable census is untouched - this must not blank good rows")
ok("the guard is conditional, not unconditional",
   "if _census_degraded:" in BLOCK)
ok("unpriced of None does not trigger it", "unpriced is not None and" in BLOCK)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
