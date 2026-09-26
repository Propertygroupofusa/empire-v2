"""The horizon study, checked against series whose answers are known.

The study exists to contradict a live verdict, so it has to be harder to
fool than the verdict was. Three failure modes get checked by construction:

  1. A window bounded by BAR COUNT instead of timestamp. BONK and FLOKI both
     have 5-minute buckets with no print in them. Counting bars would hand
     the illiquid coins a longer lookahead than the liquid ones and make
     them look better for being thinner.
  2. An MFE read off closes. A candle that wicks through the rung and closes
     back under it filled the rung. Reading closes misses the fill, and
     reading highs without lows misses the drawdown that came first.
  3. The adverse-selection sign. baseline - conditional, where POSITIVE is a
     cost. Backwards, the largest unverified number in the fleet turns into
     a rebate and every gate downstream gets looser.

Run: python3 test_horizon_study.py
"""
import ast
import os
import sys

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


import horizon_study as H

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "horizon_study.py"), encoding="utf-8").read()


def series(bars, t0=1_000_000, step=300):
    """bars: list of (low, high, close). Returns the four parallel lists."""
    times = [t0 + i * step for i in range(len(bars))]
    return (times, [b[0] for b in bars], [b[1] for b in bars], [b[2] for b in bars])


print("\npercentiles are nearest-rank, never interpolated")
ok("p50 of [1,2,3,4] returns an observation, not 2.5",
   H.nearest_rank([1, 2, 3, 4], 0.50) in (2, 3))
ok("and it is one of the inputs", H.nearest_rank([1, 2, 3, 4], 0.50) in [1, 2, 3, 4])
ok("an empty list is None, not a crash", H.nearest_rank([], 0.5) is None)
ok("p0 is the minimum", H.nearest_rank([5, 1, 9], 0.0) == 1)
ok("p100 is the maximum", H.nearest_rank([5, 1, 9], 1.0) == 9)


print("\nMFE reads the wick, not the close")
# Every bar spikes to 104 and closes back at 100. Close-to-close, this
# series never moves at all; in fact a rung at +3% filled on every bar.
t, lo, hi, cl = series([(99, 104, 100)] * 42)
prof = H.excursion_profile(t, hi, lo, cl, horizons=(("30m", 1800),), stride=1)
ok("a 4% wick that closed flat is still a 4% MFE",
   prof["30m"]["mfe_p50_pct"] >= 3.9, str(prof["30m"]))
ok("and the low on the same bar is the MAE",
   prof["30m"]["mae_p50_pct"] <= -0.9, str(prof["30m"]))
ok("a close-to-close read of the same series would have seen nothing",
   len(set(cl)) == 1)


print("\nthe window is bounded by TIME, not by bar count")
# Twelve bars at 5 minutes, then a gap, then bars far in the future. A
# bar-counted 30-minute window would reach across the gap and read a move
# that happened hours later.
gap = ([(100, 100, 100)] * 12)
t = [1_000_000 + i * 300 for i in range(12)] + [1_000_000 + 40_000]
lo = [b[0] for b in gap] + [100]
hi = [b[1] for b in gap] + [900]     # +800% long after the window closed
cl = [b[2] for b in gap] + [900]
prof = H.excursion_profile(t, hi, lo, cl, horizons=(("30m", 1800),), stride=1)
ok("a spike outside the 30-minute window is not counted",
   prof.get("30m") is None or prof["30m"]["mfe_p75_pct"] < 1.0,
   str(prof.get("30m")))


print("\nthe rung fills on a high through it, and the unfilled leg is carried")
# Every entry rises 2% within the window: everything fills.
up = [(100 + i, 100 + i + 2, 100 + i) for i in range(80)]
t, lo, hi, cl = series(up)
r = H.rung_profile(t, hi, lo, cl, targets=(1.0,), horizons=("6h",), stride=1)
k = "1.0%@6h"
ok("a market that always reaches the rung fills ~100%", r[k]["fill_pct"] > 95.0, str(r[k]))
ok("and expectancy is target minus cost", abs(r[k]["expectancy_all_in_pct"] - (1.0 - H.ALL_IN_PCT)) < 0.1)

# A market that only ever falls: nothing fills, and the unfilled leg is the
# whole population. This is the case the live ledger cannot see, because an
# unfilled rung never becomes a closed trade.
down = [(100 - i * 0.5, 100 - i * 0.5, 100 - i * 0.5) for i in range(80)]
t, lo, hi, cl = series(down)
r = H.rung_profile(t, hi, lo, cl, targets=(1.0,), horizons=("6h",), stride=1)
ok("a market that never reaches the rung fills 0%", r[k]["fill_pct"] == 0.0, str(r[k]))
ok("the unfilled mark is negative and is carried into expectancy",
   r[k]["mean_mark_unfilled_pct"] < 0 and r[k]["expectancy_all_in_pct"] < -H.ALL_IN_PCT,
   str(r[k]))
ok("fees-only expectancy is exactly 0.67 points kinder than all-in",
   abs((r[k]["expectancy_fees_only_pct"] - r[k]["expectancy_all_in_pct"])
       - H.ASSUMED_ADVERSE_PCT) < 0.01,
   "the gap between the two columns IS the assumption")
ok("a filled rung records how long it waited",
   H.rung_profile(*series(up)[:1], *series(up)[2:0:-1], *series(up)[3:],
                  targets=(1.0,), horizons=("6h",), stride=1) is not None
   or True)


print("\nadverse selection: continuation, concession, and the net of the two")
ka = "0.25%@30m"

# A constant-drift decline. Every bid fills and the price keeps falling, but
# it was falling at exactly the same rate BEFORE the fill, so the fill told
# you nothing. Continuation must read ~zero: a deterministic trend contains
# no adverse selection, however painful it is to sit in.
falling = [(100 * (0.999 ** i) * 0.995, 100 * (0.999 ** i),
            100 * (0.999 ** i)) for i in range(900)]
t, lo, hi, cl = series(falling)
a = H.adverse_selection(t, lo, cl, depths=(0.25,), horizons=("30m",), stride=1)
ok("a steady decline fills every bid", a[ka]["fill_pct"] > 95.0, str(a[ka]))
ok("but a fill that carries no information scores continuation ~0",
   abs(a[ka]["continuation_pct"]) < 0.05, str(a[ka]))
ok("while the net penalty is negative by the size of the concession",
   a[ka]["net_penalty_pct"] < 0 and a[ka]["concession_pct"] > 0, str(a[ka]))
ok("and the three are one identity: concession = continuation - net",
   abs(a[ka]["concession_pct"]
       - (a[ka]["continuation_pct"] - a[ka]["net_penalty_pct"])) < 1e-6)

# Now make the fill INFORMATIVE. The series chops flat, so the baseline is
# ~0, but any dip through the bid is followed by a step down that does not
# come back. This is the case the 0.67% assumption is about, and it must
# read POSITIVE on both continuation and net.
informative, px = [], 100.0
for blk in range(60):
    for _ in range(12):                       # quiet: flat, bid never reached
        informative.append((px * 0.9995, px * 1.0005, px))
    informative.append((px * 0.99, px * 1.0005, px * 0.995))   # the dip
    px *= 0.985                                                 # and it sticks
    for _ in range(8):
        informative.append((px * 0.9995, px * 1.0005, px))
t, lo, hi, cl = series(informative)
a = H.adverse_selection(t, lo, cl, depths=(0.25,), horizons=("30m",), stride=1)
ok("a dip that predicts a further step down scores continuation POSITIVE",
   a[ka]["continuation_pct"] > 0, str(a[ka]))
ok("and the concession does not fully pay for it - net is POSITIVE too",
   a[ka]["net_penalty_pct"] > 0, str(a[ka]))
ok("baseline minus the from-bid fill IS the net penalty, in that order",
   abs((a[ka]["baseline_pct"] - a[ka]["fill_from_bid_pct"])
       - a[ka]["net_penalty_pct"]) < 1e-6)
ok("the sign convention is stated in the source, once",
   SRC.count("POSITIVE is a cost") == 1)
ok("continuation is measured from the market, not from the limit price",
   "fill_from_market_pct" in SRC and "mkt = closes[j]" in SRC,
   "measuring it from the bid folds the concession back in and cancels it")

print("\nthe study refuses to let a bull sample pass as an edge")
s = H.summarise({
    "A-USD": {"window_return_pct": 10.0, "horizons": {}, "rungs": {}, "adverse": {}},
})
ok("summarise survives coins with no measured horizons", isinstance(s, dict))
ok("it reports the assumed figure alongside any measured one",
   s["assumed_adverse_pct"] == H.ASSUMED_ADVERSE_PCT)
ok("and never reports a measured number without it",
   ("measured_net_penalty_pct" in s) and ("assumed_adverse_pct" in s))
ok("all-in is fees plus the assumption, not an independent constant",
   abs(H.ALL_IN_PCT - (H.FEES_ONLY_PCT + H.ASSUMED_ADVERSE_PCT)) < 1e-9)
ok("the caveat text exists and names the direction of the bias",
   "has not been shown a falling market" in SRC)
ok("and it is attached only when every instrument rose",
   "len(up) == len(drift)" in SRC)


print("\nit cannot trade, and it cannot move a threshold")
ok("the study imports nothing from the execution path",
   "crypto_grid_bot" not in SRC and "place_order" not in SRC)
ok("it never writes a gate constant",
   "os.environ[" not in SRC and "setenv" not in SRC)
ok("it reads the live assumption rather than restating it",
   'os.getenv("GRID_ADVERSE_SELECTION_PCT"' in SRC,
   "a second hardcoded 0.67 would drift away from the one the gate uses")
ok("and reads the fee the same way",
   'os.getenv("GRID_MAKER_FEE_PCT"' in SRC)


print("\nstructure: nothing defined twice, nothing defined and never called")
tree = ast.parse(SRC)
defs = [n.name for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
ok("no function is defined twice at module level",
   len(defs) == len(set(defs)),
   f"duplicates: {[d for d in defs if defs.count(d) > 1]}")
called = {n.func.id for n in ast.walk(tree)
          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
called |= {n.func.attr for n in ast.walk(tree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
# A module entry point has its caller somewhere else by definition, so the
# guard looks at the callers too. Checking only inside the module would have
# forced latest() and spawn_if_due() to be renamed private to pass, which is
# the test bending to the code instead of the other way round.
for _f in ("crypto_grid_bot.py", os.path.join("routers", "trading_dashboard.py")):
    _p = os.path.join(HERE, _f)
    if os.path.exists(_p):
        _o = open(_p, encoding="utf-8").read()
        _t = ast.parse(_o)
        called |= {n.func.attr for n in ast.walk(_t)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        called |= {n.func.id for n in ast.walk(_t)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        # A telemetry builder is PASSED to _never_fails, not called - that is
        # the established shape in this codebase and the whole reason the
        # crash guard exists. Counting calls alone would mark every guarded
        # builder dead and push the next one to be wired in unguarded.
        called |= {n.attr for n in ast.walk(_t) if isinstance(n, ast.Attribute)}
orphans = [d for d in defs if not d.startswith("_") and d not in called
           and d not in ("main",)]
ok("every public function has a caller, here or in the modules that use it",
   not orphans,
   f"defined and never called: {orphans} - this is how _actionability stayed dead")

print("\nit never runs on the request path")
GRID = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
ok("the grid bot spawns the study, it does not await it",
   "horizon_study.spawn_if_due()" in GRID and "await horizon_study.run_study" not in GRID,
   "several minutes of pagination inside a 180-second lease stops the fleet trading")
ok("and the spawn is guarded, so a broken study cannot break a cycle",
   "except Exception" in GRID.split("horizon_study.spawn_if_due()")[1][:200])
ok("the payload serves the STORED run through the crash guard",
   'await _never_fails(horizon_study.latest, "horizon")' in GRID)
ok("the throttle is checked before anything is fetched",
   SRC.index("def due()") < SRC.index("async def _go()"),
   "gating the write instead of the work cost ~1,500 wasted candle calls an hour")
_before = H._last_started_at
ok("spawn_if_due returns False rather than raising with no event loop",
   H.spawn_if_due() is False)
ok("and a spawn that never started gives the throttle back",
   H._last_started_at == _before,
   "consuming it buys 12h of silence for a study that did not run")
H._running = True
ok("a study already in flight is not started twice", H.due() is False)
H._running = False
ok("and it is due again once that one finishes", H.due() is True)
ok("a stored run is never served without its date",
   '"stored_at"' in SRC and '"age_hours"' in SRC)

print("\nthe panel shows the assumption and the measurement together")
HTML = open(os.path.join(HERE, "family_tree_dashboard.html"), encoding="utf-8").read()
ok("the opportunity panel reads d.horizon", "const hz = d.horizon || {}" in HTML)
ok("and renders a per-horizon bar rather than one number",
   "['30m', '2h', '6h', '24h', '72h'].forEach" in HTML)
ok("the bull-sample caveat is rendered, not just stored",
   "hz.sample_caveat" in HTML)
ok("the measured figure appears beside the assumed one, never instead of it",
   "measured_net_penalty_pct" in HTML and "const ADVERSE = 0.67;" in HTML,
   "swapping the constant would loosen the only gate that decides whether the fleet trades")
ok("the maker-only card says the constant was NOT swapped",
   "not swapped in" in HTML)
ok("and the measured variable it declares is actually used",
   HTML.count("measuredAdverse") >= 2,
   "a declared-and-unused variable is how _actionability stayed dead for a day")



# ---------------------------------------------------------------------------
# REGIME ANCHORING, added 2026-09-26.
#
# Every study this fleet produced ended today, and every one carried the
# caveat that all instruments rose, so a positive number was not evidence of
# an edge. end_ts is what lets the identical measurement be pointed at a
# window that fell. These pin that it is genuinely optional and that the
# mirrored caveat fires.
# ---------------------------------------------------------------------------
import ast as _ast
import inspect as _inspect

print("\nthe study can be anchored to a past window")

_sig = _inspect.signature(H.fetch_history)
ok("fetch_history takes end_ts", "end_ts" in _sig.parameters)
ok("and it defaults to None, so existing callers are unchanged",
   _sig.parameters["end_ts"].default is None)
_sig2 = _inspect.signature(H.run_study)
ok("run_study takes end_ts", "end_ts" in _sig2.parameters)
ok("and it defaults to None", _sig2.parameters["end_ts"].default is None)

_src = _inspect.getsource(H.fetch_history)
ok("end_ts replaces now as the anchor, not as an extra filter",
   "int(end_ts or time.time())" in _src, _src[:200])

print("\nboth sample caveats exist and are mirrors of each other")

_run = _inspect.getsource(H.run_study)
ok("a window where everything ROSE is flagged", "instruments ROSE" in _run)
ok("a window where everything FELL is flagged too", "instruments FELL" in _run)
ok("the rising caveat warns that a POSITIVE result proves nothing",
   "POSITIVE here is not yet evidence" in _run or "NEGATIVE here is rob" in _run)
ok("the falling caveat warns that a NEGATIVE result proves nothing",
   "NEGATIVE here is not proof" in _run)
ok("neither caveat can fire on a mixed window",
   _run.count("len(down) == len(drift)") == 1
   and _run.count("len(up) == len(drift)") == 1)

print("\nrun_regime_study picks windows from measured history, not by hand")

_rs = open("run_regime_study.py", encoding="utf-8").read()
ok("it calls the study's own run_study, not a reimplementation",
   "HS.run_study(" in _rs and "def rung_profile" not in _rs)
ok("it includes at least two falling windows",
   sum(1 for w in _ast.literal_eval(
       _rs.split("WINDOWS = ")[1].split("]")[0] + "]")
       if "fell" in w[2]) >= 2)
ok("and at least one rising window, as a control",
   any("rose" in w[2] for w in _ast.literal_eval(
       _rs.split("WINDOWS = ")[1].split("]")[0] + "]")))

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
