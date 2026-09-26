"""The reconciliation must count CLOSES on SPOT, not fills on everything.

Three versions of this comparison existed and two were wrong:

  74%  counted every fill on the account and halved it
  51%  excluded event contracts but still halved fills
  11.4% counts distinct SELL orders on -USD pairs only

The difference is not cosmetic - the first number was read as "the ledger
is worthless" and nearly caused the modelling work to be thrown out.

These tests pin the third.
"""
import ast
import json
import types

# routers/trading_dashboard.py imports fastapi, which is not installed in the
# test environment. Lifting the one function out by AST and EXECUTING it keeps
# this a real test of behaviour rather than a grep over source text - the
# distinction that let an earlier "the endpoint exists" check pass while the
# endpoint's arithmetic was wrong.
_tree = ast.parse(open("routers/trading_dashboard.py").read())
_fn = next((n for n in _tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_reconcile"), None)
if _fn is None:
    raise SystemExit("FAIL: _reconcile is not defined in routers/trading_dashboard.py")
T = types.ModuleType("_lifted")
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "<lifted>", "exec"), T.__dict__)

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def prod(pid, fills=0, orders=0, buys=0, sells=0, comm=0.0):
    return {"product_id": pid, "fills": fills, "orders": orders,
            "buy_orders": buys, "sell_orders": sells, "commission_usd": comm}


print("\nevent contracts are excluded from the ledger comparison")

st = {"fills": 1951, "commission_usd": 1857.01, "products": [
    prod("POL-USD", fills=315, orders=202, buys=114, sells=88, comm=106.42),
    prod("BTC-USD", fills=90, orders=64, buys=49, sells=15, comm=85.77),
    prod("KXBTC15M-26SEP060915-15-KALSHI", fills=90, orders=9, buys=4, sells=5, comm=242.37),
]}
r = T._reconcile(st, recorded_rows=100)
ok("two spot products", r["spot"]["products"] == 2, r["spot"])
ok("one event-contract product", r["event_contracts"]["products"] == 1)
ok("closes counts spot sells only, 88+15=103",
   r["closes_at_exchange"] == 103, r["closes_at_exchange"])
ok("the event contract's 5 sells are NOT counted as closes",
   r["closes_at_exchange"] == 103)
ok("spot commission excludes the event contract",
   abs(r["spot"]["commission_usd"] - 192.19) < 0.01, r["spot"]["commission_usd"])
ok("event commission is reported separately, not hidden",
   abs(r["event_contracts"]["commission_usd"] - 242.37) < 0.01)

print("\nthe gap is closes minus rows, not fills halved")

ok("gap is 3", r["gap"] == 3, r["gap"])
ok("gap_pct is 2.9", r["gap_pct"] == 2.9, r["gap_pct"])
ok("fills/2 would have claimed 247 round trips", st["fills"] // 2 == 975)
ok("the basis no longer says a round trip is two fills",
   "two fills" not in r["basis"].lower(), r["basis"])
ok("the basis names sell orders", "sell order" in r["basis"].lower())

print("\nthe event-contract note says they are Coinbase, not elsewhere")

note = r["event_contracts"]["note"]
ok("it says Coinbase", "Coinbase" in note, note)
ok("it explains the -KALSHI tag as a settlement venue",
   "settles on" in note, note)
ok("it warns that expiry is invisible to a fills feed",
   "expir" in note.lower() and "not a fill" in note, note)

print("\ndegenerate inputs do not raise")

r0 = T._reconcile({"products": []}, recorded_rows=0)
ok("no products -> zero closes", r0["closes_at_exchange"] == 0)
ok("no products -> gap_pct is None, not a divide-by-zero",
   r0["gap_pct"] is None, r0["gap_pct"])

rn = T._reconcile({"products": [prod("X-USD")]}, recorded_rows=5)
ok("more rows than closes gives a negative gap, not an absolute value",
   rn["gap"] == -5, rn["gap"])

print("\nthe whole block serialises")
json.dumps(r)
ok("JSON-serialisable", True)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
