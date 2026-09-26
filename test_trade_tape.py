"""A tape that lets you read market volume as your own activity is worse than none.

The account owner saw Kalshi's floating +$19 / +$24 prints and asked for the
same thing on his coins. The data is there - Coinbase publishes every fill
on the venue - but the prints are OTHER PEOPLE'S trades, and this account is
not currently placing orders. A panel that blurs that turns "the market is
busy" into "I am making money", which is the single worst thing this
dashboard could imply.
"""
import json
from pathlib import Path

import trade_tape as T

HTML = Path(__file__).with_name("family_tree_dashboard.html").read_text(encoding="utf-8")

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def raw(size="1", price="100", side="buy", tid=1, t="2026-09-26T19:42:27Z"):
    return {"size": size, "price": price, "side": side, "trade_id": tid, "time": t}


print("\nIT NEVER IMPLIES A PRINT IS YOURS")

s = T.summarise(T.normalise("ZEC-USD", [raw()]))
ok("the summary says these are other people's trades",
   "OTHER PEOPLE'S trades" in s["note"], s["note"])
ok("and that this account is not placing orders",
   "not currently placing orders" in s["note"])
ok("the panel says it too", "other people's trades, not yours" in HTML)
ok("and explains why a tape of your own fills would be empty",
   "empty box" in HTML)

print("\nCOINBASE'S SIDE IS THE MAKER'S; the tape shows the AGGRESSOR")

ok("a maker 'buy' prints as a taker SELL",
   T.normalise("X-USD", [raw(side="buy")])[0]["side"] == "sell",
   "the resting order was a buy, so the aggressor sold into it")
ok("a maker 'sell' prints as a taker BUY",
   T.normalise("X-USD", [raw(side="sell")])[0]["side"] == "buy")
ok("an unknown side stays None, not guessed",
   T.normalise("X-USD", [raw(side="")])[0]["side"] is None)
ok("and the panel renders that as UNKNOWN", "'UNKNOWN'" in HTML)

print("\ndust is filtered, real prints are not")

ok("a $0.50 print is dropped",
   T.normalise("X-USD", [raw(size="0.005", price="100")]) == [])
ok("a $1.00 print is kept",
   len(T.normalise("X-USD", [raw(size="0.01", price="100")])) == 1)
ok("the floor is a stated constant", T.MIN_PRINT_USD == 1.0)
ok("a $310 print keeps its size, not an average",
   T.normalise("X-USD", [raw(size="0.2", price="1554.27")])[0]["usd"] == 310.85)

print("\nbad rows are skipped, never crash, never become zero")

ok("a zero size is dropped", T.normalise("X-USD", [raw(size="0")]) == [])
ok("a zero price is dropped", T.normalise("X-USD", [raw(price="0")]) == [])
ok("a negative size is dropped", T.normalise("X-USD", [raw(size="-1")]) == [])
ok("garbage is dropped", T.normalise("X-USD", [raw(size="x", price="y")]) == [])
ok("a non-dict row is skipped", T.normalise("X-USD", ["nope", None, 7]) == [])
ok("None input gives an empty tape", T.normalise("X-USD", None) == [])

print("\nDEDUPE IS PER PRODUCT - trade_id is not globally unique")

a = T.normalise("ZEC-USD", [raw(tid=7)])
b = T.normalise("BTC-USD", [raw(tid=7)])
ok("two coins sharing an id both survive", len(T.merge([a, b])) == 2,
   "a global id set silently drops one of them")
ok("the same print twice collapses to one", len(T.merge([a, a])) == 1)
ok("rows with no id are not collapsed together",
   len(T.merge([T.normalise("X-USD", [raw(tid=None), raw(tid=None, size="2")])])) == 2)

print("\nnewest first, and bounded")

rows = T.merge([T.normalise("X-USD", [
    raw(tid=1, t="2026-09-26T19:00:00Z"),
    raw(tid=2, t="2026-09-26T19:42:00Z"),
    raw(tid=3, t="2026-09-26T19:20:00Z")])])
ok("sorted newest first", [r["trade_id"] for r in rows] == [2, 3, 1],
   [r["trade_id"] for r in rows])
big = T.merge([T.normalise("X-USD", [raw(tid=i) for i in range(200)])])
ok(f"capped at {T.MAX_ROWS} rows", len(big) == T.MAX_ROWS)

print("\nthe summary counts both sides and names the busiest coin")

mixed = T.normalise("ZEC-USD", [raw(tid=1, side="sell", size="1", price="100")]) \
      + T.normalise("BTC-USD", [raw(tid=2, side="buy", size="3", price="100")])
s = T.summarise(mixed)
ok("one buy, one sell", s["buy_prints"] == 1 and s["sell_prints"] == 1)
ok("buy dollars are the taker buys", s["buy_usd"] == 100.0, s["buy_usd"])
ok("sell dollars are the taker sells", s["sell_usd"] == 300.0, s["sell_usd"])
ok("net is buys minus sells", s["net_usd"] == -200.0)
ok("the busiest coin is by dollars, not count", s["busiest"] == "BTC", s["busiest"])
ok("an empty tape summarises without raising",
   T.summarise([])["prints"] == 0 and T.summarise(None)["busiest"] is None)

print("\nthe panel only floats NEW prints")

ok("it tracks what it has already shown", "_tapeSeen" in HTML)
ok("and filters the poll against it", "_tapeSeen.has(k)" in HTML)
ok("the key is product + trade id", "x.product_id + ':' + x.trade_id" in HTML)
ok("the seen-set is bounded, so a long-open tab cannot grow forever",
   "_tapeSeen.size > 4000" in HTML)
ok("floated nodes are removed after the animation",
   "setTimeout(() => el.remove()" in HTML)
ok("prints appear oldest-first within a batch",
   ".reverse().forEach" in HTML,
   "so they arrive in the order they actually happened")

print("\nmotion is optional")

ok("there is a float animation", "@keyframes tape-float" in HTML)
ok("and it is disabled under prefers-reduced-motion",
   "prefers-reduced-motion: reduce" in HTML and ".tape-print { animation: none" in HTML)

print("\nan error says so instead of showing a stale window")

ok("a failed poll reports it", "Tape unavailable" in HTML)
ok("and refuses to pass stale data off as live",
   "stale window as if it were live" in HTML)
ok("a quiet tape is called quiet, not broken",
   "That is quiet, not broken" in HTML)
ok("a per-coin fetch failure is surfaced by name",
   "no data for" in HTML,
   "a rate limit written down as 'no trades' is a mistake already made once")

print("\nthe endpoint is read-only and public-data")

DASH = Path(__file__).with_name("routers").joinpath("trading_dashboard.py").read_text(encoding="utf-8")
i = DASH.index('@router.get("/trade-tape")')
ep = DASH[i:DASH.index("@router.get", i + 10)]
ok("it is a GET", '@router.get("/trade-tape")' in DASH)
ok("it writes nothing", "commit" not in ep)
ok("it uses the public trades endpoint", "api.exchange.coinbase.com" in ep and "/trades" in ep)
ok("a non-200 per coin is recorded, not treated as no trades",
   "errors[a] = f\"HTTP {r.status}\"" in ep)
ok("it flags the payload as market flow", "is_market_flow_not_yours" in ep)
ok("the default set is the biggest holdings",
   "key=lambda h: -(h.get(\"usd\") or 0)" in ep)

print("\nthe panel names the thing")

ok("it says what the display is called",
   "trade tape" in HTML and "time &amp; sales" in HTML)
ok("and credits where the owner saw it", "Kalshi" in HTML)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
