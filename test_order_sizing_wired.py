"""No order may be sized against a guessed precision.

WHY IT EXISTS. All three order-placing functions sized with
math.floor(qty * 10**decimals) / 10**decimals, where `decimals` came from
get_product_size_decimals - which returns 8 on ANY failure. Eight is the
most permissive value on the venue, so an unreadable product became an
order sized against a guess. ALGO-USD's real base_increment is 0.1.

These checks are structural: the functions do live I/O against Coinbase,
so what is asserted is the code, by AST. The decision logic itself is
exercised directly in test_execution_quantity.py, which needs no key.

The one thing a substring could never catch was found by this file's own
AST sweep: after the rewrite, place_maker_buy still formatted its payload
with `decimals`, a variable that no longer existed - a NameError on every
buy.
"""
import ast
import io
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


SRC = io.open("crypto_btc_compound_bot.py", encoding="utf-8").read()
TREE = ast.parse(SRC)
FN = {n.name: n for n in ast.walk(TREE)
      if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}
SIZERS = ("place_maker_sell", "place_maker_buy", "place_market_sell")


def seg(name):
    return ast.get_source_segment(SRC, FN[name]) or ""


def loads(name):
    return {n.id for n in ast.walk(FN[name])
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def call_named(name, fname):
    """The ast.Call node for `fname(...)` inside function `name`, or None."""
    for n in ast.walk(FN[name]):
        if isinstance(n, ast.Call):
            f = n.func
            if (getattr(f, "id", None) or getattr(f, "attr", None)) == fname:
                return n
    return None


def kwarg(call, key):
    if call is None:
        return None
    for k in call.keywords:
        if k.arg == key:
            return k.value
    return None


def attrs(name):
    """Every attribute NAME loaded in the function, e.g. plan.reason -> reason."""
    return {n.attr for n in ast.walk(FN[name]) if isinstance(n, ast.Attribute)}


def str_consts(name):
    return [c.value for c in ast.walk(FN[name])
            if isinstance(c, ast.Constant) and isinstance(c.value, str)]


def dict_value_for(name, key):
    """The value node for a given string key in any dict literal in the function."""
    for n in ast.walk(FN[name]):
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value == key:
                    return v
    return None


def calls(name):
    out = set()
    for n in ast.walk(FN[name]):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(getattr(f, "id", None) or getattr(f, "attr", None))
    return out


print("== every sizer exists ==")
for n in SIZERS:
    ok(f"{n} is defined", n in FN)
if any(n not in FN for n in SIZERS):
    print("\nFAILED: a sizer is missing")
    sys.exit(1)

print("== no float floor and no decimal count in the sizing path ==")
for n in SIZERS:
    # AST, not text: the comments in these functions QUOTE the old
    # math.floor line to explain what was wrong, so a substring search
    # matches the explanation and fails correct code.
    ok(f"{n} never loads a `decimals` variable", "decimals" not in loads(n))
    ok(f"{n} does not call math.floor", "floor" not in calls(n))
    ok(f"{n} does not call get_product_size_decimals",
       "get_product_size_decimals" not in calls(n))

print("== every sizer reads the venue's real rules ==")
for n in SIZERS:
    ok(f"{n} calls get_product_rules", "get_product_rules" in calls(n))
    # A rules read that is not checked for None is the fail-open bug again.
    # By AST: an `if <name> is None:` whose body returns None. A text match
    # for "rules is None" would pass on a comment and fail on a rename.
    # The guard must be on the RULES variable specifically. An earlier
    # version accepted any `if X is None: return None`, which the
    # balance-unreadable guard already satisfies - so deleting the rules
    # guard entirely left the check passing. A mutant proved it.
    _rules_names = {a.targets[0].id for a in ast.walk(FN[n])
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                    and isinstance(a.value, ast.Await)
                    and isinstance(a.value.value, ast.Call)
                    and getattr(a.value.value.func, "id", None) == "get_product_rules"}
    ok(f"{n} assigns the rules from a bare get_product_rules await",
       len(_rules_names) == 1, str(_rules_names))
    _guarded = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id in _rules_names
        and isinstance(node.test.ops[0], ast.Is)
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value is None
        and any(isinstance(x, ast.Return) and isinstance(x.value, ast.Constant)
                and x.value.value is None for x in ast.walk(node))
        for node in ast.walk(FN[n]))
    ok(f"{n} refuses when THAT variable is None", _guarded)
    ok(f"{n} names the guess it is refusing to make",
       "against a guess" in seg(n))

print("== the payload uses the planner's own string ==")
for n in SIZERS:
    ok(f"{n} reads .order_size_string", "order_size_string" in attrs(n))
    # The exact regression, by AST: the base_size value must not be an
    # f-string. A correctly floored number re-formatted separately can
    # still come out invalid - that is how `decimals` survived the rewrite
    # in place_maker_buy and would have raised NameError on every buy.
    _bs = dict_value_for(n, "base_size")
    ok(f"{n} sets a base_size value", _bs is not None)
    ok(f"{n} does not build base_size with an f-string",
       not isinstance(_bs, ast.JoinedStr))
    ok(f"{n} builds it from an attribute of the plan",
       isinstance(_bs, ast.Attribute) and _bs.attr == "order_size_string",
       ast.dump(_bs)[:80] if _bs is not None else "none")

print("== the planner is used, not reimplemented ==")
for n in SIZERS:
    ok(f"{n} calls plan_order_quantity", "plan_order_quantity" in calls(n))

print("== get_product_rules never invents a rule ==")
ok("get_product_rules exists", "get_product_rules" in FN)
grs = seg("get_product_rules")
_returns = [r for r in ast.walk(FN["get_product_rules"])
            if isinstance(r, ast.Return)]
_const_returns = [r for r in _returns
                  if isinstance(r.value, ast.Constant) and r.value.value is not None]
ok("it returns no constant fallback", not _const_returns,
   str([getattr(r.value, "value", None) for r in _const_returns]))
ok("a non-200 returns None", "if r.status != 200" in grs and "return None" in grs)
ok("an exception returns None rather than a default",
   "except Exception" in grs)
# AST again, for the same reason twice over: the refusal message is split
# across two string literals by line wrapping, so "no base_increment" never
# appears contiguously; and the docstring EXPLAINS the old 8-decimal
# fallback, so a search for "8" matches the explanation of the bug rather
# than the bug.
_GR = FN["get_product_rules"]
_none_returns = [r for r in ast.walk(_GR) if isinstance(r, ast.Return)
                 and isinstance(r.value, ast.Constant) and r.value.value is None]
ok("it has a refusal for each way the read can fail",
   len(_none_returns) >= 3, f"{len(_none_returns)} None-returns")
# By AST: the increment is read from the response and a falsy one refuses.
_inc_assigns = [a for a in ast.walk(_GR) if isinstance(a, ast.Assign)
                and isinstance(a.value, ast.Call)
                and getattr(a.value.func, "attr", None) == "get"
                and a.value.args and isinstance(a.value.args[0], ast.Constant)
                and a.value.args[0].value == "base_increment"]
ok("the increment is read from the venue's response", len(_inc_assigns) == 1,
   str(len(_inc_assigns)))
_inc_name = _inc_assigns[0].targets[0].id if _inc_assigns else None
_guarded_inc = any(
    isinstance(n, ast.If)
    and _inc_name is not None
    and _inc_name in {x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}
    and any(isinstance(x, ast.Return) and isinstance(x.value, ast.Constant)
            and x.value.value is None for x in ast.walk(n))
    for n in ast.walk(_GR))
ok("and a missing one refuses rather than assuming a default", _guarded_inc)
# The precise regression: 8 must not appear as a value anywhere in the body.
_body_consts = [c.value for n in _GR.body[1:] for c in ast.walk(n)
                if isinstance(c, ast.Constant) and isinstance(c.value, (int, float))]
ok("the number 8 is never a value in this function", 8 not in _body_consts,
   str(_body_consts))

print("== only successes are cached ==")
# Caching a failure would let one blip disable a product until restart.
_assigns = [n for n in ast.walk(FN["get_product_rules"])
            if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)]
ok("the cache is written exactly once", len(_assigns) == 1, str(len(_assigns)))
# A subscript-store count alone missed a `setdefault` mutant, so every
# other way of writing the cache is refused too.
_cache_methods = {getattr(c.func, "attr", None) for c in ast.walk(FN["get_product_rules"])
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                  and getattr(c.func.value, "id", None) == "_PRODUCT_RULES_CACHE"}
ok("and never written by setdefault/update/pop",
   not (_cache_methods - {"get"}), str(_cache_methods))
_stored = [a.value for a in ast.walk(FN["get_product_rules"])
           if isinstance(a, ast.Assign) and isinstance(a.targets[0], ast.Subscript)]
ok("what is stored is the built rules dict, never a failure",
   len(_stored) == 1 and isinstance(_stored[0], ast.Name),
   str([ast.dump(x)[:40] for x in _stored]))

print("== maker-only and the forced exit are untouched ==")
# By AST: maker-only is the policy the spec says to preserve, so assert
# the literal True in the payload rather than the text of the line.
for n in ("place_maker_sell", "place_maker_buy"):
    _po = dict_value_for(n, "post_only")
    ok(f"{n} is still post_only",
       isinstance(_po, ast.Constant) and _po.value is True,
       ast.dump(_po)[:60] if _po is not None else "absent")
ok("the forced-exit escape still exists in the market sell",
   "FORCED EXIT" in seg("place_market_sell"))
# The protection path deliberately does NOT enforce the notional floor,
# because evaluating it needs a live price and this is the path that exits
# a position. Pinned so it is not "fixed" later without that reasoning.
# By AST: the keyword really is the constant None on the protection path.
_mcall = call_named("place_market_sell", "plan_order_quantity")
_qms = kwarg(_mcall, "quote_min_size")
ok("the market sell passes quote_min_size=None on purpose",
   isinstance(_qms, ast.Constant) and _qms.value is None,
   ast.dump(_qms)[:60] if _qms is not None else "absent")
# And the other two DO enforce it, so this is a deliberate exception.
for n in ("place_maker_sell", "place_maker_buy"):
    _q = kwarg(call_named(n, "plan_order_quantity"), "quote_min_size")
    ok(f"{n} does enforce the notional floor",
       _q is not None and not (isinstance(_q, ast.Constant) and _q.value is None))
ok("and the market sell says why", "larger risk" in seg("place_market_sell"))

print("== dust is reported as dust, not as a failed sale ==")
# By AST: no string LITERAL in the function carries the old wording, and
# the recorded reason is the planner's attribute rather than a sentence.
ok("no string literal still says 'nothing sellable'",
   not any("nothing sellable" in c for c in str_consts("place_maker_sell")))
ok("the reason recorded is the planner's code", "reason" in attrs("place_maker_sell"))
_dec = dict_value_for("place_maker_sell", "decision")
ok("the block detail carries the planner's decision",
   isinstance(_dec, ast.Attribute) and _dec.attr == "decision",
   ast.dump(_dec)[:60] if _dec is not None else "absent")

print("== the module is imported once, at module level ==")
_imports = [a for a in ast.walk(TREE) if isinstance(a, ast.Import)
            for n in a.names if n.name == "execution_quantity"]
ok("execution_quantity is imported", len(_imports) >= 1)

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all order-sizing wiring checks passed")
