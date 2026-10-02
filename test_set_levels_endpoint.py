"""Is the set-levels endpoint safe in the ways that matter?

Three failures it is written against, all of which would be silent:
a dry run that writes, an apply that sets a count below the branch's own
open slices (the parked condition it exists to relieve), and an apply that
writes a plan the planner refused.
"""
import ast

FAILED = []


def ok(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


def before(haystack, first, second):
    """True only if BOTH appear and `first` comes first.

    A missing substring must FAIL this check, not raise: str.index raising
    ends the run and the remaining checks never report at all. Found that
    the first time a negative control was tried - the suite crashed instead
    of naming what broke, which is a weaker signal than a plain FAIL.
    """
    i, j = haystack.find(first), haystack.find(second)
    return i != -1 and j != -1 and i < j


SRC = open("routers/trading_dashboard.py").read()
tree = ast.parse(SRC)
fn = next((n for n in ast.walk(tree)
           if isinstance(n, ast.AsyncFunctionDef)
           and n.name == "set_grid_branch_levels_endpoint"), None)
ok("the endpoint exists", fn is not None)
body = ast.unparse(fn) if fn else ""

print("\n[1] the dry run cannot write")
ok("dry_run defaults to True",
   "dry_run: bool = True" in SRC)
ok("it returns before any session is opened on the dry path",
   before(body, "if payload.dry_run", "get_session_factory"))
ok("the dry path says PREVIEW ONLY", "PREVIEW ONLY" in body)
ok("only ONE place writes num_levels", body.count("row.num_levels =") == 1)
ok("that write is after the dry-run return",
   before(body, "if payload.dry_run", "row.num_levels ="))

print("\n[2] it only applies what the planner approved")
ok("it filters on r.get('ok')", "if r.get('ok')" in body)
ok("it writes from `ready`, not from every plan",
   "for r in ready" in body)
ok("an empty ready list writes nothing and says so",
   "no plan was applicable" in body)

print("\n[3] it re-checks the one thing that must never be written")
ok("it compares levels_after against open_slices before writing",
   "r['levels_after'] < (r['open_slices'] or 0)" in body
   or 'r["levels_after"] < (r["open_slices"] or 0)' in body)
ok("...and that check sits BEFORE the write",
   before(body, "levels_after'] < (r['open_slices'", "row.num_levels ="))
ok("a missing row is skipped, not crashed on", "if row is None" in body)

print("\n[4] it changes nothing else")
for forbidden in ("grid_pct", "allocated_usd =", "reference_price",
                  "stop_pct", "stop_loss_pct_override", "place_order",
                  "create_order", "market_market_ioc", "active ="):
    ok(f"it never touches {forbidden!r}", forbidden not in body)
ok("it honours STOP_TRADING", "STOP_TRADING" in body)
ok("it is a POST, so the write guard covers it",
   '@router.post("/grid-status/set-levels")' in SRC)

print("\n[5] end to end on the live branches")
import json
import branch_levels
g = json.load(open("/tmp/claude-0/-home-user-Delfina/"
                   "8bc5b730-e02d-506c-9c98-0f6519adc72d/scratchpad/g11.json"))
by = {b["product_id"]: b for b in g["branches"]}
r = branch_levels.plan(by["LINK-USD"], 6)
ok("LINK at 6 levels is READY and opens 2 rungs",
   r["ok"] and r["rungs_it_could_actually_open"] == 2)
r2 = branch_levels.plan(by["NEAR-USD"], 6)
ok("NEAR at 6 levels opens 0 rungs and says so",
   r2["rungs_it_could_actually_open"] == 0
   and r2.get("buys_nothing_without_more_allocation") is True)
z = branch_levels.plan(by["ZEC-USD"], 6)
ok("ZEC (7 slices on 3 levels) is allowed up to 6? no - 6 < 7 open, REFUSED",
   z["status"] == "REFUSED")
ok("ZEC at 7 levels is permitted but opens 0 rungs (it is -$68.83 over)",
   branch_levels.plan(by["ZEC-USD"], 7)["rungs_it_could_actually_open"] == 0)
m = branch_levels.plan_many(g["branches"], {"LINK-USD": 6, "NEAR-USD": 6})
ok("plan_many covers both", len(m["plans"]) == 2 and not m["missing"])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")
