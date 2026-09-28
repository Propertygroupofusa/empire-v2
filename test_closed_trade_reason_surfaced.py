"""A closed grid trade must carry the reason it closed.

WHY THIS FILE EXISTS. _log_grid_trade writes seven analysis columns on
every completed round trip - exit_reason, stop_pct, mae_pct, mfe_pct,
entry_atr_pct, entry_spread_pct, entry_gate_json - and
CryptoGridTradeHistory.to_dict() dropped all seven. Every consumer of the
table goes through to_dict(), so the trade-history endpoint, the dashboard
and any later experiment were served rows with the analysis stripped out.

exit_reason is the one that stings. The model's own comment on that column
says it exists because "without it the ledger shows a loss and cannot say
whether the stop did its job or the grid sold badly" - and the ledger could
not say, because the value never left the database. Same shape as
GridMakerExpiry: a system that records the answer and cannot repeat it.

SEPARATELY, the reason itself has to be a reason. The shadow-analytics
path computed it as `'profit_target' if pnl >= 0 else 'stop_loss'`, which
is the sign of the P&L under a different name: it adds nothing the P&L does
not already carry, while being labelled as the independent fact that
explains it. A profit-target sale left slightly negative by fees came
through as a stop; a stop that closed up came through as a target hit. The
persisted ledger got this right and its own comment already said the sign
"cannot tell a stop from an ordinary sale that happened to lose" - the two
write sites simply disagreed, with the honest source in scope at both.

The to_dict checks are behavioural (a real instance, real attributes). The
write-site check is on the parsed tree, because it is about which
expression is used, not what a call returns.
"""
import ast
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


ANALYSIS = ("exit_reason", "stop_pct", "mae_pct", "mfe_pct",
            "entry_atr_pct", "entry_spread_pct", "entry_gate_json")

print("== every column written on close is readable ==")

import models
from datetime import datetime

cls = models.CryptoGridTradeHistory
cols = {c.name for c in cls.__table__.columns}
ok("the model still carries all seven analysis columns",
   set(ANALYSIS) <= cols,
   f"missing from the table: {sorted(set(ANALYSIS) - cols)}")

row = cls()
row.id = 1
row.bot_name = "crypto_grid_9"
row.product_id = "TIA-USD"
row.entry_price = 2.10
row.exit_price = 2.31
row.qty = 5.0
row.pnl = 1.05
row.opened_at = datetime(2026, 9, 28, 10, 0, 0)
row.closed_at = datetime(2026, 9, 28, 12, 0, 0)
row.exit_reason = "stop_loss"
row.stop_pct = 8.0
row.mae_pct = -3.2
row.mfe_pct = 4.4
row.entry_atr_pct = 1.9
row.entry_spread_pct = 0.04
row.entry_gate_json = '{"net_edge_pct": 0.31}'

d = row.to_dict()

# EVERY one, not "at least exit_reason" - a fix that surfaces the headline
# field and leaves the other six buried is the same bug with better PR.
for f in ANALYSIS:
    ok(f"to_dict carries {f}", f in d, "written on every close by "
       "_log_grid_trade and dropped by to_dict until now")

ok("and the values are the real ones, not placeholders",
   d.get("exit_reason") == "stop_loss" and d.get("mae_pct") == -3.2
   and d.get("entry_gate_json") == '{"net_edge_pct": 0.31}',
   f"got exit_reason={d.get('exit_reason')} mae={d.get('mae_pct')}")

# NEVER-OMITS, stated over the table rather than a fixed list, so a column
# added later is covered without anyone remembering to update this file.
_dropped = sorted(c for c in cols if c not in d)
ok("to_dict omits NO column of this table at all",
   not _dropped,
   f"dropped: {_dropped} - a column written on every close and absent from "
   f"the only serialiser is data collected and never read")

# An unset analysis field must come through as None, not be absent and not
# be coerced: a gap in one trade's record is not a zero.
bare = cls()
bare.id = 2
bare.opened_at = None
bare.closed_at = None
b = bare.to_dict()
for f in ANALYSIS:
    ok(f"an unrecorded {f} is present as None, not missing",
       f in b and b[f] is None,
       f"got {b.get(f, '<absent>')!r} - absent and zero are both wrong here")


print("== the reason is a reason, not the P&L sign renamed ==")

src = open("crypto_grid_bot.py", encoding="utf-8").read()
tree = ast.parse(src)

assigns = []
for n in ast.walk(tree):
    if isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name) and t.id == "exit_reason":
                assigns.append(n.value)
ok("exit_reason is assigned somewhere in the grid bot", bool(assigns))

# The defect shape: an IfExp whose test compares pnl against zero.
def _is_pnl_sign(node):
    if not isinstance(node, ast.IfExp):
        return False
    dumped = ast.dump(node.test)
    return "pnl" in dumped and "Compare" in dumped

ok("NO assignment derives exit_reason from the sign of the P&L",
   not any(_is_pnl_sign(a) for a in assigns),
   "`'profit_target' if pnl >= 0 else 'stop_loss'` restates the P&L and "
   "calls it the explanation; it cannot tell a stop from an ordinary sale "
   "that happened to lose")

# And the honest source must be what is used, at every site.
def _uses_stop_slice(node):
    return "_stop_slice" in (ast.dump(node) or "")

ok("every exit_reason assignment is derived from _stop_slice",
   bool(assigns) and all(_uses_stop_slice(a) for a in assigns),
   "_stop_slice is set only when the stop actually chose the slice, which "
   "is the only independent fact available here")

# THE FORCED EXIT IS A THIRD REASON, NOT AN ABSENT ONE.
#
# close_all_grid_branches logged its trades with no exit_reason at all, so
# every owner-requested close landed as None - UNKNOWN - when the reason
# was never in doubt. A close-all is a market exit at the taker rate; it is
# neither a target nor a stop, and lumping it into the unknown bucket
# contaminates the one question the column exists to answer.
_reason_values = []
for n in ast.walk(tree):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
            and n.func.id == "_log_grid_trade":
        for k in n.keywords:
            if k.arg == "exit_reason":
                _reason_values.append(ast.dump(k.value))
ok("a close-all trade records close_all, not nothing",
   any("close_all" in v for v in _reason_values),
   f"exit_reason expressions at the log sites: {len(_reason_values)}")

# The persisted ledger call must pass it too - surfacing a field that is
# never written is no better than writing one that is never read.
logged = []
for n in ast.walk(tree):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
            and n.func.id == "_log_grid_trade":
        logged.append({k.arg for k in n.keywords})
ok("the persisted ledger is written with exit_reason",
   bool(logged) and all("exit_reason" in kw for kw in logged),
   f"found {len(logged)} _log_grid_trade call(s)")


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all closed-trade reason checks passed")
