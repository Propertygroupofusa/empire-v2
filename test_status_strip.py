"""The dashboard must answer "is it running" before anything else.

For sixteen days - 2026-09-09 to 2026-09-25 - this page showed correct
balances, correct branches and correct settings while the grid loop was not
running at all. Nothing on it reported whether the bot was ALIVE, only what
it WOULD do if it were. A stopped fleet and a healthy one rendered
identically.

Worse, the first real number was several phone-screens down, behind a
ticker, a header, five conditional banners and a large panel for closing a
BTC position that had already been sold.

THE RULE: verdict first, detail after. The strip answers, in order:

    is it running      from the heartbeat - MEASURED, never inferred
    what is it worth   cash + working capital
    did it trade TODAY a date comparison, not a running total
    how many branches  active / total, and the step they run

Each one exists because its absence cost real time:

  * liveness was previously inferred from a branch's stored grid_pct
    matching a promoted override - which is wrong, because a PAUSED branch
    never cycles and so never updates it
  * "traded today" is a DATE check because a cumulative $19.55 from
    September read as current profit for over two weeks

Run: python3 test_status_strip.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
page = open(os.path.join(HERE, "family_tree_dashboard.html"), encoding="utf-8").read()
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# --- it exists, and it is FIRST ------------------------------------------
ok("the page has a status strip", 'id="status-strip"' in page)
ok("the strip is above the combined-progress panel",
   page.index('id="status-strip"') < page.index('id="combined-progress-panel"'))
ok("and above the KPI row", page.index('id="status-strip"') < page.index('id="stat-profit-card"'))
ok("and above every conditional banner",
   page.index('id="status-strip"') < page.index('id="crypto-passive-mode-banner"'))

# --- liveness is MEASURED, from the heartbeat ----------------------------
ok("liveness comes from the heartbeat", "d.heartbeat" in page or "(d && d.heartbeat)" in page)
def _fn_source(src, name):
    """Exactly one function's body, by brace matching.

    This used to slice from "function renderStatusStrip" to the next
    known marker ("async function loadGridStatus") and treat everything
    between as the strip. That held only while nothing was ever defined
    between those two points - so when four live-grid render functions
    were added there, their legitimate use of total_realized_pnl was
    attributed to the status strip and failed the check below. Match the
    function's own braces instead of trusting what happens to sit after
    it.
    """
    m = re.search(r"^(?:async )?function %s\s*\(" % re.escape(name), src, re.M)
    assert m, f"{name} not found"
    i = src.index("{", m.end() - 1)
    depth, j, in_s, esc, q = 0, i, False, False, ""
    while j < len(src):
        c = src[j]
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
                    return src[i:j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces in {name}")


strip = _fn_source(page, "renderStatusStrip")
ok("it renders a RUNNING state", "RUNNING" in strip)
ok("it distinguishes STALLED from never-started", "STALLED" in strip and "NOT RUNNING" in strip)
ok("a missing heartbeat says the loop never ran, not 'no data'",
   "never been recorded" in strip or "never having" in strip or "has ever been recorded" in strip)
# grid_pct is legitimate in the BRANCHES section (it shows the step in
# force). What must never happen is liveness being derived from it, so this
# checks the liveness block alone - everything before the money section.
_liveness_block = strip.split("strip-total")[0]
ok("liveness is never inferred from grid_pct", "grid_pct" not in _liveness_block)
ok("liveness comes from the heartbeat's own alive flag", "hb.alive" in _liveness_block)

# --- today is a DATE comparison, not a running total ---------------------
ok("today is computed from closed_at dates", "closed_at" in strip)
ok("it compares against today's date", "toISOString" in strip and "slice(0, 10)" in strip)
ok("it does NOT show the lifetime realized total as today's figure",
   "total_realized_pnl" not in strip)
ok("with no trades today it says so and names the last one",
   "no trades on record" in strip or "last trade" in strip)

# --- money -----------------------------------------------------------------
ok("the total combines cash and working capital",
   "real_free_cash_usd" in strip and "total_allocated_usd" in strip)
ok("a negative figure renders as -$, never $-",
   "'-$'" in strip and "$-" not in strip)

# --- branches --------------------------------------------------------------
ok("it reports ACTIVE out of total, not just a count",
   "activeCount" in strip and "branches.length" in strip)
ok("it surfaces the step percentage in force", "grid_pct" in strip)

# --- it must never break the page -----------------------------------------
ok("the strip is wrapped so a failure cannot take the page down",
   "try {" in strip and "catch" in strip)
ok("it is called from the existing loader, costing no extra request",
   "renderStatusStrip(data, history)" in page)

# --- the dead panel was demoted, not deleted ------------------------------
ok("the btc_compound close tool still exists", "confirmCloseBtcCompound()" in page)
ok("but it is now collapsed behind a details element",
   "<details" in page and "Recovery tools" in page)
ok("details tags are balanced", page.count("<details") == page.count("</details>"))

# --- "today" must not report a partial day as a whole one -------------------
#
# recent_trades is capped (50) while total_trade_count is the real total -
# 50 of 87 the day this was written. The newest trades survive the cap, so
# the figure is exact only while the window reaches back past midnight.
# With 21 branches a busy day can fill every row inside today, and then the
# strip would silently report PART of the day as the day. It undercounts,
# which is the direction that gets missed.
ok("it knows the real total, not just the window",
   "total_trade_count" in strip)
ok("it establishes whether midnight is inside the window",
   "dayFullyCovered" in strip and "oldestInWindow" in strip)
ok("a window that IS the whole ledger counts as covered",
   "haveAll" in strip and "trades.length >= totalCount" in strip)
ok("an uncovered day is marked as a floor, not a total",
   "at least " in strip)
ok("and the headline carries the same caveat as the subtitle",
   "dayFullyCovered ? '' :" in strip)
ok("a covered day still reads exactly as before",
   "' trade' + (todays.length === 1 ? '' : 's') + ' today'" in strip)
ok("no trades at all still names the last one",
   "no trades on record" in strip and "last trade " in strip)

# --- the scoreboard reads like a broadcast, and the lamp cannot lie ---------
ok("the strip carries the tv class", 'id="status-strip"' in page and 'class="panel tv"' in page)
ok("it defines its own palette rather than inheriting the light one",
   "#status-strip.tv {" in page and "--tv-green" in page and "--tv-dim" in page)
ok("the ON AIR lamp exists", 'id="strip-onair"' in page and 'id="strip-onair-text"' in page)
ok("the lamp is driven by the same heartbeat as the word beside it",
   "var alive = !!(hb.seen && hb.alive)" in strip and "alive ? 'ON AIR'" in strip)
ok("a never-started loop reads NO SIGNAL, not OFF AIR",
   "'NO SIGNAL'" in strip and "hb.seen ? 'OFF AIR'" in strip)
ok("the pulse is disabled under prefers-reduced-motion",
   "prefers-reduced-motion" in page and "tvPulse" in page)
ok("the figures use the broadcast palette, not the white-card one",
   "TV_GREEN = '#3fd68c'" in strip and "#1a7f37" not in strip)
ok("numbers line up in columns", "tabular-nums" in page)
ok("the four cells stack on a phone",
   "grid-template-columns:1fr" in page and "max-width:420px" in page)
ok("all four captions survived the restyle",
   page.count('class="tv-cap"') == 4)
ok("the backing banner stays inside the strip", 'id="backing-banner"' in page)

# --- a locked tab must say it is locked, not that a read failed -------------
#
# The btc_compound dry run is a POST to a state-changing endpoint, so the
# write guard refuses it without a token - correctly. The old call site
# wrapped that refusal in "Could not read the position:", which made a
# locked tab look like a broken read on a button whose next step sells
# real coin.
ok("the dry run goes through the guarded poster, not a raw fetch",
   "postGuarded(API_BASE + '/btc-compound/close-position?dry_run=true')" in page)
ok("the real sell does too",
   "postGuarded(API_BASE + '/btc-compound/close-position?dry_run=false')" in page)
ok("no call site reports a locked tab as a failed read",
   "Could not read the position" not in page)
ok("postGuarded names the missing token before sending anything",
   "This browser tab is locked" in page)
ok("and says plainly that nothing was sent",
   "Nothing was sent and nothing" in page)
ok("the guard itself is not bypassed for the preview",
   "dry_run=true" in page and "x-dashboard-token" in page)

# --- the two halves of TOTAL PROFIT are not the same kind of number --------
#
# Realized is banked and only moves on a completed sell. Unrealized marks
# coin the owner ALREADY OWNED - a branch on it reports the price move, it
# does not create one. So the unrealized half scales with how much coin is
# being MEASURED, and the headline adds the two together. When deployed
# coin went $3,641 -> $5,642 in an afternoon, unrealized went -$13 -> -$68
# on an ordinary -1.2% day and the card turned red.
ok("unrealized is shown against what it is measured on",
   "lastDeployedCoin" in page and "deployed_coin_usd" in page)
ok("the percentage is computed, not asserted",
   "unrealized / dep * 100" in page)
ok("a missing or zero denominator falls back to the plain label",
   "pct === null" in page and "dep > 0" in page)
ok("the card says realized only moves on a completed sell",
   "only moves on a completed sell" in page)
ok("and that unrealized is not a loss until a slice is sold below entry",
   "sold below its entry" in page)
ok("the headline still adds both halves rather than hiding the red one",
   "(lastUnrealizedProfit || 0) + (lastRealizedProfit || 0)" in page)

# --- hygiene ---------------------------------------------------------------
ids = re.findall(r'id="([a-zA-Z0-9_-]+)"', page)
ok("no duplicate element ids", len({i for i in ids if ids.count(i) > 1}) == 0)
ok("no non-ASCII identifiers slipped in",
   not re.search(r"\bvar\s*[^\x00-\x7F]", page))

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
