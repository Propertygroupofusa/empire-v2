"""The headline must not add two numbers that are not the same kind.

Run as written: python3 test_dashboard_headline_is_one_kind_of_number.py

THE BILL, 2026-10-08. The owner photographed the top of his own dashboard,
circled the headline and wrote "fix". It read:

    TOTAL PROFIT  -$7.32
    Realized (at your cost) +$430.39 · Unrealized (-9.20% of $4,755.98 of
    coin) -$437.71 · Locked +$0.00

Three different kinds of number, summed, painted red:

  +$132.61  money the grid actually banked across 244 completed sells.
            Real. In the account. Only moves when a slice really sells.
  +$297.78  a bookkeeping restatement of 4 inherited ZEC rows booked
            against an adoption-day mark nobody paid. The card's own fine
            print says "not money the grid earned".
  -$437.71  a mark-to-market on coin the owner ALREADY OWNED. Putting a
            branch on it started REPORTING that exposure; it did not
            create it. Not a loss unless a slice sells below its entry.

The old code knew. The comment directly under the sum read, in capitals,
"THE TWO HALVES ARE NOT THE SAME KIND OF NUMBER, AND THE HEADLINE ADDS
THEM." It documented the fault and committed it anyway - the same shape as
the heartbeat that called a waiting loop dead, and the exit classifier that
called a grid short of its own step a defect.

WHAT THIS FILE WILL NOT ACCEPT:
  * the headline reading from unrealised, in any arithmetic;
  * the headline's colour coming from anything but banked;
  * the open book being hidden - it is real and it stays visible;
  * the restated adoption rows being presented as earnings.
"""

import re
import sys

SRC = open("family_tree_dashboard.html", encoding="utf-8").read()
SCRIPT = re.search(r"<script>(.*)</script>", SRC, re.S).group(1)
MARKUP = SRC[: SRC.index("<script>")]


def strip_comments(js):
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", ln) for ln in js.split("\n"))


FAILURES = []


def ok(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"  ({detail})" if not cond and detail else ""))
    if not cond:
        FAILURES.append(label)


def section(t):
    print("\n" + t)


def fn_body(name):
    m = re.search(r"^(?:async )?function %s\s*\(" % re.escape(name), SCRIPT, re.M)
    if not m:
        return None
    i = SCRIPT.index("{", m.end() - 1)
    depth, j, in_s, esc, q = 0, i, False, False, ""
    while j < len(SCRIPT):
        c = SCRIPT[j]
        if in_s:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                in_s = False
        elif c in "\"'`":
            in_s, q = True, c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[i:j + 1]
        j += 1
    return None


RAW = fn_body("renderTotalProfit")
ok("renderTotalProfit() exists and was extracted", bool(RAW))
BODY = strip_comments(RAW or "")

section("[1] THE HEADLINE IS BANKED MONEY, AND NOTHING ELSE")
m = re.search(r"const\s+headline\s*=\s*([^;]+);", BODY)
ok("the headline is computed into a named `headline`", bool(m),
   "it used to be an anonymous `const total` that summed both halves")
expr = m.group(1) if m else ""
ok("the headline expression never touches unrealised",
   "lastUnrealizedProfit" not in expr, expr.strip()[:90])
ok("it is built from the banked figure",
   "lastRealizedOwn" in expr or "lastRealizedProfit" in expr, expr.strip()[:90])

section("[2] THE COLOUR COMES FROM BANKED TOO")
m2 = re.search(r"const\s+cls\s*=\s*([^;]+);", BODY)
ok("the card's class is a named expression", bool(m2))
cexpr = m2.group(1) if m2 else "x"
ok("the colour never reads unrealised", "lastUnrealizedProfit" not in cexpr, cexpr[:90])
ok("the colour reads the headline", "headline" in cexpr, cexpr[:90])

section("[3] NOTHING IN THE CARD ADDS THE TWO HALVES")
adds = re.findall(r"lastUnrealizedProfit[^\n;]*\+[^\n;]*lastRealized", BODY)
adds += re.findall(r"lastRealized[^\n;]*\+[^\n;]*lastUnrealizedProfit", BODY)
ok("no expression sums realised and unrealised", not adds, str(adds)[:160])

section("[4] THE OPEN BOOK STAYS VISIBLE - hiding it is the opposite mistake")
ok("unrealised is still rendered", "lastUnrealizedProfit" in BODY)
ok("it still carries its percentage of the coin it marks", "lastDeployedCoin" in BODY)
ok("it is labelled an open book, not profit", re.search(r"[Oo]pen book", BODY) is not None)
ok("and the card says it is not added to the headline",
   re.search(r"not added|never added|not in the headline", BODY) is not None)

section("[5] THE ADOPTION RESTATEMENT IS NOT PRESENTED AS EARNINGS")
ok("the restated-rows explanation survives",
   "lastRealizedIsRestated" in BODY and "lastRealizedOwn" in BODY)
ok("and still says it is not money the grid earned",
   "not money the grid earned" in (RAW or ""))

section("[6] THE TILE SAYS WHAT IT IS")
ok("the KPI label no longer reads 'Total Profit'", "💰 Total Profit" not in MARKUP)
ok("it names banked money instead", re.search(r"Banked", MARKUP) is not None)

section("[7] TOTAL ALLOCATED NO LONGER READS AS A LOSS WHEN IT FALLS")
ok("the allocated tile carries a note", 'id="stat-total-note"' in MARKUP)
ok("the note says a fall is not a loss", re.search(r"not a loss", MARKUP) is not None)

section("[8] WHERE YOUR MONEY IS - the question the page could not answer")
mm = fn_body("renderMoneyMap")
ok("renderMoneyMap() exists", bool(mm))
MM = strip_comments(mm or "")
ok("it reads cash from the measured wallet figure", "wallet_cash_usd" in MM)
ok("it reads the coin the branches claim", "deployed_coin_usd" in MM)
ok("it reads owned units from the OWNED block, never the available one",
   "backing_owned" in MM)
ok("it adds no network request of its own", "apiGet" not in MM and "fetch(" not in MM)
ok("the container is in the markup", 'id="money-map"' in MARKUP)
ok("it is called with the grid payload", re.search(r"renderMoneyMap\(", SCRIPT) is not None)

section("[9] DISPLAY ONLY - no money path is touched")
for forbidden in ("reconcile-slices", "rightsize", "set-levels", "close-branch",
                  "spread-evenly", "move-cash", "deploy-cash"):
    ok(f"neither function calls {forbidden}", forbidden not in BODY and forbidden not in MM)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
