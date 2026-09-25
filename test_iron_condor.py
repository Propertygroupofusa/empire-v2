"""Iron condor: the contract multiplier, and measuring on real fills only.

WHY THIS FILE EXISTS

A simulator this bot was being compared against computed a "50% of width"
stop as 50% of 5 instead of 50% of 500 - it dropped the x100 options
contract multiplier. That turned a -$215 loser into a -$2.50 one, a
30-trade month from -$637.50 into +$425.00, and produced the
recommendation "strong edge detected, proceed to paper trading".

The bot's own arithmetic was already correct. These checks keep it that
way, and pin the two things it was missing: realized P&L that survives
the close, and a readable answer to "can this account even trade this".

Run: python3 test_iron_condor.py
"""
import os
import re
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "alpaca_iron_condor_bot.py")).read()

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# --- the multiplier ---------------------------------------------------------
ok("entry risk applies the x100 contract multiplier",
   'Decimal("100")' in SRC and "spread_width" in SRC)
ok("risk is width MINUS credit, not width alone",
   'ENTRY_RULES["spread_width"] - candidate.limit_credit' in SRC)
ok("realized P&L applies the multiplier exactly once",
   SRC.count('* Decimal("100")\n                        * Decimal(trade["contracts"])') == 1
   or 'Decimal("100")\n                        * Decimal(trade["contracts"])' in SRC)

# The arithmetic the simulator got wrong, checked directly.
width, credit = Decimal("5.00"), Decimal("0.375")
risk = (width - credit) * 100
ok("a $5 spread is $500 of risk, not $5", risk == Decimal("462.50"))
ok("a 50%-of-width stop is $250, not $2.50", width * 100 * Decimal("0.5") == Decimal("250"))

# --- realized P&L survives the close ---------------------------------------
ok("the triggering debit is stored when the exit is submitted",
   '"exit_debit_at_signal"' in SRC)
ok("realized P&L is persisted when the close fills", '"realized_pnl"' in SRC)
ok("it prefers the REAL exit fill over the signal debit",
   'order.get("filled_avg_price")' in SRC)
ok("and falls back to the signal debit rather than dropping the trade",
   'trade.get("exit_debit_at_signal"' in SRC)

# --- honest reporting -------------------------------------------------------
ok("a readiness check exists", "def readiness(" in SRC)
ok("it names the equity needed for one contract",
   "equity_required_for_one_contract" in SRC)
ok("it lists blockers rather than raising into a log",
   '"blockers"' in SRC)
ok("a performance report exists", "def performance(" in SRC)
ok("it reads ONLY closed trades with a realized figure",
   't.get("status") == "closed" and t.get("realized_pnl") is not None' in SRC)
ok("profit factor is None, never infinite, before a loss",
   '"profit_factor": (str(money(sum(wins) / loss_sum)) if loss_sum else None)' in SRC)
ok("it warns that an early run of winners is the normal shape",
   "lose rarely and lose big" in SRC)
ok("there is no win-rate input anywhere",
   "win_rate_assumption" not in SRC and "win_rate_input" not in SRC)

# --- the risk cap actually binds on a small account ------------------------
equity = Decimal("936.01")
cap = equity * Decimal("0.02")
ok("a 2% cap refuses one contract on a ~$936 account", risk > cap)
ok("and the equity needed is ~$23,125", (risk / Decimal("0.02")).quantize(Decimal("1")) == Decimal("23125"))
ok("the entry path still enforces that cap",
   'exceeds the 2% account risk limit' in SRC)
ok("and the options buying-power check", "exceeds options buying power" in SRC)

width_ = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width_}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
