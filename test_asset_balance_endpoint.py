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

    # WHY THIS IS NO LONGER "every handler raises".
    #
    # There are two venue reads now. The census read is the one the whole
    # response is built on, so its failure must abort. The direct
    # per-currency read is a refinement: when it fails the endpoint can
    # still return the census figures, PROVIDED it says so and reports no
    # number. So the property is not "always raise" - it is that a failed
    # read never turns into a value. Stated as never-does-Y, because a
    # guard that only checks the one correct handler passes a mutant that
    # adds a wrong one beside it.
    ok("at least one handler aborts outright, and no aborting handler returns",
       any(_handler_raises_and_never_returns(h) for h in handlers)
       and all(not any(isinstance(x, ast.Return) for x in ast.walk(h))
               for h in handlers if any(isinstance(x, ast.Raise)
                                        for x in ast.walk(h))),
       "an early return above an unreachable raise is exactly the bug")
    _num_in_handler = [
        h for h in handlers
        for a in ast.walk(h)
        if isinstance(a, ast.Assign)
        and isinstance(a.value, ast.Constant)
        and isinstance(a.value.value, (int, float))
        and not isinstance(a.value.value, bool)
    ]
    ok("NO handler ever substitutes a number for a read that failed",
       not _num_in_handler,
       "assigning 0.0 (or any figure) on the failure path is precisely how "
       "an UNKNOWN becomes a zero - the failure must carry a reason, not a "
       "quantity")
    ok("an unavailable account list also raises", len(raises) >= 3,
       "not available, bad currency and the exception path each need one")

    # EXACTLY ONE RETURN, AND IT IS THE SUCCESS RETURN.
    #
    # The handler-scoped rule above missed a whole family of the same bug:
    # `if not bal.get("available"): return {...}` sits in a plain `if`, not
    # an except clause, so "no handler returns" passed a mutant that put an
    # early return above the unreachable raise. Every failure path in this
    # endpoint must abort, so there is no legitimate second return, and
    # counting them is the one formulation that covers every block shape.
    _returns = [r for r in ast.walk(fn) if isinstance(r, ast.Return)]
    ok("the endpoint has EXACTLY ONE return, so no failure path can answer",
       len(_returns) == 1,
       f"found {len(_returns)} - any extra one is a path that answers instead "
       f"of aborting, which is how an unreadable venue becomes a balance")
    if len(_returns) == 1 and isinstance(_returns[0].value, ast.Dict):
        _rk = [k.value for k in _returns[0].value.keys
               if isinstance(k, ast.Constant)]
        ok("and that one return is the readable-success payload",
           "readable" in _rk and "verdict" in _rk)

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
    ok("presence in the census map is an explicit field, not inferred from a null",
       "census_map_lists_this_account" in keys,
       "and it is named for the MAP, not the venue: the first version called "
       "this venue_lists_this_account while it only ever described a map that "
       "drops any account where available + hold is zero")
    ok("the field name never claims the census map speaks for the venue",
       "venue_lists_this_account" not in keys,
       "that name asserted the venue had been consulted about existence when "
       "only the filtered map had")
    ok("venue-level absence is reported as its own field",
       "venue_lists_no_such_account" in keys,
       "absent-from-the-map and absent-from-the-venue are different facts and "
       "the filter cannot tell them apart")
    ok("the unfiltered per-currency read is reported, value and reason both",
       {"direct_available_units", "direct_read_reason"} <= keys,
       "this is the only read that can distinguish an account holding zero "
       "from no account at all")
    ok("a disagreement between the two reads is surfaced, not resolved",
       "reads_disagree" in keys,
       "picking one silently is how a disagreement becomes an unexamined fact")

    # The word "absence" may only be reached via the direct read. If the
    # verdict can say it off the census flag alone, the original bug is back.
    _absent_flag = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id.lstrip("_").startswith("absent"):
                    _absent_flag = n.value
    ok("the absence verdict is computed from the direct read, never the map",
       _absent_flag is not None
       and "direct" in (ast.dump(_absent_flag) or "")
       and "present" not in (ast.dump(_absent_flag) or ""),
       "the census map's own flag cannot license the word absent")
    ok("it reports how many accounts were scanned",
       "accounts_seen" in keys,
       "an absence is only real if the scan was complete")

    # WHETHER A CURRENCY SPANS MORE THAN ONE ACCOUNT, as a countable fact.
    #
    # The two reads on this page resolve such a currency DIFFERENTLY:
    # fetch_balances sums across accounts, get_asset_balance returns the
    # first match and stops. The direct read would then under-report - and
    # it is the read that gates the sell-refusal path and the "nothing
    # sellable" branch, so under-reporting there refuses to sell coin that
    # exists.
    #
    # Reported as counts, not a verdict, and deliberately NOT yet acted on:
    # summing in get_asset_balance would be wrong if the extra accounts are
    # separate portfolios, because it would then OVERSTATE what is sellable
    # from the trading portfolio. Measure before changing a live sell path.
    ok("it reports how many distinct CURRENCIES were seen",
       "currencies_seen" in keys,
       "accounts_seen alone cannot say whether any currency is duplicated")
    ok("and the difference between the two is surfaced directly",
       "accounts_exceed_currencies" in keys,
       "zero rules the problem out for this account; non-zero says look")
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
