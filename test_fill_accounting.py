"""The fee the learning layer is told about is the fee that was paid.

WHY IT EXISTS. §15 asks that a fill be accounted from what actually
filled, and §16 that realized P&L be gross, fees and net kept distinct and
real - "do not use theoretical P&L as realized P&L".

The sell path already meets most of that: grid_sell() returns the ACTUAL
filled quantity and price, grid_sell_residual() handles a partial fill, and
_grid_slice_net_pnl() prices the round trip with the rate this slice's own
buy leg really paid. What did not meet it was the fee reported alongside
it, which was a second hand-written copy of the fee formula and was wrong
by a factor of about a hundred.

The check that matters most here is the identity the fix rests on:
_grid_slice_net_pnl returns gross - fee, so gross - net IS the fee. If that
ever stops being true, the derived figure is wrong and this test says so
before anyone reads a fabricated fee as a real one.
"""
import ast
import sys

import crypto_grid_bot as g

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


print("== gross minus net is exactly the fee charged ==")
# Real shapes: a LINK-sized slice, a FLOKI-sized one (tiny unit price, huge
# quantity), and a loss - the derived fee must hold for all three, since a
# stop books a negative net and the fee is still positive.
CASES = [
    ("LINK-ish", 4.1, 22.80, 23.016, 0.0070),
    ("FLOKI-ish", 12_000_000.0, 0.00008412, 0.00008520, 0.0070),
    ("a loss", 4.1, 23.50, 22.90, 0.0070),
    ("adopted, exit leg only", 4.1, 22.80, 23.016, 0.0035),
    ("no rise at all", 4.1, 23.00, 23.00, 0.0070),
]
for name, qty, entry, exit_, rate in CASES:
    gross = qty * (exit_ - entry)
    net = g._grid_slice_net_pnl(qty, entry, exit_, rate)
    derived = gross - net
    stated = qty * (entry + exit_) * (rate / 2)
    ok(f"{name}: derived fee equals the formula's fee",
       abs(derived - stated) < max(1e-9, abs(stated) * 1e-9),
       f"derived {derived!r} vs {stated!r}")
    ok(f"{name}: the fee is a cost, never a credit", derived >= 0, str(derived))

print("== the old expression was wrong by about a hundred ==")
# Not an estimate: rate*qty*entry/100 against rate*qty*(entry+exit)/2.
for name, qty, entry, exit_, rate in CASES[:1]:
    real = qty * (entry + exit_) * (rate / 2)
    old = rate * qty * entry / 100
    ok("the old fee is about 1/100th of the real one",
       99 < real / old < 102, f"ratio {real / old!r} (real {real!r}, old {old!r})")
    # What the learning layer would have concluded from it.
    gross = qty * (exit_ - entry)
    ok("and it made a $0.23 trade look like a $0.88 one",
       abs((gross - old) - g._grid_slice_net_pnl(qty, entry, exit_, rate)) > 0.5,
       f"old net {gross - old!r} vs real {g._grid_slice_net_pnl(qty, entry, exit_, rate)!r}")

print("== the call site does not rebuild the formula ==")
_tree = ast.parse(open("crypto_grid_bot.py").read())
_calls = [n for n in ast.walk(_tree)
          if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
          and n.func.attr == "on_position_closed"]
ok("the shadow close call is still there", len(_calls) == 1, str(len(_calls)))
if _calls:
    _kw = {k.arg: k.value for k in _calls[0].keywords if k.arg}
    ok("it reports a fee at all", "total_fees" in _kw, str(sorted(_kw)))
    _fee = _kw.get("total_fees")
    # Not just "a plain name": a plain name that is the DERIVED fee. Any
    # other number already in scope there - gross_pnl, pnl, filled_qty - is
    # also a plain name, and passing one would be silent.
    ok("the fee it reports is the derived one",
       isinstance(_fee, ast.Name) and _fee.id == "fees_paid",
       getattr(_fee, "id", type(_fee).__name__))
    # The specific shape that was wrong: any division inside the argument.
    _divs = [n for n in ast.walk(_fee)
             if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)] if _fee else []
    ok("it divides by nothing", not _divs, str(len(_divs)))
    _awaits = [n for n in ast.walk(_fee) if isinstance(n, ast.Await)] if _fee else []
    ok("it does not re-await the rate", not _awaits, str(len(_awaits)))
    _pnl = _kw.get("realized_pnl")
    # shadow_mode_init.on_position_closed does `gross_pnl = realized_pnl`
    # and `net_pnl = realized_pnl - fees`. That module is the one imported
    # here, so it wants GROSS, whatever bot_integration_points' own
    # docstring says about the same parameter name.
    ok("it reports gross, which is what the imported consumer subtracts from",
       isinstance(_pnl, ast.Name) and _pnl.id == "gross_pnl",
       getattr(_pnl, "id", type(_pnl).__name__))

print("== the derived fee is bound from the booked numbers ==")
_assigns = [n for n in ast.walk(_tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "fees_paid" for t in n.targets)]
ok("fees_paid is assigned exactly once", len(_assigns) == 1, str(len(_assigns)))
if _assigns:
    v = _assigns[0].value
    ok("and it is a subtraction",
       isinstance(v, ast.BinOp) and isinstance(v.op, ast.Sub), type(v).__name__)
    # Order matters and a name-set check cannot see it: pnl - gross_pnl is
    # the same two names and is the fee with the sign flipped, which the
    # consumer would then ADD to the P&L.
    ok("with gross on the left",
       isinstance(v, ast.BinOp) and isinstance(v.left, ast.Name)
       and v.left.id == "gross_pnl",
       ast.dump(v.left) if isinstance(v, ast.BinOp) else "?")
    ok("and the booked net on the right",
       isinstance(v, ast.BinOp) and isinstance(v.right, ast.Name)
       and v.right.id == "pnl",
       ast.dump(v.right) if isinstance(v, ast.BinOp) else "?")

print("== the fill path uses what filled, not what was asked for ==")
# §15: "Never assume the requested order quantity equals the filled
# quantity." grid_sell_residual exists precisely for the gap.
ok("a full fill retires the slice", g.grid_sell_residual(4.1, 4.1)[1] is True,
   str(g.grid_sell_residual(4.1, 4.1)))
_res, _ret = g.grid_sell_residual(4.1, 2.0)
ok("a half fill leaves the remainder on the slice", abs(_res - 2.1) < 1e-9, str(_res))
ok("  and does not retire it", _ret is False, str(_ret))
_res, _ret = g.grid_sell_residual(4.1, 4.0999999)
ok("a rounding-dust remainder still retires", _ret is True, str((_res, _ret)))

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all fill-accounting checks passed")
