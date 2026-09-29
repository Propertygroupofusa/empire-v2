"""A maker-expiry row must say whether an order was ever on the book.

WHY THIS FILE EXISTS. GridMakerExpiry is the evidence base for one
question: should a resting rung be given longer before it is cancelled?
_record_maker_expiry was called on EVERY None return from
place_maker_sell, and two of those three returns never create an order at
all. So rows that recorded "no order was placed" were being fed into a
study about how long orders should rest, where they are not weak evidence
but no evidence.

Scale is what made it urgent rather than untidy: ALGO and QNT alone were
writing ~2,600 such rows a day, against a 5,000-row study window. Within
about two days the study would have been made almost entirely of orders
that never existed, while still reporting a confident mean.

The fix is a structural flag, order_rested, set by the engine on every
return path and carried onto the row:

    True  - an order id was minted, it sat for its whole window, nobody
            crossed it. The only rows the study may speak about.
    False - no order was ever created.
    None  - UNKNOWN. Pre-existing rows, and the one path that genuinely
            cannot tell (the POST raised after it may have reached the
            venue).

Asserted on the PARSED TREE, never on source text: three previous guards
in this repo misfired by matching a substring that also occurred inside a
comment or a longer identifier.
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
MODELS_SRC, MODELS = load("models.py")
ROUTER_SRC, ROUTER = load("routers/trading_dashboard.py")


def func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def classdef(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.name == name:
            return n
    return None


def writes_to(node, dict_name):
    """Every value assigned into dict_name[...] anywhere under node, as a
    list of ast nodes. Subscript assignment only - a bare name read does
    not count as recording a verdict."""
    found = []
    for n in ast.walk(node):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                        and t.value.id == dict_name):
                    found.append(n.value)
    return found


def constant_writes(node, dict_name):
    return [v.value for v in writes_to(node, dict_name)
            if isinstance(v, ast.Constant)]


print("== the engine records whether an order actually rested ==")

_top_dicts = [t.id for n in ENGINE.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name)]
ok("_last_order_rested is a module-level name in the engine",
   "_last_order_rested" in _top_dicts)

pmo = func(ENGINE, "_place_maker_order")
ok("_place_maker_order exists", pmo is not None)

if pmo is not None:
    vals = constant_writes(pmo, "_last_order_rested")
    ok("_place_maker_order records both verdicts (a rejection and a rest)",
       True in vals and False in vals, f"recorded: {vals}")

    # THE POINT OF THE WHOLE FILE. The placement-exception path cannot know
    # whether the order was created, so it must leave the verdict absent.
    # Asserted as "never assigns under that handler" rather than "contains
    # no such line", so a mutant that adds one is caught wherever it sits.
    handlers = [h for h in ast.walk(pmo) if isinstance(h, ast.ExceptHandler)]
    ok("_place_maker_order has an except handler around placement",
       len(handlers) >= 1)
    leaked = [h for h in handlers if writes_to(h, "_last_order_rested")]
    ok("the placement-exception path NEVER claims a verdict (a gap is not a zero)",
       not leaked,
       "an except handler assigns _last_order_rested; a POST that raised may "
       "still have created the order, so False there could be flatly untrue")

pms = func(ENGINE, "place_maker_sell")
ok("place_maker_sell exists", pms is not None)
if pms is not None:
    vals = constant_writes(pms, "_last_order_rested")
    ok("place_maker_sell marks its no-order returns False",
       vals.count(False) >= 3 and True not in vals,
       f"recorded: {vals} (expected three Falses - unreadable balance, "
       f"nothing sellable, no ask - and never a True: this function never "
       f"rests an order itself)")
    ok("place_maker_sell clears any previous cycle's verdict at entry",
       any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "pop" and isinstance(n.func.value, ast.Name)
           and n.func.value.id == "_last_order_rested"
           for n in ast.walk(pms)),
       "without the pop, a stale verdict from an earlier cycle is readable "
       "as this call's fact")

pmb = func(ENGINE, "place_maker_buy")
ok("place_maker_buy exists", pmb is not None)
if pmb is not None:
    vals = constant_writes(pmb, "_last_order_rested")
    ok("place_maker_buy marks its silent no-order returns False too",
       vals.count(False) >= 3 and True not in vals,
       f"recorded: {vals} - the buy side carried the identical defect and "
       f"the asymmetry was the tell")


print("== the row carries the fact ==")

gme = classdef(MODELS, "GridMakerExpiry")
ok("GridMakerExpiry exists", gme is not None)
if gme is not None:
    cols = {}
    for n in gme.body:
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            cols[n.target.id] = n.value
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    cols[t.id] = n.value
    ok("GridMakerExpiry has an order_rested column", "order_rested" in cols)
    ok("GridMakerExpiry has a reason column", "reason" in cols)

    node = cols.get("order_rested")
    if node is not None:
        kw = {k.arg: k.value for k in getattr(node, "keywords", [])}
        nullable = kw.get("nullable")
        ok("order_rested is NULLABLE, so UNKNOWN is representable",
           isinstance(nullable, ast.Constant) and nullable.value is True,
           "a NOT NULL column would force every pre-existing row to claim "
           "False, turning an unknown into a zero")
        args = [a.id for a in getattr(node, "args", []) if isinstance(a, ast.Name)]
        ok("order_rested is a Boolean column", "Boolean" in args, f"args: {args}")

rme = func(GRID, "_record_maker_expiry")
ok("_record_maker_expiry exists", rme is not None)
if rme is not None:
    names = [a.arg for a in rme.args.args] + [a.arg for a in rme.args.kwonlyargs]
    ok("_record_maker_expiry takes order_rested", "order_rested" in names)
    ok("_record_maker_expiry takes reason", "reason" in names)

    # It must pass them through to the row, not accept and drop them.
    passed = set()
    for n in ast.walk(rme):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id == "GridMakerExpiry":
            passed = {k.arg for k in n.keywords}
    ok("the row is constructed with order_rested", "order_rested" in passed)
    ok("the row is constructed with reason", "reason" in passed)


print("== both call sites pass a real verdict, never a manufactured one ==")

for fname, side in (("grid_sell", "sell"), ("grid_buy", "buy")):
    f = func(GRID, fname)
    ok(f"{fname} exists", f is not None)
    if f is None:
        continue
    calls = [n for n in ast.walk(f)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_record_maker_expiry"]
    ok(f"{fname} records a maker expiry", len(calls) == 1,
       f"found {len(calls)}")
    if not calls:
        continue
    kw = {k.arg: k.value for k in calls[0].keywords}
    ok(f"{fname} passes order_rested", "order_rested" in kw)
    ok(f"{fname} passes reason", "reason" in kw)

    v = kw.get("order_rested")

    # A .get with no default yields None for an absent key - UNKNOWN. A
    # default here would invent a verdict for a product the engine said
    # nothing about this cycle.
    #
    # Accepted EITHER inline or through a local name. grid_buy now binds the
    # lookup once and hands the same value to both the ledger row and the
    # cause it reports to its caller, deliberately: read twice, the two could
    # disagree. Demanding the call be written inline would have failed that
    # improvement, which is the hazard of testing a shape instead of a fact -
    # so the name is resolved back to what it was assigned.
    def is_bare_get(node):
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and len(node.args) == 1
                and not node.keywords)

    def resolves_to_bare_get(node):
        if is_bare_get(node):
            return True
        if not isinstance(node, ast.Name):
            return False
        # Every assignment to that name in this function must be a bare .get;
        # one that is not means the value reaching the call can be a claim.
        bound = [a.value for a in ast.walk(f)
                 if isinstance(a, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == node.id
                         for t in a.targets)]
        return bool(bound) and all(is_bare_get(b) for b in bound)

    ok(f"{fname} reads order_rested as a defaulted-to-UNKNOWN lookup",
       resolves_to_bare_get(v),
       "must be a .get(product_id) with NO second argument - inline, or via a "
       "local assigned nothing else - so an absent verdict stays None rather "
       "than becoming a claim")
    ok(f"{fname} never hardcodes order_rested to a constant",
       not isinstance(v, ast.Constant))


print("== the study admits only rows that really rested ==")

study = None
for n in ast.walk(GRID):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        seg = ast.get_source_segment(GRID_SRC, n) or ""
        if "cancel_benefit_" in seg and "_EXPIRY_HORIZONS" in seg and "verdict" in seg:
            study = n
            break
ok("the expiry study function was located", study is not None)

if study is not None:
    # The horizon values must be gathered by iterating the FILTERED list.
    iter_names = set()
    for n in ast.walk(study):
        if isinstance(n, ast.ListComp):
            seg = ast.get_source_segment(GRID_SRC, n) or ""
            if "cancel_benefit_" in seg:
                for g in n.generators:
                    if isinstance(g.iter, ast.Name):
                        iter_names.add(g.iter.id)
    ok("horizon values are gathered from exactly one list", len(iter_names) == 1,
       f"iterates: {sorted(iter_names)}")
    ok("horizon values are NEVER gathered from the unfiltered recent window",
       "recent" not in iter_names,
       "iterating the unfiltered list is the contamination this file exists "
       "to prevent")

    # And that list must come from a query filtered on order_rested is True.
    filtered = False
    for n in ast.walk(study):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in iter_names:
                    seg = ast.get_source_segment(GRID_SRC, n) or ""
                    tree = ast.parse(seg.strip()) if seg else None
                    if tree is not None:
                        for c in ast.walk(tree):
                            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                                    and c.func.attr == "is_":
                                if any(isinstance(a, ast.Constant) and a.value is True
                                       for a in c.args):
                                    filtered = True
    ok("the study sample is drawn with an `order_rested.is_(True)` filter",
       filtered,
       "filtering must happen in SQL, not Python: a single capped query "
       "would be starved of usable rows by the ~2,600 no-order rows a day")

    # The excluded rows must be REPORTED, not silently dropped.
    keys = set()
    for n in ast.walk(study):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                        and isinstance(t.slice.value, str):
                    keys.add(t.slice.value)
    ok("the study reports how many rows placed no order",
       "excluded_no_order_placed" in keys)
    ok("the study reports how many rows are UNKNOWN",
       "excluded_unknown_whether_rested" in keys)
    ok("the study reports its own sample size separately from the total",
       "sample" in keys and "expiries" in keys,
       "without both, a shrunken sample reads as a smaller market rather "
       "than a filtered one")


print("== the endpoint splits the count three ways ==")

ep = None
for n in ast.walk(ROUTER):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for d in n.decorator_list:
            seg = ast.get_source_segment(ROUTER_SRC, d) or ""
            if "maker-expiries" in seg:
                ep = n
ok("the maker-expiries endpoint was located", ep is not None)

if ep is not None:
    keys = set()
    for n in ast.walk(ep):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    for want in ("order_rested", "rested", "no_order_placed",
                 "unknown_whether_rested", "reason"):
        ok(f"the endpoint surfaces '{want}'", want in keys)

    ok("the endpoint still raises rather than returning an empty list on failure",
       any(isinstance(n, ast.Raise) for n in ast.walk(ep)))


print("== a non-attempt is not counted as an attempt ==")

pce = func(GRID, "_per_coin_execution")
ok("_per_coin_execution exists", pce is not None)

if pce is not None:
    # The funnel's `attempted` is filled + expired, and every expiry row used
    # to land in `expired`. A coin that placed no orders therefore read as a
    # coin attempting constantly and never filling - the opposite diagnosis,
    # on the one number read to find the bottleneck.
    incs = {}
    for n in ast.walk(pce):
        if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Subscript) \
                and isinstance(n.target.slice, ast.Constant):
            incs[n.target.slice.value] = incs.get(n.target.slice.value, 0) + 1
    ok("it counts a separate no_order bucket", "no_order" in incs)

    # The increments must be mutually exclusive: one row is EITHER an expiry
    # or a non-attempt, never both.
    branch = None
    for n in ast.walk(pce):
        if isinstance(n, ast.If) and n.orelse:
            names = set()
            for b in (n.body, n.orelse):
                for x in b:
                    for y in ast.walk(x):
                        if isinstance(y, ast.AugAssign) \
                                and isinstance(y.target, ast.Subscript) \
                                and isinstance(y.target.slice, ast.Constant):
                            names.add(y.target.slice.value)
            if names == {"expired", "no_order"}:
                branch = n
    ok("expired and no_order are the two arms of one if/else, never both",
       branch is not None,
       "counting a row into both would put non-attempts back into attempted "
       "by the side door")

    if branch is not None:
        cond = ast.dump(branch.test)
        ok("the split tests order_rested, not the reason text",
           "order_rested" in cond and "reason" not in cond)
        # UNKNOWN must stay on the attempted side: those rows predate the
        # column and may have been real rests. Only a confirmed False is out.
        ok("only a confirmed False is excluded, never an UNKNOWN",
           "Is" in cond and "False" in cond,
           f"condition must be an identity test against False; got: {cond[:160]}")

    ok("attempted is still filled + expired and never folds in no_order",
       any(isinstance(n, ast.Assign)
           and isinstance(n.targets[0], ast.Subscript)
           and isinstance(n.targets[0].slice, ast.Constant)
           and n.targets[0].slice.value == "attempted"
           and "no_order" not in (ast.get_source_segment(GRID_SRC, n.value) or "")
           for n in ast.walk(pce)),
       "a cycle that never placed an order is not a failed attempt; folding "
       "it in would restore the exact number this split exists to correct")


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all maker-expiry order_rested checks passed")
