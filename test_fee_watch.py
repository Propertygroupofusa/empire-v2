"""The fee watchdog, exercised against synthetic fills.

$1,426.39 in one day, 12.8% of the account, unnoticed for twenty days. The
money was never hidden - Coinbase charged it per fill and reported it per
fill. Nothing was looking. These check that something is looking now, and
that it looks at the right things:

  * thresholds as a share of the ACCOUNT, not dollars. This account was
    both $11,121 and $572 inside one morning depending on which number you
    believed, and a dollar threshold means something different in each.
  * spot separated from everything else. One day of event contracts hid
    inside a month of spot trading precisely because they were summed.
  * named as DETECTION. A monitor that reads like a brake invites the
    assumption that something will stop the next one.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

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


import fee_watch as F
import crypto_btc_compound_bot as engine

NOW = datetime.now(timezone.utc)


def fill(hours_ago, comm, product="BTC-USD"):
    t = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"trade_time": t, "commission": str(comm), "product_id": product}


def run(fills, account=11121.07):
    async def fake(session, start, end, **kw):
        return {"available": True, "fills": fills, "truncated": False}
    orig = engine.fetch_fills_between
    engine.fetch_fills_between = fake
    try:
        return asyncio.run(F.watch(None, account_usd=account))
    finally:
        engine.fetch_fills_between = orig


print("\nthe September 6 shape is caught")
# 712 fills, $1,426 of event contracts, inside 24h.
sept6 = [fill(3, 2.00, f"KXBTC15M-26SEP0603{i%60:02d}-30-KALSHI") for i in range(712)]
r = run(sept6)
ok("it fires ALARM", r["status"] == "ALARM", r["status"])
ok("on the DAY window", r["day_verdict"] == "ALARM", r["day_verdict"])
ok("and quantifies it as a share of the account",
   r["windows"]["24h"]["pct_of_account"] > 10, str(r["windows"]["24h"]["pct_of_account"]))
ok("the alarm names the precedent so the scale is legible",
   "12.8% in one day" in r.get("alarm", ""), r.get("alarm", "")[:90])
ok("it separates non-spot from spot",
   r["windows"]["24h"]["non_spot_usd"] > 1400 and r["windows"]["24h"]["spot_usd"] == 0,
   str(r["windows"]["24h"]))
ok("and says so, because summing them is how it hid",
   "NON-SPOT" in r.get("non_spot_note", ""), r.get("non_spot_note", "")[:90])

print("\na normal spot week does not cry wolf")
quiet = [fill(h, 0.03) for h in range(1, 160, 4)]
r = run(quiet)
ok("status ok", r["status"] == "ok", str(r["status"]))
ok("no alarm text", "alarm" not in r)
ok("no non-spot note when there is no non-spot", "non_spot_note" not in r)

print("\nthresholds are a share of the ACCOUNT, not dollars")
# $5 in a day: 0.045% of $11k, 0.87% of $572. Identical dollars, opposite
# verdicts - which is the entire reason the thresholds are percentages.
same = [fill(2, 5.0)]
big = run(same, account=11121.07)
small = run(same, account=572.47)
ok("$5 on an $11k account is fine", big["day_verdict"] == "ok", big["day_verdict"])
ok("the same $5 on a $572 account is not",
   small["day_verdict"] in ("elevated", "ALARM"), small["day_verdict"])
ok("identical dollars, opposite verdicts",
   big["windows"]["24h"]["commission_usd"] == small["windows"]["24h"]["commission_usd"])
# And the warn level is not arbitrary: 0.25%/day compounds to ~91%/yr.
big30 = run([fill(2, 30.0)], account=11121.07)
ok("$30/day on $11k reads elevated - 0.27%/day is ~98% a year",
   big30["day_verdict"] == "elevated", big30["day_verdict"])

print("\nwindows are independent")
old = [fill(80, 40.0) for _ in range(6)]        # a week ago, not today
r = run(old)
ok("yesterday stays quiet when the spend was last week",
   r["windows"]["24h"]["commission_usd"] == 0.0, str(r["windows"]["24h"]))
ok("but the 7-day window sees it",
   r["windows"]["7d"]["commission_usd"] > 200, str(r["windows"]["7d"]))
ok("and the week verdict reacts", r["week_verdict"] in ("elevated", "ALARM"), r["week_verdict"])

print("\nit is honest about what it is")
ok("named DETECTION ONLY in the payload", "DETECTION ONLY" in r["note"])
ok("and says it cannot reach the process that spent it",
   "outside this codebase" in r["note"])
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fee_watch.py")).read()
ok("the module says the same, not just the payload",
   "Detection, not prevention" in SRC)
ok("it never places an order", "place_market" not in SRC and "place_order" not in SRC)
ok("and never writes", "db.add" not in SRC and "commit()" not in SRC)

print("\nedge cases")
ok("no fills at all", run([])["status"] == "ok")
ok("an unknown account size yields no percentage, not a false ok",
   run([fill(1, 500.0)], account=None)["windows"]["24h"]["pct_of_account"] is None)
ok("and the verdict is 'unknown' rather than 'ok'",
   run([fill(1, 500.0)], account=None)["day_verdict"] == "unknown")
ok("a malformed commission is skipped, not fatal",
   isinstance(run([{"trade_time": "2026-09-26T00:00:00Z", "commission": "x",
                    "product_id": "BTC-USD"}]), dict))

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
