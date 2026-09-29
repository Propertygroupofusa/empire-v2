"""The floor is never undercut, and a market that cannot clear it gets no trade.

WHY IT EXISTS. §6 opens with "Do NOT simply increase TP to make the system
appear more profitable", and the account's standing rule is that no
threshold is ever lowered to manufacture activity. Both failure modes point
the same way: the edge requirement must be a floor that binds, and the
answer to a market that cannot clear it must be NO TRADE rather than a
smaller ask.

So almost every check here is about a number that must NOT come out.
"""
import sys

import slice_edge as se
import slice_target as st

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


FLOOR = 0.010  # GRID_PARKED_MIN_NET_PCT, the account's own threshold

print("== the floor binds ==")
p = se.plan_edge(configured_floor_pct=FLOOR, requested_edge=0.005)
ok("a request under the floor is raised to it", p.target_edge == FLOOR, str(p.target_edge))
ok("and it still sets a target", p.should_set_target)
p = se.plan_edge(configured_floor_pct=FLOOR, requested_edge=0.02)
ok("a request above the floor is honoured", p.target_edge == 0.02)
p = se.plan_edge(configured_floor_pct=FLOOR)
ok("no request at all falls back to the floor", p.target_edge == FLOOR)
p = se.plan_edge(configured_floor_pct=FLOOR, requested_edge=0.0)
ok("a zero request is raised to the floor", p.target_edge == FLOOR)
p = se.plan_edge(configured_floor_pct=FLOOR, requested_edge=-0.05)
ok("a negative request is raised to the floor", p.target_edge == FLOOR)

print("== the adverse margin can only raise the floor ==")
ok("a positive margin raises it",
   se.minimum_profitable_edge(FLOOR, 0.002) == FLOOR + 0.002)
ok("a negative margin cannot lower it",
   se.minimum_profitable_edge(FLOOR, -0.005) == FLOOR)
ok("an absent margin leaves it alone", se.minimum_profitable_edge(FLOOR) == FLOOR)
p = se.plan_edge(configured_floor_pct=FLOOR, requested_edge=FLOOR,
                 adverse_margin_pct=0.003)
ok("a request equal to the bare floor is raised by the margin",
   p.target_edge == FLOOR + 0.003, str(p.target_edge))

print("== the floor itself is never invented ==")
for bad in (None, "abc", float("nan"), -1):
    p = se.plan_edge(configured_floor_pct=bad)
    ok(f"floor={bad!r} refuses", p.decision == se.REFUSED, p.decision)
    ok(f"  and sets no edge for {bad!r}", p.target_edge is None)
ok("the refusal names the floor as the gap",
   se.plan_edge(configured_floor_pct=None).reason == se.FLOOR_UNKNOWN)
ok("and says why it will not default one",
   "threshold lowered" in se.plan_edge(configured_floor_pct=None).detail)

print("== a market that cannot clear the floor gets NO trade ==")
p = se.plan_edge(configured_floor_pct=FLOOR, expected_move_pct=0.004)
ok("an expected move under the floor means DO_NOT_TRADE",
   p.decision == se.DO_NOT_TRADE, p.decision)
ok("and the reason names the market", p.reason == se.MARKET_CANNOT_SUPPORT)
ok("no target edge is produced", p.target_edge is None)
# The specific failure this guards: quietly shrinking the ask to fit.
ok("it does NOT return a reduced target", p.target_edge is None)
ok("and says lowering the ask would move a threshold",
   "manufacture activity" in p.detail)
p = se.plan_edge(configured_floor_pct=FLOOR, expected_move_pct=FLOOR)
ok("a move exactly at the floor is allowed", p.should_set_target, p.reason)
p = se.plan_edge(configured_floor_pct=FLOOR, expected_move_pct=None)
ok("an UNKNOWN move does not permit a trade under the floor",
   p.target_edge == FLOOR, str(p.target_edge))
ok("  and an unknown move is not treated as a large one",
   p.expected_move_pct is None)

print("== adaptive widening is off by default and can only raise ==")
p = se.plan_edge(configured_floor_pct=FLOOR, requested_edge=FLOOR,
                 expected_move_pct=0.05)
ok("a generous market does NOT widen the target by default",
   p.target_edge == FLOOR, str(p.target_edge))
p = se.plan_edge(configured_floor_pct=FLOOR, requested_edge=FLOOR,
                 expected_move_pct=0.05, allow_adaptive_widening=True)
ok("with widening on it rises toward the move", p.target_edge == 0.05)
ok("  but never beyond what the market offers",
   p.target_edge <= 0.05)
p = se.plan_edge(configured_floor_pct=FLOOR, requested_edge=0.03,
                 expected_move_pct=0.015, allow_adaptive_widening=True)
ok("widening never LOWERS an already-higher request",
   p.target_edge == 0.03, str(p.target_edge))

print("== the chosen edge feeds a target that really clears it ==")
# End to end against the real fee formula, via slice_target.
from crypto_grid_bot import _grid_slice_net_pnl
for entry, rate in ((100.0, 0.007), (2654.53, 0.007), (0.57783, 0.0075)):
    p = se.plan_edge(configured_floor_pct=FLOOR, requested_edge=0.005)
    t = st.target_price(entry, rate, p.target_edge)
    got = _grid_slice_net_pnl(1.0, entry, t, rate) / entry
    ok(f"entry={entry} nets the FLOOR, not the under-floor request",
       abs(got - FLOOR) < 1e-9, f"got {got!r}")

print("== a bad requested edge refuses rather than defaulting ==")
for bad in ("abc", float("nan"), float("inf")):
    p = se.plan_edge(configured_floor_pct=FLOOR, requested_edge=bad)
    ok(f"requested={bad!r} refuses", p.decision == se.REFUSED, p.decision)
    ok(f"  and yields no edge for {bad!r}", p.target_edge is None)

print("== it does not re-derive the buy-side gate ==")
import ast, inspect
tree = ast.parse(inspect.getsource(se))
_names = {n.name for n in ast.walk(tree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("it defines no second net-edge gate",
   not any("net_edge_gate" in n for n in _names), str(_names))
ok("and no second fee formula",
   "_grid_slice_net_pnl" not in _names and "target_price" not in _names)
_imports = {a.module for a in ast.walk(tree) if isinstance(a, ast.ImportFrom)}
_imports |= {n.name.split(".")[0] for a in ast.walk(tree)
             if isinstance(a, ast.Import) for n in a.names}
ok("it imports no market data of its own", not (_imports & {"aiohttp", "requests"}),
   str(_imports))

print()
if failures:
    print("FAILED %d check(s): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all slice-edge checks passed")
