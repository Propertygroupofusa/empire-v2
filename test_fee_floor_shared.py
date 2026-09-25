"""The fee-floor rule belongs in ONE place, and every engine must reach it.

The bill for not having this, 2026-09-25 — the same defect in four places
in one evening:

  1.25%   a spacing recommended for the live grid against a 1.70% floor
  1.00%   five grid branches CREATED at this, because the floor only ran
          when a dynamic source set the value and all three had just been
          switched off
  0.25%   a committed, LIVE-FLAGGED config (bot_config.json, SHIB/PEPE/
          LINK, paper_trading false, live_trading true, $1,200) whose
          target was SIX TIMES SMALLER than the taker round trip. Every
          winning trade would have netted -1.25%.
  1.00%   crypto_grid_bot's create-branch default, source of the five

Three engines already had fee protection and each had built its own,
reactively, after its own incident, with a different name and shape:

  crypto_grid_bot.fee_safe_floor_pct          pct floor on grid spacing
  prop_bot.fee_safe_target_dollars            dollar floor on a target
  crypto_btc_compound.min_profit_target_pct   dollar floor as a pct

Four had nothing: crypto_mean_reversion_bot, crypto_coinbase_bot,
crypto_family_tree_bot, alpaca_swing_bot. That is how a 0.25% target
reached a live config - not because anyone argued for it, but because
nothing in the path it took had ever been given the rule.

THE RULES THIS FILE PROTECTS:

  1. The rule is stated ONCE, in fee_floor.py, and is importable by any
     engine. A fifth private copy is how this happened.
  2. The floor prices the WORST round trip the trade can pay. A maker
     order that does not fill becomes a market order, so the optimistic
     rate is an estimate and the pessimistic one is the guarantee.
  3. A target below the floor is REFUSED at configuration time, with the
     arithmetic in the message - not silently corrected, not logged and
     traded anyway.
  4. raise_to_floor never LOWERS a target. It is for values already
     running, where stopping would be worse than correcting.
  5. net_per_win_pct exists so the specific failure has a name: a WIN
     that is a loss.

Run: python3 test_fee_floor_shared.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fee_floor as ff

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# --- the floor itself -----------------------------------------------------
ok("floor = fee + margin", abs(ff.fee_floor_pct(0.015, 0.002) - 0.017) < 1e-12)
ok("a zero fee still demands the margin", abs(ff.fee_floor_pct(0.0, 0.002) - 0.002) < 1e-12)
ok("a negative fee is treated as zero, never as a credit",
   abs(ff.fee_floor_pct(-0.01, 0.002) - 0.002) < 1e-12)
ok("None is treated as zero, not crashed", ff.fee_floor_pct(None) >= 0)

# --- the four real values from the incident -------------------------------
TAKER, MAKER = 0.015, 0.007
ok("REGRESSION: 0.25% (the live-flagged config) is refused at taker rates",
   not ff.clears_fees(0.0025, TAKER))
ok("REGRESSION: 0.25% is refused at MAKER rates too - it is not a fee-tier problem",
   not ff.clears_fees(0.0025, MAKER))
ok("REGRESSION: 1.00% (five created branches) is refused", not ff.clears_fees(0.010, TAKER))
ok("REGRESSION: 1.25% (my own recommendation) is refused", not ff.clears_fees(0.0125, TAKER))
ok("1.70% - the floor itself - is accepted", ff.clears_fees(0.017, TAKER))
ok("2.00% - what the fleet actually runs - is accepted", ff.clears_fees(0.020, TAKER))

# --- a win that is a loss has a name --------------------------------------
ok("a 0.25% win at taker nets -1.25%",
   abs(ff.net_per_win_pct(0.0025, TAKER) + 0.0125) < 1e-12)
ok("a 0.25% win at MAKER still nets negative",
   ff.net_per_win_pct(0.0025, MAKER) < 0)
ok("a 2.00% win at taker nets +0.50%",
   abs(ff.net_per_win_pct(0.020, TAKER) - 0.005) < 1e-12)
ok("no win rate rescues a target below the fee: even 100% wins loses",
   ff.net_per_win_pct(0.0025, TAKER) < 0)

# --- refusal carries the arithmetic ---------------------------------------
try:
    ff.require_clears_fees(0.0025, TAKER, context="scalping target_profit_pct")
    refused, msg = False, ""
except ff.TargetBelowFeeFloor as e:
    refused, msg = True, str(e)
ok("a below-floor target RAISES, it is not silently corrected", refused)
ok("the refusal names the caller's setting", "scalping target_profit_pct" in msg)
ok("the refusal states what a WIN would net", "-1.250%" in msg)
ok("the refusal states the minimum", "1.700%" in msg)
ok("a clearing target passes through unchanged",
   ff.require_clears_fees(0.020, TAKER) == 0.020)

# --- raise_to_floor never lowers ------------------------------------------
ok("raise_to_floor lifts a bad value", abs(ff.raise_to_floor(0.010, TAKER) - 0.017) < 1e-12)
ok("raise_to_floor leaves a good value alone",
   abs(ff.raise_to_floor(0.030, TAKER) - 0.030) < 1e-12)
ok("raise_to_floor NEVER lowers a target",
   all(ff.raise_to_floor(t, TAKER) >= t - 1e-12
       for t in (0.001, 0.010, 0.017, 0.025, 0.050)))
ok("no input can produce a result under the floor",
   all(ff.raise_to_floor(t, f) >= ff.fee_floor_pct(f) - 1e-12
       for t in (0.0, 0.001, 0.02) for f in (0.0, 0.007, 0.015)))

# --- the rule lives in one place ------------------------------------------
src = open(os.path.join(HERE, "fee_floor.py"), encoding="utf-8").read()
ok("the module explains why it is shared, not private", "fourth private copy" in src or
   "SHARED module" in src)
ok("it records the four values that reached production",
   "0.25%" in src and "1.00%" in src and "1.25%" in src)
ok("it names the engines that had no floor",
   "crypto_mean_reversion_bot" in src and "alpaca_swing_bot" in src)
ok("it answers 'we'll never reach the floor' with the record",
   "never reach the floor" in src.lower() or "NEVER REACH" in src)
ok("it is dependency-free so any engine can import it",
   "import " not in src.split('"""')[2])

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
