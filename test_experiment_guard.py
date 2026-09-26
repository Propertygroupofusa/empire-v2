"""A budget nobody enforces is a wish.

The gates come off as a trial with a stated cost. These pin the three ways
it ends, and the two design choices that make it a real guard rather than a
label:

  * it ends itself, in a background loop, with no token and no operator
  * it can only ever move the profile in the SAFE direction

And the one people leave out: BLIND. If the spend cannot be measured, the
honest state is not "carry on" - being unable to see what something costs
is exactly the condition a budget exists to prevent.
"""
import ast
from datetime import datetime, timedelta

import experiment_guard as G

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


NOW = datetime(2026, 9, 26, 20, 0)
DL = NOW + timedelta(days=14)
BASE = -488.89


def d(current, budget=200.0, deadline=DL, now=NOW, blind=0):
    return G.decide(baseline_pnl=BASE, current_pnl=current, budget_usd=budget,
                    deadline_at=deadline, now=now, blind_checks=blind)


print("\nSPEND IS MEASURED FROM THE BASELINE, not absolutely")

ok("no change is zero spend", G.spend_usd(BASE, BASE) == 0.0)
ok("losing $120 since the switch is $120 of spend",
   G.spend_usd(BASE, BASE - 120) == 120.0)
ok("being UP shows as negative spend", G.spend_usd(BASE, BASE + 50) == -50.0)
ok("an unreadable current P&L gives None, not zero",
   G.spend_usd(BASE, None) is None,
   "zero would read as 'it has cost nothing' - the worst possible answer")
ok("an unreadable baseline also gives None", G.spend_usd(None, -400) is None)
ok("garbage gives None", G.spend_usd("x", "y") is None)

print("\nTHE BUDGET ENDS IT")

ok("$120 spent keeps running", d(BASE - 120)["end"] is False)
ok("$199.99 keeps running", d(BASE - 199.99)["end"] is False)
ok("exactly $200 ends it", d(BASE - 200)["end"] is True)
ok("and names BUDGET", d(BASE - 200)["reason"] == G.BUDGET)
ok("with the figures in the detail",
   "$200.00" in d(BASE - 250)["detail"], d(BASE - 250)["detail"])
ok("being up does NOT end it", d(BASE + 500)["end"] is False)

print("\nTHE DEADLINE ENDS IT, and it is checked FIRST")

late = NOW + timedelta(days=15)
ok("past the date ends it", d(BASE, now=late)["end"] is True)
ok("and names DEADLINE", d(BASE, now=late)["reason"] == G.DEADLINE)
ok("exactly at the deadline ends it", d(BASE, now=DL)["end"] is True)
ok("A PAST DEADLINE ENDS IT EVEN WHEN THE SPEND IS UNREADABLE",
   G.decide(baseline_pnl=None, current_pnl=None, budget_usd=200,
            deadline_at=DL, now=late)["reason"] == G.DEADLINE,
   "'we could not tell, so we kept going past the date' is not defensible")
ok("no deadline set does not crash", d(BASE - 10, deadline=None)["end"] is False)

print("\nBLIND ENDS IT, but not on one blip")

for n in range(1, G.MAX_BLIND_CHECKS):
    r = G.decide(baseline_pnl=None, current_pnl=None, budget_usd=200,
                 deadline_at=DL, now=NOW, blind_checks=n - 1)
    ok(f"  blind check {n} rides it out", r["end"] is False and r["blind_checks"] == n)
r = G.decide(baseline_pnl=None, current_pnl=None, budget_usd=200,
             deadline_at=DL, now=NOW, blind_checks=G.MAX_BLIND_CHECKS - 1)
ok(f"  blind check {G.MAX_BLIND_CHECKS} ends it", r["end"] is True and r["reason"] == G.BLIND)
ok("and says why that is the right call",
   "condition the budget exists to prevent" in r["detail"], r["detail"])
ok("a good check resets the blind counter", d(BASE - 10)["blind_checks"] == 0)
ok("the threshold is a stated constant", 2 <= G.MAX_BLIND_CHECKS <= 12)

print("\nSTATUS reads for a human, running or finished")

s = G.status({"profile": "aug2026", "baseline_realized_pnl": BASE,
              "budget_usd": 200.0, "deadline_at": DL.isoformat() + "Z",
              "started_at": NOW.isoformat() + "Z", "blind_checks": 0},
             current_pnl=BASE - 75, now=NOW)
ok("it reports running", s["running"] is True)
ok("spend so far", s["spend_usd"] == 75.0)
ok("and what is left", s["remaining_usd"] == 125.0)
ok("and days left", s["days_left"] == 14, s["days_left"])
fin = G.status({"profile": "aug2026", "ended_at": "2026-10-01T00:00:00Z",
                "ended_reason": "BUDGET", "ended_spend_usd": 201.4})
ok("a finished one reports what it cost", "201.40" in fin["note"], fin["note"])
ok("and why it ended", fin["ended_reason"] == "BUDGET")
ok("no experiment reads as gates on",
   "gates are on" in G.status(None)["note"].lower())
ok("unreadable spend is said, not shown as zero",
   "unreadable" in G.status({"budget_usd": 200, "baseline_realized_pnl": BASE},
                            current_pnl=None)["note"])

print("\nTHE WORKER ENDS IT WITHOUT AN OPERATOR")

W = open("experiment_worker.py", encoding="utf-8").read()
wt = ast.parse(W)
ok("it runs on a loop", "async def run_periodically" in W and "while True" in W)
ok("that never dies", "except Exception" in W)
ok("an unreadable P&L returns None, not 0.0",
   "return None" in W and "would read as" in W)
ok("it commits the row BEFORE flipping the profile",
   W.index("exp.ended_reason = d[\"reason\"]") < W.index('await set_profile("guarded")'),
   "the other order can leave a row marked running with the gates already on")
ok("IT ONLY EVER TURNS GATES ON",
   W.count('set_profile("guarded")') == 1 and "aug2026" not in W,
   "a safety task that can disable a check is not a safety task")
ok("it says why it may write without the token",
   "not a request" in W and "must not depend on" in W)

print("\nTHE SWITCH CANNOT LEAVE GATES OFF UNGUARDED")

BOT = open("crypto_grid_bot.py", encoding="utf-8").read()
fns = {n.name: n for n in ast.walk(ast.parse(BOT))
       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
src = ast.get_source_segment(BOT, fns["set_trading_profile"]) or ""
ok("switching always records an experiment", "_record_profile_experiment" in src)
ok("a failure to open the budget REVERTS to guarded",
   "r2.base_capital = 0.0" in src and "return trading_profile.GUARDED" in src,
   "otherwise the gates are off with nothing watching the cost")
rec = ast.get_source_segment(BOT, fns["_record_profile_experiment"]) or ""
ok("switching back closes the running experiment", '"MANUAL"' in rec)
ok("switching on twice does NOT reset the clock",
   "already budgeted" in rec or "do not reset" in rec,
   "re-flipping would otherwise grant a fresh two weeks each time")
ok("the baseline is the ledger's combined realized P&L",
   "CryptoCoinTradeHistory.pnl" in rec and "CryptoGridTradeHistory.pnl" in rec)

print("\nthe endpoint bounds what can be asked for")

DASH = open("routers/trading_dashboard.py", encoding="utf-8").read()
dfns = {n.name: n for n in ast.walk(ast.parse(DASH))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
psrc = ast.get_source_segment(DASH, dfns["set_trading_profile_endpoint"]) or ""
ok("budget must be positive and bounded", "0 < budget_usd <= 5000" in psrc)
ok("days must be positive and bounded", "0 < days <= 90" in psrc)
ok("the bounds apply only when turning gates OFF",
   "wanted != trading_profile.GUARDED" in psrc,
   "reverting to safe must never be refused on a validation error")
ok("a refused switch is reported, not silently swallowed",
   "The gates were NOT turned off" in psrc)
ok("the status carries the experiment", "_experiment_status()" in psrc)

print("\nthe model keeps the record")

M = open("models.py", encoding="utf-8").read()
for col in ("budget_usd", "deadline_at", "baseline_realized_pnl",
            "ended_reason", "ended_spend_usd", "blind_checks"):
    ok(f"  TradingExperiment.{col}", f"{col} = Column(" in M)
ok("active is derived from ended_at, not stored separately",
   '"active": self.ended_at is None' in M,
   "two stored flags for one fact eventually disagree")

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
