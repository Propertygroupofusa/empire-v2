"""Per-trade instrumentation: what settles the stop level later, on real entries.

Fixed 8% beat ATR x 3 by $0.74 across two windows. That is a tie broken on
simplicity, not a demonstrated edge. These columns are what eventually
settles it - MAE on every real trade answers "would a 5% stop have fired on
this one?" from the closed book, on IDENTICAL entries, instead of a backtest
that also changes which trades happened.

These tests assert the instrumentation cannot affect trading.
"""

import ast
import re
import sys

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


SRC = open("crypto_grid_bot.py").read()
MODELS = open("models.py").read()
TREE = ast.parse(SRC)


def func(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n


CYCLE = ast.get_source_segment(SRC, func("run_grid_branch_cycle")) or ""
LOGFN = ast.get_source_segment(SRC, func("_log_grid_trade")) or ""


def cls_body(name):
    return MODELS.split(f"class {name}(Base):")[1].split("\nclass ")[0]


print("\nthe columns exist and are all nullable")
for cls, cols in (("CryptoGridSlice", ["mae_pct", "mfe_pct", "entry_atr_pct"]),
                  ("CryptoGridTradeHistory",
                   ["exit_reason", "mae_pct", "mfe_pct", "entry_atr_pct", "stop_pct"])):
    body = cls_body(cls)
    for c in cols:
        decl = f"{c} = Column("
        present = decl in body
        nullable = present and "nullable=True" in body.split(decl)[1].split("\n")[0]
        ok(f"{cls}.{c} present and nullable", present and nullable,
           "a non-nullable add would break the ALTER TABLE migration on a live table")


print("\nexcursion tracking cannot break trading")
ok("it is wrapped in try/except", "excursion tracking skipped" in CYCLE)
ok("the failure path is debug, not an exception",
   re.search(r"log\.debug\([^)]*excursion tracking skipped", CYCLE) is not None,
   "instrumentation must never stop the thing it measures")
ok("it runs BEFORE the stop and sell logic, so a sale always sees fresh data",
   CYCLE.index("excursion tracking") < CYCLE.index("STOP LOSS"))
ok("it only writes the two excursion fields, never price/qty/pnl",
   "_row.mae_pct = _exc" in CYCLE and "_row.mfe_pct = _exc" in CYCLE
   and "_row.entry_price" not in CYCLE and "_row.qty" not in CYCLE)


print("\nMAE is the WORST point, MFE the BEST - not swapped")
ok("mae keeps the minimum", "if _row.mae_pct is None or _exc < _row.mae_pct" in CYCLE)
ok("mfe keeps the maximum", "if _row.mfe_pct is None or _exc > _row.mfe_pct" in CYCLE)
ok("excursion is measured against the slice's own entry",
   "_exc = (price / _entry) - 1.0" in CYCLE)


print("\nexit_reason is taken from what actually happened")
ok("_log_grid_trade accepts exit_reason", "exit_reason=None" in LOGFN)
ok("the reason comes from _stop_slice, not the sign of P&L",
   'exit_reason=("stop_loss" if _stop_slice is not None' in CYCLE,
   "pnl < 0 cannot tell a stop from an ordinary sale that happened to lose")
ok("both outcomes are named", '"stop_loss"' in CYCLE and '"profit_target"' in CYCLE)


print("\nthe close carries the slice's recorded excursions through")
for field in ("mae_pct", "mfe_pct", "entry_atr_pct"):
    ok(f"{field} is passed at close from the slice",
       f'{field}=getattr(oldest, "{field}", None)' in CYCLE)
ok("stop_pct records the level that was actually in force",
   "stop_pct=(GRID_STOP_LOSS_PCT or None)" in CYCLE)
ok("a missing value writes None, never 0",
   'getattr(oldest, "mae_pct", None)' in CYCLE,
   "a zero would read as 'never moved' and silently corrupt the later test")



print("\nentry_atr_pct is actually POPULATED, not just declared")
ok("it is written when the slice is created",
   "entry_atr_pct=((_atr / filled_price)" in CYCLE,
   "a declared-but-never-written column is worse than no column: it looks "
   "like data and is always NULL")
ok("it is normalised to a fraction of price, not raw dollars",
   "_atr / filled_price" in CYCLE,
   "raw ATR is not comparable across BTC at $84k and BONK at $0.0000037")
ok("a missing ATR writes None, never 0",
   "if _atr and filled_price else None" in CYCLE)
ok("it reuses the cycle's existing volatility read, no extra API call",
   CYCLE.count("await engine.get_price_and_volatility(") == 1,
   "count the CALL, not mentions - a comment naming the function is not a call")

print("\nno column is declared without a writer")
import re as _re
_written = {c for c in _re.findall(r"(\w+)=", CYCLE)}
for _c in ("entry_atr_pct", "entry_fee_rate", "entry_expected_price"):
    ok(f"CryptoGridSlice.{_c} has a writer in the cycle", _c in _written)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
