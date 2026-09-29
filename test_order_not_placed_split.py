"""A confirmed non-order must never be written to the expiry study.

THE SEPARATION. GridMakerExpiry answers one question - should a resting rung
be given longer before it is cancelled? - and every row in it is a data
point in that study. A cycle where no order reached the venue is not a weak
data point there; it is not a data point at all. ALGO, QNT and PRIME were
producing ~2,600 of them a day against a 5,000-row study window.

So the two are separated at the WRITE SITE: _record_maker_expiry refuses a
confirmed non-order and routes it to GridOrderNotPlaced instead.

WHY THE INVARIANT IS IN CODE AND NOT IN THE SCHEMA. It was proposed as
`assert expiry.order_id is not None`, or equivalently a NOT NULL column.
Both are wrong here, for two independent reasons:

  1. This function is instrumentation and must never be able to stop the
     thing it measures. An assert on a live sell path does exactly that.
  2. order_rested has THREE states. _place_maker_order's POST can raise
     AFTER it may already have reached Coinbase, so an order may exist
     whose id never came back. A NOT NULL column cannot represent that, and
     forcing it turns an unread into a no - the bug this entire line of
     work started from. main.py's migration loop also adds every column as
     nullable regardless of the model, so a NOT NULL declaration would not
     even take effect on the live table.

Three states, three destinations, none of them a silent drop:
    True  -> GridMakerExpiry, and the study uses it
    None  -> GridMakerExpiry, and the study excludes it BY NAME
    False -> GridOrderNotPlaced
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


GRID_SRC, GRID = load("crypto_grid_bot.py")
ENGINE_SRC, ENGINE = load("crypto_btc_compound_bot.py")
ROUTER_SRC, ROUTER = load("routers/trading_dashboard.py")


def func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


print("== the table exists and can represent UNKNOWN ==")

import models

tbl = getattr(models, "GridOrderNotPlaced", None)
ok("GridOrderNotPlaced is a model", tbl is not None)
if tbl is not None:
    cols = {c.name: c for c in tbl.__table__.columns}
    for f in ("bot_name", "product_id", "side", "reason", "available_units",
              "size_decimals", "requested_qty", "blocked_at"):
        ok(f"it carries {f}", f in cols)
    # Every figure must be nullable: the balance read itself can fail, and a
    # NOT NULL column would force a fabricated zero into the gap.
    for f in ("available_units", "size_decimals", "requested_qty"):
        ok(f"{f} is nullable, so UNKNOWN is representable",
           f in cols and cols[f].nullable is True,
           "a NOT NULL figure here would turn an unreadable balance into a "
           "zero, which is the bug this table exists downstream of")
    # No drift ladder: nothing rested, so there is nothing to measure after.
    ok("it carries NO price-drift ladder",
       not any(c.startswith("price_") or c.startswith("drift_")
               or c.startswith("cancel_benefit_") for c in cols),
       "resolving horizons for a rung that never rested would be measuring "
       "the market against an event that did not happen")


print("== the invariant is enforced at the write site ==")

rec = func(GRID, "_record_maker_expiry")
ok("_record_maker_expiry exists", rec is not None)

notplaced = func(GRID, "_record_order_not_placed")
ok("_record_order_not_placed exists", notplaced is not None)

if rec is not None:
    # A guard comparing order_rested against False, before any row is added.
    guards = []
    for n in ast.walk(rec):
        if isinstance(n, ast.If):
            d = ast.dump(n.test)
            if "order_rested" in d and "False" in d and "Is" in d:
                guards.append(n)
    ok("it tests order_rested against False by identity",
       len(guards) >= 1,
       "`is False` and not a truthiness test: None is falsy, and treating "
       "UNKNOWN as a confirmed non-order is the whole error")

    if guards:
        g = guards[0]
        body = ast.dump(ast.Module(body=g.body, type_ignores=[]))
        ok("the False branch routes to the not-placed writer",
           "_record_order_not_placed" in body)
        ok("and it RETURNS, so no expiry row can follow",
           any(isinstance(x, ast.Return) for x in ast.walk(g)),
           "without the return, the row is written anyway and the split is "
           "cosmetic")

    # NEVER-DOES-Y: the expiry row must not be constructible after a False.
    # Stated as: every GridMakerExpiry construction is lexically after the
    # guard, and the guard returns.
    adds = [n for n in ast.walk(rec)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "GridMakerExpiry"]
    ok("exactly one GridMakerExpiry row is constructed here", len(adds) == 1)
    if adds and guards:
        ok("the expiry row is constructed only after the guard",
           adds[0].lineno > guards[0].lineno,
           "a row added before the guard is a row the guard cannot prevent")

    # The invariant must NOT be an assert, and must not be able to raise.
    ok("the invariant is NOT an assert statement",
       not any(isinstance(n, ast.Assert) for n in ast.walk(rec)),
       "an assert on a live sell path lets instrumentation stop the thing "
       "it measures")

if notplaced is not None:
    ok("the not-placed writer cannot raise either",
       any(isinstance(n, ast.Try) for n in ast.walk(notplaced)),
       "same rule: a ledger write must never break trading")
    ok("it takes no book quote",
       "get_best_bid_ask" not in (ast.get_source_segment(GRID_SRC, notplaced) or ""),
       "there is no rung to anchor, so a quote would cost an API call to "
       "record a number with no question attached")
    # Detail must be read with .get, so a path that knew nothing writes NULL.
    seg = ast.get_source_segment(GRID_SRC, notplaced) or ""
    sub = ast.parse(seg.strip()) if seg else None
    gets = 0
    if sub is not None:
        for n in ast.walk(sub):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "get" and len(n.args) == 1:
                gets += 1
    ok("every detail field is read with a defaultless .get",
       gets >= 3,
       f"found {gets} - a default here fabricates a figure for a path that "
       f"did not know one")


print("== both call sites hand over the detail ==")

for fname in ("grid_sell", "grid_buy"):
    f = func(GRID, fname)
    ok(f"{fname} exists", f is not None)
    if f is None:
        continue
    calls = [n for n in ast.walk(f)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_record_maker_expiry"]
    ok(f"{fname} passes block_detail",
       bool(calls) and all(any(k.arg == "block_detail" for k in c.keywords)
                           for c in calls),
       "without it the not-placed row has prose and no fields")


print("== the engine records the block as fields ==")

_top = [t.id for n in ENGINE.body if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)]
ok("_last_order_block is a module-level name", "_last_order_block" in _top)

for fname in ("place_maker_sell", "place_maker_buy"):
    f = func(ENGINE, fname)
    ok(f"{fname} exists", f is not None)
    if f is None:
        continue
    seg = ast.get_source_segment(ENGINE_SRC, f) or ""
    sub = ast.parse(seg.strip()) if seg else None
    writes, pops = 0, 0
    if sub is not None:
        for n in ast.walk(sub):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                            and t.value.id == "_last_order_block":
                        writes += 1
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "pop" and isinstance(n.func.value, ast.Name) \
                    and n.func.value.id == "_last_order_block":
                pops += 1
    ok(f"{fname} records a block detail on each no-order return",
       writes >= 3, f"found {writes}")
    ok(f"{fname} clears the previous cycle's detail at entry", pops >= 1,
       "a stale detail read as this call's fact is the same error as a "
       "stale balance")

# THE CONTRADICTION THAT FORCED THE BUY-SIDE FIX. grid_buy defaults a missing
# reason to "the order rested at the bid...", so a silent buy-side return
# would have put text asserting the order rested onto a NOT-PLACED row.
buy = func(ENGINE, "place_maker_buy")
if buy is not None:
    seg = ast.get_source_segment(ENGINE_SRC, buy) or ""
    sub = ast.parse(seg.strip()) if seg else None
    err_writes = 0
    if sub is not None:
        for n in ast.walk(sub):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                            and t.value.id == "_last_order_error":
                        err_writes += 1
    ok("place_maker_buy records its OWN reason on every no-order return",
       err_writes >= 3,
       f"found {err_writes} - silent returns let the caller's default "
       f"('the order rested at the bid and no seller crossed') land on a "
       f"not-placed row, which contradicts the table it sits in")


print("== the new table is readable ==")

ep = None
for n in ast.walk(ROUTER):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for d in n.decorator_list:
            if "orders-not-placed" in (ast.get_source_segment(ROUTER_SRC, d) or ""):
                ep = n
ok("a GET endpoint exposes the new table", ep is not None,
   "a table written and read by nothing is the defect this whole line of "
   "work started from")

if ep is not None:
    keys = set()
    for n in ast.walk(ep):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    for want in ("available_units", "size_decimals", "requested_qty",
                 "reason", "by_product_and_side", "an_empty_result_is",
                 "available_unknown_rows"):
        ok(f"the endpoint surfaces '{want}'", want in keys)
    ok("it raises on an unreadable read rather than returning empty",
       any(isinstance(n, ast.Raise) for n in ast.walk(ep)),
       "an empty list would read as 'every order was placed'")


print("== the funnel counts non-orders from the new table ==")

pce = func(GRID, "_per_coin_execution")
ok("_per_coin_execution exists", pce is not None)
if pce is not None:
    seg = ast.get_source_segment(GRID_SRC, pce) or ""
    ok("it reads GridOrderNotPlaced", "GridOrderNotPlaced" in seg,
       "new blocked cycles go to the new table; a funnel reading only the "
       "old one would report no_order dropping to zero and call it progress")
    ok("and still reads the legacy False rows in the expiry table",
       "order_rested is False" in seg,
       "dropping them loses the history written between the flag shipping "
       "and the split")


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all order-not-placed split checks passed")
