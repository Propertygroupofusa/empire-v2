"""A stop is the last thing between an open slice and an unbounded loss.

Every path that cannot measure must keep the existing stop, never drop
it. Turning one off has to be explicit, and has to say so.
"""

import os

import adaptive_stop as A

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


# Measured daily volatility, 60 days of hourly candles, 2026-09-26.
VOL = {"BTC-USD": 1.90, "NEAR-USD": 6.02, "BONK-USD": 5.71,
       "ONDO-USD": 4.61, "FLOKI-USD": 4.61, "TIA-USD": 4.81}

print("\nthe same 8% is a different policy on every coin - scaling fixes that")

wide = A.resolve("NEAR-USD", 0.08, VOL["NEAR-USD"], overrides={}, mode_override="adaptive")
tight = A.resolve("BTC-USD", 0.08, VOL["BTC-USD"], overrides={}, mode_override="adaptive")
ok("NEAR's stop WIDENS from 8% (its 8% fired on 13.8% of entries)",
   wide["stop_pct"] > 0.08, wide)
ok("BTC's stop TIGHTENS from 8% (its 8% never fired at all)",
   tight["stop_pct"] < 0.08, tight)
ok("a quiet coin gets a nearer stop than a wild one",
   tight["stop_pct"] < wide["stop_pct"])
ok("and the reason names the volatility it was derived from",
   "daily volatility" in wide["reason"] and "6.02" in wide["reason"], wide["reason"])

ordered = sorted(VOL, key=lambda p: A.resolve(p, 0.08, VOL[p], overrides={},
                                              mode_override="adaptive")["stop_pct"])
ok("stops rank in the same order as volatility",
   ordered == sorted(VOL, key=VOL.get), ordered)

print("\nnothing silently removes a stop")

no_vol = A.resolve("NEAR-USD", 0.08, None, overrides={}, mode_override="adaptive")
ok("unreadable volatility KEEPS the fixed stop", no_vol["stop_pct"] == 0.08, no_vol)
ok("and says the stop was kept, not applied", "kept rather" in no_vol["reason"])
ok("and is sourced honestly", no_vol["source"] == "fixed_no_volatility")

for bad in (0, -1, float("nan"), float("inf"), "abc"):
    r = A.resolve("NEAR-USD", 0.08, bad, overrides={}, mode_override="adaptive")
    ok(f"volatility {bad!r:>6.6} keeps the fixed stop", r["stop_pct"] == 0.08, r)

ok("fixed mode ignores volatility entirely",
   A.resolve("NEAR-USD", 0.08, 99.0, overrides={}, mode_override="fixed")["stop_pct"] == 0.08)

print("\nthe computed stop is always inside the floor and the cap")

calm = A.scaled_stop(0.2, multiple=2.5)
wild = A.scaled_stop(90.0, multiple=2.5)
ok("a near-motionless coin is floored, not given a hair trigger",
   calm == A.DEFAULT_FLOOR, calm)
ok("a violent coin is capped, not given an unbounded stop",
   wild == A.DEFAULT_CAP, wild)
ok("floor and cap swap if passed backwards rather than inverting the clamp",
   0 < A.scaled_stop(5.0, multiple=2.5, floor=0.25, cap=0.03) <= 0.25)

print("\nturning a stop OFF is explicit, and loud")

off = A.resolve("NEAR-USD", 0.08, VOL["NEAR-USD"], overrides={"NEAR-USD": 0.0})
ok("an override of 0 disables the stop", off["stop_pct"] == 0.0, off)
ok("and the reason says the downside is now unbounded",
   "no downside trigger" in off["reason"] and "without limit" in off["reason"], off["reason"])
ok("it does not leak to other coins",
   A.resolve("BTC-USD", 0.08, VOL["BTC-USD"], overrides={"NEAR-USD": 0.0})["stop_pct"] == 0.08)

pinned = A.resolve("BTC-USD", 0.08, VOL["BTC-USD"], overrides={"BTC-USD": 0.05})
ok("an override can also pin a specific stop", pinned["stop_pct"] == 0.05, pinned)
ok("an override beats adaptive mode",
   A.resolve("BTC-USD", 0.08, VOL["BTC-USD"], overrides={"BTC-USD": 0.05},
             mode_override="adaptive")["stop_pct"] == 0.05)

print("\na typo must never be what removes a stop")

ok("a malformed pair is skipped", A.parse_overrides("NEAR-USD") == {})
ok("a non-numeric value is skipped", A.parse_overrides("NEAR-USD:off") == {})
ok("an out-of-range value is skipped", A.parse_overrides("NEAR-USD:1.5") == {})
ok("a negative value is skipped", A.parse_overrides("NEAR-USD:-0.1") == {})
ok("a good pair beside a bad one still parses",
   A.parse_overrides("NEAR-USD:0,JUNK") == {"NEAR-USD": 0.0})
ok("case and spacing do not matter",
   A.parse_overrides(" near-usd : 0.05 ") == {"NEAR-USD": 0.05})
ok("semicolons work as separators too",
   A.parse_overrides("A-USD:0.05;B-USD:0.06") == {"A-USD": 0.05, "B-USD": 0.06})
ok("an empty setting disables nothing", A.parse_overrides("") == {})

print("\nthe default is the behaviour that is already running")

for raw in (None, "", "fixed", "FIXED", "adaptive-ish", "true"):
    if raw is None:
        os.environ.pop(A.MODE_ENV, None)
    else:
        os.environ[A.MODE_ENV] = raw
    ok(f"GRID_STOP_MODE={raw!r:>14} resolves to fixed", A.mode() == "fixed", A.mode())

os.environ[A.MODE_ENV] = "Adaptive"
ok("only an explicit 'adaptive' switches it on", A.mode() == "adaptive")
os.environ.pop(A.MODE_ENV, None)

ok("with nothing configured, an 8% fixed stop stays an 8% fixed stop",
   A.resolve("NEAR-USD", 0.08, VOL["NEAR-USD"])["stop_pct"] == 0.08)

print("\ndaily volatility is computed from the candles, never guessed")

import random
random.seed(7)
c = [100.0]
for _ in range(60):
    c.append(c[-1] * (1 + random.gauss(0, 0.01)))
v = A.daily_vol_pct_from_closes(c)
ok("1% hourly noise reads as roughly 5% daily", 3.0 < v < 7.0, v)

quiet = [100.0]
for _ in range(60):
    quiet.append(quiet[-1] * (1 + random.gauss(0, 0.001)))
ok("a quieter series reads lower", A.daily_vol_pct_from_closes(quiet) < v)

ok("too short to judge returns None", A.daily_vol_pct_from_closes([1, 2, 3]) is None)
ok("a flat series returns None, not zero",
   A.daily_vol_pct_from_closes([5.0] * 40) is None)
ok("empty returns None", A.daily_vol_pct_from_closes([]) is None)
ok("None returns None", A.daily_vol_pct_from_closes(None) is None)
ok("junk values return None", A.daily_vol_pct_from_closes(["a", "b", "c", "d", "e"]) is None)
ok("and every one of those keeps the fixed stop",
   all(A.resolve("X-USD", 0.08, A.daily_vol_pct_from_closes(bad),
                 overrides={}, mode_override="adaptive")["stop_pct"] == 0.08
       for bad in ([1, 2, 3], [5.0] * 40, [], None)))

print("\nthe volatility window is long enough to mean something")

import asyncio

ok("the default window is 30 days, not the ~25 candles the price uses",
   A.VOL_WINDOW_DAYS >= 7, A.VOL_WINDOW_DAYS)

# Measured on the live fleet 2026-09-26. The short window reads 3-4x low
# and every stop scaled from it came out TIGHTER, which is backwards.
SHORT = {"BTC-USD": 0.49, "NEAR-USD": 2.44, "BONK-USD": 1.84}
LONG = {"BTC-USD": 1.81, "NEAR-USD": 7.40, "BONK-USD": 5.84}
for pid in SHORT:
    a = A.resolve(pid, 0.08, SHORT[pid], overrides={}, mode_override="adaptive")["stop_pct"]
    b = A.resolve(pid, 0.08, LONG[pid], overrides={}, mode_override="adaptive")["stop_pct"]
    ok(f"{pid.replace('-USD',''):5} short window gives a TIGHTER stop than the long one",
       a < b, f"{a:.4f} vs {b:.4f}")
ok("and NEAR on the long window is genuinely wider than the fixed 8%",
   A.resolve("NEAR-USD", 0.08, LONG["NEAR-USD"], overrides={},
             mode_override="adaptive")["stop_pct"] > 0.08)

calls = []


def counting_fetcher(series):
    async def _f(session, pid, start, end, granularity=3600):
        calls.append(pid)
        if pid not in series:
            raise RuntimeError("HTTP 404")
        return (series[pid], [], [])
    return _f


random.seed(3)
noisy = [100.0]
for _ in range(400):
    noisy.append(noisy[-1] * (1 + random.gauss(0, 0.01)))

A._VOL_CACHE.clear()
calls.clear()
f = counting_fetcher({"A-USD": noisy})
v1 = asyncio.run(A.measure_daily_vol(object(), "A-USD", fetcher=f))
v2 = asyncio.run(A.measure_daily_vol(object(), "A-USD", fetcher=f))
ok("a real reading is returned", v1 is not None and v1 > 0, v1)
ok("and the second call is served from cache, not a second fetch",
   len(calls) == 1 and v2 == v1, calls)

A._VOL_CACHE.clear()
calls.clear()
bad = asyncio.run(A.measure_daily_vol(object(), "B-USD", fetcher=counting_fetcher({})))
ok("a failed fetch returns None", bad is None)
ok("and is NOT cached - one failure must not pin a branch for six hours",
   "B-USD" not in A._VOL_CACHE, A._VOL_CACHE)
ok("None from the fetch still keeps the fixed stop",
   A.resolve("B-USD", 0.08, bad, overrides={}, mode_override="adaptive")["stop_pct"] == 0.08)

A._VOL_CACHE.clear()

print("\nthe live cycle calls it, and cannot lose the stop if it throws")

import ast
src = open("crypto_grid_bot.py").read()
tree = ast.parse(src)
cyc = next(n for n in ast.walk(tree)
           if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_grid_branch_cycle")
cyc_src = ast.get_source_segment(src, cyc) or ""

ok("the cycle resolves a stop rather than reading the constant directly",
   "adaptive_stop.resolve" in cyc_src)
ok("it measures volatility over the long cached window",
   "measure_daily_vol" in cyc_src)
ok("and NOT from the short candle series the price came from",
   "daily_vol_pct_from_closes(_stop_closes)" not in cyc_src,
   "the ~25-candle window reads 3-4x low and tightens every stop")
ok("the trigger compares against the RESOLVED stop",
   "price <= _entry * (1 - _stop_pct)" in cyc_src)
ok("the old constant is no longer the trigger",
   "price <= _entry * (1 - GRID_STOP_LOSS_PCT)" not in cyc_src)
ok("a failure falls back to the configured stop, inside an except",
   "_stop_pct = GRID_STOP_LOSS_PCT" in cyc_src and "except Exception" in cyc_src)
ok("a zero stop on a branch holding slices is logged at WARNING",
   "NO STOP" in cyc_src)
ok("the stop-loss log reports the distance that actually fired",
   "{_stop_pct * 100:.1f}% stop" in cyc_src)

print("\nthe policy in force is readable, so 'did it take' is answerable")

pol = A.policy()
for field in ("mode", "vol_multiple", "vol_window_days", "floor_pct", "cap_pct",
              "overrides", "measured_coins"):
    ok(f"policy() reports {field}", field in pol, pol)
ok("mode is one of the two real values", pol["mode"] in ("fixed", "adaptive"))

os.environ[A.MODE_ENV] = "adaptive"
os.environ[A.OVERRIDES_ENV] = "NEAR-USD:0"
pol = A.policy()
ok("a live mode change shows up in policy()", pol["mode"] == "adaptive", pol)
ok("and so does an override", pol["overrides"] == {"NEAR-USD": 0.0}, pol)
os.environ.pop(A.MODE_ENV, None)
os.environ.pop(A.OVERRIDES_ENV, None)

print("\nthe read side never pays for a month of candles")

A._VOL_CACHE.clear()
calls.clear()
got = asyncio.run(A.measure_daily_vol(object(), "Z-USD",
                                      fetcher=counting_fetcher({"Z-USD": noisy}),
                                      cached_only=True))
ok("cached_only with an empty cache fetches nothing", calls == [], calls)
ok("and returns None rather than a number it did not measure", got is None)
ok("which resolves to the fixed stop, the honest answer",
   A.resolve("Z-USD", 0.08, got, overrides={}, mode_override="adaptive")["stop_pct"] == 0.08)
A._VOL_CACHE.clear()

import ast as _ast
_src = open("crypto_grid_bot.py").read()
_tree = _ast.parse(_src)
_st = next(n for n in _ast.walk(_tree)
           if isinstance(n, _ast.AsyncFunctionDef) and n.name == "_resolve_branch_stop")
ok("the status reporter asks for cached_only",
   "cached_only=True" in (_ast.get_source_segment(_src, _st) or ""))

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
