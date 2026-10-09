"""A tape that lets you read market volume as your own activity is worse than none.

The account owner saw Kalshi's floating +$19 / +$24 prints and asked for the
same thing on his coins. The data is there - Coinbase publishes every fill
on the venue - but the prints are OTHER PEOPLE'S trades, and this account is
not currently placing orders. A panel that blurs that turns "the market is
busy" into "I am making money", which is the single worst thing this
dashboard could imply.
"""
import json
import re
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
# THIS TEST ASSERTED A FALSE CLAIM AND SO PROTECTED IT. The note said "this
# account is not currently placing orders", and /fills-by-source reads
# Coinbase's own account-level record: 36 orders and 43 fills in 24 hours,
# $1,812.93 of notional, every one maker. The account trades. What was true
# is that nobody had built a tape of its OWN fills - a different sentence.
ok("it does NOT claim the account places no orders - it does place them",
   "not currently placing orders" not in s["note"], s["note"])
ok("and it points at where the owner's own fills really are",
   "fills-by-source" in s["note"], s["note"])
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

# ASSERTED ON THE RULE, NOT THE NAME. These matched "@keyframes tape-float"
# and ".tape-print { animation: none", and both broke on changes that were
# improvements: the animation was renamed tape-lane, and reduced motion now
# swaps to a tape-lane-still variant instead of `animation: none`. The swap
# is the better behaviour - a print still has to clear the lane for the next
# one, and `none` would leave them stacked on screen forever - so the test
# follows the code rather than the other way round.
#
# The property is what matters: a print is animated, and under
# prefers-reduced-motion whatever animation it gets MOVES NOTHING.
_print_anim = re.search(r"\.tape-print\s*\{[^}]*animation:\s*([\w-]+)", HTML)
ok("there is a tape-print animation", _print_anim is not None,
   "nothing animates a print, so nothing clears the lane")

def _block(text, start):
    """The balanced {...} body beginning at the first brace at/after start.

    Brace-matched rather than regex-windowed. A `.*?` to a guessed indent is
    the same fixed-window mistake that broke five checks in test_invariants.py
    - CSS nests, and the page has more than one reduced-motion block.
    """
    i = text.find("{", start)
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    return ""


# Every reduced-motion block on the page, because there is more than one and
# only the tape's is relevant here.
_reduced_anim = None
for _m in re.finditer(r"@media\s*\(prefers-reduced-motion:\s*reduce\)", HTML):
    _body = _block(HTML, _m.end())
    _hit = re.search(r"\.tape-print\s*\{[^}]*animation:\s*([\w-]+)", _body)
    if _hit:
        _reduced_anim = _hit
        break
ok("and prefers-reduced-motion overrides it", _reduced_anim is not None,
   "the reduced-motion preference is not honoured for the tape")

if _reduced_anim:
    _name = _reduced_anim.group(1)
    if _name == "none":
        ok("and that override moves nothing", True)
    else:
        _kf = re.search(r"@keyframes\s+" + re.escape(_name) + r"\s*\{(.*?)\n\s*\}",
                        HTML, re.S)
        ok(f"the reduced-motion keyframes ({_name}) exist", _kf is not None)
        # Compared in Python, not with a lookahead: `\s*(?!none)` backtracks
        # to zero spaces and then happily matches " none".
        _moves = [t.strip() for t in
                  (re.findall(r"transform:\s*([^;}]+)", _kf.group(1)) if _kf else [])
                  if t.strip() not in ("none", "")]
        ok("and they move nothing - only opacity changes",
           not _moves, f"reduced motion still animates {_moves}")
        ok("while the ordinary animation DOES move, so the two really differ",
           _name != _print_anim.group(1), f"both are {_name}")

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
