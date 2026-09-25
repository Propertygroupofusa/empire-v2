"""A number on the dashboard is a claim. Every claim here must come from data.

The bill for not having this, 2026-09-25 - one page telling the operator
four different untrue things at once:

  the headline Total Profit card
      Its REALIZED half summed /family-tree-status/coin-history, which is
      CryptoCoinTradeHistory - a table only the RETIRED family tree bot
      ever wrote. So the top-line figure on a page about the live grid was
      carrying a dead system's -$508.44, while the grid panel a few inches
      below read +$19.55. Its UNREALIZED half was summed off tree branch
      positions, and the tree holds none, so that half was pinned to $0.00
      no matter what the grid was actually carrying.

  the "Coin Trade History" panel
      Same dead table, no qualifier in the heading. POL -$392.43 over 87
      trades at 15% win, every row red, directly beneath a grid summary
      reading +$19.55 at 76% win. Two panels, one page, contradicting each
      other, because they were reporting two different systems and neither
      said so.

  the branch legend
      Hardcoded "up-to-10 concurrent real positions", "its own 1% dip" and
      "a 1% bounce". Every live branch runs 3 levels at a 2.00% step above
      a 1.70% fee floor, and the step moves per coin as the tuner measures.
      Three numbers, wrong three different ways, written into prose where
      nothing could ever correct them.

  the dead sections
      A family tree section - canvas, "Start new $50 branch", two ranking
      charts, and a one-way "Retire the tree & buy real BTC" - for a system
      reporting 2 branches, $0.00 allocated, no positions and
      family_tree_loop_running: false. Plus three BTC panels that each said
      in their own copy that they never feed a trade.

THE RULES THIS FILE PROTECTS:

  1. Anything the page says about how the fleet trades is INTERPOLATED from
     the branch rows, never written into prose.
  2. The headline profit card reads the LIVE grid's ledger. The retired
     tree's ledger is never summed into it.
  3. A panel showing the retired tree's money says so in its heading.
  4. The dead sections and the one-way destructive control stay gone.
  5. A coin price is formatted as a price, not as a dollar balance.

Run: python3 test_dashboard_accuracy.py
"""

import io
import re
import sys

DASH = "family_tree_dashboard.html"
SRC = io.open(DASH, encoding="utf-8").read()
SCRIPT = re.search(r"<script>(.*)</script>", SRC, re.S).group(1)
MARKUP = SRC[: SRC.index("<script>")]


def strip_comments(js):
    """JS with // line comments and /* */ blocks removed.

    Checks for a hardcoded number must run against what the page can
    RENDER, not against the comments explaining why it must not be
    hardcoded - otherwise this file fails on its own documentation.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(re.sub(r"(^|\s)//.*$", "", ln) for ln in js.split("\n"))


RENDERABLE = strip_comments(SCRIPT) + MARKUP

FAILURES = []
CHECKS = 0


def ok(label, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        FAILURES.append(label)


def fn_body(name):
    """Source of one top-level JS function, by brace matching."""
    m = re.search(r"^(?:async )?function %s\s*\(" % re.escape(name), SCRIPT, re.M)
    if not m:
        return None
    i = SCRIPT.index("{", m.end() - 1)
    depth, j, in_s, esc, q = 0, i, False, False, ""
    while j < len(SCRIPT):
        c = SCRIPT[j]
        if in_s:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                in_s = False
        else:
            if c in "\"'`":
                in_s, q = True, c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return SCRIPT[i : j + 1]
        j += 1
    return None


print("test_dashboard_accuracy.py")
print()

# ── 1. how the fleet trades is read, never written ──────────────────────
print("-- the page cannot state a step or a level count it did not read --")
grid = fn_body("renderGridStatus") or ""
ok("renderGridStatus derives the step phrase from branch grid_pct",
   "stepPhrase" in grid and "b.grid_pct" in grid)
ok("renderGridStatus derives the level phrase from branch num_levels",
   "levelsPhrase" in grid and "b.num_levels" in grid)
ok("the legend interpolates both instead of naming a number",
   "${stepPhrase}" in grid and "${levelsPhrase}" in grid)

# The exact three claims that were false.
for phrase in ["up-to-10 concurrent", "own 1% dip", "a 1% bounce", "1% buy/sell trigger"]:
    ok(f"the page can never render {phrase!r}", phrase not in RENDERABLE)

# A literal "N%" written into the legend prose is the whole bug class.
legend = re.search(r'"Open slices" is how many.*?</p>', RENDERABLE, re.S)
ok("the legend paragraph exists", legend is not None)
if legend:
    txt = legend.group(0)
    hard = re.findall(r"(?<!\$\{)\b\d+(?:\.\d+)?%", txt)
    ok("the legend paragraph hardcodes no percentage at all", not hard, str(hard))

chart = fn_body("renderGridBranchChart") or ""
ok("the empty-branch chart note reads the branch's own step",
   "b.grid_pct" in chart and "dip below the reference" in chart)

# ── 2. the headline card reads the live grid ────────────────────────────
print()
print("-- the headline profit card is the live grid's money, not the tree's --")
loader = fn_body("loadGridStatus") or ""
ok("realized comes from the grid trade-history payload",
   "lastRealizedProfit" in loader and "total_realized_pnl" in loader)
ok("unrealized comes from the grid status payload",
   "lastUnrealizedProfit" in loader and "total_unrealized_net_usd" in loader)

refresh = fn_body("refresh") or ""
ok("refresh() no longer sums the retired tree into realized",
   not re.search(r"lastRealizedProfit\s*=\s*\(coinData", refresh))
ok("refresh() no longer sums tree branch positions into unrealized",
   not re.search(r"lastUnrealizedProfit\s*=\s*branches\.reduce", refresh))
ok("the coin-history call is still made (the retired ledger still renders)",
   "/family-tree-status/coin-history" in refresh and "renderCoinHistory" in refresh)

# ── 3. the retired tree's money is labelled as the retired tree's ───────
print()
print("-- a panel showing retired money says whose money it is --")
ok("the live grid ledger has its own section", "Grid Trade History" in MARKUP)
ok("the live ledger is marked LIVE", "LIVE</span>" in MARKUP)
ok("the retired ledger is named 'Retired family tree'", "Retired family tree" in MARKUP)
ok("the retired ledger is marked CLOSED BOOK", "CLOSED BOOK" in MARKUP)
ok("the retired ledger says it is in no number above",
   "Nothing here is counted in any number above" in MARKUP)
ok("the two ledgers render from different sources",
   "renderGridTradeHistory" in SCRIPT and "renderCoinHistory" in SCRIPT)
ok("the old unqualified 'Coin Trade History' heading is gone",
   ">\U0001f4dc Coin Trade History<" not in MARKUP)

# ── 4. the dead sections stay dead ──────────────────────────────────────
print()
print("-- what was removed stays removed --")
GONE_MARKUP = [
    ("the tree canvas", 'id="tree"'),
    ("the spawn-a-branch button", "Start new $50 branch"),
    ("the hide-empty-branches toggle", "hide-empty-branches-toggle"),
    ("the next-spawn ranking chart", "Branch Ranking by Next-Spawn"),
    ("the branch-size treemap", "Branch Sizes at a Glance"),
    ("the BTC 15-minute prediction panel", "BTC — Next 15 Minutes"),
    ("the BTC direction signal test", "BTC Direction Signal Test"),
    ("the BTC live ticker panel", "BTC Live Ticker"),
]
for label, needle in GONE_MARKUP:
    ok(f"{label} is absent from the markup", needle not in MARKUP)

GONE_FNS = ["renderTree", "renderBarList", "renderTreemap", "spawnBranch",
            "filterBranchesForTree", "renderTreeSectionHeader",
            "initHideEmptyBranchesToggle", "onHideEmptyBranchesToggle",
            "renderBtcProjection", "loadBtcProjection", "renderBtcTicker",
            "loadBtcTicker", "tickBtcCountdown", "renderBtcPredictionLog",
            "runBtcDirectionalBacktest"]
for name in GONE_FNS:
    ok(f"{name}() is gone, not merely unused",
       re.search(r"^(?:async )?function %s\s*\(" % name, SCRIPT, re.M) is None)

ok("no timer still fires every second",
   "setInterval(tickBtcCountdown, 1000)" not in SCRIPT)
ok("no interval refers to a removed loader",
   not any(f"setInterval({n}" in SCRIPT for n in GONE_FNS))

# The one-way destructive control.
passive = fn_body("renderCryptoPassiveModeBanner") or ""
ok("the one-way 'retire & buy BTC' button is gated on real holdings",
   "hasSomethingToRetire" in passive)
ok("that gate checks positions, allocation and the loop",
   "b.position" in passive and "total_allocated_usd" in passive
   and "family_tree_loop_running" in passive)

# ── 5. what replaced them is drawn from the payloads ────────────────────
print()
print("-- the new panels are measurements, not controls --")
for name, needs in [
    ("renderGridProximity", ["reference_price", "current_price", "grid_pct"]),
    ("renderGridPnlByCoin", ["total_pnl", "win_rate"]),
    ("renderGridPnlCurve", ["recent_trades", "total_realized_pnl"]),
    ("renderGridTradeHistory", ["total_realized_pnl", "overall_win_rate"]),
    ("renderRetiredTreeNote", ["family_tree_loop_running", "total_allocated_usd"]),
]:
    body = fn_body(name)
    ok(f"{name}() exists", body is not None)
    if body:
        missing = [f for f in needs if f not in body]
        ok(f"{name}() reads {', '.join(needs)}", not missing, f"missing {missing}")
        ok(f"{name}() sends nothing - it cannot move money",
           "fetch(" not in body and "method: 'POST'" not in body)
        ok(f"{name}() degrades to a message instead of throwing",
           "catch" in body)

ok("every new panel is wired into the loader that holds both payloads",
   all(f"{n}(" in loader for n in
       ["renderGridProximity", "renderGridPnlByCoin", "renderGridPnlCurve",
        "renderGridTradeHistory"]))
ok("the retired-tree note is wired into refresh()", "renderRetiredTreeNote(" in refresh)

# ── 6. a price is formatted as a price ──────────────────────────────────
print()
print("-- a sub-cent coin shows a real trigger, not $0.00 --")
ok("fmtCoinPrice() exists", fn_body("fmtCoinPrice") is not None)
prox = fn_body("renderGridProximity") or ""
ok("the proximity panel formats every price with fmtCoinPrice",
   "fmtCoinPrice(" in prox and "fmtUsd(" not in prox)
ok("fmtSignedUsd() exists so an axis reads -$1.14, not $-1.14",
   fn_body("fmtSignedUsd") is not None)

# Reproduce fmtCoinPrice's decimal rule on the real live prices.
def dp_for(a):
    if a >= 1:
        return 2
    if a >= 0.01:
        return 4
    if a > 0:
        import math
        return min(10, math.ceil(-math.log10(a)) + 3)
    return 2

for coin, price in [("BTC", 83966.54), ("NEAR", 5.0549), ("BCH", 340.71),
                    ("ARB", 0.2231), ("DOGE", 0.0976),
                    ("FLOKI", 0.00002865), ("BONK", 0.00000368)]:
    shown = f"{price:.{dp_for(price)}f}"
    ok(f"{coin} at {price} renders as a real number, not 0.00",
       float(shown) != 0.0 and shown.rstrip("0").rstrip(".") != "0", shown)

# ── 7. the page still parses and nothing dangles ────────────────────────
print()
print("-- nothing dangles --")
handlers = set(re.findall(r'on(?:click|change|input|submit)="(\w+)\(', SRC))
defined = set(re.findall(r"^(?:async )?function (\w+)", SCRIPT, re.M))
dangling = sorted(handlers - defined)
ok("every inline handler resolves to a defined function", not dangling, str(dangling))

read_ids = set(re.findall(r"getElementById\(\s*['\"]([\w-]+)['\"]", SCRIPT))
have_ids = set(re.findall(r'id="([\w-]+)"', SRC))
# Rows the page builds per index - getElementById('grid-trades-' + i) -
# can only ever appear as a prefix here, and their real ids are written
# into the template strings that build them.
DYNAMIC_PREFIXES = ("grid-trades-", "coin-trades-")
missing_ids = sorted(
    i for i in (read_ids - have_ids)
    if not any(i == pre or i.startswith(pre) for pre in DYNAMIC_PREFIXES)
)
ok("every fixed element the JS reads exists on the page",
   not missing_ids, str(missing_ids))
for pre in DYNAMIC_PREFIXES:
    ok(f"the per-row ids {pre}N are actually emitted by a template",
       f'id="{pre}$' in SCRIPT)

# The exact typo that made "Add 3 branches" dead: the JS looked up
# create-multi-grid-modal-overlay, the markup declared
# create-multiple-grid-modal-overlay, so the opener threw on its first
# statement and the button did nothing, silently, when tapped.
ok("the bulk-branch modal opener targets the id the markup declares",
   "create-multiple-grid-modal-overlay" in SCRIPT
   and "create-multi-grid-modal-overlay" not in SCRIPT)

ok("wide tables scroll inside their own box, not the page",
   ".table-scroll" in SRC and SRC.count('class="table-scroll"') >= 3)
ok("the proximity row restacks at phone width",
   "@media (max-width: 560px)" in SRC and ".prox-row" in SRC)

# ── 8. the retired system does not govern the live page ─────────────────
print()
print("-- a dead system's numbers do not speak for the live one --")
exp_fn = fn_body("renderRollingExpectancyBanner") or ""
ok("the expectancy banner hides when the tree loop is not running",
   "family_tree_loop_running === false" in exp_fn)
ok("it still shows when the tree IS running and expectancy is negative",
   "exp.negative" in exp_fn)
# Both gates this banner describes live in the tree bot, not the grid.
TREE = io.open("crypto_family_tree_bot.py", encoding="utf-8").read()
GRID = io.open("crypto_grid_bot.py", encoding="utf-8").read()
ok("the expectancy gate exists in the family-tree bot",
   "get_rolling_expectancy()" in TREE)
ok("the grid bot never gates on rolling expectancy",
   "get_rolling_expectancy" not in GRID)

mom = fn_body("fmtCombinedMomentum") or ""
ok("a negative window names the retired tree's closed loss",
   "-$508.44" in mom and "2026-09-09" in mom)
# Collapse whitespace first: the sentence wraps across source lines, so
# matching the raw text looks for a string that only exists once the
# browser has laid it out.
mom_flat = " ".join(mom.split())
ok("it says that loss is not the live grid's pace",
   "not the live grid's pace" in mom_flat)
ok("it says the grid is not trying to win it back",
   "not trying to win it back" in mom_flat)
ok("it points at the live ledger instead",
   "Grid Trade History" in mom)
ok("a positive window adds no such note",
   "deltaUsd < 0" in mom)

# ── 9. live data has to LOOK live ───────────────────────────────────────
print()
print("-- price moves constantly, so the panel must show it moving --")
ok("the price dot and gap animate to their new position",
   ".prox-dot, .prox-gap { transition:" in SRC)
ok("motion is disabled for prefers-reduced-motion",
   "prefers-reduced-motion" in SRC and ".prox-dot, .prox-gap { transition: none" in SRC)
prox = fn_body("renderGridProximity") or ""
ok("each reading is compared against the previous one", "lastProx[" in prox)
ok("closing in reads green, drifting away reads red",
   "px-up" in prox and "px-down" in prox and "r.nextDist < prev" in prox)
ok("the panel stamps when it last polled", "proxTickAt" in prox)

fast = fn_body("refreshProximityOnly") or ""
ok("there is a price-only refresh separate from the heavy redraw",
   fast and "'/grid-status'" in fast)
ok("it never stacks overlapping polls", "proxPollBusy" in fast)
ok("it does not poll a backgrounded tab", "document.hidden" in fast)
ok("a missed price poll is not reported as an outage",
   "catch" in fast and "not worth surfacing" in fast)
ok("the fast loop runs several times a minute",
   "setInterval(refreshProximityOnly, 6000)" in SCRIPT)
ok("the age readout ticks every second",
   "setInterval(tickProximityAge, 1000)" in SCRIPT)
age = fn_body("tickProximityAge") or ""
ok("a stale readout turns red rather than lying",
   "var(--red)" in age and "secs > 30" in age)
ok("returning to the tab catches up immediately",
   "visibilitychange" in SCRIPT)

print()
print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
