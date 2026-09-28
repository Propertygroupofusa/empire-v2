"""The direct asset-balance read must never turn an UNKNOWN into a zero.

WHY THIS FILE EXISTS. /account-census drops assets it cannot price and rolls
sub-dust ones into an unnamed bucket. Both are correct for valuing an account
and both make it useless for "is X held at all". That ambiguity was read as a
zero on QNT-USD and the wrong conclusion was reported to the owner.
coin_tracked_is_held had it right - it reports a coin missing from the wallet
map as UNREADABLE, not short - and the census was simply asked a question it
cannot answer.

So this endpoint's whole value is the distinction the census cannot make, and
these checks defend exactly that: an unreadable venue must RAISE, never return
a zero or a false, and the payload must separate held from available.

fastapi is not installed here, so the route is examined as an AST.
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


def find_route(fragment, method):
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
                        and fragment in a.value:
                    return node, a.value
    return None, None


fn, path = find_route("asset-balance", "get")

print("the route")
ok("a GET route for asset-balance exists", fn is not None)
ok("it is mounted under grid-status",
   bool(path) and path.startswith("/grid-status/"), f"path is {path!r}")

if fn is not None:
    args = [a.arg for a in fn.args.args]
    ok("it takes a currency", "currency" in args, f"args are {args}")

    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    ok("it raises on a bad or missing input", len(raises) >= 1)

    # The whole point: an unreadable venue must NOT come back as a balance.
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    ok("the venue call is wrapped", len(handlers) >= 1)
    # A handler that merely CONTAINS a raise is not enough - an early
    # `return {...}` above an unreachable raise satisfies that and is exactly
    # the bug. The property is that the handler returns NOTHING, ever.
    def _handler_raises_and_never_returns(h):
        has_raise = any(isinstance(x, ast.Raise) for x in ast.walk(h))
        has_return = any(isinstance(x, ast.Return) for x in ast.walk(h))
        return has_raise and not has_return

    ok("an exception RAISES and never returns a value",
       bool(handlers) and all(_handler_raises_and_never_returns(h) for h in handlers),
       "returning a value on an unreadable venue is how UNKNOWN becomes zero")
    ok("an unavailable account list also raises", len(raises) >= 3,
       "not available, bad currency and the exception path each need one")

    keys = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    ok("held and available are reported SEPARATELY",
       {"held_units", "available_units"} <= keys,
       "held counts units behind resting orders; available is what can sell")
    ok("the lock is reported", "locked_units" in keys)
    ok("presence is an explicit field, not inferred from a null",
       "venue_lists_this_account" in keys)
    ok("it reports how many accounts were scanned",
       "accounts_seen" in keys,
       "an absence is only real if the scan was complete")
    ok("it states what it does NOT answer",
       "what_this_does_not_say" in keys)
    ok("it carries a verdict in words", "verdict" in keys)

    # It must read the unfiltered balance source, not the census view.
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    ok("it calls fetch_balances, not census", "fetch_balances" in attrs,
       "census filters unpriced and dust - that is the bug this endpoint exists for")
    ok("it does NOT call the filtered census",
       "census" not in attrs and "census_cached" not in attrs)

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all asset-balance endpoint checks passed")
