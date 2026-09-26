"""A memory that blocks a winning coin is worse than no memory at all.

bot_learning_engine.py had the right idea and would have been a disaster
wired in as written:

  _update_losing_pattern() created a "losing pattern" on the FIRST red
  trade. check_before_trade() returned safe=False on ANY match, and it
  checked losing patterns BEFORE winning ones. So one red trade blocked a
  coin permanently, whatever its record.

  This fleet closes 75.6% of round trips green - about one in four is red
  BY DESIGN. DOGE is +$12.80 over 15 trades with 4 losses. ETH is +$2.30
  with 2 losses. STX is +$1.78 with 2 losses. Every one of them would have
  been switched off while profitable, within days.

It also stored to bot_learnings.json - a file on Railway's ephemeral disk,
wiped on every redeploy - and no module imported it.

THE RULES THIS FILE PROTECTS:

  1. A verdict cannot turn "avoid" on a small sample, and never on a
     single trade.
  2. A coin that is NET POSITIVE is never "avoid", no matter how many
     individual losses it has.
  3. A lesson cannot block a buy unless enforcement is explicitly on AND
     the sample is large. Default is advisory.
  4. Every failure path fails OPEN. An unreadable memory must never be
     what halts trading.
  5. It persists to Postgres, not to a file.

Run: python3 test_grid_learning.py
"""

import ast
import io
import re
import sys

sys.path.insert(0, ".")
from grid_learning import (MIN_TRADES, MIN_TRADES_TO_BLOCK, lesson_for,
                           verdict_for)

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


print("test_grid_learning.py")
print()

# ── 1. the exact regression: real winning coins stay tradeable ──────────
print("-- the fleet's real winners are never marked avoid --")
# Straight from /grid-status/trade-history on 2026-09-25.
LIVE = [
    ("DOGE-USD", 15, 11, 4, 12.80),
    ("ETH-USD", 9, 7, 2, 2.30),
    ("STX-USD", 13, 11, 2, 1.78),
    ("WIF-USD", 12, 8, 4, 1.06),
    ("ETC-USD", 9, 6, 3, 1.00),
    ("LINK-USD", 7, 5, 2, 0.25),
    ("AAVE-USD", 4, 3, 1, 0.10),
    ("BCH-USD", 3, 2, 1, 0.03),
]
for pid, n, w, l, pnl in LIVE:
    v = verdict_for(n, pnl)
    ok(f"{pid} (+{pnl:.2f} over {n}, {l} losses) is not 'avoid'", v != "avoid", v)
ok("every profitable coin reads 'earning'",
   all(verdict_for(n, p) == "earning" for _, n, _, _, p in LIVE))

# ── 2. a single loss is never a lesson ─────────────────────────────────
print()
print("-- thin evidence is not a verdict --")
ok("one losing trade is 'watch', not 'avoid'", verdict_for(1, -5.0) == "watch")
ok("three losing trades are still 'watch'", verdict_for(3, -20.0) == "watch")
ok(f"{MIN_TRADES - 1} losing trades are still 'watch'",
   verdict_for(MIN_TRADES - 1, -50.0) == "watch")
ok(f"{MIN_TRADES} losing trades finally reads 'avoid'",
   verdict_for(MIN_TRADES, -50.0) == "avoid")
ok("no trades at all is 'watch'", verdict_for(0, 0.0) == "watch")
ok("a coin up on a big sample is 'earning'", verdict_for(100, 5.0) == "earning")
ok("a coin down on a big sample is 'avoid'", verdict_for(100, -5.0) == "avoid")

# The retired tree's worst coin is the case a memory SHOULD catch.
ok("POL (-392.43 over 87 trips) would read 'avoid'",
   verdict_for(87, -392.43) == "avoid")

# ── 3. blocking needs far more evidence than flagging ──────────────────
print()
print("-- a block strands capital, so it needs a bigger sample --")
ok("the block threshold is stricter than the verdict threshold",
   MIN_TRADES_TO_BLOCK > MIN_TRADES, f"{MIN_TRADES_TO_BLOCK} vs {MIN_TRADES}")

SRC = io.open("grid_learning.py", encoding="utf-8").read()
tree = ast.parse(SRC)
fn = next((n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == "check_before_buy"), None)
body = ast.get_source_segment(SRC, fn) if fn else ""
ok("check_before_buy() exists", fn is not None)
ok("a block requires enforcement to be explicitly on", "enforce" in body)
ok("a block requires a losing total", "total_pnl" in body and "< 0" in body)
ok("a block requires the large sample", "MIN_TRADES_TO_BLOCK" in body)
# Parse the guard instead of slicing the text: the previous version split
# on the first ")" and cut the expression in half, then judged what was
# left. AST says what the code actually does.
blocking_node = None
for node in ast.walk(fn):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "blocking":
                blocking_node = node.value
ok("the block guard exists", blocking_node is not None)
ok("all three conditions are required together (and, never or)",
   isinstance(blocking_node, ast.BoolOp) and isinstance(blocking_node.op, ast.And)
   and len(blocking_node.values) == 3,
   ast.dump(blocking_node)[:90] if blocking_node else "")

# ── 4. every failure path fails open ───────────────────────────────────
print()
print("-- an unreadable memory never halts trading --")
ok("check_before_buy returns allow=True on any exception",
   'except Exception' in body and '"allow": True' in body.split("except Exception")[-1])
enf = ast.get_source_segment(SRC, next(
    n for n in ast.walk(tree)
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    and n.name == "is_enforcement_active"))
ok("enforcement defaults to False when unreadable",
   "return False" in enf.split("except")[-1])
ok("enforcement is OFF by default (needs a positive stored value)",
   "base_capital > 0" in enf)

GRID = io.open("crypto_grid_bot.py", encoding="utf-8").read()
ok("the buy path wraps the memory in its own try/except",
   "memory unavailable" in GRID and "trading anyway" in GRID)
ok("the write path never lets a lesson unwind a real sale",
   "the real trade is unaffected" in GRID)

# ── 5. it is on Postgres, not a file ───────────────────────────────────
print()
print("-- the memory survives a redeploy --")
ok("it stores through the database session factory", "get_session_factory" in SRC)
# Strip docstrings and comments before looking for the old file: this
# module's own docstring NAMES bot_learnings.json to explain what it
# replaced, and the first version of this check failed on that sentence.
CODE_ONLY = ast.unparse(ast.parse(SRC))
for node in ast.walk(ast.parse(SRC)):
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            node.body.pop(0)
            CODE_ONLY = ast.unparse(ast.parse(SRC))
stripped = ast.parse(SRC)
for node in ast.walk(stripped):
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
                and len(node.body) > 1):
            node.body.pop(0)
CODE_ONLY = ast.unparse(stripped)
ok("it uses a real model", "GridLesson" in SRC)
ok("no executable line touches the old JSON file",
   "bot_learnings.json" not in CODE_ONLY)
ok("nothing in the new engine writes to a local file",
   "open(" not in SRC.replace("get_session_factory()", ""))
MODELS = io.open("models.py", encoding="utf-8").read()
ok("the GridLesson table exists", "class GridLesson(Base)" in MODELS)
ok("it is keyed uniquely per coin",
   re.search(r"product_id\s*=\s*Column\(String,\s*unique=True", MODELS) is not None)

# ── 6. it is actually wired in this time ───────────────────────────────
print()
print("-- unlike the file version, something imports it --")
ok("the grid bot records a lesson on every closed round trip",
   "grid_learning.record_closed_trade" in GRID)
ok("the grid bot consults the memory before buying",
   "grid_learning.check_before_buy" in GRID)
ROUTER = io.open("routers/trading_dashboard.py", encoding="utf-8").read()
for ep in ["/grid-status/lessons", "/grid-status/lessons/backfill",
           "/grid-status/lessons/enforce"]:
    ok(f"{ep} is exposed", f'"{ep}"' in ROUTER)
DASH = io.open("family_tree_dashboard.html", encoding="utf-8").read()
ok("the dashboard can show what it learned", "renderLessons" in DASH)
ok("the dashboard can backfill from past trades", "backfillLessons" in DASH)

# ── 7. the lesson reads like a sentence a person can act on ────────────
print()
print("-- the lesson is plain English --")
thin_up = lesson_for("BCH-USD", 3, 2, 1, 0.03)
thin_down = lesson_for("XLM-USD", 3, 0, 3, -2.41)
ok("a thin POSITIVE sample reads provisional, matching its 'earning' badge",
   "up" in thin_up and "pattern rather than a run" in thin_up, thin_up)
ok("a thin NEGATIVE sample says it is not held against the coin",
   "not being held against" in thin_down, thin_down)
ok("the badge and the sentence never contradict on a thin winner",
   verdict_for(3, 0.03) == "earning" and "Too few to conclude" not in thin_up)
thin = thin_down
win = lesson_for("DOGE-USD", 15, 11, 4, 12.80, 0.02)
ok("a winner says keep trading it", "earns" in win and "Keep trading" in win, win)
ok("a winner names the step it earned at", "2.00% step" in win, win)
lose = lesson_for("POL-USD", 87, 13, 74, -392.43, 0.02)
ok("a loser names the real total", "-392.43" in lose, lose)
ok("a loser points at the fee floor rather than just scolding",
   "fee floor" in lose, lose)
ok("no lesson promises a future return",
   not any(w in (thin + win + lose).lower()
           for w in ["will make", "guaranteed", "expected profit", "projected"]))

print()
print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
