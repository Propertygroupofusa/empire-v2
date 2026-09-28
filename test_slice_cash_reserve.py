"""The fee reserve has to bind at the moment money is spent, not only when
it is planned.

Found on the live account 2026-09-26: wallet $79.36 against an $88.00
reserve, a branch carrying a $69.23 allocation. Every planning path
(redistribute_grid_cash, get_grid_spend_ceiling_usd,
_auto_deploy_idle_free_cash) subtracted the reserve. The buy itself sized
as min(slice_usd, real_balance) - straight against the whole wallet - so
the reserve was real in every projection and decorative at the only moment
it mattered.

These call the real function, and the last check parses the real buy path
to prove it is the one being called.
"""

import ast
import os
import crypto_grid_bot as G

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


print("\nthe reserve binds the spend")

spend, why = G.spendable_for_slice(69.23, 79.36, reserve=88.0, min_trade=5.0)
ok("THE LIVE CASE: $69.23 wanted, $79.36 wallet, $88 reserve -> no buy",
   spend == 0.0, f"got {spend} ({why})")
ok("and it says which limit bound it, with the numbers",
   "88.00" in why and "79.36" in why, why)

spend, why = G.spendable_for_slice(69.23, 79.36, reserve=0.0, min_trade=5.0)
ok("the OLD behaviour is what a zero reserve reproduces - $69.23 spent",
   spend == 69.23, f"got {spend} ({why})")

print("\nit shrinks to the wallet rather than refusing, while it can")

spend, why = G.spendable_for_slice(69.23, 120.0, reserve=88.0, min_trade=5.0)
ok("a $32.00 gap above the reserve funds a $32.00 slice, not $69.23",
   spend == 32.0, f"got {spend} ({why})")
ok("and names the wallet as the binding limit",
   "wallet less the fee reserve" in why, why)

spend, why = G.spendable_for_slice(69.23, 500.0, reserve=88.0, min_trade=5.0)
ok("with room to spare the branch allocation binds instead",
   spend == 69.23 and "branch allocation" in why, f"got {spend} ({why})")

print("\ndust is refused outright, never sent as a fee-paying micro-order")

spend, why = G.spendable_for_slice(69.23, 91.0, reserve=88.0, min_trade=5.0)
ok("$3.00 above the reserve is below the $5.00 minimum -> no buy",
   spend == 0.0, f"got {spend} ({why})")
ok("and it is refused, not rounded up to the minimum",
   spend != 5.0)

spend, why = G.spendable_for_slice(69.23, 93.0, reserve=88.0, min_trade=5.0)
ok("$5.00 exactly is allowed through",
   spend == 5.0, f"got {spend} ({why})")

print("\nan unreadable balance is never treated as money")

spend, why = G.spendable_for_slice(69.23, None, reserve=88.0, min_trade=5.0)
ok("a None balance spends nothing", spend == 0.0, f"got {spend} ({why})")

spend, why = G.spendable_for_slice(69.23, 0.0, reserve=88.0, min_trade=5.0)
ok("an empty wallet spends nothing", spend == 0.0, f"got {spend} ({why})")

spend, why = G.spendable_for_slice(69.23, 88.0, reserve=88.0, min_trade=5.0)
ok("exactly at the reserve spends nothing", spend == 0.0, f"got {spend} ({why})")

print("\nthe real buy path is the caller - not a parallel copy of this rule")

tree = ast.parse(open(G.__file__).read())
cycle = next(n for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_grid_branch_cycle")
calls = [n for n in ast.walk(cycle)
         if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "spendable_for_slice"]
ok("run_grid_branch_cycle calls spendable_for_slice", len(calls) == 1,
   f"found {len(calls)} call(s)")

# The old expression must be gone from the buy path, or the reserve is
# being computed and then ignored. Checked on the AST so a comment that
# merely quotes the old line cannot satisfy it.
bad = []
for n in ast.walk(cycle):
    if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "min":
        names = {getattr(a, "id", None) for a in n.args}
        if {"slice_usd", "real_balance"} <= names:
            bad.append(n.lineno)
ok("and the old min(slice_usd, real_balance) sizing is gone from it",
   not bad, f"still present at line(s) {bad}")

# ── the deployment reserve, subtracted alongside the fee reserve ─────────
import crypto_grid_bot as _g

ok("a wallet with no deployment reserve behaves exactly as before",
   _g.spendable_for_slice(50.0, 200.0, reserve=88.0)
   == _g.spendable_for_slice(50.0, 200.0, reserve=88.0, deployment_reserve=0.0))

ok("cash held for an unfunded coin is subtracted from what a slice may spend",
   _g.spendable_for_slice(100.0, 200.0, reserve=88.0, deployment_reserve=50.0)[0]
   < _g.spendable_for_slice(100.0, 200.0, reserve=88.0)[0])

ok("the fee reserve is still subtracted on top of it",
   _g.spendable_for_slice(1000.0, 200.0, reserve=88.0, deployment_reserve=50.0)[0]
   == round(200.0 - 88.0 - 50.0, 2))

ok("a slice smaller than what is left is unaffected by the reservation",
   _g.spendable_for_slice(10.0, 500.0, reserve=88.0, deployment_reserve=50.0)[0] == 10.0)

# wallet 138 - 88 fee reserve - 50 held = exactly 0 deployable
_spend, _why = _g.spendable_for_slice(1000.0, 138.0, reserve=88.0, deployment_reserve=50.0)
ok("when the reservation is what stops the buy, the reason says so",
   _spend == 0.0 and "held for coins not yet funded" in _why)

# and when it merely shrinks the buy under the minimum, EVERY subtraction is
# named - blaming the fee reserve alone sends the reader hunting a shortfall
# that is really a deliberate reservation. This one failed first.
_spend3, _why3 = _g.spendable_for_slice(1000.0, 140.0, reserve=88.0, deployment_reserve=50.0)
ok("a sub-minimum spend names the reservation too, not just the fee reserve",
   _spend3 == 0.0 and "held for coins not yet funded" in _why3 and "fee reserve" in _why3)

_spend2, _why2 = _g.spendable_for_slice(1000.0, 300.0, reserve=88.0, deployment_reserve=50.0)
ok("and when it merely bounds the size, the reason names it too",
   _spend2 > 0 and "held for coins not yet funded" in _why2)

ok("a negative or missing reservation is never a credit",
   _g.spendable_for_slice(1000.0, 200.0, reserve=88.0, deployment_reserve=-50.0)[0]
   == _g.spendable_for_slice(1000.0, 200.0, reserve=88.0)[0]
   == _g.spendable_for_slice(1000.0, 200.0, reserve=88.0, deployment_reserve=None)[0])

ok("the reserve is read INSIDE the buy branch, not on every cycle",
   "_dep_reserve, _dep_why = await unfunded_deployment_reserve()" in open(
       os.path.join(os.path.dirname(os.path.abspath(__file__)), "crypto_grid_bot.py"), encoding="utf-8").read())

_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "crypto_grid_bot.py"), encoding="utf-8").read()
_i = _src.index("async def unfunded_deployment_reserve")
_blk = _src[_i:_src.index("def spendable_for_slice", _i)]
ok("an unreadable reserve FAILS OPEN - it must never stop the fleet trading",
   'return 0.0, "reserve could not be computed' in _blk)
ok("and it returns nothing to hold back once every target has a branch",
   "every target coin already has a branch" in _blk)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
