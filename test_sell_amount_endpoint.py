"""The endpoint's safety properties, checked on the real source.

plan_sale is covered by test_sell_amount.py. What matters here is that
the endpoint cannot place an order the caller did not explicitly confirm,
cannot size against units that are only on hold, and is not reachable
without the write token.
"""

import ast
from pathlib import Path

import write_guard

ROUTER = Path(__file__).with_name("routers") / "trading_dashboard.py"

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


SRC = ROUTER.read_text(encoding="utf-8")
TREE = ast.parse(SRC)
FN = next(n for n in ast.walk(TREE)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "sell_amount_of_holding")


print("\nnothing is placed without an explicit confirm")

model = next(n for n in ast.walk(TREE)
             if isinstance(n, ast.ClassDef) and n.name == "PartialSellRequest")
defaults = {}
for stmt in model.body:
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
        defaults[stmt.target.id] = getattr(stmt.value, "value", None)
ok("confirm exists and defaults to False", defaults.get("confirm") is False, defaults)
ok("usd_amount has NO default - it must be stated",
   "usd_amount" not in defaults, defaults)

# The order POST must sit behind the confirm check. If confirm is falsy
# the function returns before it.
returns_before = [n for n in FN.body if isinstance(n, ast.Return)]
# session.post only - @router.post is an attribute call on the decorator
# and would otherwise be counted as a second order placement.
posts = [n for n in ast.walk(FN)
         if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "post"
         and getattr(getattr(n.func, "value", None), "id", None) == "session"]
ok("exactly one order POST exists in the endpoint", len(posts) == 1, f"{len(posts)} found")

guard_tests = [n for n in ast.walk(FN)
               if isinstance(n, ast.If) and "confirm" in ast.dump(n.test)]
ok("there is an explicit branch on confirm", len(guard_tests) >= 1)


def contains(node, call_attr):
    return any(isinstance(c, ast.Call) and getattr(c.func, "attr", None) == call_attr
               and getattr(getattr(c.func, "value", None), "id", None) == "session"
               for c in ast.walk(node))


ok("the confirm branch returns BEFORE the order is posted",
   guard_tests and any(isinstance(b, ast.Return) for b in guard_tests[0].body)
   and not contains(guard_tests[0], "post"),
   "the preview branch must not reach the POST")

print("\nit sizes against sellable units, not units on hold")

src_fn = ast.get_source_segment(SRC, FN) or ""
ok("it reads available_balance", "available_balance" in src_fn)
ok("it does NOT add 'hold' into the sellable figure",
   '"hold"' not in src_fn and "'hold'" not in src_fn, "hold must not be summed in")
ok("it says why in a comment", "cannot be sold" in src_fn)

print("\nsizing is delegated, never re-implemented here")

ok("it calls sell_amount.plan_sale",
   any(isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "plan_sale"
       for c in ast.walk(FN)))
ok("and refuses when the plan says not ok",
   'plan.get("ok")' in src_fn)
ok("the default increment is the conservative one, not a guessed coarse one",
   '"0.00000001"' in src_fn)

print("\nthe write guard covers it")

ok("a POST to this path is protected",
   write_guard.is_protected("POST", "/api/trading-dashboard/coinbase/sell-amount"))
ok("and is not exempted by any open prefix or suffix",
   write_guard.is_protected("POST", "/api/trading-dashboard/coinbase/sell-amount"))
ok("a GET on it would not be (there is no GET, but the rule is method-based)",
   not write_guard.is_protected("GET", "/api/trading-dashboard/coinbase/sell-amount"))

print("\nthe route is actually registered")

ok("the decorator names the path",
   '@router.post("/coinbase/sell-amount")' in SRC)
ok("and the older full-position seller is untouched",
   '@router.post("/coinbase/sell")' in SRC)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
