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
ok("exit_reason is assigned exactly once", len(_assigns) == 1, str(len(_assigns)))
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
ok("the parked_sell literal appears once, in that one computation",
   _lits == 1, str(_lits))

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

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all slice-state write checks passed")
