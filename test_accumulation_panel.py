"""The accumulation panel's arithmetic, checked against hand-computed answers.

A long-only spot grid cannot profit from a fall; it accumulates. The page
showed the per-slice entries and one combined unrealized figure, and left out
the two numbers that decide whether accumulating is working: the BLENDED
average the position really breaks even against, and what the next level does
to it.

Three ways that panel could lie, all checked here:

  1. A SIMPLE MEAN of the entries instead of a quantity-weighted one. NEAR
     holds $6.92 at $4.9698 and $69.23 at $4.8447 - ten to one. The simple
     mean is $4.9073, the real blended is $4.8558, and the difference is
     1.06% of a position whose whole round trip earns 1.80%.
  2. BREAK-EVEN AT THE BLENDED ENTRY. Both legs pay a fee, so the exit has
     to clear the basis grossed up by the round trip. Quoting the entry as
     break-even understates what a bounce must reach.
  3. SHOWING ONLY THE UPSIDE OF AVERAGING DOWN. The next level lowers the
     average AND raises the money at risk. NEAR's third would take one coin
     from $76 to $145, a quarter of the fleet. A panel that printed the
     first number without the second would be arguing for the trade rather
     than describing it.

Needs node on PATH. Skips cleanly (exit 0) if node is absent.
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HTML = open(os.path.join(HERE, "family_tree_dashboard.html"), encoding="utf-8").read()

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


if not shutil.which("node"):
    print("node not on PATH - skipping")
    sys.exit(0)


def extract(src, start):
    i = src.index('{', start); depth = 0; instr = None; j = i
    while j < len(src):
        c = src[j]
        if instr:
            if c == '\\': j += 2; continue
            if c == instr: instr = None
        elif c in '"\'`': instr = c
        elif c == '/' and src[j + 1] == '/': j = src.index('\n', j); continue
        elif c == '{': depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0: break
        j += 1
    return src[start:j + 1]


print("\nthe source prices break-even against the LIVE rate, not a constant")
ok("the fee rate is read from the payload",
   "data.effective_round_trip_fee_rate" in HTML and "data.real_round_trip_fee_rate" in HTML)
ok("and defaults to the TAKER round trip when it is missing",
   "let gridRoundTripFeeRate = 0.015;" in HTML,
   "a missing payload must overstate the hurdle, never understate it")
ok("maker-only is what licenses the cheaper rate",
   "data.maker_only_active &&" in HTML)

fn = extract(HTML, HTML.index("function renderGridBranchChart"))
# NEAR as it stands live: two slices, ten to one by size.
branch = {
    "product_id": "NEAR-USD", "current_price": 4.8401, "reference_price": 4.8447,
    "grid_pct": 0.025, "num_levels": 3, "allocated_usd": 207.69,
    "total_unrealized_net_usd": -0.78, "total_unrealized_net_pct": -0.0102,
    "slices": [
        {"entry_price": 4.9698, "qty": 1.392, "unrealized_net_usd": -0.19},
        {"entry_price": 4.8447, "qty": 14.289, "unrealized_net_usd": -0.59},
    ],
}
js = f"""
let gridRoundTripFeeRate = 0.007;
globalThis.fmtUsd = n => {{ const x=Number(n); const y=Math.abs(x)<0.005?0:x;
  return (y<0?'-$':'$')+Math.abs(y).toFixed(2); }};
{fn}
const b = {json.dumps(branch)};
const full = JSON.parse(JSON.stringify(b)); full.num_levels = 2;
console.log(JSON.stringify({{
  two: renderGridBranchChart(b).replace(/<[^>]+>/g,' ').replace(/\\s+/g,' '),
  held: renderGridBranchChart(full).replace(/<[^>]+>/g,' ').replace(/\\s+/g,' '),
}}));
"""
p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
if p.returncode != 0:
    print("  FAIL  the renderer threw:", p.stderr.strip()[:400])
    sys.exit(1)
res = json.loads(p.stdout.strip().splitlines()[-1])
two, held = res["two"], res["held"]

print("\nthe blended average is quantity-weighted")
# 76.1456 / 15.681 = 4.85579...  A simple mean would be 4.90725.
ok("it is the real blended entry, $4.86", "blended $4.86" in two, two[:300])
ok("and NOT the simple mean of the two entries, $4.91",
   "blended $4.91" not in two,
   "$6.92 and $69.23 are not equal weights and must not be averaged as if they were")

print("\nbreak-even sits above the blended entry, by the round trip")
# 4.85579 * 1.0035/0.9965 = 4.88990
ok("break-even is $4.89, not the blended $4.86", "break-even $4.89" in two, two[:300])
ok("and it is quoted as a distance from the CURRENT price",
   "from here" in two and "+1.03%" in two, two[:300])
ok("break-even is strictly above blended",
   two.index("break-even") > two.index("blended"))

print("\nthe next level shows what it costs as well as what it fixes")
ok("it names the trigger price", "$4.72" in two, two[:400])
ok("and how far away it is", "-2.41%" in two or "(-2.41" in two, two[:400])
ok("it shows the average being pulled down", "pulls blended to $4.79" in two, two[:400])
ok("AND the position being raised - never one without the other",
   "$145.37" in two and "$76.14" in two,
   "showing only the lowered average would be advocacy, not description")

print("\na branch with every level held says so")
ok("it states there is no further averaging down",
   "no further averaging down" in held, held[:300])
ok("and that the only move left is the exit",
   "only exits" in held and "$4.89" in held, held[:300])
ok("it does not offer a next level that cannot fire",
   "Next level at" not in held)

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
