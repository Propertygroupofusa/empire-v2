"""A newsroom that reports numbers nobody measured is a propaganda outlet.

The whole value of this reframe is that it makes the real figures watchable.
The moment one desk shows something invented, the screen becomes worse than
the spreadsheet it replaced - because it reads with the authority of a
broadcast.

These tests exist to keep every desk tied to a measurement.
"""
import ast
import json

import newsroom_brief as N

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def row(asset="BTC", usd=1000.0, status="OK", to_stop=5.0, stop_pct=0.10,
        vol=2.0, share=9.0, peak=100.0):
    return {"asset": asset, "usd": usd, "status": status, "pct_to_stop": to_stop,
            "stop_pct": stop_pct, "daily_vol_pct": vol, "stop_level": 90.0,
            "share_of_account_pct": share, "pct_from_peak": -1.0,
            "units": 1.0, "price": 95.0}


WATCH = {
    "coin_usd": 11237.01, "cash_usd": 79.36, "covered_share_pct": 99.47,
    "dust_usd": 30.27, "unpriced_assets": 1,
    "disclaimer": "These are ALERT LEVELS, not stop-loss orders.",
    "breached": [dict(row("PEPE", 89.02, "BREACHED", -4.47, 0.1407, 12.0, 0.8),
                      pct_from_peak=-17.9)],
    "near": [row("XRP", 2393.0, "NEAR_STOP", 1.63, 0.089, 3.56, 21.14)],
    "over_concentration": [row("ZEC", 2859.5, "OK", 10.49, 0.1564, 6.2, 25.27)],
    "unpriced": [{"asset": "RNDR", "units": 9.87}],
    "rows": [row("ZEC", 2859.5, "OK", 10.49, 0.1564, 6.2, 25.27),
             row("XRP", 2393.0, "NEAR_STOP", 1.63, 0.089, 3.56, 21.14),
             dict(row("PEPE", 89.02, "BREACHED", -4.47, 0.1407, 12.0, 0.8),
                  pct_from_peak=-17.9),
             row("AERGO", 1.32, "BELOW_MIN_TRADE", None, None, None, 0.01)],
}

print("\nTODAY'S P&L IS NOT INVENTED")

b = N.build(WATCH)
ok("daily P&L is null, not a number", b["capital"]["daily_pnl_usd"] is None)
ok("and it says WHY it is null",
   "nobody measured" in b["capital"]["daily_pnl_note"], b["capital"]["daily_pnl_note"])
ok("the page renders it as NOT REPORTED, not as $0",
   "NOT REPORTED" in open("newsroom.html", encoding="utf-8").read())

print("\nthe book total is the CENSUS total, not a target")

ok("total is coin + cash", b["capital"]["total_usd"] == round(11237.01 + 79.36, 2))
ok("coin is carried unchanged", b["capital"]["coin_usd"] == 11237.01)
ok("cash is carried unchanged", b["capital"]["cash_usd"] == 79.36)

print("\nthe allocation is the REAL one, including what breaks the rule")

alloc = {a["asset"]: a["share_pct"] for a in b["capital"]["allocation"]}
ok("ZEC shows its real 25.27%", alloc.get("ZEC") == 25.27, alloc)
ok("XRP shows its real 21.14%", alloc.get("XRP") == 21.14, alloc)
ok("no aspirational 50/25/10/10/5 anywhere",
   "50% BTC" not in json.dumps(b) and "25% ETH" not in json.dumps(b))

print("\nthe quant score is decomposable, or it is absent")

q = N.quant_score(row(to_stop=15.0, stop_pct=0.10, vol=1.0, share=5.0))
ok("a strong position scores high", q["score"] >= 80, q["score"])
ok("every component is returned with it",
   set(q["components"]) == {"headroom", "steadiness", "weight_ok"}, q["components"])
ok("and the formula is stated in words", "0.55 x headroom" in q["basis"])
ok("no sentiment or price targets are claimed",
   "No sentiment" in q["basis"] and "unvalidated model" in q["basis"])
ok("missing volatility gives NO SCORE, not a default 50",
   N.quant_score(row(vol=None)) is None,
   "a defaulted score is a horoscope with a number on it")
ok("missing stop distance gives no score", N.quant_score(row(to_stop=None)) is None)
ok("a zero stop distance cannot divide by zero",
   N.quant_score(row(stop_pct=0)) is None)
ok("breaking the 20% rule costs score",
   N.quant_score(row(share=30.0))["score"] < N.quant_score(row(share=5.0))["score"])
ok("a position AT its stop scores its headroom at zero",
   N.quant_score(row(to_stop=0.0))["components"]["headroom"] == 0.0)

print("\nproducer notes rank by DOLLARS, not by drama")

notes = N.producer_notes(WATCH)
usds = [n["usd"] for n in notes]
ok("sorted biggest-first", usds == sorted(usds, reverse=True), usds)
ok("a 43% collapse worth $6 does not lead",
   notes[0]["usd"] > N.MIN_STORY_USD)
tiny = dict(WATCH, breached=[dict(row("ALEO", 5.97, "BREACHED", -24.7, 0.25, 30.0, 0.05),
                                  pct_from_peak=-43.6)], near=[], over_concentration=[],
            unpriced=[])
ok("a sub-$25 breach is filtered out of the notes entirely",
   not any(n["kind"] == "BREACH" for n in N.producer_notes(tiny)),
   "true, and still not news")

print("\nthe unpriced are reported as UNKNOWN, never as zero")

kinds = {n["kind"] for n in notes}
ok("a BLIND story exists for unpriced assets", "BLIND" in kinds)
blind = next(n for n in notes if n["kind"] == "BLIND")
ok("it says nothing can be said about them",
   "Nothing can be said" in blind["detail"], blind["detail"])
lvl, action = N.risk_level({"status": "UNPRICED"})
ok("the risk desk calls it UNKNOWN", lvl == "UNKNOWN")
ok("and explicitly not small", "not a small position" in action, action)

print("\nthe ticker only runs measured facts")

t = N.breaking(WATCH)
ok("every line is a string", all(isinstance(x, str) for x in t))
ok("the breach is on the ticker", any("PEPE" in x and "BREAKS LEVEL" in x for x in t))
ok("the concentration rule is on the ticker", any("CONCENTRATION" in x for x in t))
ok("coverage is reported", any("COVERAGE" in x for x in t))
quiet = N.breaking({"breached": [], "near": [], "over_concentration": [],
                    "covered_share_pct": None})
ok("an empty tape says so rather than padding",
   len(quiet) == 1 and "Quiet tape" in quiet[0], quiet)

print("\nthe script is checkable sentence by sentence")

s = N.build(WATCH)["script"]
joined = " ".join(s)
ok("it opens with the anchor's name", "Delfine" in s[0])
ok("it states the real book total", "11,316.37" in joined, joined[:200])
ok("it reports coverage", "99.5 percent" in joined)
ok("it names the concentration breach", "ZEC at 25.3 percent" in joined)
ok("it corrects itself on air about the unpriced",
   "cannot be priced" in joined and "not going to guess" in joined)
ok("it repeats the owner's own rule about compounding",
   "Compounding comes after the profit is made" in joined)
ok("a custom anchor is honoured", N.build(WATCH, anchor="Ace")["script"][0].count("Ace") == 1)

print("\nthe page fails loudly, never quietly")

HTML = open("newsroom.html", encoding="utf-8").read()
ok("a failed fetch shows a FEED DOWN banner", "FEED DOWN" in HTML)
ok("and says the numbers are not updating",
   "Nothing below is being updated" in HTML)
ok("rather than showing stale numbers as live",
   "stale numbers dressed" in HTML)
ok("the on-air light flips to FEED DOWN", 'textContent = "FEED DOWN"' in HTML)

print("\nthe page escapes everything it prints")

ok("there is an escaper", "const esc =" in HTML)
ok("asset names go through it", "esc(r.asset)" in HTML)
ok("ticker lines go through it", "esc(lines)" in HTML)
ok("script lines go through it", "esc(l)" in HTML)
ok("the story line uses textContent, not innerHTML",
   '$("story").textContent' in HTML)

print("\nthe broadcast cycle is the one asked for")

ok("15 minutes", "15 * 60 * 1000" in HTML)
ok("it is a real interval, not a one-shot", "setInterval(load" in HTML)

print("\nthe endpoint is read-only and built on the watch")

DASH = open("routers/trading_dashboard.py", encoding="utf-8").read()
fn = next((n for n in ast.walk(ast.parse(DASH))
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == "get_newsroom"), None)
ok("the endpoint exists", fn is not None)
src = ast.get_source_segment(DASH, fn) or ""
ok("it is a GET", any("router.get" in ast.unparse(d) for d in fn.decorator_list))
ok("it reuses the holdings watch rather than refetching its own data",
   "get_holdings_watch(" in src)
ok("it writes nothing", "commit" not in src and "post(" not in src)

print("\nthe disclaimer survives all the way to the screen")

ok("the brief carries it", "not stop-loss orders" in (b["disclaimer"] or ""))
ok("and the page renders it", 'id="disclaimer"' in HTML and '$("disclaimer")' in HTML)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
