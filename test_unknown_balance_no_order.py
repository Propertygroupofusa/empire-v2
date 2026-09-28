"""An unknown balance must never generate an order.

THE RULE, stated by the account owner: "an unknown balance can never generate
an order. Speed comes after the accounting and inventory gates, not before
them."

WHAT WAS WRONG. Both sell paths clamped the size like this:

    real_balance, _ = await get_asset_balance(session, base_currency)
    if real_balance is not None and real_balance < qty:
        qty = real_balance

get_asset_balance returns (None, reason) when the read FAILS - a 401, a
timeout, an HTTP error, or the currency simply not being listed. On that
None the condition is False, the clamp is skipped, and the order goes out at
the TRACKED quantity: the one number already known to drift away from the
wallet. The reason was discarded into `_`, so nothing logged why. The buy
path already failed closed on the same condition ("real balance unavailable
... skipping this cycle"); only the sell side fell through.

THE ONE EXCEPTION. close_all_grid_branches is a protection, and protections
fail open: refusing there leaves a live position unprotected. It must opt in
explicitly via allow_unverified_balance and the risk is logged.

Asserted on the PARSED TREE. A guard that matches source text in this repo
has already fired on a docstring that explained a bug.
"""
import ast
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


def load(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    return src, ast.parse(src)


ENGINE_SRC, ENGINE = load("crypto_btc_compound_bot.py")
GRID_SRC, GRID = load("crypto_grid_bot.py")


def func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def guards_none_balance(fn):
    """True when the function has an `if <balance> is None:` whose body can
    stop the order - a return, or a raise - rather than falling through."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        t = node.test
        if not (isinstance(t, ast.Compare) and len(t.ops) == 1
                and isinstance(t.ops[0], ast.Is)
                and isinstance(t.comparators[0], ast.Constant)
                and t.comparators[0].value is None):
            continue
        names = {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        if not any("balance" in n for n in names):
            continue
        if any(isinstance(x, (ast.Return, ast.Raise)) for x in ast.walk(node)):
            return True
    return False


def discards_the_reason(fn):
    """True if the balance call still unpacks its reason into `_`."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Tuple):
                names = [e.id for e in tgt.elts if isinstance(e, ast.Name)]
                if len(names) == 2 and names[1] == "_" and \
                        any("balance" in n for n in names):
                    return True
    return False


print("place_market_sell")
pms = func(ENGINE, "place_market_sell")
ok("it exists", pms is not None)
if pms is not None:
    args = [a.arg for a in pms.args.args]
    ok("it takes allow_unverified_balance", "allow_unverified_balance" in args,
       f"args are {args}")
    defaults = pms.args.defaults
    ok("and it defaults to REFUSING",
       any(isinstance(d, ast.Constant) and d.value is False for d in defaults),
       "the safe default must be to not send the order")
    ok("an unreadable balance can stop the order", guards_none_balance(pms),
       "a None balance must not fall through to the order")
    ok("the failure reason is kept, not discarded into _",
       not discards_the_reason(pms),
       "nothing could say WHY the balance was unknown")

print("\nplace_maker_sell")
pmk = func(ENGINE, "place_maker_sell")
ok("it exists", pmk is not None)
if pmk is not None:
    ok("an unreadable balance stops the order", guards_none_balance(pmk))
    ok("it has NO unverified escape hatch",
       "allow_unverified_balance" not in [a.arg for a in pmk.args.args],
       "the opportunistic path always has a next cycle")
    ok("the failure reason is kept", not discards_the_reason(pmk))

print("\nthe one permitted exception")
callers = []
for node in ast.walk(GRID):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "place_market_sell"):
        callers.append(node)
ok("the grid calls place_market_sell somewhere", len(callers) >= 1)
opted_in = [c for c in callers
            if any(k.arg == "allow_unverified_balance" for k in c.keywords)]
ok("exactly ONE caller opts in to an unverified sell", len(opted_in) == 1,
   f"{len(opted_in)} callers pass it; only the forced close-all may")
if opted_in:
    src = ast.get_source_segment(GRID_SRC, opted_in[0]) or ""
    ok("and it is the close-branch/close-all path",
       "close_branch" in src or "close_all" in src,
       f"segment was {src[:120]!r}")

print()
if failures:
    print(f"FAILED {len(failures)}: {failures}")
    sys.exit(1)
print("all unknown-balance checks passed")
