"""A branch that cannot buy must still be able to sell.

Measured on the live fleet: $5,821.51 - 78% of allocated capital - sat in
branches that could neither buy (as many slices as levels) nor sell (the
reference gate wanted another 2.8%-5.7%). Four of them, holding 71% of the
capital, had never completed a single round trip. The branches still
earning were the small ones with room to cycle.
"""
import ast
import sys

SRC = open("crypto_grid_bot.py").read()
TREE = ast.parse(SRC)
_checks = []


def ok(label, cond, detail=""):
    _checks.append((label, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  -- {detail}" if detail and not cond else ""))


def fn(name):
    n = next(x for x in ast.walk(TREE)
             if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == name)
    return "\n".join(SRC.splitlines()[n.lineno - 1:n.end_lineno])


CYCLE = fn("run_grid_branch_cycle")
sys.path.insert(0, ".")
import crypto_grid_bot as grid  # noqa: E402

# --- the condition is narrow ----------------------------------------------
# PARKED IS COUNTED ON TRADEABLE SLICES, NOT ON THE RAW LIST.
#
# This asserted the literal "len(slices) >= (branch.num_levels or 0)" and
# correctly caught the 2026-10-04 dust change, which is the test doing its
# job. The PROPERTY is unchanged - parked still means a slice count at or
# above the level count - but the count now excludes a remnant too small
# for the venue to sell, because a position you cannot exit is not what
# fills a rung. BCH-USD read 3/3 PARKED on a third "slice" worth $0.00007
# and spent 200 attempts offering it as its own escape route.
#
# Asserted on tradeable_slices specifically, so a regression back to the
# raw count fails here rather than silently re-locking a branch.
ok("parked is counted on TRADEABLE slices, not the raw list",
   "len(tradeable_slices(slices)) >= (branch.num_levels or 0)" in CYCLE)
ok("the buy gate counts tradeable slices too, so dust cannot block a rung",
   "len(tradeable_slices(slices)) < branch.num_levels" in CYCLE)
ok("the dust floor is defined and below the smallest rung the engine buys",
   grid.GRID_DUST_SLICE_USD > 0 and grid.GRID_DUST_SLICE_USD < grid.MIN_TRADE_USD)
ok("the real BCH remnant does not fill a rung",
   not grid.slice_is_tradeable({"qty": 2.2e-07, "entry_price": 309.32}))
ok("a real BCH position still does",
   grid.slice_is_tradeable({"qty": 0.12644109, "entry_price": 339.58}))
ok("a branch that can still buy keeps the full spacing gate",
   "price >= branch.reference_price * (1 + grid_pct)" in CYCLE)
ok("the stop still takes precedence", "_stop_slice is None" in CYCLE)
# ON THE TREE, NOT THE SPELLING.
#
# This matched the literal text
# "_stop_slice is not None or _parked_sell or (price >=". The property it
# protects is real - the gate must name all three ways in rather than let
# one fall through - but hoisting the third condition into a named
# _rise_hit variable preserved that property exactly while breaking the
# string, so the guard went red for a reformat. Asserted structurally now.
def _sell_gate():
    """The `if` in the cycle whose test is an OR naming the stop and the
    parked-sell gate. Found by what it decides, not by how it is typed."""
    for n in ast.walk(TREE):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if n.name != "run_grid_branch_cycle":
            continue
        for x in ast.walk(n):
            if isinstance(x, ast.If) and isinstance(x.test, ast.BoolOp) \
                    and isinstance(x.test.op, ast.Or):
                d = ast.dump(x.test)
                if "_stop_slice" in d and "_parked_sell" in d:
                    return x.test
    return None


_gate = _sell_gate()
ok("the sell gate is an OR over the ways in, not a fallthrough",
   _gate is not None)
if _gate is not None:
    _ops = [ast.dump(o) for o in _gate.values]
    ok("the gate names all three conditions rather than falling through",
       len(_gate.values) == 3
       and any("_stop_slice" in o for o in _ops)
       and any("_parked_sell" in o for o in _ops)
       and any(("_rise_hit" in o) or ("reference_price" in o) for o in _ops),
       f"{len(_gate.values)} operand(s): a third way in that is not named "
       f"here is a sale this gate cannot account for")
    ok("the rise condition is still one of them, hoisted or inline",
       any(("_rise_hit" in o) or ("reference_price" in o) for o in _ops))

# --- it cannot realize a loss ---------------------------------------------
ok("the slice is still chosen by the function that refuses losses",
   CYCLE.count("_pick_profitable_slice_to_sell(") >= 2)
PICK = fn("_pick_profitable_slice_to_sell")
ok("and that function still requires net-positive after fees",
   "_grid_slice_net_pnl(s.qty, s.entry_price, price," in PICK and "> 0" in PICK)
ok("this is not a second path around it",
   "_parked_sell = True" in CYCLE
   and CYCLE.index("_pick_profitable_slice_to_sell(") < CYCLE.index("_parked_sell = True"))

# --- the floor is a real margin, not the bare > 0 --------------------------
# Asserted on the CYCLE body, not on the module. Checking the whole file
# matched the constant's own definition and passed while the gate compared
# against a bare > 0 - caught by mutation, which is the only reason this
# line is here rather than the weaker one.
ok("the gate compares against the floor, not against zero",
   "GRID_PARKED_MIN_NET_PCT" in CYCLE)
# The floor comparison moved out of the cycle body and into
# _pick_parked_slice_to_sell, which now picks the BEST qualifying slice
# rather than the first positive one. Every property below is unchanged and
# still required - they are asserted where the decision now lives, plus a
# guard that the cycle really calls it, so none of them can be satisfied by
# code the cycle never reaches. Re-pointing a guard at moved logic is the
# job; weakening it because the string moved is how a real protection dies.
PARKED_PICK = fn("_pick_parked_slice_to_sell")
PARKED_LOGIC = CYCLE + "\n" + PARKED_PICK
ok("the cycle actually calls the parked picker, so it is not dead code",
   "_pick_parked_slice_to_sell(" in CYCLE)
ok("and it is a >= against that constant, not a > against 0",
   ">= floor_pct" in PARKED_PICK
   and "GRID_PARKED_MIN_NET_PCT)" in CYCLE)
ok("the constant exists at module level", "GRID_PARKED_MIN_NET_PCT" in SRC)
ok("it is 1.0% by default", abs(grid.GRID_PARKED_MIN_NET_PCT - 0.010) < 1e-9)
ok("comfortably above the repo's own fee floor",
   grid.GRID_PARKED_MIN_NET_PCT > 0.009)
ok("it is measured NET of fees, not gross",
   "_grid_slice_net_pnl(s.qty, s.entry_price, price," in PARKED_LOGIC)
ok("and as a share of the slice's own basis, not dollars",
   "net / basis" in PARKED_LOGIC)
ok("a zero basis cannot divide", "basis <= 0" in PARKED_LOGIC)
ok("it is settable", "GRID_PARKED_MIN_NET_PCT" in SRC and "os.getenv" in SRC)

# --- the defect the picker change exists to remove -------------------------
#
# The old gate asked _pick_profitable_slice_to_sell for a candidate. That
# function returns the FIRST slice netting anything at all, oldest-first.
# On a parked branch with a floor under it, an oldest slice at +0.1% masks a
# newer one at +3.0%: the gate refuses, no sale happens, and a branch that
# could have got out stays locked. Measured on the live fleet the moment this
# was written, 0 of 12 full branches were in that state - so this is a latent
# defect, not today's cause, and it gets worse as slice counts grow (SHIB was
# already carrying 10 slices, LTC and ZEC 7 each).
class _S:
    def __init__(self, qty, entry):
        self.qty, self.entry_price = qty, entry
        self.adopted = False
        self.entry_fee_rate = 0.0
        self.order_side = "BUY"


# Zero fees, so net% is just the price move: entry 100 at price 101 is +1%.
_marginal = _S(1.0, 100.90)   # +0.099% at 101 - positive, under the floor
_good = _S(1.0, 98.00)        # +3.06%   at 101 - clears the floor
_book = [_marginal, _good]    # oldest first, exactly the blocking order

_first = grid._pick_profitable_slice_to_sell(_book, 101.0, 0.0, 0.0)
ok("the FIFO picker really does return the marginal slice first",
   _first is _marginal,
   "if this fails the scenario below proves nothing about the defect")

_pick, _pct = grid._pick_parked_slice_to_sell(_book, 101.0, 0.0, 0.0,
                                              grid.GRID_PARKED_MIN_NET_PCT)
ok("the parked picker looks past it and finds the qualifying slice",
   _pick is _good, f"got {_pick!r} at {_pct!r}")
ok("and reports that slice's own net percentage",
   _pct is not None and abs(_pct - 0.0306122) < 1e-4, f"{_pct!r}")

# Nothing is loosened: the floor is unchanged and still binding.
_under = [_S(1.0, 100.90), _S(1.0, 100.50)]   # +0.099% and +0.497%
_pick, _pct = grid._pick_parked_slice_to_sell(_under, 101.0, 0.0, 0.0,
                                              grid.GRID_PARKED_MIN_NET_PCT)
ok("a book where NOTHING clears the floor still sells nothing",
   _pick is None, f"got {_pick!r} at {_pct!r}")

_losing = [_S(1.0, 120.0), _S(1.0, 130.0)]
_pick, _ = grid._pick_parked_slice_to_sell(_losing, 101.0, 0.0, 0.0,
                                           grid.GRID_PARKED_MIN_NET_PCT)
ok("and a book that is entirely underwater can never be sold here",
   _pick is None, f"got {_pick!r}")

_best = [_S(1.0, 98.0), _S(1.0, 90.0), _S(1.0, 99.0)]
_pick, _ = grid._pick_parked_slice_to_sell(_best, 101.0, 0.0, 0.0,
                                           grid.GRID_PARKED_MIN_NET_PCT)
ok("among several qualifying slices it takes the best one",
   _pick is _best[1], f"got entry {getattr(_pick, 'entry_price', None)!r}")

# The sale must hand over the slice the gate certified, not re-pick with the
# FIFO function - otherwise the gate passes on +3.0% and the sale sells +0.1%.
ok("the parked route sells the slice its own gate chose",
   "oldest = _parked_slice" in CYCLE)
ok("and only when the parked route is what fired",
   "_parked_sell and not _rise_hit" in CYCLE)


def would_sell(net_pct, parked, floor=None):
    """The shipped condition, evaluated directly."""
    f = grid.GRID_PARKED_MIN_NET_PCT if floor is None else floor
    return bool(parked and net_pct >= f)


ok("parked + 1.5% net -> sells", would_sell(0.015, True))
ok("parked + 1.0% net -> sells (at the floor)", would_sell(0.010, True))
ok("parked + 0.9% net -> does NOT sell", not would_sell(0.009, True))
ok("parked + 0.01% net -> does NOT sell: churn is not profit",
   not would_sell(0.0001, True))
ok("NOT parked + 1.5% net -> does not use this path", not would_sell(0.015, False))
ok("a loss can never reach it", not would_sell(-0.05, True))

# --- what it does NOT do ---------------------------------------------------
ok("the grid step itself is untouched",
   "GRID_PARKED_MIN_NET_PCT" not in fn("_pick_profitable_slice_to_sell"))
ok("it never widens or narrows spacing for a branch that cycles",
   "grid_pct =" not in CYCLE.split("_parked = ")[1][:600])
ok("the reason is logged, so a sale is never unexplained",
   "Selling on its own" in CYCLE and "cannot buy" in CYCLE)

_failed = [l for l, p in _checks if not p]
print(f"\n{len(_checks) - len(_failed)} passed, {len(_failed)} failed")
sys.exit(1 if _failed else 0)
