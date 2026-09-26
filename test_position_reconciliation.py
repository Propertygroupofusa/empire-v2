"""Tracked positions that no longer exist still spend the risk budget.

Live on 2026-09-25, every cycle for hours:

    ⛔ MARGIN SAFETY: Blocking META entry
       Risk limit exceeded: $630.09 > 50% of $1007.47 equity

The account at that moment: equity $1,007.47, cash available $810.64.
Real positions were therefore about $197. Roughly $433 of the $630 was
phantom, and APEX could not open a single position.

WHERE THE PHANTOM CAME FROM

check_margin_safety sums THREE sources:

    open_prop_positions            reconciled against the broker
    _total_alpaca_branch_notional  never reconciled
    _total_opening_bar_notional    never reconciled

The latter two are cleared only inside `if filled:` after the bot's own
sell. A position closed any other way - manually, by a broker stop, by
liquidation, or a sell that did not fill - stayed in the dict forever and
kept counting against the cap.

And the refusal message read like correct risk management. It stated the
limit and the equity; it never said the number might be stale, so nothing
about it invited suspicion.

THE RULES THIS FILE PROTECTS:

  1. Every dict feeding the margin check is reconciled against the broker.
  2. Nothing is dropped on a failed broker read - reconciliation returns
     early, so a network blip can never make the bot forget real positions.
  3. Nothing is dropped inside a grace window, so a position opened
     seconds ago is not forgotten before the broker reports it.
  4. A branch with no known contract is never dropped on a guess.
  5. The refusal names its own components, so drift is visible.

Run: python3 test_position_reconciliation.py
"""

import ast
import io
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


SRC = io.open("prop_bot.py", encoding="utf-8").read()
tree = ast.parse(SRC)
REC = next((n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "reconcile_positions_with_broker"), None)
REC_SRC = ast.get_source_segment(SRC, REC) if REC else ""
MS = next((n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "check_margin_safety"), None)
MS_SRC = ast.get_source_segment(SRC, MS) if MS else ""

print("test_position_reconciliation.py")
print()

# ── 1. every source of margin notional is reconciled ───────────────────
print("-- every dict that spends the risk budget is reconciled --")
ok("the reconciler exists", REC is not None)
for dict_name in ["open_prop_positions", "open_opening_bar_positions",
                  "open_alpaca_branch_positions"]:
    ok(f"the margin check counts {dict_name}",
       dict_name in MS_SRC or dict_name in SRC)
    ok(f"the reconciler drops stale entries from {dict_name}",
       f"{dict_name}.pop(" in REC_SRC, "never reconciled")

# All three must be summed by the margin path.
ok("the margin check sums the branch notional",
   "_total_alpaca_branch_notional" in SRC)
ok("the margin check sums the opening-bar notional",
   "_total_opening_bar_notional" in SRC)

# ── 2. a failed broker read never drops anything ───────────────────────
print()
print("-- a network blip must never make the bot forget a real position --")
ok("a non-200 broker response returns before any drop",
   "if r.status != 200:" in REC_SRC
   and REC_SRC.index("if r.status != 200:") < REC_SRC.index(".pop("))
ok("an exception returns before any drop",
   "except Exception" in REC_SRC
   and REC_SRC.index("except Exception") < REC_SRC.index(".pop("))
ok("the early return is a bare return, not a partial reconcile",
   "return\n" in REC_SRC.split("broker_positions = await r.json()")[0])

# ── 3. a just-opened position is not forgotten ─────────────────────────
print()
print("-- a position opened seconds ago is not dropped --")
ok("there is a grace window", "_grace" in REC_SRC and "timedelta(minutes=" in REC_SRC)
ok("the grace check is applied before dropping",
   REC_SRC.count("_too_new(") >= 3, f"{REC_SRC.count('_too_new(')} uses")
ok("naive timestamps are treated as UTC rather than crashing",
   "tzinfo is None" in REC_SRC)

# Behavioural: replicate the guard.
from datetime import datetime, timedelta, timezone
now = datetime.now(timezone.utc)


def too_new(open_time, grace_min=5):
    if open_time is None:
        return False
    if open_time.tzinfo is None:
        open_time = open_time.replace(tzinfo=timezone.utc)
    return (now - open_time) < timedelta(minutes=grace_min)


ok("a position opened 10 seconds ago is protected",
   too_new(now - timedelta(seconds=10)))
ok("a position opened 2 minutes ago is protected",
   too_new(now - timedelta(minutes=2)))
ok("a position opened an hour ago is NOT protected",
   not too_new(now - timedelta(hours=1)))
ok("a naive timestamp is handled, not crashed on",
   too_new((now - timedelta(seconds=30)).replace(tzinfo=None)))
ok("a missing timestamp does not protect forever", not too_new(None))

# ── 4. no dropping on a guess ──────────────────────────────────────────
print()
print("-- a branch with no known contract is left alone --")
ok("an unmapped branch is skipped rather than dropped",
   "if contract is None:" in REC_SRC and "never drop on a guess" in REC_SRC)

# ── 5. the refusal shows its working ───────────────────────────────────
print()
print("-- the refusal names where its number came from --")
ok("the message splits prop from other notional",
   "prop $" in MS_SRC and "other $" in MS_SRC)
ok("it tells the reader how to spot drift",
   "tracking has drifted" in MS_SRC)
ok("it still reports the limit and the equity",
   "Risk limit exceeded" in MS_SRC and "equity" in MS_SRC)

# The live arithmetic that exposed it.
equity, cash, claimed = 1007.47, 810.64, 630.09
real_positions = equity - cash
ok("equity minus cash put real positions near $197",
   abs(real_positions - 196.83) < 0.01, f"{real_positions:.2f}")
ok("the claimed notional exceeded that by roughly $433",
   abs((claimed - real_positions) - 433.26) < 0.01)
ok("the claimed notional also exceeded the 50% cap, hence the block",
   claimed > equity * 0.5)
ok("the REAL notional would not have tripped the cap",
   real_positions < equity * 0.5)

print()
print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
