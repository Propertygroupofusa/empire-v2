"""Measuring every coin is not proposing to trade every coin.

402 USD pairs are live on the venue; the fleet trades six. Widening what
is MEASURED is free. Widening what is TRADED is the thing the account
owner already lost money on: "last time I tried to do this and go and try
to pull all these different coins, they all dragged down."

These tests keep the two apart, and keep a failing coin from being
silently dropped.
"""

import asyncio

import universe_scan as U

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def row(pid, vol, notional):
    return {"product_id": pid, "daily_vol_pct": vol, "notional_24h_usd": notional,
            "bars": 180, "last_price": 1.0}


print("\na coin must clear BOTH bars - movement and depth")

r = U.assess(row("GOOD-USD", 5.0, 5_000_000))
ok("moves enough and deep enough is tradeable", r["verdict"] == "tradeable", r)
ok("and the reason states both figures",
   "a day" in r["reason"] and "depth" in r["reason"].lower() or "$" in r["reason"], r)

r = U.assess(row("THIN-USD", 5.0, 95_020))       # the live FLOKI case
ok("deep movement on a thin book is NOT viable", r["verdict"] == "not viable", r)
ok("and the reason names the depth floor", "depth floor" in r["reason"], r)
ok("and says why it matters - taker, not maker", "taker" in r["reason"], r)

r = U.assess(row("FLAT-USD", 0.40, 50_000_000))
ok("a deep book that barely moves is NOT viable", r["verdict"] == "not viable", r)
ok("and the reason compares the day's move to the fee",
   "round-trip fee" in r["reason"], r)

r = U.assess(row("BOTH-USD", 0.30, 100_000))
ok("failing both is reported with both reasons", r["reason"].count(";") == 1, r)

print("\nthe fee is the yardstick, and it is adjustable")

r = U.assess(row("EDGE-USD", 0.80, 5_000_000), fee_pct=0.70)
ok("0.80%/day clears a 0.70% fee", r["verdict"] == "tradeable", r)
r = U.assess(row("EDGE-USD", 0.80, 5_000_000), fee_pct=1.50)
ok("the same coin fails a 1.50% fee", r["verdict"] == "not viable", r)
ok("the multiple of fee is reported, not just a pass/fail",
   U.assess(row("X-USD", 7.0, 5_000_000))["daily_vol_over_fee"] == 10.0)

print("\na coin with no data is reported, never silently dropped")

r = U.assess({"product_id": "DEAD-USD", "skipped": "only 3 daily bars"})
ok("it gets a 'no data' verdict", r["verdict"] == "no data", r)
ok("and keeps the reason it failed", "3 daily bars" in r["reason"], r)
ok("it is NOT counted as tradeable", r["verdict"] != "tradeable")

print("\na rate limit is not a fact about the coin")

r = U.assess({"product_id": "ZEC-USD", "skipped": "candles HTTP 429", "retried": 4})
ok("429 reads as 'unmeasured', not 'no data'", r["verdict"] == "unmeasured", r)
ok("and the reason says it is about the venue, not the coin",
   "says nothing about the coin" in r["reason"], r)
ok("and says how many attempts were made", "4 attempts" in r["reason"], r)
for code in (500, 502, 503, 504):
    rr = U.assess({"product_id": "X-USD", "skipped": f"candles HTTP {code}", "retried": 4})
    ok(f"HTTP {code} is also 'unmeasured'", rr["verdict"] == "unmeasured", rr)

r = U.assess({"product_id": "DEAD-USD", "skipped": "only 3 daily bars"})
ok("a genuinely short history is still 'no data'", r["verdict"] == "no data", r)
ok("neither is ever counted as tradeable",
   U.assess({"product_id": "Z", "skipped": "candles HTTP 429"})["verdict"] != "tradeable")

ok("the retry list covers rate limits and 5xx",
   U.RETRYABLE_STATUS == {429, 500, 502, 503, 504}, U.RETRYABLE_STATUS)
ok("and it retries more than once", U.RETRY_ATTEMPTS >= 3, U.RETRY_ATTEMPTS)

src_fn = open(U.__file__).read()
ok("the fetch retries rather than returning on the first bad status",
   "RETRY_ATTEMPTS" in src_fn and "RETRY_BACKOFF_SECONDS" in src_fn)
ok("a NON-retryable status breaks out instead of burning four attempts",
   "if r.status not in RETRYABLE_STATUS:" in src_fn)
ok("the live ZEC/XRP incident is recorded where the code is",
   "ZEC and XRP" in src_fn)

print("\nthe scan separates the three outcomes and counts them")


def fake_session(products, candles):
    class Resp:
        def __init__(self, payload, status=200):
            self.payload, self.status = payload, status

        async def json(self):
            return self.payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class S:
        def get(self, url, params=None, timeout=None):
            if url.endswith("/products"):
                return Resp(products)
            pid = url.split("/products/")[1].split("/")[0]
            return Resp(candles.get(pid, []))
    return S()


# [time, low, high, open, close, volume], newest first.
def bars(n, price, vol_usd):
    import math
    out = []
    for i in range(n):
        p = price * (1 + 0.03 * math.sin(i))
        out.append([i, p * 0.99, p * 1.01, p, p, vol_usd / max(p, 1e-9)])
    return out


products = [
    {"id": "GOOD-USD", "quote_currency": "USD", "status": "online"},
    {"id": "THIN-USD", "quote_currency": "USD", "status": "online"},
    {"id": "DEAD-USD", "quote_currency": "USD", "status": "online"},
    {"id": "OFF-USD", "quote_currency": "USD", "status": "offline"},
    {"id": "BTC-EUR", "quote_currency": "EUR", "status": "online"},
    {"id": "GONE-USD", "quote_currency": "USD", "status": "online",
     "trading_disabled": True},
]
candles = {"GOOD-USD": bars(180, 10.0, 9_000_000),
           "THIN-USD": bars(180, 10.0, 10_000),
           "DEAD-USD": bars(3, 10.0, 9_000_000)}

res = asyncio.run(U.scan(session=fake_session(products, candles), concurrency=2))
ids = [r["product_id"] for r in res["rows"]]
ok("offline pairs are not scanned", "OFF-USD" not in ids, ids)
ok("non-USD pairs are not scanned", "BTC-EUR" not in ids, ids)
ok("trading-disabled pairs are not scanned", "GONE-USD" not in ids, ids)
ok("three real pairs are scanned", res["scanned"] == 3, res["scanned"])
ok("the deep mover is tradeable", res["tradeable"] == 1, res)
ok("the thin one is not viable", res["not_viable"] == 1, res)
ok("the one without history is no data", res["no_data"] == 1, res)
ok("tradeable coins sort first",
   res["rows"][0]["verdict"] == "tradeable", ids)

print("\nit says out loud that measuring is not proposing to trade")

ok("the result carries the warning", "not proposing to trade it" in res["note"])
ok("and points at the lock that still governs money",
   "coin_rotation.universe()" in res["note"])
ok("and states the correlation that makes more coins not more bets",
   "0.574" in res["note"])

print("\nit never widens the TRADING universe")

src = open(U.__file__).read()
for forbidden in ("GRID_COIN_UNIVERSE", "create_grid_branch", "move_cash",
                  "add_cash_to_grid_branch", "grid_buy"):
    ok(f"the scanner never touches {forbidden}", forbidden not in src)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
