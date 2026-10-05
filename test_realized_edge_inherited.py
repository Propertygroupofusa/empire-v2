"""Realized edge must not count a position the grid never opened.

THE READING THIS PINS

Live 2026-10-05, realized_edge.current read:

    126 cycles | gross -1.541% | net -2.068% | net_usd -$186.78

and was read as evidence that the step had got too tight. It had not.
Four ZEC closes tagged adopted_exit sat inside that window and booked
-$311.24 between them. The other 122 cycles are +$124.46 on $7,459 of
notional - about +1.67%, which is the +1.636% this same metric reported
on 114 trips before those four landed.

An adopted_exit's entry price is the ADOPTION MARK: a number nobody
paid and no step chose. Its gross and net describe the inherited
position's history, not what a round trip of this configuration earns.

THE RULE ALREADY EXISTED IN THREE PLACES. loss_study.analyse,
capital_kpis.compute and get_grid_performance_metrics all drop
adopted_exit, and ADOPTED_EXIT_REASON's own comment says such rows are
"kept out of the grid's own performance record". get_realized_edge was
the record that kept them.

Run: python3 test_realized_edge_inherited.py
"""
import os
import sys
from datetime import datetime, timedelta

import crypto_grid_bot as grid

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


class Row:
    def __init__(self, pid, entry, exit_, qty, pnl, reason, closed_at):
        self.product_id, self.entry_price, self.exit_price = pid, entry, exit_
        self.qty, self.pnl, self.exit_reason = qty, pnl, reason
        self.closed_at = closed_at


EPOCH = grid._config_epoch()
AFTER = EPOCH + timedelta(days=1)

# The four real ZEC adopted exits, to the cent.
ZEC = [
    Row("ZEC-USD", 1650.61, 1330.17, 0.11690454, -38.07, "adopted_exit", AFTER),
    Row("ZEC-USD", 1650.61, 1329.95, 0.37816667, -123.24, "adopted_exit", AFTER),
    Row("ZEC-USD", 1650.61, 1330.02, 0.37816667, -123.21, "adopted_exit", AFTER),
    Row("ZEC-USD", 1659.17, 1331.94, 0.08036146, -26.72, "adopted_exit", AFTER),
]
# Grid cycles of its own: small, positive, the shape this fleet really closes.
OWN = [Row("ALGO-USD", 1.0, 1.03, 100.0, 2.00, "profit_target",
           AFTER + timedelta(hours=i)) for i in range(25)]


def edge(rows):
    import asyncio
    from unittest.mock import patch

    class R:
        def __init__(s, v): s._v = v
        def scalars(s): return s
        def all(s): return s._v

    class DB:
        async def execute(s, q): return R(rows)
        async def __aenter__(s): return s
        async def __aexit__(s, *a): return False

    with patch.object(grid, "get_session_factory", lambda: (lambda: DB())):
        return asyncio.run(grid.get_realized_edge())


# --- the split -------------------------------------------------------------
e = edge(OWN + ZEC)
cur, inh, pooled = e["current"], e["current_inherited"], e["current_including_inherited"]

ok("inherited rows leave `current`", cur["trades"] == 25)
ok("they land in `current_inherited`", inh["trades"] == 4)
ok("the pooled cohort still has all 29", pooled["trades"] == 29)
ok("`current` is POSITIVE with the ZEC exits out", cur["net_pct"] > 0)
ok("the pooled figure is NEGATIVE, as it was live", pooled["net_pct"] < 0)
ok("the inherited cohort carries the whole -311.24",
   abs(inh["net_usd"] - -311.24) < 0.01)
ok("the two cohorts sum to the pooled net_usd",
   abs(cur["net_usd"] + inh["net_usd"] - pooled["net_usd"]) < 0.01)
ok("`current` names no inherited coin", "ZEC-USD" not in (cur.get("coins") or []))
ok("`current_inherited` names only ZEC", inh.get("coins") == ["ZEC-USD"])

# --- an inherited GAIN is excluded too -------------------------------------
GAIN = [Row("ZEC-USD", 100.0, 500.0, 1.0, 400.0, "adopted_exit", AFTER)]
e2 = edge(OWN + GAIN)
ok("an inherited WIN is excluded as well, not just a loss",
   e2["current"]["trades"] == 25 and e2["current_inherited"]["trades"] == 1)
ok("and it does not flatter `current`",
   abs(e2["current"]["net_pct"] - e["current"]["net_pct"]) < 1e-9)
ok("but it is still published", e2["current_inherited"]["net_usd"] == 400.0)

# --- every other exit reason stays in -------------------------------------
MIXED = OWN + [
    Row("ONDO-USD", 1.0, 0.9, 30.0, -2.97, "stop_loss", AFTER),
    Row("XRP-USD", 1.0, 1.02, 100.0, 1.50, "parked_sell", AFTER),
    Row("BCH-USD", 1.0, 1.02, 100.0, 1.50, None, AFTER),
]
e3 = edge(MIXED)
ok("stop_loss, parked_sell and untagged rows all stay in `current`",
   e3["current"]["trades"] == 28)
ok("a real stop_loss is still counted", e3["current"]["stop_loss_closes"] == 1)
ok("nothing inherited means an EMPTY inherited cohort",
   e3["current_inherited"] == {"trades": 0})
ok("an empty cohort reports NO percentage, not a measured 0.00%",
   "net_pct" not in e3["current_inherited"])

# --- the headline says so --------------------------------------------------
ok("the headline counts the grid's own cycles", "25 completed cycles" in e["headline"])
ok("and says the inherited ones are shown separately",
   "4 inherited exits shown separately" in e["headline"])
ok("with none inherited it stays quiet about them",
   "inherited" not in e3["headline"])

# --- the cohort boundary still works --------------------------------------
BEFORE = EPOCH - timedelta(days=1)
e4 = edge(OWN + [Row("LTC-USD", 1.0, 1.02, 100.0, 1.50, "profit_target", BEFORE)])
ok("a pre-epoch row is still RETIRED, not current", e4["retired"]["trades"] == 1)
ok("and the epoch split happens before the inherited split",
   e4["current"]["trades"] == 25)
e5 = edge(OWN + [Row("ZEC-USD", 1650.0, 1330.0, 0.1, -32.0, "adopted_exit", BEFORE)])
ok("a pre-epoch INHERITED row retires rather than landing in current_inherited",
   e5["retired"]["trades"] == 1 and e5["current_inherited"]["trades"] == 0)

# --- consumers keep the keys they read ------------------------------------
for k in ("trades", "coins", "notional_usd", "mean_slice_usd", "gross_pct",
          "net_pct", "net_usd", "implied_cost_pct", "stop_loss_closes"):
    ok(f"`current` still carries {k} for existing readers", k in cur)
# 18 own + 4 inherited = 22 rows pooled, but only 18 cycles of this
# configuration. Counting the pooled set would read "baseline reached" off
# four exits the grid never opened. The earlier version of this check used
# 5 own + 4 inherited, where both readings are under 20 and agree - it
# passed against the bug.
_near = edge(OWN[:18] + ZEC)
ok("current_has_baseline counts the grid's own cycles, not the pooled set",
   _near["current_inherited"]["trades"] == 4
   and _near["current"]["trades"] == 18
   and _near["current_has_baseline"] is False)
ok("and it is True once the grid's own cycles reach 20",
   e["current_has_baseline"] is True)

# --- the rule's three existing homes still have it -------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
for mod in ("loss_study.py", "capital_kpis.py"):
    ok(f"{mod} still drops inherited rows",
       "adopted_exit" in open(os.path.join(HERE, mod)).read())
SRC = open(os.path.join(HERE, "crypto_grid_bot.py")).read()
CODE = "\n".join(l for l in SRC.splitlines() if not l.lstrip().startswith("#"))
ok("the split uses the shared constant, not a literal",
   "ADOPTED_EXIT_REASON" in CODE.split("def get_realized_edge")[1][:3000])

failed = [l for l, c in checks if not c]
for l, c in checks:
    print(f"  {'PASS' if c else 'FAIL'}  {l}")
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
