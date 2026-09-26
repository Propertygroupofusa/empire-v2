"""The horizon gate, and the one way it is allowed to earn promotion.

A longer horizon passes more setups. So does lowering the threshold, and the
two are indistinguishable from the pass rate alone - which is exactly why the
account owner's standing rule is never to lower a threshold because nothing
has traded. These tests pin the three things that keep this a measurement
rather than that:

  1. ONE VARIABLE. The horizon-gate runs the same cost, the same 0.5 haircut and
     the same "> 0" rule as the live gate. Only the prediction horizon
     differs. A second moved variable makes any result unattributable.
  2. IT CANNOT TRADE. Nothing on the execution path reads horizon_gate_would_trade,
     and the horizon-gate columns never reach an order.
  3. PASSING IS NOT PAYING. The verdict turns on horizon_gate_paid, and it refuses
     to call it at all under 10 resolved. A gate that passes more and pays
     worse must produce "do not promote" from its own numbers.

Run: python3 test_horizon_gate.py
"""
import ast
import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:////tmp/t_horizon_gate.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


import opportunity_signals as S

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "opportunity_signals.py"), encoding="utf-8").read()
GRID = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
MODELS = open(os.path.join(HERE, "models.py"), encoding="utf-8").read()


class Row:
    """A ShortTermSignal stand-in. Only the fields the summary reads."""
    def __init__(self, **kw):
        for f in ("would_trade", "horizon_gate_would_trade", "horizon_gate_paid"):
            setattr(self, f, kw.pop(f, None))
        for f in ("resolved_at", "horizon_gate_resolved_at"):
            setattr(self, f, kw.pop(f, None))
        for f in ("net_after_costs_pct", "horizon_gate_net_pct"):
            setattr(self, f, kw.pop(f, None))
        self.product_id = kw.pop("product_id", "X-USD")
        assert not kw, kw


print("\nthe backward-looking predictor looks backward")
hi = [10, 11, 12, 13, 14, 99]
lo = [9, 9, 9, 9, 9, 9]
ok("it reads the LAST n bars, not the first",
   abs(S.trailing_excursion_pct(hi, lo, 1) - ((99 / 9 - 1) * 100)) < 1e-9)
ok("a shorter window excludes the older bars",
   abs(S.trailing_excursion_pct([10, 12], [9, 9], 1) - ((12 / 9 - 1) * 100)) < 1e-9)
ok("an empty series is None, not zero", S.trailing_excursion_pct([], [], 5) is None)
ok("a zero low does not divide", S.trailing_excursion_pct([5], [0], 1) is None)
ok("it takes a BAR COUNT, so a forward slice cannot be handed in by index",
   "def trailing_excursion_pct(highs, lows, bars: int)" in SRC
   and "highs[-bars:]" in SRC)

print("\none variable moved, and it is not the bar")
ok("the horizon-gate horizon is six hours, not twenty-four",
   S.HORIZON_GATE_MIN == 360,
   "the same simulation passed 85% of scans at 24h - that is not a gate")
ok("and the reason 24h was rejected is written down",
   "85% of scans" in SRC,
   "a phrase that survives comment re-wrapping, not one that spans a line break")
ok("the horizon-gate charges the SAME cost the live gate charged",
   "hg_move - cost" in SRC and "hg_cost" not in SRC,
   "a round trip does not cost more for being held longer")
ok("the same 0.5 haircut applies to both",
   "abs(_hg_mom) * 0.5" in SRC and "abs(_mom) * 0.5" in SRC)
ok("the same '> 0' rule decides both",
   'bool(net_edge is not None and net_edge > 0)' in SRC
   and 'bool(hg_edge is not None and hg_edge > 0)' in SRC)
ok("the range estimator is recorded too, not substituted",
   "horizon_gate_move_range_pct" in SRC and "horizon_gate_move_range_pct" in MODELS,
   "the study used range; quoting its pass rates for a momentum gate would be dishonest")

print("\nit cannot trade")
ok("no execution path reads the horizon-gate verdict",
   "horizon_gate_would_trade" not in GRID,
   "the grid bot must not be able to act on an unvalidated gate")
cycle = None
for n in ast.walk(ast.parse(GRID)):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run_grid_branch_cycle":
        cycle = ast.get_source_segment(GRID, n)
ok("and the per-branch trading cycle never mentions it",
   cycle is not None and "horizon_gate" not in cycle)
# crypto_grid_bot imports a SHADOW_MODE_ENABLED trade observer from
# stage2.orchestration that logs REAL orders. The first draft of this gate
# called itself shadow_*, which put two unrelated meanings of one word in the
# same file - and made the assertion above unwriteable, because the trading
# cycle legitimately says "shadow" nine times.
ok("the name does not collide with the existing SHADOW_MODE trade observer",
   "SHADOW_MODE_ENABLED" in GRID and "shadow_would_trade" not in GRID
   and "horizon_gate_would_trade" not in GRID)
ok("and models.py says why, so it is not reintroduced",
   'NOT "shadow mode"' in MODELS)
ok("the live switch is still off by default", S.SIGNALS_LIVE is False)

print("\npassing is not paying")
# Ten horizon-gate crossings, nine of which paid. A units fix looks like this.
good = [Row(would_trade=False, horizon_gate_would_trade=True, horizon_gate_paid=(i != 0),
            horizon_gate_resolved_at=datetime.utcnow()) for i in range(10)]
v = S.horizon_gate_summary(good)
ok("ten resolved crossings produce a verdict", "not enough data" not in v["verdict"], v["verdict"])
ok("a high paid rate reads as a units fix, not a looser bar",
   "units fix" in v["verdict"], v["verdict"])
ok("and the paid rate is over the PASSED-and-resolved rows only",
   v["horizon_paid_pct"] == 90.0, str(v))

# Same pass count, mostly unpaid. This is what a lowered threshold looks like
# and the verdict has to say so in its own words.
bad = [Row(would_trade=False, horizon_gate_would_trade=True, horizon_gate_paid=(i < 2),
           horizon_gate_resolved_at=datetime.utcnow()) for i in range(10)]
v = S.horizon_gate_summary(bad)
ok("passing more and paying worse produces 'do not promote'",
   "Do not promote" in v["verdict"], v["verdict"])
ok("and it names the paid rate that condemned it", "20.0%" in v["verdict"], v["verdict"])

thin = [Row(would_trade=False, horizon_gate_would_trade=True, horizon_gate_paid=True,
            horizon_gate_resolved_at=datetime.utcnow()) for _ in range(9)]
v = S.horizon_gate_summary(thin)
ok("nine resolved is still 'not enough data'", "not enough data" in v["verdict"], v["verdict"])
ok("it refuses to call it either way at nine",
   "units fix" not in v["verdict"] and "Do not promote" not in v["verdict"])
ok("an empty ledger does not divide by zero",
   S.horizon_gate_summary([])["horizon_pass_pct"] is None)
ok("an unresolved crossing is not counted as paid",
   S.horizon_gate_summary([Row(horizon_gate_would_trade=True)])["horizon_paid_pct"] is None)
ok("the basis line says what was held fixed",
   "ONLY the prediction horizon differs" in S.horizon_gate_summary([])["basis"])
ok("and it says nothing reads the verdict",
   "Observation only" in S.horizon_gate_summary([])["basis"])

print("\nthe resolver is its own pass, and shares its candles")
ok("the horizon-gate resolves separately from the live ledger",
   "async def resolve_horizon_gate(" in SRC)
ok("because the two horizons come due at different times",
   "five and a half hours" in SRC)
ok("it bounds the excursion at the horizon-gate horizon, not the live one",
   "until_epoch=t0 + HORIZON_GATE_MIN * 60" in SRC)
_rs = None
for _n in ast.walk(ast.parse(SRC)):
    if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _n.name == "resolve_horizon_gate":
        _rs = ast.get_source_segment(SRC, _n)
ok("it reads highs AND lows, so a stop before the peak still counts",
   _rs is not None and "window_excursion(times, highs, lows" in _rs,
   "bounded by the function, not by a character count that moves with every edit")
ok("horizon_gate_paid is priced against the SAME assumed cost",
   "mfe >= row.cost_assumed_pct" in SRC)
ok("all three resolvers share one candle fetch per coin per pass",
   "_candles_for" in GRID and GRID.count("await signals.fetch_candles_full(session, pid)") == 1,
   "a coin in two resolvers was fetched twice for the identical 300 bars")
ok("and the memo is per-pass, so candles cannot go stale",
   "_candles = {}" in GRID)
ok("every resolver still respects the telemetry deadline",
   GRID.count("deadline=_deadline") == 3)

print("\nthe summary carries it where it cannot be read alone")
ok("the horizon-gate block sits inside the live summary",
   '"horizon_gate": horizon_gate_summary(rows),' in SRC)
ok("horizon_gate_summary has a caller",
   SRC.count("horizon_gate_summary(") >= 2)
ok("resolve_horizon_gate has a caller", "signals.resolve_horizon_gate(" in GRID)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
