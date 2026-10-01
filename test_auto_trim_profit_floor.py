"""An armed trimmer must not be able to realise a loss.

The measurement: live 2026-10-01, XRP sat at 20.7% of the account - over the
line, $120.22 of excess, auto_trim armed - and the only thing between it and
an automatic sale at -4.18% was the ACTIVELY_TRADED guard, which exists for
an unrelated reason. Remove the grid slices from XRP and this module would
have booked that loss by itself.

auto_trim had no entry_price, no net_pct and no round-trip anywhere in it.
Its own docstring said as much.
"""
import sys
from datetime import datetime, timezone

import auto_trim as at

FAILS = []
NOW = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)


def ok(label, cond, got=None):
    if cond:
        print(f"  PASS  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"   got: {got!r}" if got is not None else ""))


def H(asset, usd, price=None, units=None):
    return {"asset": asset, "usd": usd, "price": price, "units": units}


def one(holdings, total, **kw):
    kw.setdefault("now", NOW)
    return {p["asset"]: p for p in at.plan_trims(holdings, total, **kw)}


print("\n[1] THE LIVE CASE: XRP over the limit and underwater")
# 20.7% of the account, bought at 1.5252, now 1.4868 -> about -2.5% gross.
p = one([H("XRP", 2078.42, price=1.4868, units=1398.0)], 10042.06,
        cost_basis={"XRP": 1.5252})["XRP"]
ok("it is recognised as over the limit", p["excess_usd"] > 0, p["excess_usd"])
ok("and it is NOT trimmed", p["act"] is False, p)
ok("the reason names the loss", p["reason"] == "WOULD_REALISE_A_LOSS", p["reason"])
ok("the net is reported after the round trip",
   p["net_pct_if_sold"] < 0, p.get("net_pct_if_sold"))
ok("the detail says over-limit is not a reason to book a loss",
   "not a reason to book a loss" in p["detail"], p["detail"][:90])

print("\n[2] the SAME holding in profit IS trimmed")
p = one([H("XRP", 2078.42, price=1.70, units=1398.0)], 10042.06,
        cost_basis={"XRP": 1.5252})["XRP"]
ok("a profitable over-limit holding is trimmed", p["act"] is True, p)
ok("the reason is OVER_LIMIT", p["reason"] == "OVER_LIMIT", p["reason"])
ok("the net is positive and recorded", p["net_pct_if_sold"] > 0, p.get("net_pct_if_sold"))

print("\n[3] AN UNKNOWN BASIS IS NOT A PROFIT")
p = one([H("ZEC", 2500.0, price=1400.0, units=1.78)], 10000.0)["ZEC"]
ok("no basis supplied -> refused", p["act"] is False, p)
ok("named BASIS_UNKNOWN", p["reason"] == "BASIS_UNKNOWN", p["reason"])
ok("the detail says an unknown basis cannot be shown to be a profit",
   "cannot be shown to be a profit" in p["detail"], p["detail"][:90])
for bad in (None, 0, -1, "abc"):
    p = one([H("ZEC", 2500.0, price=1400.0)], 10000.0,
            cost_basis={"ZEC": bad})["ZEC"]
    ok(f"a basis of {bad!r} is refused, not trusted",
       p["act"] is False and p["reason"] == "BASIS_UNKNOWN", p["reason"])

print("\n[4] an unreadable PRICE refuses too")
p = one([H("ZEC", 2500.0, price=None)], 10000.0, cost_basis={"ZEC": 1000.0})["ZEC"]
ok("no price -> no trim", p["act"] is False, p)
ok("named PRICE_UNREADABLE", p["reason"] == "PRICE_UNREADABLE", p["reason"])

print("\n[5] the floor is the ROUND TRIP, not break-even")
# +1.0% gross does not clear a 1.6407% round trip.
p = one([H("X", 2500.0, price=101.0)], 10000.0, cost_basis={"X": 100.0})["X"]
ok("a +1.00% gross gain is refused", p["act"] is False, p)
ok("...because it nets negative", p["net_pct_if_sold"] < 0, p.get("net_pct_if_sold"))
p = one([H("X", 2500.0, price=103.0)], 10000.0, cost_basis={"X": 100.0})["X"]
ok("a +3.00% gross gain clears it", p["act"] is True, p)

print("\n[6] the cost model is SHARED, not restated")
import concentration_rotation as rot
ok("auto_trim uses the rotation's round trip",
   at.ROUND_TRIP_COST_PCT == rot.DEFAULT_ROUND_TRIP_COST_PCT,
   (at.ROUND_TRIP_COST_PCT, rot.DEFAULT_ROUND_TRIP_COST_PCT))
ok("which charges the TAKER exit", at.ROUND_TRIP_COST_PCT == 1.6407,
   at.ROUND_TRIP_COST_PCT)

print("\n[7] the floor is ON by default")
ok("REQUIRE_PROFIT is True", at.REQUIRE_PROFIT is True)
p = one([H("ZEC", 2500.0, price=1400.0)], 10000.0)["ZEC"]
ok("a caller that passes nothing gets the floor", p["act"] is False, p)

print("\n[8] it does not override the guards that already existed")
p = one([H("XRP", 2078.42, price=1.70)], 10042.06,
        cost_basis={"XRP": 1.0}, actively_traded={"XRP"})["XRP"]
ok("ACTIVELY_TRADED still wins over a profitable trim",
   p["reason"] == "ACTIVELY_TRADED", p["reason"])
p = one([H("ZEC", 1000.0, price=100.0)], 10000.0, cost_basis={"ZEC": 50.0})["ZEC"]
ok("a within-limit holding is still WITHIN_LIMIT",
   p["reason"] == "WITHIN_LIMIT", p["reason"])

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
