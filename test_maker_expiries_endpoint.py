"""The maker-expiry read endpoint must not repeat this router's old mistakes.

WHY THIS FILE EXISTS. When maker-ONLY mode is on and a resting order does not
fill in its window, the grid cancels it and HOLDS the slice rather than paying
the taker leg. Deliberate - but the branch then retries indefinitely, so a
slice can sit unsold through a rise of any size. The only trace is a
GridMakerExpiry row, and those rows were written and never read: no endpoint
exposed them. A system that records the answer and cannot say it is the same
defect as a fallback literal.

Three prior mistakes in this very router are asserted against here:
  - a wide window with a small LIMIT served the OLDEST rows (trade-history)
  - a failed read returned an empty result, which reads as "nothing happened"
  - a params dict was checked by grepping source text, and the guard matched
    the docstring that explained the bug
so everything below is asserted on the PARSED TREE.

fastapi is not installed in this environment, so the endpoint is examined as
an AST rather than called.
"""
import ast
import sys

SRC_PATH = "routers/trading_dashboard.py"
with open(SRC_PATH, encoding="utf-8") as fh:
    SOURCE = fh.read()
TREE = ast.parse(SOURCE)

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


def find_route(path_fragment, method):
    """The function carrying @router.<method>("<path>"), found by decorator."""
    for node in ast.walk(TREE):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            f = dec.func
            if not (isinstance(f, ast.Attribute) and f.attr == method):
                continue
            for a in dec.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                        and path_fragment in a.value:
                    return node, a.value
    return None, None


fn, route_path = find_route("maker-expiries", "get")

print("the route")
ok("a GET route for maker-expiries exists", fn is not None)
ok("it is mounted under grid-status",
   bool(route_path) and route_path.startswith("/grid-status/"),
   f"path is {route_path!r}")

if fn is not None:
    args = [a.arg for a in fn.args.args]
    ok("it takes product_id, hours and limit",
       {"product_id", "hours", "limit"} <= set(args), f"args are {args}")

    # ---- the wide-window / small-limit trap -------------------------------
    # trade-history capped rows WITHOUT ordering newest-first, so a 30-day
    # window with limit 50 returned the FIRST 50 days, not the last.
    orders = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "order_by"]
    ok("the query is ordered", len(orders) >= 1)
    desc_on_expired = False
    for o in orders:
        for a in o.args:
            # expecting <Model>.expired_at.desc()
            if (isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
                    and a.func.attr == "desc"
                    and isinstance(a.func.value, ast.Attribute)
                    and a.func.value.attr == "expired_at"):
                desc_on_expired = True
    ok("ordered by expired_at DESCENDING, so a small limit keeps the NEWEST rows",
       desc_on_expired,
       "a wide window with a small limit otherwise serves the oldest rows")

    limits = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "limit"]
    ok("a limit is applied", len(limits) >= 1)

    # ---- a gap is not a zero ---------------------------------------------
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    raising = [h for h in handlers
               if any(isinstance(x, ast.Raise) for x in ast.walk(h))]
    returning_empty = []
    for h in handlers:
        for x in ast.walk(h):
            if isinstance(x, ast.Return) and isinstance(x.value, (ast.List, ast.Dict)):
                returning_empty.append(x)
    ok("a failed read RAISES rather than returning a value",
       len(raising) >= 1 and not returning_empty,
       "an unreadable table returning [] reads as 'nothing expired'")

    # ---- bounds ----------------------------------------------------------
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    ok("hours and limit are clamped", {"min", "max"} <= calls,
       "an unbounded window or limit is a way to hang the database")

    # ---- the honesty fields ----------------------------------------------
    keys = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    ok("the response says what an EMPTY result does not mean",
       "an_empty_result_is" in keys,
       "absence of rows is UNKNOWN, not evidence a sell filled")
    ok("the response says what a ROW means", "a_row_is" in keys)
    ok("the response reports whether it was truncated", "truncated" in keys)
    ok("the response carries a window and a timestamp",
       {"window_hours", "as_of"} <= keys)
    ok("rows are grouped so one product's repeats are visible",
       "by_product_and_side" in keys,
       "repeated sell holds on one product is the QNT question")

    # ---- the filter actually filters -------------------------------------
    ifs = [n for n in ast.walk(fn) if isinstance(n, ast.If)]
    filtered = any("product_id" in {x.id for x in ast.walk(n.test)
                                    if isinstance(x, ast.Name)} for n in ifs)
    ok("product_id narrows the query when given", filtered)

# The model must be imported at module scope, not locally - a local import that
# shadows a module-level name is a bug this repo has a guard for.
mod_imports = set()
for node in TREE.body:
    if isinstance(node, ast.ImportFrom) and node.module == "models":
        mod_imports |= {a.name for a in node.names}
ok("GridMakerExpiry is imported at module scope", "GridMakerExpiry" in mod_imports)

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all maker-expiry endpoint checks passed")
