"""A setup whose target is smaller than its fee is decided before it trades.

The proposed 5-minute scalping matrix: 10x leverage, 0.20% take-profit,
0.10% stop, assuming a 0.05% round trip. This account pays 0.70% maker
both legs - fourteen times that. These tests pin the arithmetic that makes
the difference visible.
"""

import trade_viability as V

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


print("\nthe fee is paid on a LOSS too - forgetting that understates the risk")

ok("a win keeps the target minus the fee",
   V.net_on_win(0.20, 0.70) == -0.50, V.net_on_win(0.20, 0.70))
ok("a loss costs the stop PLUS the fee",
   V.net_on_loss(0.10, 0.70) == -0.80, V.net_on_loss(0.10, 0.70))
ok("so a '0.10% stop' is really a 0.80% event at this fee",
   abs(V.net_on_loss(0.10, 0.70)) == 8 * 0.10)

print("\na target below the fee has NO viable win rate - not a 100% one")

for lev, tp, sl in ((5, 0.40, 0.20), (10, 0.20, 0.10), (20, 0.10, 0.05)):
    r = V.assess(tp, sl, V.MEASURED_MAKER_RT_PCT)
    ok(f"{lev:>2}x (TP {tp}%) is refused at the real maker fee", r["ok"] is False, r)
    ok(f"{lev:>2}x reports NO break-even win rate, not 100%",
       r["breakeven_win_rate_pct"] is None, r["breakeven_win_rate_pct"])
    ok(f"{lev:>2}x says a win is itself a loss", r["net_on_win_pct"] < 0, r)

ok("the refusal explains what would have to change",
   "must exceed" in V.assess(0.20, 0.10, 0.70)["verdict"])

print("\nthe SAME setups pass at the fee the model assumed - the fee is the variable")

for tp, sl in ((0.40, 0.20), (0.20, 0.10), (0.10, 0.05)):
    ok(f"TP {tp}% clears a 0.05% round trip", V.assess(tp, sl, 0.05)["ok"] is True)

print("\na setup that clears costs is judged on its break-even win rate")

r = V.assess(2.50, 1.25, V.MEASURED_MAKER_RT_PCT)
ok("a 2.50% target at the real fee is viable", r["ok"] is True, r)
ok("and reports a real break-even win rate",
   0 < r["breakeven_win_rate_pct"] < 100, r["breakeven_win_rate_pct"])
# The textbook claim for 1:2 risk-reward is a 33.3% break-even win rate.
# That figure assumes no fee. At this account's 0.70% round trip the same
# setup needs 52.0% - the fee turns a favourable-looking ratio into worse
# than a coin flip, and that gap is the entire point of this module.
free = V.breakeven_win_rate(2.50, 1.25, 0.0) * 100
ok("with no fee a 1:2 setup breaks even near 33%", abs(free - 33.33) < 0.1, free)
ok("with the real fee it needs 52%",
   abs(r["breakeven_win_rate_pct"] - 52.0) < 0.1, r["breakeven_win_rate_pct"])
ok("so the fee costs nearly 19 points of required win rate",
   r["breakeven_win_rate_pct"] - free > 18, r["breakeven_win_rate_pct"] - free)

thin = V.assess(0.72, 0.36, 0.70)
ok("a target barely over the fee is refused as impractical", thin["ok"] is False, thin)
ok("with the win rate it would need stated",
   thin["breakeven_win_rate_pct"] > 95, thin)

print("\nthe minimum viable target is the fee itself")

for fee in (0.05, 0.70, 1.1931, 1.50):
    ok(f"at {fee}% round trip the target must exceed {fee}%",
       V.min_tp_for_fee(fee) == fee, V.min_tp_for_fee(fee))
ok("asking for real margin on top raises it",
   V.min_tp_for_fee(0.70, 0.30) == 1.00)

print("\nposition size is bankroll x leverage, and the loss follows from it")

# The proposed matrix listed a flat $1,000 position at every leverage with
# a flat $10 loss beside it. Those cannot both be true.
p = V.position_and_loss(1000, 10, 0.10)
ok("$1,000 at 10x is a $10,000 position", p["position_usd"] == 10000.0, p)
ok("stopped 0.10% away that is a $10.00 loss", p["loss_usd"] == 10.0, p)
flat = V.position_and_loss(200, 5, 0.20)   # the matrix's own 5x row
ok("the matrix's $1,000 position at 0.20% loses $2.00, not $10.00",
   V.position_and_loss(1000, 1, 0.20)["loss_usd"] == 2.0,
   V.position_and_loss(1000, 1, 0.20))

print("\nleverage is derived from the stop, not chosen first")

ok("risking 1% with a 0.35% stop implies about 2.9x",
   abs(V.max_leverage_for_risk(1.0, 0.35) - 2.857) < 0.01,
   V.max_leverage_for_risk(1.0, 0.35))
ok("a tighter stop implies MORE leverage for the same risk",
   V.max_leverage_for_risk(1.0, 0.10) > V.max_leverage_for_risk(1.0, 0.35))
ok("a zero stop is refused rather than dividing by it",
   V.max_leverage_for_risk(1.0, 0) is None)

print("\nat this account's real rates, the viable setups are wide and slow")

for fee, label in ((V.MEASURED_MAKER_RT_PCT, "maker both legs"),
                   (V.MEASURED_BLENDED_RT_PCT, "measured blended"),
                   (V.MEASURED_TAKER_RT_PCT, "taker both legs")):
    tp = V.min_tp_for_fee(fee)
    lev = V.max_leverage_for_risk(1.0, tp / 2)
    ok(f"{label:17} needs TP > {tp:.4f}% and caps leverage near {lev:.1f}x",
       lev < 3.0, (tp, lev))

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
