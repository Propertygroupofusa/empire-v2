"""A trend filter that peeks at the future looks brilliant and is worthless.

These pin the two things that make the downtrend study trustworthy:

  1. CAUSALITY. Every filter reads closes up to and including bar i, and
     never bar i+1. Tested by making the future garbage and checking the
     answer does not move.
  2. PARTICIPATION. Sitting out earns zero, not a profit. A filter that
     skips almost everything must SAY so, or its per-trade expectancy will
     read as a result when it is an artefact of a tiny sample.
"""
import ast
import inspect
import types

import trend_filter as TF

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


print("\nsma is causal and refuses to guess")

closes = [float(x) for x in range(1, 101)]
ok("mean of the last 10 ending at index 99 is 95.5",
   TF.sma(closes, 99, 10) == 95.5, TF.sma(closes, 99, 10))
ok("it ends AT i, it does not straddle i",
   TF.sma(closes, 50, 3) == (49 + 50 + 51) / 3, TF.sma(closes, 50, 3))
ok("too little history returns None, not a short average",
   TF.sma(closes, 3, 10) is None)
ok("index past the end returns None", TF.sma(closes, 500, 10) is None)
ok("a zero window returns None, not a divide-by-zero",
   TF.sma(closes, 50, 0) is None)

print("\nTHE FUTURE CANNOT CHANGE A DECISION ABOUT THE PAST")

rising = [100.0 + i for i in range(3000)]
sane = TF.build(rising)
# Same series, but everything after bar 1500 replaced with nonsense.
poisoned = rising[:1501] + [1e9 if i % 2 else 1e-9 for i in range(1499)]
mad = TF.build(poisoned)
for name in sane:
    if sane[name] is None:
        continue
    same = all(sane[name](i) == mad[name](i) for i in range(200, 1501))
    ok(f"  {name}: unchanged by a poisoned future", same)

print("\nthe filters actually discriminate")

falling = [10000.0 - i * 2 for i in range(3000)]
up, down = TF.build(rising), TF.build(falling)
# Start past the longest warm-up any rule needs. "7d avg rising" compares a
# 7-day average against the same average 24h earlier, so it cannot speak
# until 8 days of bars exist - 2304 of them. Scoring it from bar 2000 marks
# it down for correctly refusing to guess, which is the behaviour the block
# above just asserted it should have.
WARM = 7 * 24 * 12 + 24 * 12          # 2304
for name in up:
    if up[name] is None:
        continue
    taken_up = sum(1 for i in range(WARM, 3000) if up[name](i))
    taken_dn = sum(1 for i in range(WARM, 3000) if down[name](i))
    span = 3000 - WARM
    ok(f"  {name}: takes a rising market ({taken_up}/{span})",
       taken_up > span * 0.9, taken_up)
    ok(f"  {name}: sits out a falling one ({taken_dn}/{span})",
       taken_dn < span * 0.1, taken_dn)

print("\nno history means NO TRADE, never a free pass")

short = [100.0, 101.0, 102.0]
for name, fn in TF.build(short).items():
    if fn is None:
        continue
    ok(f"  {name}: refuses when the average cannot be formed", not fn(2))

print("\nthe baseline really is unfiltered")

ok("'none (baseline)' is None, so rung_profile takes every entry",
   TF.build(rising)["none (baseline)"] is None)
ok("the headline rule is named in the module, not chosen later",
   TF.HEADLINE in TF.build(rising) and TF.HEADLINE == "price > 7d avg")
ok("the rule list is fixed, not built from data",
   len(TF.build(rising)) == len(TF.build(falling)) == 6)

print("\nrung_profile reports participation, so sitting out cannot hide")

_t = ast.parse(open("horizon_study.py", encoding="utf-8").read())
_rp = next(n for n in ast.walk(_t)
           if isinstance(n, ast.FunctionDef) and n.name == "rung_profile")
_src = ast.get_source_segment(open("horizon_study.py", encoding="utf-8").read(), _rp)
ok("it takes an allow callback", "allow=None" in _src)
ok("with a default that changes nothing", "if allow is not None and not allow(i)" in _src)
ok("it counts what was OFFERED, not just what was taken", '"offered": offered' in _src)
ok("it counts what was SKIPPED", '"skipped": skipped' in _src)
ok("and reports participation as a percentage", '"participation_pct"' in _src)
ok("a skipped entry never reaches the fill loop - it continues first",
   _src.index("skipped += 1") < _src.index("attempts += 1"))

print("\nfiltered and unfiltered come from ONE implementation")

_runner = open("run_trend_filter_study.py", encoding="utf-8").read()
ok("the runner calls horizon_study.rung_profile",
   "HS.rung_profile(" in _runner)
ok("and does not reimplement it", "def rung_profile" not in _runner)
ok("it fetches warm-up bars so a 7d average exists at the window start",
   "WARMUP_DAYS" in _runner and "days=days + WARMUP_DAYS" in _runner)
ok("it runs every filter, not a chosen one",
   "for name, fn in filters.items()" in _runner)
ok("it runs every window, bear and bull",
   _runner.count('("20') >= 3 and '"now"' in _runner)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
