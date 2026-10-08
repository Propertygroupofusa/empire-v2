"""Branch cards read 1, 2, 3 - and the trading loop's order is left alone.

Run as written: python3 test_branch_cards_are_in_number_order.py

2026-10-08. The owner photographed the grid panel and asked why the cards
were not in order: "grid bot 1, 2, 3, 4, 5 ... all the way down to 23. Why
they are not in order". They were in order - ALPHABETICAL order, which for
crypto_grid_N is 1, 10, 11, 12, 13, 14, 15, 16, 17, 19, 2, 20, 21, 22, 23,
3, 4, 6, 7, 8, 9. That is what a string sort does to a numbered list.

WHY THE FIX IS IN THE PAGE AND NOT AT THE SOURCE, which is the part worth
protecting. crypto_grid_bot.get_grid_branches() carries the same ORDER BY
and has fourteen callers, among them the trading cycle. The loop is
sequential over branches and one resting maker order can hold it for its
whole 3,600s budget - measured 2026-10-08, a 65-minute stall on a single
XLM buy - so the order branches are visited in decides who gets served
first when the loop is slow. Re-sorting there to tidy a page would be a
money change wearing a cosmetic hat.
"""
import re
import sys

SRC = open("family_tree_dashboard.html", encoding="utf-8").read()
BOT = open("crypto_grid_bot.py", encoding="utf-8").read()
FAILS = []


def ok(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"  ({detail})" if not cond and detail else ""))
    if not cond:
        FAILS.append(label)


print("\n[1] the page sorts its own copy, numerically")
ok("a branchNum helper exists", "const branchNum" in SRC)
ok("it parses a trailing number", re.search(r"branchNum\s*=.*\n?.*\\d\+", SRC) is not None
   or "(\\d+)" in SRC.split("const branchNum")[1][:300])
ok("the rendered list is sorted", re.search(
    r"const branches = \(data\.branches \|\| \[\]\)\.slice\(\)\.sort", SRC) is not None)
ok("it sorts a COPY - .slice() before .sort(), never the payload in place",
   ".slice().sort(" in SRC.split("const branchNum")[1][:600])
ok("an unnumbered branch sorts LAST, not first",
   "MAX_SAFE_INTEGER" in SRC.split("const branchNum")[1][:400],
   "a 0 default would put an unnumbered branch at the top as though it were first")
ok("ties fall back to the name so a redraw cannot shuffle cards",
   "localeCompare" in SRC.split("const branchNum")[1][:900])

print("\n[2] THE TRADING PATH IS NOT RE-SORTED - this is the one that matters")
ok("get_grid_branches still orders by bot_name, untouched",
   "order_by(CryptoGridBranch.bot_name)" in BOT)
fn = BOT.split("async def get_grid_branches()")[1][:400]
ok("and it gained no numeric sort of its own",
   "sorted(" not in fn and "key=" not in fn, fn.strip()[:120])

print("\n[3] the sort is pure display - it moves no money")
blk = SRC.split("const branchNum")[1][:1200]
for forbidden in ("apiGet", "fetch(", "POST", "reconcile", "rightsize", "withdraw"):
    ok(f"the sort block never calls {forbidden}", forbidden not in blk)

print("\n[4] the real sequence, measured 2026-10-08: 21 branches, 5 and 18 absent")
# A numeric sort of the live names must produce exactly this reading order.
live = ["crypto_grid_%d" % n for n in
        [1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20, 21, 22, 23]]
shuffled = sorted(live)  # what the string sort gives, and what the page got
def num(b):
    m = re.search(r"(\d+)\s*$", b)
    return int(m.group(1)) if m else 2 ** 53
ok("the string order really is wrong - this is the bug, reproduced",
   shuffled != live and shuffled[1] == "crypto_grid_10")
ok("sorting by the trailing number restores reading order",
   sorted(shuffled, key=num) == live)
ok("21 names, not 23 - 5 and 18 do not exist",
   len(live) == 21 and "crypto_grid_5" not in live and "crypto_grid_18" not in live)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
