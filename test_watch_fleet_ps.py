"""Static checks on watch_fleet.ps1, the Windows PowerShell fleet watcher.

watch_fleet.sh is bash and needs Git Bash or WSL. On stock Windows PowerShell
5.1 the documented command failed outright:

    cd ~/empire-v2 && git pull && ./watch_fleet.sh 10
    The token '&&' is not a valid statement separator in this version.

So this file exists to keep the PowerShell version free of the constructs that
caused that, and to pin the output contract it shares with the bash version:
TOTAL leads, a missing half is unmeasurable rather than zero, free cash is
visible, and a negative figure never renders as "$-1.23".

Checks run against CODE, not comments. An earlier version of these assertions
flagged the very comment that explains the '&&' bug - the same prose-matching
trap already hit twice in this repo's tests.

Run: python3 test_watch_fleet_ps.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "watch_fleet.ps1"), encoding="utf-8").read()

# Strip whole-line comments. PowerShell has no block comments here, and every
# '#' in this file starts a line comment, so this is exact rather than a
# heuristic - verified by the "no stray inline #" check below.
CODE = "\n".join(l for l in SRC.splitlines() if not l.lstrip().startswith("#"))

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


ok("comment stripping left real code behind", "Invoke-RestMethod" in CODE)
ok("no inline '#' that stripping would have missed",
   not re.search(r'[^\s"\']#', CODE.replace("`#", "")))

# --- the bug this file exists to prevent ---------------------------------
ok("no '&&' in executable code", "&&" not in CODE)
ok("but the comments still explain why", "&&" in SRC)
ok("no '||' either - same PowerShell 5.1 limitation", "||" not in CODE)
ok("no bash shebang", not SRC.startswith("#!"))

# --- PowerShell 5.1 compatibility ----------------------------------------
ok("param block precedes any other statement",
   CODE.index("param(") < CODE.index("Invoke-RestMethod"))
ok("TLS 1.2 is forced before the first request",
   CODE.index("SecurityProtocol") < CODE.index("Invoke-RestMethod"))
ok("no ternary operator (PowerShell 7+ only)", "? " not in CODE.replace("?t=", ""))
ok("no null-coalescing ?? (PowerShell 7+ only)", "??" not in CODE)
ok("no -Parallel ForEach (PowerShell 7+ only)", "-Parallel" not in CODE)
ok("braces balance", CODE.count("{") == CODE.count("}"))
ok("parens balance", CODE.count("(") == CODE.count(")"))

# --- the output contract, shared with watch_fleet.sh ----------------------
ok("TOTAL is printed before the realized line",
   CODE.index('"TOTAL') < CODE.index('"  taken'))
ok("an unmeasurable total says so rather than printing a number",
   "not measurable" in CODE)
ok("the total is gated on the server's own measurable flag",
   "$head.measurable" in CODE)
ok("realized and unrealized are both shown as components",
   "realized_usd" in CODE and "unrealized_usd" in CODE)
ok("the gap warning is surfaced when the server flags one", "$head.warning" in CODE)
ok("free cash is printed - the figure that gates every fill",
   "real_free_cash_usd" in CODE)
ok("per-branch step % is shown, so a spacing promotion is visible",
   "grid_pct" in CODE and "step {0:N2}%" in CODE)
ok("open slices are marked distinctly from flat branches",
   "flat, waiting for a dip" in CODE)

# --- money rendering ------------------------------------------------------
ok("sign sits OUTSIDE the currency symbol", '"{0}{1}`${2:N2}"' in CODE)
ok("a null figure renders as a dash, never as 0.00", '"{0}  --  "' in CODE)
ok("null is tested explicitly rather than by truthiness",
   "$null -eq $Value" in CODE)

# --- behaviour under failure ---------------------------------------------
ok("a failed poll is announced rather than silently retried",
   "Cannot reach the server" in CODE)
ok("and the loop continues instead of exiting", "continue" in CODE)
ok("an older deploy without the headline section still renders",
   "REALIZED" in CODE)
ok("the loop sleeps between polls", "Start-Sleep" in CODE)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
