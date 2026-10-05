"""The compounding ladder must not project off trades the grid never opened.

THE READING THIS PINS

Live 2026-10-05, /growth-model returned:

    blended_net_pct  -1.8103%   median_net_pct  +1.4341%
    win_rate_pct      93.4%     total_pnl_usd   -$172.82
    monthly_pct      -2.2736%   36-month ladder $7,714 -> $3,370.51

Those first three cannot describe one distribution. A 93.4% win rate with
a +1.43% median and a -1.81% blend means a handful of enormous losers,
and they were not the grid's. Four ZEC closes tagged adopted_exit booked
-$311.24 on $1,574.71 of cost basis. The other 133 round trips were
+$138.42 - about +1.74% blended, +1.82% a month. The front page was
projecting capital destruction off four rows that cannot recur: ZEC holds
zero coin, so there is nothing left for it to exit.

An adopted_exit's entry price is the ADOPTION MARK - a number nobody paid
and no step chose. The rule to drop those rows already existed in four
places (loss_study.analyse, capital_kpis.compute,
get_grid_performance_metrics and, since 985f759, get_realized_edge).
/growth-model was the fifth, and the only one whose number the dashboard
puts on the front page. It could not even have told them apart: the
endpoint never carried exit_reason into its trade dicts.

TWO CONTAMINATIONS, NOT ONE. Dropping the rows from the numerator while
leaving them in the SPAN is the same error more quietly: own profit
divided by a window four inherited closes helped define. The span is
measured per cohort below, and that is what the span tests pin.

Run: python3 test_growth_model_inherited.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

import crypto_grid_bot as real_grid
import routers.trading_dashboard as td

checks = []

# A cohort that measured nothing publishes {"readable": False, "reason": ...}
# and NOTHING else - no round_trips, no percentages. That is deliberate:
# absence, not a measured zero. So a check that reads a cohort must not
# index it. NaN propagates through the arithmetic below and every
# comparison with it is False, so a cohort that went empty fails the check
# BY NAME instead of killing the run with a KeyError on line one.
NAN = float("nan")


def g(cohort, key):
    v = (cohort or {}).get(key, NAN)
    return NAN if v is None else v


def ok(label, cond):
    checks.append((label, bool(cond)))


class Row:
    """Only the columns the endpoint reads."""

    def __init__(self, entry, qty, pnl, reason, closed_at):
        self.entry_price, self.qty, self.pnl = entry, qty, pnl
        self.exit_reason, self.closed_at = reason, closed_at


NOW = datetime.utcnow()

# The four real ZEC adopted exits, to the cent, in the 11 minutes they
# really landed in on 2026-10-04 (08:26 - 08:37Z).
ZEC = [
    Row(1650.61, 0.11690454, -38.07, "adopted_exit", NOW - timedelta(days=29)),
    Row(1650.61, 0.37816667, -123.24, "adopted_exit",
        NOW - timedelta(days=29) + timedelta(minutes=4)),
    Row(1650.61, 0.37816667, -123.21, "adopted_exit",
        NOW - timedelta(days=29) + timedelta(minutes=8)),
    Row(1659.17, 0.08036146, -26.72, "adopted_exit",
        NOW - timedelta(days=29) + timedelta(minutes=11)),
]

# The grid's own cycles: small, positive, the shape this fleet closes.
# Spread over exactly 10 days, ending a day ago - so the OWN span (10d)
# and the POOLED span (28d) are different numbers and a test can tell
# which one the rate divided by.
OWN = [Row(1.0, 100.0, 2.00, "profit_target",
           NOW - timedelta(days=11) + timedelta(days=10 * i / 24.0))
       for i in range(25)]
OWN_PNL = 25 * 2.00            # +$50.00
ZEC_PNL = -311.24
CAPITAL = 7714.0               # allocated 7714 + $0 free, as it read live


class DB:
    def __init__(self, rows):
        self._rows = rows

    class _R:
        def __init__(self, v):
            self._v = v

        def scalars(self):
            return self

        def all(self):
            return self._v

    async def execute(self, q):
        return DB._R(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeGrid:
    """Just the three surfaces /growth-model touches."""

    def __init__(self, rows, inherited_reason=real_grid.ADOPTED_EXIT_REASON,
                 allocated=CAPITAL, deployed_qty=5000.0, free_cash=0.0):
        self.ADOPTED_EXIT_REASON = inherited_reason
        self._rows = rows
        self._allocated = allocated
        self._deployed_qty = deployed_qty
        self._free = free_cash

    async def get_grid_status(self):
        return {
            "real_free_cash_usd": self._free,
            "branches": [{
                "product_id": "ALGO-USD", "bot_name": "crypto_grid_1",
                "allocated_usd": self._allocated, "num_levels": 4,
                "total_unrealized_net_usd": 0.0,
                "slices": [{"entry_price": 1.0, "qty": self._deployed_qty,
                            "adopted": False}],
            }],
        }

    def get_session_factory(self):
        rows = self._rows
        return lambda: DB(rows)


def model(rows, **kw):
    grid = FakeGrid(rows, **kw)
    old = td.crypto_grid_bot_module
    td.crypto_grid_bot_module = grid
    try:
        return json.loads(asyncio.run(td.get_growth_model()).body)
    finally:
        td.crypto_grid_bot_module = old


# --- the split --------------------------------------------------------------
m = model(OWN + ZEC)
edge, inh, pooled = m["edge"], m["edge_inherited"], m["edge_including_inherited"]

ok("inherited rows leave `edge`", g(edge, "round_trips") == 25)
ok("they land in `edge_inherited`", g(inh, "round_trips") == 4)
ok("the pooled cohort still has all 29", g(pooled, "round_trips") == 29)
ok("`edge` total is the grid's own +$50.00",
   abs(g(edge, "total_pnl_usd") - OWN_PNL) < 0.01)
ok("`edge_inherited` carries the whole -$311.24",
   abs(g(inh, "total_pnl_usd") - ZEC_PNL) < 0.01)
ok("the two cohorts sum to the pooled total",
   abs(g(edge, "total_pnl_usd") + g(inh, "total_pnl_usd")
       - g(pooled, "total_pnl_usd")) < 0.01)
ok("`edge` blends POSITIVE with the ZEC exits out", g(edge, "blended_net_pct") > 0)
ok("the pooled blend is NEGATIVE, as it read live",
   g(pooled, "blended_net_pct") < 0)
ok("`edge` win rate is no longer a 93% that contradicts its own blend",
   g(edge, "win_rate_pct") == 100.0)
ok("`edge` median and blend now agree in sign",
   g(edge, "median_net_pct") > 0 and g(edge, "blended_net_pct") > 0)

# --- the rate, and the ladder it feeds -------------------------------------
rate, rate_all = m["rate"], m["rate_including_inherited"]
ok("the own rate is MEASURED", rate.get("verdict") == "MEASURED")
ok("the own rate is POSITIVE", g(rate, "monthly_pct") > 0)
ok("the pooled rate is still published and still negative",
   rate_all.get("readable") and g(rate_all, "monthly_pct") < 0)
ok("the 36-month ladder now ENDS ABOVE the capital it starts from",
   (rate.get("compounding") or [{}])[-1].get("value_usd", NAN) > g(rate, "capital_usd"))
ok("the pooled ladder still ends below it, on the record",
   (rate_all.get("compounding") or [{}])[-1].get("value_usd", NAN) < g(rate_all, "capital_usd"))
ok("both rates price the same capital",
   g(rate, "capital_usd") == g(rate_all, "capital_usd") == CAPITAL)

# --- THE SPAN IS PER COHORT ------------------------------------------------
# The own trades cover 10 days; pooled with the ZEC closes it is 28. Using
# the pooled span for the own rate would divide the grid's own profit by a
# window four inherited exits defined - a 2.8x understatement, and a bug no
# numerator check can see.
ok("the own rate divides by the OWN span, not the pooled one",
   abs(g(rate, "span_days") - 10.0) < 0.05)
ok("the pooled rate divides by the pooled span",
   abs(g(rate_all, "span_days") - 28.0) < 0.05)
ok("so the two spans really are different numbers here",
   abs(g(rate, "span_days") - g(rate_all, "span_days")) > 1.0)
ok("and the own daily rate is the own arithmetic",
   abs(g(rate, "daily_pct") - (OWN_PNL / CAPITAL / 10.0 * 100.0)) < 1e-4)

# The inherited cohort gets an EDGE and no rate. Four closes inside eleven
# minutes has no span to annualise, and the endpoint never offers one.
ok("the inherited cohort is published as an edge", m["edge_inherited"].get("readable") is True)
ok("and no inherited RATE is offered for eleven minutes of ZEC",
   "rate_inherited" not in m)

# A short own span must stay UNKNOWN even when the pooled span is long.
SHORT = [Row(1.0, 100.0, 2.00, "profit_target",
             NOW - timedelta(days=3) + timedelta(hours=i)) for i in range(25)]
m2 = model(SHORT + ZEC)
ok("a 3-day own span is UNKNOWN, never borrowed from the pooled 26 days",
   m2["rate"].get("readable") is False and m2["rate"].get("verdict") == "UNKNOWN")
ok("and the pooled rate over 26 days is still readable, so the two differ",
   m2["rate_including_inherited"].get("readable") is True)
ok("with no own rate the ceiling says so instead of guessing",
   m2["ceiling"].get("readable") is False)

# --- the ceiling is priced off the OWN rate --------------------------------
ok("the ceiling uses the own monthly rate",
   abs(g(m["ceiling"], "working_monthly_pct") - g(rate, "monthly_pct")) < 1e-9)
ok("not the pooled one",
   abs(g(m["ceiling"], "working_monthly_pct") - g(rate_all, "monthly_pct")) > 1e-6)
ok("so idle capital is priced at a POSITIVE rate, not a destructive one",
   g(m["ceiling"], "extra_monthly_usd_if_it_worked") > 0)

# --- an inherited GAIN is excluded too -------------------------------------
GAIN = [Row(100.0, 1.0, 400.0, "adopted_exit", NOW - timedelta(days=29)),
        Row(100.0, 1.0, 300.0, "adopted_exit", NOW - timedelta(days=28))]
m3 = model(OWN + GAIN)
ok("an inherited WIN is excluded as well, not just a loss",
   g(m3["edge"], "round_trips") == 25 and g(m3["edge_inherited"], "round_trips") == 2)
ok("and it does not flatter `edge`",
   abs(g(m3["edge"], "total_pnl_usd") - OWN_PNL) < 0.01)
ok("nor the rate the ladder compounds",
   abs(g(m3["rate"], "monthly_pct") - g(rate, "monthly_pct")) < 1e-9)
ok("but the inherited gain is still published",
   abs(g(m3["edge_inherited"], "total_pnl_usd") - 700.0) < 0.01)
ok("and the pooled cohort shows it",
   g(m3["edge_including_inherited"], "total_pnl_usd")
   > g(m3["edge"], "total_pnl_usd"))

# --- every other exit reason stays in --------------------------------------
MIXED = OWN + [
    Row(1.0, 30.0, -2.97, "stop_loss", NOW - timedelta(days=5)),
    Row(1.0, 100.0, 1.50, "parked_sell", NOW - timedelta(days=4)),
    Row(1.0, 100.0, 1.50, None, NOW - timedelta(days=3)),
    Row(1.0, 100.0, 1.50, "", NOW - timedelta(days=2)),
]
m4 = model(MIXED)
ok("stop_loss, parked_sell, untagged and empty-string rows all stay in `edge`",
   g(m4["edge"], "round_trips") == 29)
ok("a real loss is NOT dropped - only inherited rows are",
   g(m4["edge"], "win_rate_pct") < 100.0)
ok("nothing inherited means an UNREADABLE inherited cohort",
   m4["edge_inherited"].get("readable") is False)
ok("an empty cohort reports NO percentage, not a measured 0.00%",
   "blended_net_pct" not in m4["edge_inherited"]
   and "total_pnl_usd" not in m4["edge_inherited"])
ok("and it says why rather than returning a bare false",
   bool(m4["edge_inherited"].get("reason")))
ok("with nothing inherited the pooled cohort equals the own one",
   g(m4["edge_including_inherited"], "total_pnl_usd")
   == g(m4["edge"], "total_pnl_usd"))

# --- the module's constant decides, not a literal in the router ------------
m5 = model([Row(1.0, 100.0, 2.00, "profit_target", NOW - timedelta(days=11)),
            Row(1.0, 100.0, 2.00, "profit_target", NOW - timedelta(days=1)),
            Row(500.0, 1.0, -400.0, "inherited_close", NOW - timedelta(days=5))],
           inherited_reason="inherited_close")
ok("the endpoint reads the engine's ADOPTED_EXIT_REASON, not a hardcoded string",
   g(m5["edge"], "round_trips") == 2 and g(m5["edge_inherited"], "round_trips") == 1)

# --- unpriceable rows are still dropped and counted, per cohort ------------
m6 = model(OWN + ZEC + [Row(None, 100.0, 5.00, "profit_target", NOW - timedelta(days=2)),
                        Row(1650.61, None, -9.00, "adopted_exit",
                            NOW - timedelta(days=29))])
ok("an unpriceable OWN row is dropped from `edge`, not called 0%",
   g(m6["edge"], "round_trips") == 25 and g(m6["edge"], "dropped_unpriceable") == 1)
ok("and an unpriceable INHERITED row is dropped from its own cohort",
   g(m6["edge_inherited"], "round_trips") == 4
   and g(m6["edge_inherited"], "dropped_unpriceable") == 1)

# --- the payload still answers the questions it answered before ------------
for k in ("window_hours_requested", "edge", "capital", "rate", "ceiling", "as_of"):
    ok(f"the payload still carries {k} for existing readers", k in m)
for k in ("readable", "round_trips", "dropped_unpriceable", "median_net_pct",
          "mean_net_pct", "win_rate_pct", "median_notional_usd", "total_pnl_usd",
          "cumulative_notional_usd", "blended_net_pct"):
    ok(f"`edge` still carries {k}", k in m["edge"])
for k in ("readable", "verdict", "span_days", "realised_usd", "capital_usd",
          "daily_pct", "monthly_pct", "annual_pct_compounded", "compounding"):
    ok(f"`rate` still carries {k}", k in m["rate"])
ok("the split is named in the payload, not left to be inferred",
   "adopted_exit" in (m.get("inherited_is") or ""))
ok("and the payload says the exclusion cuts BOTH ways",
   "BOTH directions" in (m.get("inherited_is") or ""))
ok("and that the span is per cohort",
   "per cohort" in (m.get("inherited_is") or ""))

# --- the source, for the two mutations a payload cannot show ---------------
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "routers", "trading_dashboard.py")).read()
BODY = SRC.split("async def get_growth_model")[1].split("\n@router.")[0]
CODE = "\n".join(l for l in BODY.splitlines() if not l.lstrip().startswith("#"))
ok("exit_reason is carried into the trade dicts at all",
   '"exit_reason": r.exit_reason' in CODE)
ok("the split uses the shared constant via getattr, not a literal",
   "ADOPTED_EXIT_REASON" in CODE)
ok("the own edge is measured on own_trades", "measure_edge(own_trades)" in CODE)
ok("the own rate is projected on the own span",
   "gmod.project(realised, total_capital, span_days)" in CODE)
ok("the pooled rate is projected on the pooled span",
   "span_days_all)" in CODE)
# The four other homes of the same rule must still have it.
for mod in ("loss_study.py", "capital_kpis.py"):
    ok(f"{mod} still drops inherited rows",
       "adopted_exit" in open(os.path.join(HERE, mod)).read())

failed = [l for l, c in checks if not c]
for l, c in checks:
    print(f"  {'PASS' if c else 'FAIL'}  {l}")
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
