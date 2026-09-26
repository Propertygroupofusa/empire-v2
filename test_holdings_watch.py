"""An alert that calls itself a stop is the most dangerous thing here.

$11,212 of coin has no branch, no monitoring and no stop. This computes
where a stop would go. It does not place one, and everything below exists
to make sure nobody - including a future reader of the dashboard - can
mistake the two.

The other failure mode is the census bug repeated: that census reported
$79.30 for an $11,292 account by silently dropping what it could not
price. A watch that hides what it cannot assess fails identically, so
every refusal here has to surface as an explicit status.
"""
import holdings_watch as W

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


print("\nit never claims to be an order")

s = W.summarise([])
ok("the summary says it is a watch", s["is_a_watch_not_an_order"] is True)
ok("and spells out that nothing rests at the exchange",
   "not stop-loss orders" in s["disclaimer"] and "nothing here will sell" in s["disclaimer"],
   s["disclaimer"])
ok("the module docstring says so too",
   "NOT a stop-loss order" in W.__doc__)

print("\nthe peak is the highest HIGH, not the highest close")

ok("a wick counts", W.peak_from_highs([10, 12, 9]) == 12)
ok("an empty series gives None, not zero", W.peak_from_highs([]) is None)
ok("junk is skipped, not crashed on", W.peak_from_highs(["x", None, 5, -1]) == 5)
ok("all-junk gives None", W.peak_from_highs(["x", None, -1]) is None)

print("\na breach is called a breach")

# 20% daily vol -> 2.5x = 50%, capped at 25%. Peak 100 -> stop at 75.
r = W.assess("ZEC", 1.83, 70.0, 128.0, 100.0, 20.0, account_total_usd=1000.0)
ok("status is BREACHED", r["status"] == W.BREACHED, r["status"])
ok("the stop is the 25% cap, not 50%", r["stop_pct"] == 0.25, r["stop_pct"])
ok("the level is 75", r["stop_level"] == 75.0, r["stop_level"])
ok("it says how far below the peak", abs(r["pct_from_peak"] + 30.0) < 1e-6)
ok("and names the dollars exposed", "$128.00" in r["note"], r["note"])

print("\nan untouched holding is OK, and a close one is NEAR")

r = W.assess("BTC", 1, 99.0, 500.0, 100.0, 4.0, account_total_usd=1000.0)
ok("4% vol -> 10% stop", r["stop_pct"] == 0.10, r["stop_pct"])
ok("99 against a 90 stop is OK", r["status"] == W.OK, r["status"])
r = W.assess("BTC", 1, 91.0, 500.0, 100.0, 4.0, account_total_usd=1000.0)
ok("91 is 90% of the way down and reads NEAR", r["status"] == W.NEAR, r["status"])
# pct_to_stop is rounded to 3dp for display, so compare at that precision.
ok("pct_to_stop is measured against the LEVEL, not the peak",
   abs(r["pct_to_stop"] - round((91.0 / 90.0 - 1) * 100, 3)) < 1e-9,
   r["pct_to_stop"])
ok("and it is NOT the distance from the peak",
   abs(r["pct_to_stop"] - r["pct_from_peak"]) > 1.0)
ok("exactly at the level is a breach, not NEAR",
   W.assess("X", 1, 90.0, 500.0, 100.0, 4.0)["status"] == W.BREACHED)

print("\nWHAT IT CANNOT SEE IS STATED, NEVER DROPPED")

u = W.assess("RNDR", 9.87, None, None, None, None, account_total_usd=1000.0)
ok("an unpriced asset still returns a row", u is not None)
ok("with an UNPRICED status", u["status"] == W.UNPRICED)
ok("and says it is unknown, not small",
   "NOT a small position" in u["note"], u["note"])

n = W.assess("NEWCOIN", 100, 1.0, 400.0, None, None, account_total_usd=1000.0)
ok("no volatility data gives NO_VOLATILITY_DATA", n["status"] == W.NO_VOL)
ok("and NO invented level", n["stop_level"] is None and n["stop_pct"] is None,
   "a made-up number would read as protection that does not exist")
ok("and says exactly that", "would read as protection" in n["note"])

d = W.assess("AERGO", 137.3, 0.0096, 1.32, 10.0, 5.0, account_total_usd=1000.0)
ok("a sub-minimum position is BELOW_MIN_TRADE", d["status"] == W.DUST)
ok("and explains there is no exit to take", "no exit to alert about" in d["note"])
ok("dust is not silently given a stop", d["stop_level"] is None)

print("\nconcentration is measured against the owner's own rule")

ok("the limit is the 20% he stated", W.CONCENTRATION_LIMIT_PCT == 20.0)
big = W.assess("ZEC", 1.83, 1563.9, 2861.0, 1600.0, 5.0, account_total_usd=11292.0)
ok("25.3% of the account trips it", big["over_concentration_limit"] is True,
   big["share_of_account_pct"])
small = W.assess("XLM", 2641, 0.218, 576.0, 0.25, 5.0, account_total_usd=11292.0)
ok("5.1% does not", small["over_concentration_limit"] is False)
ok("exactly 20% does not trip it - the rule is OVER twenty",
   W.assess("X", 1, 1.0, 200.0, 1.2, 5.0, account_total_usd=1000.0)["over_concentration_limit"] is False)

print("\nthe summary surfaces coverage, not just alerts")

rows = [
    W.assess("ZEC", 1.83, 70.0, 2861.0, 100.0, 20.0, account_total_usd=11292.0),
    W.assess("BTC", 1, 99.0, 1468.0, 100.0, 4.0, account_total_usd=11292.0),
    W.assess("RNDR", 9.87, None, None, None, None, account_total_usd=11292.0),
    W.assess("AERGO", 137, 0.0096, 1.32, 10.0, 5.0, account_total_usd=11292.0),
]
s = W.summarise(rows, cash_usd=79.36)
ok("four assets", s["assets"] == 4)
ok("one breached", len(s["breached"]) == 1)
ok("and its dollars are named", s["breached_usd"] == 2861.0, s["breached_usd"])
ok("one unpriced, counted separately", s["unpriced_assets"] == 1)
ok("dust is separated from real exposure", s["dust_usd"] == 1.32)
ok("coverage says what fraction actually HAS a level",
   s["covered_assets"] == 2, s["covered_assets"])
ok("as a share of the coin value",
   abs(s["covered_share_pct"] - round(100 * (2861.0 + 1468.0) / s["coin_usd"], 2)) < 0.01,
   s["covered_share_pct"])
ok("cash is carried through", s["cash_usd"] == 79.36)
ok("breached rows are biggest-first",
   all(s["breached"][i]["usd"] >= s["breached"][i + 1]["usd"]
       for i in range(len(s["breached"]) - 1)))
ok("every row is still in the output, whatever its status",
   len(s["rows"]) == 4)

print("\nthe sizing is the fleet's, not a second one")

import adaptive_stop
for vol in (1.0, 4.0, 7.4, 20.0):
    mine = W.assess("X", 1, 100.0, 500.0, 100.0, vol)["stop_pct"]
    theirs = adaptive_stop.scaled_stop(vol)
    ok(f"  vol {vol}% -> same stop as the live branches ({theirs})", mine == theirs)

print("\nthe endpoint is a GET and writes nothing")

import ast
DASH = open("routers/trading_dashboard.py", encoding="utf-8").read()
_t = ast.parse(DASH)
_fn = next((n for n in ast.walk(_t)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "get_holdings_watch"), None)
ok("the endpoint exists", _fn is not None)
_src = ast.get_source_segment(DASH, _fn) or ""
ok("it is a GET, so it works with the write token unset",
   any("router.get" in ast.unparse(d) for d in _fn.decorator_list))
ok("it commits nothing", "commit" not in _src)
ok("it places no order", "orders" not in _src and "post(" not in _src)
ok("it says it is not a stop order",
   "not a stop-loss order" in _src.lower(), _src[:0])
ok("unpriced assets are fed in too, not skipped",
   'census.get("unpriced")' in _src,
   "dropping them is exactly how the census reported $79.30")
ok("history is only fetched for positions worth acting on",
   "MIN_EXITABLE_USD" in _src,
   "42 assets x 2 calls rate-limits, and a rate limit gets written down as 'no data'")

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
