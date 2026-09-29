"""A restart that cannot see the wallet does not get to trade on faith.

WHY IT EXISTS. §24 says reconcile before resuming, and step 8 says "mark
unknown states appropriately". Almost every check here is about the third
outcome: not "consistent" and not "inconsistent", but "not known". The
failure this guards against is the cheap two-way version, where anything
that is not a detected discrepancy is treated as a pass - which turns a
failed balance read into permission to trade.

The other half is the asymmetry. Tracked above held blocks; held above
tracked does not. The fleet's ordinary adopted state is held above
tracked, so a symmetric check would halt it over nothing.
"""
import ast
import inspect
import sys

import restart_recovery as rr

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


INC = "0.001"   # LINK-USD's real base_increment shape


def plan(**over):
    kw = dict(persisted_slices=[{"product_id": "LINK-USD", "qty": "4.1"}],
              held_balances={"LINK-USD": "4.1"},
              open_orders=[],
              increments_by_product={"LINK-USD": INC},
              order_state_is_persisted=True)
    kw.update(over)
    return rr.plan_restart(**kw)


print("== a clean restart resumes ==")
p = plan()
ok("it resumes", p.decision == rr.RESUME, p.decision)
ok("and may trade", p.may_trade)
ok("with nothing unknown", p.unknowns == [], str(p.unknowns))
ok("and nothing to report", p.findings == [], str(p.findings))

print("== read-and-empty is a real answer, not a gap ==")
p = plan(persisted_slices=[], held_balances={}, open_orders=[])
ok("no slices, no orders, flat wallet resumes", p.decision == rr.RESUME, p.decision)
ok("  and may trade", p.may_trade)

print("== unread is never read-and-empty ==")
for field, code in (("persisted_slices", rr.SLICES_UNREADABLE),
                    ("held_balances", rr.BALANCES_UNREADABLE),
                    ("open_orders", rr.OPEN_ORDERS_UNREADABLE)):
    p = plan(**{field: None})
    ok(f"{field}=None refuses", p.decision == rr.REFUSED, p.decision)
    ok(f"  and does not trade on {field}", not p.may_trade)
    ok(f"  and names {code}", code in p.unknowns, str(p.unknowns))
p = plan(persisted_slices=None, held_balances=None, open_orders=None)
ok("three gaps are all reported, not just the first",
   len(p.unknowns) >= 3, str(p.unknowns))

print("== a step that cannot run is not a step that passed ==")
p = plan(order_state_is_persisted=False)
ok("unpersisted order state refuses", p.decision == rr.REFUSED, p.decision)
ok("  and says which step", rr.ORDER_STATE_NOT_PERSISTED in p.unknowns,
   str(p.unknowns))
ok("  and does not trade", not p.may_trade)

print("== tracked above held blocks; held above tracked does not ==")
p = plan(held_balances={"LINK-USD": "3.0"})
ok("short holds", p.decision == rr.HOLD, p.decision)
ok("  and does not trade", not p.may_trade)
ok("  and names the shortfall", p.reason == rr.INVENTORY_SHORT, p.reason)
ok("  and reports the product", p.findings and p.findings[0]["product_id"] == "LINK-USD",
   str(p.findings))
ok("  and the verdict on it", p.findings and p.findings[0]["verdict"] == rr.SHORT,
   str(p.findings))
p = plan(held_balances={"LINK-USD": "9.9"})
ok("excess still resumes", p.decision == rr.RESUME, p.decision)
ok("  and may trade", p.may_trade)
ok("  but is still reported", p.findings and p.findings[0]["verdict"] == rr.EXCESS,
   str(p.findings))

print("== a missing balance for a tracked product is a gap, not a zero ==")
# The wallet read succeeded but this product was not in it. That is NOT
# "you hold none of it" - that reading would make every tracked slice look
# short and halt the fleet, or, with the comparison the other way, would
# let a real shortfall through as a match.
p = plan(held_balances={})
ok("an absent product refuses", p.decision == rr.REFUSED, p.decision)
ok("  and does not trade", not p.may_trade)
ok("  and names the product as unresolved", "LINK-USD" in p.unknowns, str(p.unknowns))

print("== an unknown increment is not a guessed one ==")
p = plan(increments_by_product={})
ok("no increment refuses", p.decision == rr.REFUSED, p.decision)
ok("  and does not trade", not p.may_trade)
for bad in (0, "0", -1, None, "abc", float("nan")):
    ok(f"increment {bad!r} is UNKNOWN",
       rr.reconcile_inventory("4.1", "4.1", bad) == rr.UNKNOWN,
       rr.reconcile_inventory("4.1", "4.1", bad))

print("== a difference below one increment is not a discrepancy ==")
ok("half an increment matches",
   rr.reconcile_inventory("4.1005", "4.1", INC) == rr.MATCHED)
ok("exactly one increment does not",
   rr.reconcile_inventory("4.101", "4.1", INC) == rr.SHORT,
   rr.reconcile_inventory("4.101", "4.1", INC))
ok("and the tolerance follows the product, not a constant",
   rr.reconcile_inventory("4.1005", "4.1", "0.0001") == rr.SHORT,
   rr.reconcile_inventory("4.1005", "4.1", "0.0001"))
# The float trap this whole rebuild started from.
ok("0.29 against 0.28 at a 0.01 increment is SHORT, exactly",
   rr.reconcile_inventory("0.29", "0.28", "0.01") == rr.SHORT,
   rr.reconcile_inventory("0.29", "0.28", "0.01"))

print("== a bad read is never a small number ==")
for bad in (None, "abc", float("nan"), float("inf"), -0.5, True):
    ok(f"tracked={bad!r} is UNKNOWN",
       rr.reconcile_inventory(bad, "4.1", INC) == rr.UNKNOWN,
       rr.reconcile_inventory(bad, "4.1", INC))
    ok(f"held={bad!r} is UNKNOWN",
       rr.reconcile_inventory("4.1", bad, INC) == rr.UNKNOWN,
       rr.reconcile_inventory("4.1", bad, INC))
p = plan(persisted_slices=[{"product_id": "LINK-USD", "qty": None}])
ok("an unreadable slice refuses the whole plan", p.decision == rr.REFUSED, p.decision)
p = plan(persisted_slices=[{"qty": "4.1"}])
ok("a slice with no product refuses too", p.decision == rr.REFUSED, p.decision)

print("== slices of one product are summed, not compared one by one ==")
p = plan(persisted_slices=[{"product_id": "LINK-USD", "qty": "2.0"},
                           {"product_id": "LINK-USD", "qty": "2.1"}],
         held_balances={"LINK-USD": "4.1"})
ok("two slices totalling the balance match", p.decision == rr.RESUME, p.decision)
p = plan(persisted_slices=[{"product_id": "LINK-USD", "qty": "2.0"},
                           {"product_id": "LINK-USD", "qty": "2.1"}],
         held_balances={"LINK-USD": "2.0"})
ok("and a real total shortfall still holds", p.decision == rr.HOLD, p.decision)
# The case that separates summing from last-one-wins: held equals the LAST
# slice exactly, so a ledger that overwrote instead of adding would call it
# a match and resume while half the position is unaccounted for.
p = plan(persisted_slices=[{"product_id": "LINK-USD", "qty": "2.0"},
                           {"product_id": "LINK-USD", "qty": "2.1"}],
         held_balances={"LINK-USD": "2.1"})
ok("a balance equal to the last slice alone is still short",
   p.decision == rr.HOLD, p.decision)
ok("  and the tracked total is the sum, not the last",
   p.findings and p.findings[0]["tracked"] == "4.1", str(p.findings))
p = plan(persisted_slices=[{"product_id": "LINK-USD", "qty": "2.0"},
                           {"product_id": "ETH-USD", "qty": "0.5"}],
         held_balances={"LINK-USD": "2.0", "ETH-USD": "0.5"},
         increments_by_product={"LINK-USD": INC, "ETH-USD": "0.00000001"})
ok("two products are reconciled separately", p.decision == rr.RESUME, p.decision)
p = plan(persisted_slices=[{"product_id": "LINK-USD", "qty": "2.0"},
                           {"product_id": "ETH-USD", "qty": "0.5"}],
         held_balances={"LINK-USD": "2.0", "ETH-USD": "0.1"},
         increments_by_product={"LINK-USD": INC, "ETH-USD": "0.00000001"})
ok("one short product holds the whole restart", p.decision == rr.HOLD, p.decision)
ok("  and only the short one is reported",
   [f["product_id"] for f in p.findings] == ["ETH-USD"], str(p.findings))

print("== may_trade is granted, never merely not-denied ==")
_tree = ast.parse(inspect.getsource(rr))
_mt = [n for n in ast.walk(_tree)
       if isinstance(n, ast.FunctionDef) and n.name == "may_trade"]
ok("may_trade exists", len(_mt) == 1, str(len(_mt)))
if _mt:
    _cmps = [n for n in ast.walk(_mt[0]) if isinstance(n, ast.Compare)]
    ok("it is one comparison", len(_cmps) == 1, str(len(_cmps)))
    ok("and it is an equality, not a not-equal",
       _cmps and all(isinstance(o, ast.Eq) for o in _cmps[0].ops),
       str([type(o).__name__ for o in (_cmps[0].ops if _cmps else [])]))
    _nots = [n for n in ast.walk(_mt[0]) if isinstance(n, ast.UnaryOp)
             and isinstance(n.op, ast.Not)]
    ok("with no negation to invert", not _nots, str(len(_nots)))
# An unrecognised decision must not trade. Nothing constructs this today;
# the point is that a decision added later cannot become permission by
# default.
ok("an unknown decision does not trade",
   not rr.RecoveryPlan("SOMETHING_NEW", "x").may_trade)

print("== it reads nothing and writes nothing ==")
_imports = {n.name.split(".")[0] for a in ast.walk(_tree)
            if isinstance(a, ast.Import) for n in a.names}
_imports |= {a.module.split(".")[0] for a in ast.walk(_tree)
             if isinstance(a, ast.ImportFrom) and a.module}
ok("no I/O, no clock, no randomness",
   not (_imports & {"aiohttp", "requests", "sqlalchemy", "database", "models",
                    "time", "datetime", "random", "os"}),
   str(_imports))
_funcs = {n.name for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef)}
ok("it writes no second flooring routine",
   not any("floor" in n or "round_down" in n for n in _funcs), str(_funcs))

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all restart-recovery checks passed")
