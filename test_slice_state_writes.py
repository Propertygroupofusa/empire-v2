"""The columns are written from facts, not from inference.

WHY IT EXISTS. c45171a added the state columns and deliberately wrote
nothing to them. This is the commit that writes them, and it touches the
LIVE order path - so the checks here are about the two ways that goes
wrong: writing a state nobody observed, and adding a write that can break
a trade.

Everything is asserted on the parsed tree, because the functions involved
need a database, a venue and a running loop to execute.
"""
import ast
import sys

import slice_lifecycle as sl

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


SRC = open("crypto_grid_bot.py").read()
TREE = ast.parse(SRC)
FN = next((n for n in ast.walk(TREE)
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_grid_branch_cycle"), None)

print("== the cycle has an identity, and one per pass ==")
DRIVER = next((n for n in ast.walk(TREE)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_grid_branches_cycle"), None)
ok("the driver exists", DRIVER is not None)
_gen = [n for n in ast.walk(DRIVER) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name) and n.func.id == "current_cycle_id"] if DRIVER else []
ok("it is generated exactly once per pass, not per branch",
   len(_gen) == 1, str(len(_gen)))
ok("the branch cycle takes it as a parameter",
   FN is not None and any(a.arg == "cycle_id"
                          for a in FN.args.args + FN.args.kwonlyargs), "")
_defaults = dict(zip([a.arg for a in FN.args.args][-len(FN.args.defaults):],
                     FN.args.defaults)) if FN and FN.args.defaults else {}
ok("and it defaults to None, so an existing caller still works",
   "cycle_id" in _defaults and isinstance(_defaults["cycle_id"], ast.Constant)
   and _defaults["cycle_id"].value is None, str(sorted(_defaults)))

import crypto_grid_bot as g
_a, _b = g.current_cycle_id(), g.current_cycle_id()
ok("two reads in the same second agree", _a == _b, f"{_a} vs {_b}")
ok("it is readable, not a random token", _a.endswith("Z") and _a[:8].isdigit(), _a)
# NOT an idempotency key - a next-cycle retry gets a different id, so it
# cannot deduplicate the post-timeout case. Pinned so nobody repurposes it.
_cyc = next((n for n in ast.walk(TREE)
             if isinstance(n, ast.FunctionDef) and n.name == "current_cycle_id"), None)
_names = {n.id for n in ast.walk(_cyc) if isinstance(n, ast.Name)} if _cyc else set()
ok("it is built from the clock, not from randomness",
   "random" not in _names and "uuid" not in _names, str(sorted(_names)))

print("== the buy stamps what the buy actually did ==")
_ins = [n for n in ast.walk(FN) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name) and n.func.id == "CryptoGridSlice"]
ok("the slice insert is still there", len(_ins) == 1, str(len(_ins)))
_kw = {k.arg: k.value for k in _ins[0].keywords if k.arg} if _ins else {}
for field in ("slice_state", "state_updated_at", "cycle_id", "slice_index",
              "order_side", "order_price", "average_fill_price",
              "filled_quantity", "filled_at", "execution_reason"):
    ok(f"  it records {field}", field in _kw, str(sorted(_kw)))
# ACCOUNTED, not OPEN: the fill came back and this transaction books it.
_st = _kw.get("slice_state")
ok("the state is ACCOUNTED", isinstance(_st, ast.Attribute) and _st.attr == "ACCOUNTED",
   ast.dump(_st) if _st is not None else "absent")
ok("  and it comes from slice_lifecycle, not a literal",
   isinstance(_st, ast.Attribute) and isinstance(_st.value, ast.Name)
   and _st.value.id == "_sl", "")
ok("  which really is a terminal state there", sl.ACCOUNTED in sl.TERMINAL)
# The two prices are different facts and neither implies the other.
ok("what it paid is the FILL price",
   isinstance(_kw.get("average_fill_price"), ast.Name)
   and _kw["average_fill_price"].id == "filled_price",
   getattr(_kw.get("average_fill_price"), "id", "?"))
ok("what it expected is kept separately",
   isinstance(_kw.get("order_price"), ast.Name) and _kw["order_price"].id == "price",
   getattr(_kw.get("order_price"), "id", "?"))
# Present-as-a-keyword is not enough: slice_index=None passes that and
# records no rung at all. It must be the 1-based rung, the same number the
# log line reports - and NOT a fraction of three, which the fleet does not
# run.
_si = _kw.get("slice_index")
ok("the rung is the count of open slices plus one",
   isinstance(_si, ast.BinOp) and isinstance(_si.op, ast.Add)
   and isinstance(_si.right, ast.Constant) and _si.right.value == 1
   and isinstance(_si.left, ast.Call) and isinstance(_si.left.func, ast.Name)
   and _si.left.func.id == "len",
   ast.dump(_si) if _si is not None else "absent")
ok("  counted over the branch's own open slices",
   isinstance(_si, ast.BinOp) and any(isinstance(n, ast.Name) and n.id == "slices"
                                      for n in ast.walk(_si.left)),
   "")
ok("  and nothing in the insert hard-codes three",
   not any(isinstance(n, ast.Constant) and n.value == 3 for n in ast.walk(_ins[0])),
   "the fleet runs num_levels rungs, default 10")

ok("the quantity recorded is what FILLED",
   isinstance(_kw.get("filled_quantity"), ast.Name)
   and _kw["filled_quantity"].id == "filled_qty",
   getattr(_kw.get("filled_quantity"), "id", "?"))

print("== the partial fill is PARTIAL, which is not an error ==")
_sets = {}
for n in ast.walk(FN):
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                    and t.value.id == "slice_row"):
                _sets[t.attr] = n.value
for field in ("qty", "slice_state", "state_updated_at", "filled_quantity",
              "average_fill_price", "filled_at", "order_side", "execution_reason"):
    ok(f"  the kept row records {field}", field in _sets, str(sorted(_sets)))
_ps = _sets.get("slice_state")
ok("the kept row's state is PARTIAL",
   isinstance(_ps, ast.Attribute) and _ps.attr == "PARTIAL",
   ast.dump(_ps) if _ps is not None else "absent")
ok("  and PARTIAL is not a fault in the lifecycle", not sl.is_fault(sl.PARTIAL))
ok("  nor is DUST", not sl.is_fault(sl.DUST))
ok("the residual is still what is written to qty",
   isinstance(_sets.get("qty"), ast.Name) and _sets["qty"].id == "_residual",
   getattr(_sets.get("qty"), "id", "?"))

print("== the close reason is computed once, and is in scope where it is read ==")
# It used to live ONLY inside the shadow block - which is disabled in
# production - while a second copy of the same three-way sat in the
# ledger call. Reading the shadow one from anywhere else is a NameError
# everywhere the fleet actually runs.
_assigns = [n for n in ast.walk(FN) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "exit_reason" for t in n.targets)]
# UPDATED 2026-10-09. Two assignments now, and the second is correct:
# ADOPTED_EXIT_REASON RELABELS the first when the slice turns out to be an
# inherited position. The point of this check is that the reason is
# computed in ONE place and not re-derived per branch or per code path, so
# it now pins the exact shape: one three-way computation, plus at most one
# relabel that reads the adoption fact and nothing else.
ok("exit_reason is assigned at most twice - the three-way, plus the "
   "adopted relabel", len(_assigns) <= 2, str(len(_assigns)))
_relabels = [a for a in _assigns
             if isinstance(a.value, ast.Name) and a.value.id == "ADOPTED_EXIT_REASON"]
_threeway = [a for a in _assigns if a not in _relabels]
ok("exactly one of them is the three-way computation",
   len(_threeway) == 1, str(len(_threeway)))
ok("and any second assignment is the adopted relabel, nothing else",
   len(_assigns) - len(_threeway) == len(_relabels), str(len(_assigns)))
_shadow_ifs = [n for n in ast.walk(FN) if isinstance(n, ast.If)
               and any(isinstance(x, ast.Name) and x.id == "SHADOW_MODE_ENABLED"
                       for x in ast.walk(n.test))]
_inside = any(a in list(ast.walk(sif)) for sif in _shadow_ifs for a in _assigns)
ok("  and NOT inside the shadow-mode guard", not _inside,
   "it would be undefined in production")
_uses = [n for n in ast.walk(FN) if isinstance(n, ast.Name)
         and n.id == "exit_reason" and isinstance(n.ctx, ast.Load)]
ok("  and it has three readers, not three copies", len(_uses) >= 3, str(len(_uses)))
# The three-way itself must not have been re-spelled anywhere.
_lits = sum(1 for n in ast.walk(FN) if isinstance(n, ast.Constant)
            and n.value == "parked_sell")
# The literal now appears twice: once in the three-way, and once in the
# guard `if exit_reason == "parked_sell" and ...` that decides whether the
# adopted relabel applies. That is a READ, not a second spelling of the
# rule. Anything beyond two means the three-way has been duplicated.
ok("the parked_sell literal appears at most twice - the computation, and "
   "the adopted guard that reads it", _lits <= 2, str(_lits))

print("== no name is read that production would not have bound ==")
import builtins
_mod = {n.id for n in ast.walk(TREE) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
_mod |= {n.name for n in ast.walk(TREE) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
_mod |= {(a.asname or a.name).split(".")[0] for n in ast.walk(TREE)
         if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
_local = {n.id for n in ast.walk(FN) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
_local |= {a.arg for a in FN.args.args}
_local |= {c.name for c in ast.walk(FN) if isinstance(c, ast.ExceptHandler) and c.name}
_unbound = sorted({n.id for n in ast.walk(FN)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
                  - _local - _mod - set(dir(builtins)))
ok("nothing is unbound", not _unbound, str(_unbound))

print("== the state write cannot break a trade ==")
# It rides the ledger catch-up that already exists after the sale - which
# retries and is deliberately not one transaction with the venue order.
_retry = [n for n in ast.walk(FN) if isinstance(n, ast.For)
          and isinstance(n.iter, ast.Call) and isinstance(n.iter.func, ast.Name)
          and n.iter.func.id == "range"]
ok("the post-sale write is still inside a retry loop", bool(_retry), "")
_in_retry = any(any(t for t in ast.walk(loop)
                    if isinstance(t, ast.Attribute) and t.attr == "slice_state")
                for loop in _retry)
ok("  and the state stamp is inside it", _in_retry, "")
# THE ACTUAL CLAIM, rather than a count against a number I made up: every
# write to slice_row happens inside ONE `async with` - the session that
# already loaded the row - so the stamp opens no session of its own and
# adds no second place for a ledger write to fail.
_withs = [n for n in ast.walk(FN) if isinstance(n, ast.AsyncWith)]
_holders = [w for w in _withs
            if any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                   and t.value.id == "slice_row"
                   for a in ast.walk(w) if isinstance(a, ast.Assign)
                   for t in a.targets)]
ok("every slice_row write lives in exactly one session block",
   len(_holders) == 1, f"{len(_holders)} block(s) write slice_row")
if _holders:
    _inner = [w for w in ast.walk(_holders[0])
              if isinstance(w, ast.AsyncWith) and w is not _holders[0]]
    ok("  and that block opens no nested session",
       not any(isinstance(c, ast.Call) and isinstance(c.func, ast.Call)
               and isinstance(c.func.func, ast.Name)
               and c.func.func.id == "get_session_factory"
               for w in _inner for item in w.items for c in ast.walk(item.context_expr)),
       f"{len(_inner)} nested async-with")
    ok("  and the row it writes is the one it selected there",
       any(isinstance(n, ast.Name) and n.id == "CryptoGridSlice"
           for n in ast.walk(_holders[0])), "")

print("== and the written state is READABLE ==")
# A write nobody can read is the same trap as a protection nobody can
# observe. Both real bugs found today came from shipping observability and
# then looking, so the state the cycle stamps is served by grid-status.
_status = next((n for n in ast.walk(TREE)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_grid_status"), None)
ok("get_grid_status exists", _status is not None)
_appends = [n for n in ast.walk(_status) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "append"
            and n.args and isinstance(n.args[0], ast.Dict)] if _status else []
ok("the slice serialiser is a dict literal", bool(_appends), str(len(_appends)))
_keys = set()
for a in _appends:
    _keys |= {k.value for k in a.args[0].keys
              if isinstance(k, ast.Constant) and isinstance(k.value, str)}
for field in ("slice_state", "cycle_id", "slice_index",
              "filled_quantity", "average_fill_price", "execution_reason"):
    ok(f"  grid-status serves {field}", field in _keys, str(sorted(_keys)))
# getattr with a None default, because a pre-existing row has no state and
# NULL is UNKNOWN - not a value to invent.
_vals = {}
for a in _appends:
    d = a.args[0]
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value in ("slice_state", "cycle_id"):
            _vals[k.value] = v
ok("state is read defensively, so an old row reads UNKNOWN not a default",
   all(isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "getattr"
       and len(v.args) == 3 and isinstance(v.args[2], ast.Constant)
       and v.args[2].value is None
       for v in _vals.values()) and len(_vals) == 2,
   str(sorted(_vals)))

print("== the venue's order id reaches the slice ==")
# §24 step 7 had nothing to reconcile against: an open order could not be
# matched to the slice that placed it. Coinbase's fills feed carries
# order_id and NOT client_order_id, so this is the join that works.
_oid = _kw.get("order_id")
ok("the buy records an order_id", _oid is not None, str(sorted(_kw)))
# .get, never [] - a product the engine recorded nothing for must land as
# UNKNOWN, not raise and not claim.
ok("  read with .get, so a missing one is UNKNOWN",
   isinstance(_oid, ast.Call) and isinstance(_oid.func, ast.Attribute)
   and _oid.func.attr == "get",
   ast.dump(_oid)[:80] if _oid is not None else "absent")
ok("  from the engine's own per-product map",
   isinstance(_oid, ast.Call) and isinstance(_oid.func.value, ast.Attribute)
   and _oid.func.value.attr == "_last_order_id",
   ast.dump(_oid)[:120] if _oid is not None else "absent")

import crypto_btc_compound_bot as _eng
ok("the engine exposes the map", isinstance(getattr(_eng, "_last_order_id", None), dict))
_esrc = ast.parse(open("crypto_btc_compound_bot.py").read())
# Set exactly where the venue mints the id, and nowhere else.
_sets = [n for n in ast.walk(_esrc) if isinstance(n, ast.Assign)
         and any(isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                 and t.value.id == "_last_order_id" for t in n.targets)]
ok("it is written in exactly one place", len(_sets) == 1, str(len(_sets)))
ok("  and what it stores is the venue's order_id",
   _sets and isinstance(_sets[0].value, ast.Name) and _sets[0].value.id == "order_id",
   ast.dump(_sets[0].value)[:60] if _sets else "-")
# Cleared at every order-attempt entry, so a stale id is never a false join.
_pops = [n for n in ast.walk(_esrc) if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute) and n.func.attr == "pop"
         and isinstance(n.func.value, ast.Name) and n.func.value.id == "_last_order_id"]
ok("it is cleared at every order-attempt entry", len(_pops) >= 3, str(len(_pops)))
_rested = [n for n in ast.walk(_esrc) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute) and n.func.attr == "pop"
           and isinstance(n.func.value, ast.Name) and n.func.value.id == "_last_order_rested"]
ok("  as often as its sibling _last_order_rested",
   len(_pops) >= len(_rested), f"{len(_pops)} vs {len(_rested)}")

print("== §5: each slice carries its OWN take-profit target ==")
_tp = _kw.get("target_price"); _tr = _kw.get("target_reason")
ok("the buy records a target_price", _tp is not None, str(sorted(_kw)))
# NOT "is not None": an ast.Constant(None) node is not Python None, so
# `target_reason=None` satisfied that and the mutant survived. Present is
# not populated - assert the VALUE.
ok("and a target_reason beside it",
   isinstance(_tr, ast.Name) and _tr.id == "_target_reason",
   ast.dump(_tr)[:70] if _tr is not None else "absent")
ok("  the target itself is a real value too",
   isinstance(_tp, ast.Name) and _tp.id == "_target_price",
   ast.dump(_tp)[:70] if _tp is not None else "absent")
# And the reason must name the route, not be a bare label.
_reason_asgn = [n for n in ast.walk(FN) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_target_reason"
                        for t in n.targets)]
_joined = [n for a in _reason_asgn for n in ast.walk(a) if isinstance(n, ast.JoinedStr)]
ok("  and it is built from the real numbers, not a constant string",
   bool(_joined), f"{len(_reason_asgn)} assignment(s), no f-string")
_rtext = " ".join(c.value for j in _joined for c in j.values
                  if isinstance(c, ast.Constant) and isinstance(c.value, str))
ok("  naming the parked-sell route explicitly", "parked_sell" in _rtext, _rtext[:80])
_assigns_tp = [n for n in ast.walk(FN) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Tuple) and any(
                   isinstance(e, ast.Name) and e.id == "_target_price" for e in t.elts)
                   for t in n.targets)]
# BEFORE the try, by line number. The except block assigns the same tuple,
# so merely finding one is satisfied by deleting the initialisation - and
# then a raise before the first assignment reaches the insert unbound.
_tries_all = [n for n in ast.walk(FN) if isinstance(n, ast.Try)
              and any(isinstance(x, ast.Name) and x.id == "slice_target"
                      for x in ast.walk(n))]
ok("it is initialised before the try, not only in the except",
   bool(_assigns_tp) and bool(_tries_all)
   and min(a.lineno for a in _assigns_tp) < min(t.lineno for t in _tries_all),
   f"init at {[a.lineno for a in _assigns_tp]}, try at {[t.lineno for t in _tries_all]}")
_tries = [n for n in ast.walk(FN) if isinstance(n, ast.Try)
          and any(isinstance(x, ast.Name) and x.id == "slice_target"
                  for x in ast.walk(n))]
ok("the whole computation sits inside a try", bool(_tries),
   "a recorded fact must never lose a fill that already happened")

print("== it is priced with THIS slice's own round trip ==")
_calls = [n for n in ast.walk(FN) if isinstance(n, ast.Call)
          and isinstance(n.func, ast.Attribute) and n.func.attr == "target_price"]
ok("target_price is called once", len(_calls) == 1, str(len(_calls)))
if _calls:
    _args = _calls[0].args
    ok("  from the FILL price, not the expected one",
       _args and isinstance(_args[0], ast.Name) and _args[0].id == "filled_price",
       getattr(_args[0], "id", "?") if _args else "-")
    ok("  at the floor, not an invented edge",
       len(_args) > 2 and isinstance(_args[2], ast.Name)
       and _args[2].id == "GRID_PARKED_MIN_NET_PCT",
       getattr(_args[2], "id", "?") if len(_args) > 2 else "-")
# The rate must combine the REAL buy leg with the expected sell leg.
_rate = [n for n in ast.walk(FN) if isinstance(n, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id == "_rt_rate" for t in n.targets)]
ok("the round trip rate is built, not assumed", len(_rate) == 1, str(len(_rate)))
if _rate:
    _names = {n.id for n in ast.walk(_rate[0]) if isinstance(n, ast.Name)}
    ok("  from the buy leg's real recorded rate", "buy_leg_fee" in _names, str(sorted(_names)))
    ok("  plus the expected exit leg", "_exit_leg" in _names, str(sorted(_names)))

print("== rounded UP, and cast at the Float boundary ==")
_round = [n for n in ast.walk(FN) if isinstance(n, ast.Call)
          and isinstance(n.func, ast.Attribute) and n.func.attr == "round_target_up"]
ok("the target is rounded up", len(_round) == 1, str(len(_round)))
# round_target_up returns a Decimal; target_price is a Float column and
# _grid_slice_net_pnl raises TypeError on a Decimal exit price.
_floats = [n for n in ast.walk(FN) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Name) and n.func.id == "float"
           and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                   and c.func.attr == "round_target_up" for c in ast.walk(n))]
ok("  and cast to float, because the column and its readers are floats",
   len(_floats) == 1, str(len(_floats)))
import slice_target as _st
from decimal import Decimal as _D
ok("  (round_target_up really does return a Decimal)",
   isinstance(_st.round_target_up(23.1889, "0.001"), _D))

print("== the recorded target really nets the floor ==")
# Through the REAL formula the live sell uses, not slice_target's own.
from crypto_grid_bot import _grid_slice_net_pnl as _net, GRID_PARKED_MIN_NET_PCT as _FLOOR
for entry, rate, tick in ((22.80, 0.0070, "0.001"), (0.15, 0.0070, "0.0001"),
                          (84194.59, 0.0035, "0.01"), (8.412e-05, 0.0070, "0.00000001")):
    t = _st.target_price(entry, rate, _FLOOR)
    ok(f"entry {entry:g} at rate {rate}: unrounded nets exactly the floor",
       abs(_net(1.0, entry, t, rate) / entry - _FLOOR) < 1e-9, "")
    r = float(_st.round_target_up(t, tick))
    ok(f"  rounded to {tick} still clears it",
       _net(1.0, entry, r, rate) / entry >= _FLOOR, "")
    ok(f"  and rounding went UP for {entry:g}", r >= t, f"{r!r} < {t!r}")

print("== the venue's price tick is read, never guessed ==")
_esrc2 = ast.parse(open("crypto_btc_compound_bot.py").read())
_rules = [n for n in ast.walk(_esrc2) if isinstance(n, ast.Dict)
          and any(isinstance(k, ast.Constant) and k.value == "base_increment" for k in n.keys)]
ok("get_product_rules publishes a quote_increment", 
   any(isinstance(k, ast.Constant) and k.value == "quote_increment"
       for d in _rules for k in d.keys), "")
for d in _rules:
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value == "quote_increment":
            ok("  and it is None when absent, never a guessed tick",
               isinstance(v, ast.BoolOp) and isinstance(v.op, ast.Or)
               and isinstance(v.values[-1], ast.Constant) and v.values[-1].value is None,
               ast.dump(v)[:90])

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all slice-state write checks passed")
