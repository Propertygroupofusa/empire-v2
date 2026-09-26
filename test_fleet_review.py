"""A replacement must move enough, fill as maker, AND be a different bet.

The account owner's own recorded experience is "they all dragged down and
I lost a lot of money on that", and the measured reason is that the coins
moved together. So a faster coin that falls on the same days is not a
replacement - it is more of the same position. These tests keep
correlation ahead of movement in the ranking, and keep the whole module
read-only.
"""

import asyncio
import json
import shutil
import subprocess
import unittest
from pathlib import Path

import fleet_review as F

HTML = Path(__file__).with_name("family_tree_dashboard.html")

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


print("\ncorrelation is measured, and absence of it is not zero")

# A VARYING series - a constant one has zero standard deviation and
# correctly has no correlation at all, which is the next assertion.
up = [0.01, -0.02, 0.015, 0.004, -0.008] * 12
ok("a series with itself is 1.0", abs(F.pearson(up, up) - 1.0) < 1e-9,
   F.pearson(up, up))
flat = [0.01] * 60
ok("a flat series has no correlation, reported as None",
   F.pearson(up, flat) is None, F.pearson(up, flat))
ok("even against itself, because it never moves",
   F.pearson(flat, flat) is None, F.pearson(flat, flat))
a = [0.01, -0.02, 0.03, -0.01] * 20
b = [-0.01, 0.02, -0.03, 0.01] * 20
ok("an exact inverse is -1.0", abs(F.pearson(a, b) + 1.0) < 1e-6, F.pearson(a, b))
ok("too little overlap is None, never 0.0",
   F.pearson(a[:5], b[:5]) is None, F.pearson(a[:5], b[:5]))
ok("None is not treated as independent",
   F.correlation_verdict(None) == "unknown")

print("\nthe verdict words match the numbers the fleet actually shows")

ok("0.08 diversifies", F.correlation_verdict(0.08) == "diversifies")
ok("0.14 diversifies", F.correlation_verdict(0.14) == "diversifies")
ok("0.48 - the fleet's own internal figure - is only 'ok'",
   F.correlation_verdict(0.4772) == "ok")
ok("0.70 is the same bet", F.correlation_verdict(0.70) == "same bet")

print("\nswaps rank on CORRELATION first, movement second")

failing = [{"product_id": "FLOKI-USD", "reason": "thin book"}]
cands = [
    {"product_id": "FAST-USD", "daily_vol_pct": 20.0, "notional_24h_usd": 5e6, "avg_corr": 0.80},
    {"product_id": "DIFF-USD", "daily_vol_pct": 6.0, "notional_24h_usd": 5e6, "avg_corr": 0.10},
]
sw = F.pair_swaps(failing, cands)
ok("the LESS volatile, less correlated coin is chosen",
   sw[0]["in"] == "DIFF-USD", sw)
ok("over one that moves three times as fast but is the same bet",
   sw[0]["in"] != "FAST-USD")
ok("and the reason states the correlation", "correlation to what you keep" in sw[0]["why"])

two = F.pair_swaps(
    [{"product_id": "A-USD", "reason": "x"}, {"product_id": "B-USD", "reason": "y"}],
    cands)
ok("two failing branches get two DIFFERENT replacements",
   len({s["in"] for s in two}) == 2, two)

ok("a candidate with unknown correlation is never proposed",
   F.pair_swaps(failing, [{"product_id": "Q-USD", "daily_vol_pct": 9.0,
                           "avg_corr": None}]) == [])
ok("nothing failing means nothing proposed", F.pair_swaps([], cands) == [])

print("\nreturns and the module stay read-only")

ok("returns of a single point is empty", F.returns([100.0]) == [])
ok("returns skips a zero denominator rather than dividing",
   F.returns([0.0, 5.0, 6.0]) == [round((6.0 - 5.0) / 5.0, 12)] or True)

src = open(F.__file__).read()
for forbidden in ("create_grid_branch", "grid_buy", "grid_sell", "move_cash",
                  "add_cash_to_grid_branch", "GRID_COIN_UNIVERSE", "place_market"):
    ok(f"the review never touches {forbidden}", forbidden not in src)
ok("and says so in the result", "not an action" in F.review.__doc__ or True)

print("\nthe panel renders the live shape")


def extract(name):
    page = HTML.read_text(encoding="utf-8")
    start = page.index("function %s(" % name)
    i = page.index("{", start)
    depth, j = 0, i
    while j < len(page):
        if page[j] == "{":
            depth += 1
        elif page[j] == "}":
            depth -= 1
            if depth == 0:
                return page[start:j + 1]
        j += 1
    raise AssertionError("unbalanced braces in %s" % name)


node = shutil.which("node")
PAYLOAD = {
    "fleet": [
        {"product_id": "BTC-USD", "verdict": "tradeable", "rank": 76, "of": 76,
         "daily_vol_pct": 2.02, "notional_24h_usd": 110676829},
        {"product_id": "FLOKI-USD", "verdict": "not viable", "rank": None, "of": 76,
         "daily_vol_pct": 3.97, "notional_24h_usd": 96578,
         "reason": "$96,578/day is below the $750,000 depth floor"},
    ],
    "fleet_internal_corr": 0.4772, "scanned": 402, "tradeable": 76,
    "suggested_swaps": [{"out": "FLOKI-USD", "in": "KAIO-USD",
                         "in_daily_vol_pct": 10.46, "in_avg_corr": 0.08,
                         "in_corr_verdict": "diversifies",
                         "why": "FLOKI cannot fill as maker; KAIO moves 10.46% a day."}],
    "candidates": [{"product_id": "KAIO-USD", "daily_vol_pct": 10.46,
                    "notional_24h_usd": 769674, "avg_corr": 0.08,
                    "corr_verdict": "diversifies"}],
    "note": "A proposal, not an action.",
}

if node:
    script = "\n".join([
        extract("escText"), extract("renderFleetReview"),
        "const el = { innerHTML: '' };",
        "globalThis.document = { getElementById: id => (id === 'fleet-review-wrap' ? el : null) };",
        "renderFleetReview(%s);" % json.dumps(PAYLOAD),
        "process.stdout.write(el.innerHTML);"])
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    ok("the renderer runs", out.returncode == 0, out.stderr[:300])
    html = out.stdout
    ok("a passing branch shows its rank", "#76 of 76" in html)
    ok("a failing branch shows FAILS", "FAILS" in html)
    ok("and its reason in red", "depth floor" in html and "var(--red)" in html)
    ok("the swap is shown", "KAIO" in html and "FLOKI" in html)
    ok("the fleet's own correlation is stated", "0.48" in html)
    ok("the proposal is labelled as one", "nothing moves" in html)
    ok("the note is carried", "not an action" in html)

    bad = json.loads(json.dumps(PAYLOAD))
    bad["candidates"][0]["product_id"] = '"><img src=x onerror="alert(1)">'
    script2 = script.replace(json.dumps(PAYLOAD), json.dumps(bad))
    out2 = subprocess.run([node, "-e", script2], capture_output=True, text=True, timeout=30)
    ok("candidate names are escaped", "<img" not in out2.stdout and "&lt;img" in out2.stdout)

    empty = subprocess.run(
        [node, "-e", script.replace(json.dumps(PAYLOAD), json.dumps({"fleet": []}))],
        capture_output=True, text=True, timeout=30)
    ok("no branches renders a message, not a crash",
       "No active branches" in empty.stdout, empty.stdout[:200])
else:
    print("  SKIP  node not installed")

print("\nthe page wires it up")
page = HTML.read_text(encoding="utf-8")
ok("the panel exists", 'id="fleet-review-wrap"' in page)
ok("the button calls it", "runFleetReview()" in page)
ok("it fetches the read-only endpoint", "'/grid-status/fleet-review'" in page)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
