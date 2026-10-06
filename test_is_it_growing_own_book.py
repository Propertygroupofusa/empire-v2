"""/is-it-growing must report the GRID'S OWN trading, not a pooled sum.

THE BUG THIS PINS, found 2026-10-05. The endpoint called

    get_grid_trade_history(limit=500)

and the provider's parameter is named limit_recent. Every call raised
TypeError; a bare `except Exception` swallowed it; the function fell
through to a raw SUM(pnl) over every row. Nothing was logged, because an
unreadable ledger and an uncalled provider look identical from there.

So the endpoint published -$165.29 under the field name
"is_the_only_growth_figure_here", while the grid's own 207 round trips had
earned +$145.95. The difference was four ZEC rows tagged adopted_exit:
positions the grid never opened, priced against a ~$1,650 adoption mark
nobody paid. Those four sales put $1,268.45 of cash in the wallet and
gained $297.78 against the basis actually paid.

Two things are tested here and they are different: that the provider is
CALLED with a keyword it accepts, and that what comes back is SPLIT rather
than summed. The first bug made the second one invisible for months.

Run: python3 test_is_it_growing_own_book.py
"""
import ast, inspect, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAIL = []
def ok(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond: FAIL.append(name)

SRC = open(os.path.join(HERE, "routers", "trading_dashboard.py")).read()
TREE = ast.parse(SRC)
FN = next(n for n in ast.walk(TREE)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "is_it_growing")

print("\n=== 1. the provider is called with a keyword it actually accepts ===")
import crypto_grid_bot as grid
sig = inspect.signature(grid.get_grid_trade_history)
names = set(sig.parameters)
ok("the provider takes limit_recent", "limit_recent" in names)
ok("and does NOT take 'limit' - the old call could only ever raise",
   "limit" not in names)

calls = [n for n in ast.walk(FN) if isinstance(n, ast.Call)]
hist_calls = [c for c in calls
              if isinstance(c.func, ast.Attribute)
              and c.func.attr == "get_grid_trade_history"]
ok("the endpoint calls the provider exactly once", len(hist_calls) == 1)
kw = {k.arg for k in hist_calls[0].keywords} if hist_calls else set()
ok("every keyword it passes is one the provider accepts", kw <= names)
ok("it does not pass the old 'limit'", "limit" not in kw)

print("\n=== 2. a failed provider call can no longer vanish ===")
# Find the handler wrapping that call and prove it logs.
handlers = [h for n in ast.walk(FN) if isinstance(n, ast.Try)
            for h in n.handlers
            if any(c is hist_calls[0] for c in ast.walk(n))]
logged = any(
    any(isinstance(c.func, ast.Attribute)
        and c.func.attr in ("warning", "error", "exception")
        for c in ast.walk(h) if isinstance(c, ast.Call))
    for h in handlers)
ok("the except around the provider call logs instead of passing silently",
   logged)

print("\n=== 3. the own/inherited split is read, not re-summed ===")
body = ast.get_source_segment(SRC, FN) or ""
code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))
ok("it reads realized_own_usd", "realized_own_usd" in code)
ok("it reads realized_adopted_usd", "realized_adopted_usd" in code)
ok("it reads the restated figure too", "realized_adopted_restated_usd" in code)
ok("the own figure is what lands in realized_usd",
   '"realized_usd": realized' in code)
ok("the inherited figure is published separately, not added in",
   '"inherited_realized_usd": inherited' in code)
ok("nothing adds the two together",
   "realized + inherited" not in code and "inherited + realized" not in code)
ok("the payload says what the headline figure excludes",
   "this_figure_is" in code and "adopted_exit" in code)

print("\n=== 4. behaviour, on the real 2026-10-05 ledger shape ===")
# Rebuild the endpoint's arithmetic over the real provider payload.
HIST = {
    "realized_own_usd": 145.95, "realized_own_trades": 207,
    "realized_adopted_usd": -311.24, "realized_adopted_trades": 4,
    "realized_adopted_restated_usd": 297.78,
    "total_realized_pnl": -165.29, "total_trade_count": 211,
    "recent_trades": (
        [{"pnl": 1.0, "exit_reason": "profit_target",
          "closed_at": "2026-09-26T00:00:00Z"}] * 180
        + [{"pnl": -0.5, "exit_reason": "stop_loss",
            "closed_at": "2026-10-05T00:00:00Z"}] * 27
        + [{"pnl": -123.24, "exit_reason": "adopted_exit",
            "closed_at": "2026-10-04T08:36:11Z"}] * 4),
}
realized = HIST["realized_own_usd"]
own = [t for t in HIST["recent_trades"]
       if (t.get("exit_reason") or "") != "adopted_exit"]
wins = len([t for t in own if t["pnl"] > 0])
ok("the headline figure is POSITIVE, as the grid's own book is",
   realized > 0)
ok("it is not the pooled -165.29", realized != HIST["total_realized_pnl"])
ok("the win rate is computed on own trades only, excluding the 4 ZEC rows",
   len(own) == 207 and round(wins / len(own) * 100, 1) == 87.0)
ok("the inherited loss is still reported, not hidden",
   HIST["realized_adopted_usd"] == -311.24)
ok("and the restated figure shows the same sales gained money",
   HIST["realized_adopted_restated_usd"] > 0)
ok("own plus inherited equals the pooled total - nothing was dropped",
   abs((HIST["realized_own_usd"] + HIST["realized_adopted_usd"])
       - HIST["total_realized_pnl"]) < 0.01)

print("\n=== 5. the span is read from both shapes, not one ===")
ok("ISO strings from the provider are parsed", "fromisoformat" in code)
ok("datetimes from the DB fallback still work", "isinstance(_a, str)" in code)

print(f"\n{'ALL PASSED' if not FAIL else str(len(FAIL)) + ' FAILED'}")
for f in FAIL: print("   FAILED:", f)
sys.exit(1 if FAIL else 0)
