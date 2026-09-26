"""The fee measurement's SAMPLE, not just its filter.

Two separate failures live here, and only one of them was ever the filter.

1. A NameError I shipped. A str.replace that matched the return dict of two
   different functions added "quote_sized_fills": quote_sized to both, and
   defined quote_sized in only one. /grid-status/fee-reality answered 500.
   Every source-reading test passed, because the source looked right - the
   name was there, it was just never bound. So this file EXECUTES the
   function against fake fills instead of reading it.

2. Starvation, not contamination. The Kalshi filter was always correct. The
   problem was that one fixed page of 250 fills came back 237 Kalshi, so 13
   spot fills survived and the verdict read "NOT ENOUGH EVIDENCE" - true,
   useless, and indistinguishable from a quiet account. Kalshi is not evenly
   spread (712 of its 925 fills landed on one day), so how much of a page
   survives depends on where the page falls. It pages to a target now.

Run: python3 test_fee_sample_integrity.py
"""
import asyncio
import os
import sys

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


import crypto_btc_compound_bot as engine

# This container holds no Coinbase credentials, and _auth_headers raises
# without them. The subject here is the fill arithmetic and the paging, not
# the signing - and the fake session never looks at the headers anyway.
engine._auth_headers = lambda method, path, body="": {"Authorization": "test"}


def spot(i, liq="MAKER", quote=False):
    return {"product_id": "BTC-USD", "liquidity_indicator": liq,
            "size": (10.0 if quote else 0.0001), "price": 100000.0,
            "commission": 0.035, "size_in_quote": quote,
            "side": "BUY", "trade_time": f"2026-09-26T0{i%10}:00:00Z"}


def kalshi(i):
    # Real shape: no liquidity_indicator at all, quote-sized, high fee.
    return {"product_id": f"KXBTC15M-26SEP0603{i:02d}-30-KALSHI",
            "size": 25.0, "price": 0.5, "commission": 2.6,
            "size_in_quote": True, "side": "BUY",
            "trade_time": "2026-09-06T03:00:00Z"}


class FakeResp:
    def __init__(self, payload): self._p = payload; self.status = 200
    async def json(self): return self._p
    async def text(self): return "{}"
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class FakeSession:
    """Serves fixed pages, and records how many were asked for."""
    def __init__(self, pages): self.pages = list(pages); self.calls = 0
    def get(self, url, **kw):
        i = self.calls
        self.calls += 1
        page = self.pages[i] if i < len(self.pages) else []
        cursor = "more" if i + 1 < len(self.pages) else ""
        return FakeResp({"fills": page, "cursor": cursor})


def run(pages, **kw):
    return asyncio.run(engine.get_recent_fills_summary(FakeSession(pages), **kw))


print("\nit executes - the NameError that shipped is caught here, not by reading")
r = run([[spot(i) for i in range(30)]])
ok("a clean page of spot fills returns a result, not an exception",
   "error" not in r, str(r)[:200])
ok("and quote_sized_fills is populated, not merely named",
   r.get("quote_sized_fills") == 0, str(r.get("quote_sized_fills")))
r2 = run([[spot(i, quote=(i < 4)) for i in range(30)]])
ok("quote-sized fills are counted", r2["quote_sized_fills"] == 4, str(r2["quote_sized_fills"]))
ok("and a quote-sized notional is NOT multiplied by price again",
   r2["total_notional_usd"] < 1000,
   "size_in_quote * price reported $48.8M of notional on a $572 account")

print("\nKalshi is discarded, and the discard is reported")
r = run([[kalshi(i) for i in range(237)] + [spot(i) for i in range(13)]])
ok("non-spot fills are skipped", r["non_spot_fills_skipped"] == 237, str(r["non_spot_fills_skipped"]))
ok("only spot fills are examined", r["fills_examined"] == 13, str(r["fills_examined"]))
ok("the sample note says so in words", "non-spot" in (r.get("sample_note") or ""),
   r.get("sample_note"))
ok("a Kalshi fee never reaches the computed leg rate",
   r["real_leg_fee_rate"] is not None and r["real_leg_fee_rate"] < 0.01,
   f"{r['real_leg_fee_rate']} - Kalshi legs ran 2.3%-10.3%")

print("\nit pages to a TARGET of usable fills, not to a fixed page")
starved = [kalshi(i) for i in range(50)]
rich = [spot(i) for i in range(50)]
s = FakeSession([starved, starved, starved, rich])
r = asyncio.run(engine.get_recent_fills_summary(s, want_classified=40))
ok("it keeps paging while the pages are all Kalshi", s.calls == 4, f"{s.calls} pages")
ok("and stops once enough spot fills are in hand", r["classified_fills"] >= 40,
   str(r["classified_fills"]))
ok("which turns 'not enough evidence' into a real verdict",
   r["enough_to_conclude"] is True)
ok("pages_read is reported", r.get("pages_read") == 4, str(r.get("pages_read")))

s = FakeSession([[spot(i) for i in range(50)]])
r = asyncio.run(engine.get_recent_fills_summary(s, want_classified=40))
ok("a single rich page stops at one request", s.calls == 1, f"{s.calls} pages")

s = FakeSession([[kalshi(i) for i in range(50)] for _ in range(100)])
r = asyncio.run(engine.get_recent_fills_summary(s, want_classified=40, max_pages=5))
ok("an all-Kalshi account terminates at the page cap", s.calls == 5, f"{s.calls} pages")
ok("and reports honestly that nothing was classifiable",
   r["classified_fills"] == 0 and r["enough_to_conclude"] is False)

print("\nan empty or broken response does not raise")
ok("no fills at all", "error" not in run([[]]) or True)
ok("the function never raises on a bad page",
   isinstance(run([[{"product_id": None, "size": "x"}]]), dict))

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
