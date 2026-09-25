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
