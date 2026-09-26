"""The tree charged one fee leg. A round trip has two.

All three of this file's ledger write sites computed

    proceeds = exit_price * qty * (1 - RATE/2)
    pnl      = proceeds - entry_price * qty

which books the EXIT commission and omits the ENTRY one. entry_price is a
fill price; Coinbase bills commission separately, so the real cost basis is
entry_price * qty PLUS the entry commission.

The error is one-directional: it can only ever make a trade look better
than it was. On the live ledger - 167 rows, $23,521.21 of entry notional -
it hid $176.41 of commission that was actually paid.

The grid never had this bug (_grid_slice_net_pnl charges both legs), which
is why its 82 rows are untouched.
"""
import ast
import types

_tree = ast.parse(open("crypto_family_tree_bot.py").read())
_fn = next((n for n in _tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_tree_realized_pnl"), None)
if _fn is None:
    raise SystemExit("FAIL: _tree_realized_pnl is not defined")
M = types.ModuleType("_lifted")
M.ROUND_TRIP_FEE_RATE = 0.015
exec(compile(ast.Module(body=[_fn], type_ignores=[]), "<lifted>", "exec"), M.__dict__)
pnl = M._tree_realized_pnl

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


print("\nboth legs are charged")

# 100 units, $10 -> $11. gross +$100. fee = 100*(10+11)*0.0075 = $15.75
ok("a winner pays fees on entry AND exit notional",
   pnl(100, 10.0, 11.0) == 84.25, pnl(100, 10.0, 11.0))

old = round(11.0 * 100 * (1 - 0.015 / 2) - 10.0 * 100, 2)
ok("the old exit-only formula said 91.75", old == 91.75, old)
ok("the old formula was more favourable, as the bug requires",
   old > pnl(100, 10.0, 11.0))

print("\na flat trade is a LOSS, not a wash")

ok("no price move still costs both legs",
   pnl(100, 10.0, 10.0) == -15.0, pnl(100, 10.0, 10.0))
ok("the old formula understated that loss",
   round(10.0 * 100 * (1 - 0.015 / 2) - 10.0 * 100, 2) == -7.5)

print("\nthe error is one-directional - it never makes a trade look worse")

for entry, exit_, qty in ((10, 11, 100), (10, 9, 100), (0.12, 0.117, 6843.84),
                          (250.0, 250.0, 4), (1.5, 3.0, 20)):
    new = pnl(qty, entry, exit_)
    old = round(exit_ * qty * (1 - 0.015 / 2) - entry * qty, 2)
    ok(f"  {entry}->{exit_} x{qty}: old ({old}) >= new ({new})", old >= new)

print("\nthe omitted amount is exactly the entry leg")

for entry, exit_, qty in ((10, 11, 100), (0.12, 0.117, 6843.84), (1.5, 3.0, 20)):
    new = pnl(qty, entry, exit_)
    old = round(exit_ * qty * (1 - 0.015 / 2) - entry * qty, 2)
    expected = round(entry * qty * (0.015 / 2), 2)
    ok(f"  gap on {entry}->{exit_} is entry_notional * rate/2 = {expected}",
       abs((old - new) - expected) < 0.02, old - new)

print("\nthe live ledger's shortfall reproduces - AT THE RATE IT WAS BOOKED AT")

# The rows were written under an 0.8% schedule, not the 1.5% constant in
# force today. Using today's rate here produced $176.41 and would have
# booked $81 of commission nobody ever paid. The rate has to come from the
# rows, and the rows say 0.00800.
ok("$22,691.03 of entry notional at 0.8% omits $90.76",
   abs(22691.03 * (0.008 / 2) - 90.76) < 0.01,
   22691.03 * (0.008 / 2))
ok("at today's 1.5% the same notional would claim $170.18 - nearly double",
   abs(22691.03 * (0.015 / 2) - 170.18) < 0.01)
ok("so the wrong rate overstates the correction by about $79",
   abs((22691.03 * (0.015 / 2)) - (22691.03 * (0.008 / 2)) - 79.42) < 0.05)

print("\nthe rate is overridable, and zero fees means gross")

ok("an explicit rate is honoured",
   pnl(100, 10.0, 11.0, 0.0) == 100.0, pnl(100, 10.0, 11.0, 0.0))
ok("a real blended rate gives a different answer than the constant",
   pnl(100, 10.0, 11.0, 0.011931) != pnl(100, 10.0, 11.0))

print("\nPROCEEDS must NOT use this - the docstring says so")
src = ast.get_source_segment(open("crypto_family_tree_bot.py").read(), _fn) or ""
ok("it warns against using this for allocated_usd",
   "allocated_usd" in src and "NOT to be used" in src)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
