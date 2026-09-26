"""Correcting a ledger row is rewriting financial history. Pin it hard.

The coin-history ledger has 11 rows whose recorded P&L cannot be produced
from the entry price, exit price and quantity stored beside them. The fix
recomputes each from its own columns. These tests cover the three ways
that goes wrong:

  * correcting a row that was fine
  * correcting the same row twice, so a corrected value becomes the new
    "error" and drifts further each run
  * destroying the original, so nobody can ever check the correction

and the classification that decides which rows are in scope at all.
"""
import ast
import json

import ledger_correction as LC

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


RATE = LC.DEFAULT_ROUND_TRIP_FEE_RATE


def legacy(entry, exit_, qty, rate=RATE):
    """What the tree used to write: exit leg only."""
    gross = exit_ * qty
    return round(gross - gross * (rate / 2) - entry * qty, 2)


def row(id_=1, qty=500.0, entry=0.10, exit_=0.11, pnl=None, **kw):
    r = {"id": id_, "product_id": "POL-USD", "bot_name": "crypto_tree_pol_usd",
         "qty": qty, "entry_price": entry, "exit_price": exit_,
         "pnl": legacy(entry, exit_, qty) if pnl is None else pnl}
    r.update(kw)
    return r


print("\nthe live bad row, id 48, is caught and repaired")

# POL-USD, entry 0.11766718719983824, exit 0.12084, qty 206.57, booked -91.52.
# A WINNING price move booked as a $91.52 loss on a $24 position.
bad = row(48, qty=206.57, entry=0.11766718719983824, exit_=0.12084, pnl=-91.52)
ok("classified as a basis mismatch, not a fee problem",
   LC.classify(bad) == LC.BASIS_MISMATCH, LC.classify(bad))
p = LC.plan_row(bad)
ok("it is planned for correction", p is not None)
ok("the implied fee rate is absurd, which is what exposes it",
   p["implied_fee_rate"] > 1.0, p["implied_fee_rate"])
ok("corrected to roughly break-even, as the prices say",
   -1.0 < p["pnl_corrected"] < 1.0, p["pnl_corrected"])
ok("the recorded value is carried in the plan, not discarded",
   p["pnl_recorded"] == -91.52)
ok("the delta is about +$91.81", abs(p["delta_usd"] - 91.81) < 0.05, p["delta_usd"])

print("\na row written by the OLD formula is a fee-leg case, not a mismatch")

r = row()
ok("classified FEE_LEG", LC.classify(r) == LC.FEE_LEG, LC.classify(r))
p = LC.plan_row(r)
ok("its implied rate is about half the real one - one leg of two",
   abs(p["implied_fee_rate"] - RATE / 2) < 0.0005, p["implied_fee_rate"])
ok("correcting it makes the row WORSE, never better",
   p["delta_usd"] < 0, p["delta_usd"])
ok("by exactly the entry leg",
   abs(abs(p["delta_usd"]) - 0.10 * 500.0 * (RATE / 2)) < 0.02, p["delta_usd"])

print("\na row already correct is left alone")

good = row(pnl=LC.correct_pnl(500.0, 0.10, 0.11))
ok("classified CLEAN", LC.classify(good) == LC.CLEAN, LC.classify(good))
ok("and planned for nothing", LC.plan_row(good) is None)

print("\nIDEMPOTENCE - the failure that compounds")

r = row(48, qty=206.57, entry=0.11766718719983824, exit_=0.12084, pnl=-91.52)
first = LC.plan_row(r)
# Simulate the write: pnl becomes the corrected value, original preserved.
r["pnl_original"] = r["pnl"]
r["pnl"] = first["pnl_corrected"]
ok("a corrected row is not corrected again",
   LC.plan_row(r) is None,
   "without this, every run drifts the value further")
# TWO independent reasons it cannot drift, and both are worth having.
#
# The arithmetic converges: the corrected value reproduces its own columns,
# so a second pass classifies it CLEAN even with the guard removed. That is
# the stronger property - a correction that is not a fixed point would walk
# the number further every run.
_unguarded = {k: v for k, v in r.items() if k != "pnl_original"}
ok("the corrected value is a FIXED POINT - clean on a second look",
   LC.classify(_unguarded) == LC.CLEAN and LC.plan_row(_unguarded) is None,
   LC.classify(_unguarded))
# The flag is the belt to that pair of braces: it holds even if the rate
# used for the correction ever differs from the rate a later run assumes,
# which would otherwise make every corrected row look like a FEE_LEG case
# forever.
_drifted = dict(_unguarded)
_rerun = LC.plan_row(_drifted, round_trip_fee_rate=0.030)
ok("a DIFFERENT rate would re-plan the row, so the flag earns its place",
   _rerun is not None, str(_rerun))
_drifted["pnl_original"] = -91.52
ok("and pnl_original stops exactly that",
   LC.plan_row(_drifted, round_trip_fee_rate=0.030) is None)
# pnl_original guards even when the stored value looks wrong again.
r2 = row(9, pnl=-999.0, pnl_original=-123.0)
ok("pnl_original is the guard, whatever pnl says now",
   LC.plan_row(r2) is None)

print("\nscope keeps the two mistakes apart")

rows = [row(48, qty=206.57, entry=0.11766718719983824, exit_=0.12084, pnl=-91.52),
        row(1), row(2), row(3)]
narrow = LC.plan(rows, scope="inconsistent")
ok("scope=inconsistent changes only the mismatch",
   narrow["rows_to_change"] == 1, narrow["rows_to_change"])
ok("and SAYS how many it left out rather than hiding them",
   narrow["not_in_scope"]["rows"] == 3, narrow["not_in_scope"])
ok("with the dollars it left on the table",
   narrow["not_in_scope"]["delta_usd"] < 0, narrow["not_in_scope"])

wide = LC.plan(rows, scope="all")
ok("scope=all changes everything that needs it",
   wide["rows_to_change"] == 4, wide["rows_to_change"])
ok("the two kinds are still reported separately",
   wide["basis_mismatch"]["rows"] == 1 and wide["fee_leg"]["rows"] == 3, wide)
ok("their deltas point in opposite directions",
   wide["basis_mismatch"]["delta_usd"] > 0 > wide["fee_leg"]["delta_usd"], wide)

try:
    LC.plan(rows, scope="everything")
    ok("an unknown scope is rejected", False)
except ValueError:
    ok("an unknown scope is rejected", True)

print("\nrows that cannot be repaired by arithmetic are not touched")

ok("no quantity -> no plan", LC.plan_row(row(5, qty=0)) is None)
ok("no entry price -> no plan", LC.plan_row(row(6, entry=0)) is None)
ok("no exit price -> no plan", LC.plan_row(row(7, exit_=0)) is None)
ok("a None pnl is not silently zeroed into a correction",
   LC.plan_row({"id": 8, "qty": 0, "entry_price": None,
                "exit_price": None, "pnl": None}) is None)
ok("garbage types do not raise",
   LC.plan_row({"id": 9, "qty": "x", "entry_price": "y",
                "exit_price": "z", "pnl": "w"}) is None)
ok("zero notional gives an unknown rate, not zero",
   LC.implied_fee_rate(0, 0, 0, 0) is None)

print("\nthe arithmetic matches the bots' own")

# _grid_slice_net_pnl and _tree_realized_pnl charge both legs identically.
ok("100 units 10->11 books 84.25 at 1.5%",
   LC.correct_pnl(100, 10.0, 11.0, 0.015) == 84.25, LC.correct_pnl(100, 10.0, 11.0, 0.015))
ok("a flat trade is negative by both legs",
   LC.correct_pnl(100, 10.0, 10.0, 0.015) == -15.0)
ok("zero fee returns the gross move",
   LC.correct_pnl(100, 10.0, 11.0, 0.0) == 100.0)

print("\nthe plan is data, and says what it cannot do")

plan = LC.plan(rows, scope="all")
json.dumps(plan)
ok("JSON-serialisable", True)
ok("it states that money is not recovered",
   "does not recover money" in plan["note"] or "not recover money" in plan["note"],
   plan["note"])
ok("it names the unaccounted remainder",
   "unaccounted" in plan["note"], plan["note"])
ok("it records the rate used, so the result is reproducible",
   plan["round_trip_fee_rate"] == RATE)

print("\nthe endpoints: preview reads, apply writes and is guarded")

DASH = open("routers/trading_dashboard.py", encoding="utf-8").read()
_t = ast.parse(DASH)


def _deco(fn):
    return [ast.unparse(d) for d in fn.decorator_list]


_fns = {n.name: n for n in ast.walk(_t)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ok("there is a preview endpoint", "preview_ledger_correction" in _fns)
ok("there is an apply endpoint", "apply_ledger_correction" in _fns)
ok("preview is a GET, so it works before the token exists",
   any("router.get" in d for d in _deco(_fns["preview_ledger_correction"])),
   _deco(_fns["preview_ledger_correction"]))
ok("apply is a POST, so write_guard covers it",
   any("router.post" in d for d in _deco(_fns["apply_ledger_correction"])),
   _deco(_fns["apply_ledger_correction"]))

_apply_src = ast.get_source_segment(DASH, _fns["apply_ledger_correction"]) or ""
ok("apply demands confirm=yes on top of the token",
   'confirm != "yes"' in _apply_src)
ok("apply preserves the original only when it is still NULL",
   "if row.pnl_original is None:" in _apply_src,
   "otherwise a retry overwrites the original with a corrected value")
ok("apply writes pnl_original BEFORE pnl",
   _apply_src.index("row.pnl_original = row.pnl") < _apply_src.index('row.pnl = change["pnl_corrected"]'))
ok("apply rolls back on a commit failure",
   "await db.rollback()" in _apply_src)
ok("apply shares one planner with preview, not a second implementation",
   "ledger_correction.plan(" in _apply_src)

_prev_src = ast.get_source_segment(DASH, _fns["preview_ledger_correction"]) or ""
ok("preview writes nothing",
   "db.commit" not in _prev_src and "row.pnl" not in _prev_src)

print("\nthe model keeps a correction trail")

MODELS = open("models.py", encoding="utf-8").read()
for col in ("pnl_original", "corrected_at", "correction_reason"):
    ok(f"  {col} exists and is nullable",
       f"{col} = Column(" in MODELS and
       MODELS.split(f"{col} = Column(")[1].split(")")[0].find("nullable=True") >= 0)
ok("pnl_original is exposed in to_dict, so the trail is visible",
   '"pnl_original": self.pnl_original,' in MODELS)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
