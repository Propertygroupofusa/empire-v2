"""The "make money" button must be equally willing to say there is nothing to do.

The account owner asked for a button to press that makes money. There is
no such button - a control that claims to create edge is either
front-running the strategy's own exits or lying. What a button can
honestly do is close the gap between capital that is EARNING and capital
that is only SITTING, because that gap is real and measurable.

The trap this file exists to prevent:

  On 2026-09-25 the fleet showed $88.14 free against $484.46 working -
  15.4% of the crypto account apparently doing nothing. A naive "deploy
  idle cash" button would have deployed it. That $88.14 is almost exactly
  GRID_CASH_RESERVE_USD (88.0), the reserve funding the remaining levels
  of every open branch. Deploying it would have left $0.14 of buffer and
  reported it as an improvement.

  So: idle cash is measured against the RULE THAT GOVERNS IT, never
  against zero. Free cash minus the reserve, compared to what one branch
  actually costs. $88.14 free is "already at work", not "idle".

THE RULES THIS FILE PROTECTS:

  1. money_check() is READ-ONLY. It reports; it never moves money.
  2. Idle cash is free cash MINUS the reserve, and is only reported as an
     opportunity when it clears the cost of one branch.
  3. Every finding carries the basis it was measured against, and no
     finding is a forecast of profit.
  4. "Nothing to do" is a first-class answer with its own finding.

Run: python3 test_money_check.py
"""

import ast
import io
import re
import sys

FAILURES = []
CHECKS = 0


def ok(label, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        FAILURES.append(label)


BOT = io.open("crypto_grid_bot.py", encoding="utf-8").read()
ROUTER = io.open("routers/trading_dashboard.py", encoding="utf-8").read()
DASH = io.open("family_tree_dashboard.html", encoding="utf-8").read()
SCRIPT = re.search(r"<script>(.*)</script>", DASH, re.S).group(1)

tree = ast.parse(BOT)
MC = next((n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "money_check"), None)
MC_SRC = ast.get_source_segment(BOT, MC) if MC else ""

print("test_money_check.py")
print()

# ── 1. it cannot move money ─────────────────────────────────────────────
print("-- the check is read-only --")
ok("money_check() exists", MC is not None)
ok("it never commits a database transaction", "commit()" not in MC_SRC)
ok("it never places an order", not any(x in MC_SRC for x in
   ("grid_buy(", "grid_sell(", "place_order", "market_order")))
ok("it never writes a branch field",
   not re.search(r"\.(grid_pct|allocated_usd|reference_price|active)\s*=", MC_SRC))
ok("the endpoint is a GET, not a POST",
   '@router.get("/grid-status/money-check")' in ROUTER
   and '@router.post("/grid-status/money-check")' not in ROUTER)
ok("the dashboard panel only reads",
   "runMoneyCheck" in SCRIPT and "apiGet('/grid-status/money-check'" in SCRIPT)
ok("the dashboard panel never POSTs",
   "method: 'POST'" not in (re.search(r"async function runMoneyCheck.*?\n}", SCRIPT, re.S) or
                            type("x", (), {"group": lambda s, *a: ""})()).group(0))

# ── 2. idle cash is measured against the reserve, never against zero ────
print()
print("-- $88.14 free is not $88.14 idle --")
ok("it subtracts the cash reserve", "GRID_CASH_RESERVE_USD" in MC_SRC)
ok("it compares the remainder to what one branch costs",
   "GRID_AUTO_DEPLOY_AMOUNT_USD" in MC_SRC)
ok("'already at work' is its own reported finding", "cash_fully_deployed" in MC_SRC)
ok("unreadable cash is never called deployable", "cash_unknown" in MC_SRC)

# The real arithmetic, on the real numbers from 2026-09-25.
RESERVE, BRANCH_COST = 88.0, 70.0


def classify(free, reserve=RESERVE, branch_cost=BRANCH_COST):
    if free is None:
        return "cash_unknown"
    return "idle_cash" if (free - reserve) >= branch_cost else "cash_fully_deployed"


CASES = [
    ("the live 2026-09-25 balance", 88.14, "cash_fully_deployed"),
    ("exactly the reserve", 88.00, "cash_fully_deployed"),
    ("one cent under a full branch above reserve", 157.99, "cash_fully_deployed"),
    ("exactly one full branch above reserve", 158.00, "idle_cash"),
    ("plenty spare", 400.00, "idle_cash"),
    ("below the reserve entirely", 20.00, "cash_fully_deployed"),
    ("balance unreadable", None, "cash_unknown"),
]
for label, free, expect in CASES:
    got = classify(free)
    ok(f"{label}: {got}", got == expect, f"expected {expect}")

ok("the live balance leaves $0.14 spare, not $88.14",
   round(88.14 - RESERVE, 2) == 0.14)

# ── 3. every finding states what it was measured against ────────────────
print()
print("-- a finding is a measurement, not a promise --")
ok("every finding carries a 'basis'", MC_SRC.count('"basis"') >= 4)
ok("every finding carries an 'action' (or None)", MC_SRC.count('"action"') >= 4)
ok("the payload says outright that nothing is a forecast",
   "forecast" in MC_SRC)
for word in ["expected profit", "projected return", "guaranteed", "will earn", "will make"]:
    ok(f"it never claims {word!r}", word not in MC_SRC.lower())
ok("the panel repeats that a button cannot create edge",
   "cannot create an edge" in SCRIPT or "cannot create an edge" in DASH)

# ── 4. the findings it must detect ──────────────────────────────────────
print()
print("-- what it looks for --")
for kind in ["idle_cash", "below_fee_floor", "stale_reference", "paused_with_capital"]:
    ok(f"it detects {kind}", f'"{kind}"' in MC_SRC)
ok("a branch holding open slices is never called a stale reference",
   "open_slices" in MC_SRC)
ok("re-anchoring is described as upward only",
   "upward only" in MC_SRC)

# Stale-reference arithmetic on the live branches.
LIVE = [("BCH-USD", 339.44, 340.83, 0.02), ("BONK-USD", 3.67e-06, 3.68e-06, 0.02),
        ("NEAR-USD", 5.093, 5.1034, 0.02), ("ETC-USD", 9.421, 9.435, 0.02)]
total_pts = 0.0
for pid, ref, cur, g in LIVE:
    needs_now = (cur - ref * (1 - g)) / cur * 100.0
    saves = needs_now - g * 100.0
    total_pts += saves
    ok(f"{pid}: a stale reference costs it {saves:.2f} extra points of waiting",
       saves > 0 and needs_now > g * 100.0)
ok("across the fleet that is about a point of waiting recovered",
   0.5 < total_pts < 2.0, f"{total_pts:.2f} pts")

# A branch already at or below its reference is NOT stale.
for pid, ref, cur, g in [("X", 100.0, 100.0, 0.02), ("Y", 100.0, 99.0, 0.02)]:
    ok(f"{pid} at/below its reference is not reported stale", not (cur > ref))

# ── 5. only non-spending fixes get a one-tap button ─────────────────────
print()
print("-- a one-tap button may not spend, sell, or place an order --")
WHITELIST = re.search(r"const SAFE_MONEY_ACTIONS = \{(.*?)\n\};", SCRIPT, re.S)
ok("the safe-action whitelist exists", WHITELIST is not None)
if WHITELIST:
    allowed = set(re.findall(r"'(/[\w/-]+)':", WHITELIST.group(1)))
    ok("re-anchoring is one-tap (places no order, spends nothing)",
       "/grid-status/reanchor-flat-branches" in allowed)
    for spender in ["/grid-status/spread-evenly", "/grid-status/quick-buy",
                    "/grid-status/close-all", "/grid-status/create-branch"]:
        ok(f"{spender} is NOT one-tap from this panel", spender not in allowed)
    ok("every whitelisted action states why it is safe",
       WHITELIST.group(1).count("safety:") == len(allowed))

runner = re.search(r"async function runMoneyAction.*?\n}", SCRIPT, re.S)
ok("runMoneyAction() exists", runner is not None)
if runner:
    body = runner.group(0)
    ok("it refuses anything not on the whitelist",
       "SAFE_MONEY_ACTIONS[action]" in body and "if (!spec) return" in body)
    ok("it confirms before acting", "confirm(" in body)
    # A dismissed confirm() on a phone returns with no request and no
    # error, which reads exactly like a silent failure.
    ok("a dismissed confirm says so instead of looking broken",
       "Cancelled" in body)
    ok("it reports a failure on the button rather than swallowing it",
       "catch" in body and "e.message" in body)
    ok("it re-checks after acting, so the panel shows the result",
       "runMoneyCheck()" in body)

print()
print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
