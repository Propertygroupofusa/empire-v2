"""The cumulative realized curve must plot the GRID'S OWN closes, not the pool.

An adopted_exit closes a position the grid never opened. Its entry is the
adoption mark - a number nobody paid and no step chose - so the row says
nothing about what a round trip of this configuration earns. Four such ZEC
rows on 2026-10-04 booked -$311.24 inside eleven minutes.

The curve on the dashboard pooled them. It therefore drew the grid falling
off a cliff from +$135.58 to -$164.00 and captioned that figure "all-time
realized", on the same page whose ledger panel two blocks down reads
+$147.24 across 208 own round trips. The page contradicted itself, and the
half a reader's eye lands on was the one that was wrong.

get_realized_edge, /growth-model, /is-it-growing, loss_study.analyse and
capital_kpis.compute all already drop these rows. This curve was the last
place still pooling them.

Four ways the fix could still be wrong, each checked here:

  1. FILTERING THE ROWS BUT KEEPING THE POOLED ANCHOR. The curve starts at
     (total - window sum), so anchoring on total_realized_pnl would feed the
     inherited dollars back in through the offset even with their rows gone.
     The line would end on -$164.00 having plotted none of the trades that
     put it there - worse than before, because nothing on screen would
     explain the drop.
  2. DROPPING THEM SILENTLY. $311.24 of real cash moved. A curve that simply
     omits it is not more honest than one that pools it.
  3. REPORTING ONLY THE ADOPTION-MARK FIGURE. The same four closes are
     -$311.24 against the mark and +$297.78 against what was actually paid.
     Quoting one without the other picks a side.
  4. SHOWING THE INHERITED FOOTNOTE WHEN THERE IS NONE. Most fleets have no
     adopted branch; a permanent "not in this line: 0" is noise.

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


CURVE = extract(HTML, HTML.index("function renderGridPnlCurve"))
FMT_USD = extract(HTML, HTML.index("function fmtUsd"))
FMT_SIGNED = extract(HTML, HTML.index("function fmtSignedUsd"))


def render(history):
    js = """
%s
%s
globalThis.document = { getElementById: () => ({ set innerHTML(v) { globalThis.__out = v; },
                                                 get innerHTML() { return globalThis.__out; } }) };
%s
renderGridPnlCurve(%s);
console.log(JSON.stringify({ html: globalThis.__out,
                             text: String(globalThis.__out).replace(/<[^>]+>/g, ' ')
                                     .replace(/\\s+/g, ' ').trim() }));
""" % (FMT_USD, FMT_SIGNED, CURVE, json.dumps(history))
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        print("  FAIL  the renderer threw:", p.stderr.strip()[:500])
        sys.exit(1)
    return json.loads(p.stdout.strip().splitlines()[-1])


def row(pnl, reason, day, coin="XRP-USD"):
    return {"product_id": coin, "pnl": pnl, "exit_reason": reason,
            "closed_at": "2026-10-%02dT12:00:00Z" % day}


# The live shape on 2026-10-06: 212 closes in the ledger, the newest 50 sent,
# four of those fifty tagged adopted_exit.
own_window = [row(0.25, "profit_target", 1 + (i % 5)) for i in range(46)]
zec = [row(-38.07, "adopted_exit", 4, "ZEC-USD"), row(-123.24, "adopted_exit", 4, "ZEC-USD"),
       row(-123.21, "adopted_exit", 4, "ZEC-USD"), row(-26.72, "adopted_exit", 4, "ZEC-USD")]
LIVE = {
    "recent_trades": own_window + zec,
    "total_realized_pnl": -164.00, "total_trade_count": 212,
    "realized_own_usd": 147.24, "realized_own_trades": 208,
    "realized_adopted_usd": -311.24, "realized_adopted_trades": 4,
    "realized_adopted_restated_usd": 297.78, "realized_adopted_restated_count": 4,
}

live = render(LIVE)
t = live["text"]

print("\nthe line ends on the grid's own book")
ok("it ends at +$147.24", "$147.24" in t, t[:400])
ok("and NOT at the pooled -$164.00", "164.00" not in t,
   "anchoring on total_realized_pnl feeds the inherited dollars back through the offset")
ok("the figure is labelled as the grid's own trades",
   "the grid itself opened and closed" in t, t[:400])

print("\nthe inherited rows are not plotted")
ok("46 of the 50 window rows are drawn, not 50",
   "46 closed round trips" in t, t[:400])
ok("the 162 own trades outside the window are named",
   "162 earlier trades" in t, t[:400])
ok("the curve reads as a gain, not a loss",
   "var(--green)" in live["html"] and "stroke=\"var(--red)\"" not in live["html"])

print("\nthe inherited cash is reported, both ways, on its own line")
ok("the block exists", "Not in this line" in t, t[:400])
ok("4 inherited closes are counted", "4 inherited close" in t, t[:400])
ok("the adoption-mark figure is shown", "-$311.24" in t, t[:500])
ok("AND what was actually paid", "$297.78" in t,
   "quoting only the mark picks a side; both are real")
ok("it says the grid did not buy them",
   "the grid never bought" in t, t[:500])
ok("and that it is not a measure of the strategy",
   "not a measure of this strategy" in t, t[:500])

print("\na fleet with no adopted branch gets no footnote")
clean = dict(LIVE)
clean["recent_trades"] = own_window
clean["realized_adopted_usd"] = 0.0
clean["realized_adopted_trades"] = 0
clean["realized_adopted_restated_usd"] = 0.0
c = render(clean)["text"]
ok("no inherited block is drawn", "Not in this line" not in c, c[:300])
ok("the line still ends on the own figure", "$147.24" in c, c[:300])

print("\na ledger that does not publish the own total falls back to the window")
# shownSum is 46 x 0.25 = $11.50. The pooled figure is a long way from it, and
# must not be reached for.
blind = dict(LIVE)
del blind["realized_own_usd"]
del blind["realized_own_trades"]
blind["total_realized_pnl"] = -500.00
b = render(blind)["text"]
ok("it ends on the window's own sum, $11.50", "$11.50" in b, b[:400])
ok("and never on the pooled -$500.00", "500.00" not in b,
   "the fallback must not reintroduce inherited dollars")
ok("and it stops calling that figure all-time",
   "across the round trips shown here" in b and "all-time" not in b,
   "a window sum presented as an all-time total is a new lie for an old one")

print("\na ledger with nothing inherited keeps its all-time anchor")
# Nothing adopted anywhere, so total_realized_pnl IS the own total and the
# offset that puts the left edge below zero is real. Narrowing the anchor
# to the window here would throw away a true figure.
pure = {"recent_trades": own_window,
        "total_realized_pnl": 61.50, "total_trade_count": 200}
pu = render(pure)["text"]
ok("it ends on the all-time $61.50", "$61.50" in pu, pu[:400])
ok("the 154 trades outside the window are still named",
   "154 earlier trades" in pu, pu[:400])
ok("and it is still called all-time", "all-time realized" in pu, pu[:400])

print("\ntoo little data is said, not drawn")
thin = dict(LIVE)
thin["recent_trades"] = [own_window[0]] + zec
ok("one own trade is not a curve",
   "Not enough completed grid round trips" in render(thin)["text"],
   "four inherited rows must not pad the sample up to a drawable curve")

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
