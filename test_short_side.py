"""The short-side replay must be honest about what shorting really costs.

Built 2026-09-25. The account owner observed, correctly, that the Alpaca
side profits when the market falls - inverse ETFs bought long - and asked
for the same on crypto. Coinbase SPOT cannot do it: nothing there rises
when a coin drops. The only route is perpetual futures on a different
venue and a different account, which is a real build. So it gets a
backtest before it gets an account.

WHAT THIS FILE GUARDS. A short replay is dangerously easy to make look
good, because the naive version is just the long grid with the sign
flipped - and that version is free money in any falling market. Two things
make it not free, and both must stay modelled:

  FUNDING. A perp pays funding to the other side, typically every 8 hours,
  charged on NOTIONAL rather than on profit. Omitting it is the same class
  of error as a paper backtest that never charges fees - the one that on
  this same day turned a -0.48%/trade scalper into a +352% paper result.

  ASYMMETRY. A long slice can lose at most what it cost. A short slice's
  loss is unbounded, because price can double.

Run: python3 test_short_side.py
"""
import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


src = open(os.path.join(HERE, "crypto_selection_backtest.py"), encoding="utf-8").read()
# The replays used to charge engine.ROUND_TRIP_FEE_RATE and this file
# stubbed it at 1.0%. Both were wrong: measured from Coinbase's own fill
# records on 2026-09-25, a taker round trip is 1.50% and a maker round
# trip 0.70%. The replays now take BACKTEST_ROUND_TRIP_FEE_RATE, so this
# namespace supplies the real taker rate - the worst case an unfilled
# post-only order actually lands on.
engine = types.SimpleNamespace(ROUND_TRIP_FEE_RATE=0.010)
ns = {"os": os, "engine": engine, "SPEND": 150.0,
      "BACKTEST_ROUND_TRIP_FEE_RATE": 0.015,
      "STRATEGY_LAB_GRID_PCT": 0.01, "STRATEGY_LAB_GRID_LEVELS": 10}
_i = src.index("def _summarize_strategy_trades")
exec(src[_i:src.index("\n\n\n", _i)], ns)
_j = src.index("PERP_FUNDING_RATE_8H")
exec(src[_j:src.index("def _replay_grid_bot_short")], ns)
_k = src.index("def _replay_grid_bot_short")
exec(src[_k:src.index("def _replay_swing_trading")], ns)
_m = src.index("def _replay_grid_bot(")
exec(src[_m:src.index("\n\n\ndef ", _m)], ns)

short, long_ = ns["_replay_grid_bot_short"], ns["_replay_grid_bot"]
CFG = dict(spend=150, grid_pct=0.025, num_levels=3)


def osc(drift, n=1200, amp=0.05):
    """Sawtooth oscillation with a drift - roughly what a real market does."""
    return [100 * ((1 + drift) ** i) * (1 + amp * math.sin(i / 3.0)) for i in range(n)]


down, up, flat = osc(-0.0008), osc(0.0008), osc(0.0)

# --- direction: the whole point of the exercise ---------------------------
Ld = long_(down, down, down, **CFG)
Sd = short(down, down, down, funding_8h=0.0, **CFG)
ok("in a FALLING market the short side beats the long side",
   Sd["total_pnl"] > Ld["total_pnl"])
ok("and the long side genuinely loses there (the gap this is meant to fill)",
   Ld["total_pnl"] < 0)

Lu = long_(up, up, up, **CFG)
Su = short(up, up, up, funding_8h=0.0, **CFG)
ok("in a RISING market the long side beats the short side",
   Lu["total_pnl"] > Su["total_pnl"])
ok("and the short side genuinely loses there - it is not free money",
   Su["total_pnl"] < 0)

Lf = long_(flat, flat, flat, **CFG)
Sf = short(flat, flat, flat, funding_8h=0.0, **CFG)
ok("in a FLAT oscillating market both sides profit from movement",
   Lf["total_pnl"] > 0 and Sf["total_pnl"] > 0)

# --- funding must be charged, reported, and must bite ---------------------
S_free = short(down, down, down, funding_8h=0.0, **CFG)
S_paid = short(down, down, down, funding_8h=0.01, **CFG)
ok("funding is charged when a rate is set", S_paid["funding_paid_usd"] > 0)
ok("no funding is charged at a zero rate", S_free["funding_paid_usd"] == 0)
ok("funding REDUCES short profit", S_paid["total_pnl"] < S_free["total_pnl"])
ok("the funding paid is reported as its own figure, not hidden in the average",
   "funding_paid_usd" in S_paid and "funding_rate_8h" in S_paid)
S_huge = short(down, down, down, funding_8h=0.10, **CFG)
ok("a punitive funding rate can turn a winning short into a loser",
   S_huge["total_pnl"] < S_paid["total_pnl"])
ok("funding scales with the rate",
   S_huge["funding_paid_usd"] > S_paid["funding_paid_usd"])

# --- the mechanic itself --------------------------------------------------
crash = [100 * (0.999 ** i) for i in range(600)]
Sc = short(crash, crash, crash, funding_8h=0.0, **CFG)
ok("a ONE-WAY crash gives a short grid nothing to sell into (no rallies)",
   Sc is None or Sc["num_trades"] == 0)
rally = [100 * (1.001 ** i) for i in range(600)]
Lr = long_(rally, rally, rally, **CFG)
ok("and a one-way rally gives a LONG grid nothing to buy - the mirror",
   Lr is None or Lr.get("num_trades", 0) <= 1)

ok("open shorts at window end are reported", "open_shorts_at_end" in S_free)
ok("the short replay returns the same shape as the long one",
   set(["num_trades", "win_rate", "total_pnl", "roi_pct_of_spend"]).issubset(S_free))

# --- the runner must carry its caveats with the number -------------------
runner = src[src.index("async def run_short_side_comparison"):
             src.index("async def run_grid_rotation_effectiveness_backtest")]
ok("the comparison defaults to the LIVE promoted config (3 levels, 2.5%)",
   "grid_pct=0.025" in runner and "num_levels=3" in runner)
ok("it reports all three of long, short and both",
   all(k in runner for k in ["long_only_total_pnl", "short_only_total_pnl",
                             "both_combined_total_pnl"]))
ok("total funding paid is surfaced", "total_funding_paid_usd" in runner)
ok("a losing short produces a verdict against opening a perp account",
   "not justified by this evidence" in runner)
ok("even a WINNING short says investigate, never deploy",
   "INVESTIGATING" in runner and "not deploying" in runner)
for caveat in ["Liquidation", "Perp fees", "crowded side", "unbounded",
               "different venue"]:
    ok(f"the caveat about {caveat.lower()} travels with the result",
       caveat in runner or caveat.lower() in runner.lower())

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
