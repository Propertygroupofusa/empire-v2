"""The sales section must ship empty, and must not be able to touch trading.

Run as written: python3 test_agency_sales_invents_nothing.py

WHY THIS TEST EXISTS. The owner was handed a sales dashboard built
elsewhere whose own summary said "Illustrative sample figures, not
connected to Good's actual business records", and asked for it in his
dashboard. This repository has already deleted invented numbers twice -
"Delete a fabricated 100% win rate before anyone believes it again" and
"Remove the rest of the fabricated trading data" - because a plausible
figure on a dashboard gets believed and then acted on.

So the section was built to compute every figure from rows he types, and
these tests pin that it stays that way. A future edit that drops in a
demo pipeline "just so it looks alive" fails here.

THE SECOND THING PINNED IS ISOLATION. This sits in the same page as the
live trading panels. It must not fetch, must not call an endpoint, and
must not reach a write path - if it ever does, it is one bug away from
the account. The browser-side behaviour (add, search, filter, persist,
the arithmetic) is driven for real in scratchpad/drive.js against
Chromium; this file pins what can be checked from the source alone.
"""

import re
import sys

PAGE = "family_tree_dashboard.html"
FAILS = []


def ok(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def section(t):
    print("\n" + t)


src = open(PAGE).read()

def _region(open_mark, close_mark, after=0):
    a = src.index(open_mark, after)
    b = src.index(close_mark, a)
    return src[a:b], b

MARKUP, _after = _region("===================== AGENCY SALES",
                         "=================== END AGENCY SALES")
JS, _ = _region("===================== AGENCY SALES",
                "=================== END AGENCY SALES", _after)
BLOCK = MARKUP + JS

section("[1] IT SHIPS WITH NO DATA - the whole point")
ok("the section exists in the page", "Agency sales" in src)
ok("no hardcoded dollar figure other than a zero placeholder",
   not re.search(r'\$\s?[1-9][\d,]*\.\d\d', BLOCK))
ok("the three cards start at $0.00 / $0.00 / em-dash",
   BLOCK.count(">$0.00<") == 2 and ">—<" in BLOCK)
ok("no seed array of leads anywhere in the block",
   not re.search(r'(SALES_SEED|sampleLeads|demoLeads|placeholderLeads)', BLOCK))
for fake in ("Acme", "John Doe", "Jane Doe", "Example Corp", "Lorem"):
    ok(f"no invented name {fake!r}", fake.lower() not in BLOCK.lower())
ok("the empty leads state SAYS nothing is pre-filled and why",
   "pre-filled" in BLOCK and "sample client list" in BLOCK)

section("[2] IT CANNOT REACH THE TRADING SYSTEM")
for banned, why in [
        ("fetch(", "a network call"),
        ("XMLHttpRequest", "a network call"),
        ("/api/trading-dashboard", "a trading endpoint"),
        ("DASHBOARD_WRITE_TOKEN", "the write token"),
        ("crypto_grid", "a grid branch")]:
    ok(f"the block contains no {banned!r} - {why}", banned.lower() not in BLOCK.lower())
# "Alpaca" and "Coinbase" appear in the MARKUP deliberately - the panel says
# out loud that it has nothing to do with those accounts. Reading one would
# be code, so this check is on the script half only.
for banned in ("alpaca", "coinbase", "grid-status", "account-census"):
    ok(f"the SCRIPT never names {banned!r}", banned.lower() not in JS.lower())
ok("and the markup DOES name them, to say it is separate",
   "Coinbase or Alpaca accounts" in MARKUP)
ok("its only persistence is localStorage",
   "localStorage" in BLOCK and "indexedDB" not in BLOCK)
ok("and it says on the page that this is browser-only, not a database",
   "this browser only" in BLOCK and "not a database" in BLOCK.lower())
ok("it states it cannot place an order or move a dollar",
   "place an order or" in BLOCK and "move a dollar" in BLOCK)

section("[3] EVERY FIGURE IS A SUM, NOT A CONSTANT")
ok("revenue is reduced from rows marked Won",
   "r.stage === 'Won'" in BLOCK and "won.reduce(" in BLOCK)
ok("pipeline is reduced from rows that are NOT closed",
   "SALES_CLOSED[r.stage]" in BLOCK and "open.reduce(" in BLOCK)
ok("Won and Lost are both excluded from pipeline",
   "'Won': 1, 'Lost': 1" in BLOCK)

section("[4] IT REFUSES TO STATE A RATE IT CANNOT SUPPORT")
ok("a win rate with no decided deal is a dash, never 0%",
   "if (decided === 0)" in BLOCK and "'—'" in BLOCK)
ok("and it names what is missing",
   "needs a deal marked Won or Lost" in BLOCK)
ok("a small sample is labelled as such",
   "too few to read much into" in BLOCK)
ok("the trend refuses to draw through one point",
   "months.length < 2" in BLOCK and "decoration" in BLOCK)

section("[5] STORAGE FAILURE IS REPORTED, NOT SWALLOWED")
ok("salesSave returns a sentence rather than a boolean",
   "refused to save" in BLOCK)
ok("every localStorage read is wrapped",
   BLOCK.count("try {") >= 5 and "catch (e) { return []; }" in BLOCK)
ok("a failed save tells the owner to copy the CSV",
   "copy the CSV" in BLOCK)

section("[6] THE VIEWER'S LIMITS ARE RESPECTED")


def _code_only(js):
    """JS with comments removed, so a comment ABOUT a banned call does not
    read as the call. The first version of this file failed on its own
    explanatory comment, which is the test being naive, not the code."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


CODE = _code_only(JS)
ok("no confirm() CALL - it returns false immediately in embedded viewers",
   "confirm(" not in CODE)
ok("no alert() or prompt() either - both are inert in those viewers",
   "alert(" not in CODE and "prompt(" not in CODE)
ok("and the code says why the delete is not gated on it",
   "return false from it" in BLOCK or "returns false" in BLOCK.lower())
ok("clipboard failure falls back to a selectable textarea",
   "salesFallbackCopy" in BLOCK)

section("[7] USER TEXT IS ESCAPED BEFORE IT REACHES innerHTML")
ok("an escaper exists", "function salesEsc(" in BLOCK)
for field in ("r.name", "r.company", "r.contact"):
    ok(f"{field} is escaped where it is rendered", f"salesEsc({field})" in BLOCK)

section("[8] IT DID NOT DISTURB THE PAGE AROUND IT")
ok("script tags still balance",
   src.count("<script") == src.count("</script>"))
ok("divs balance inside the block",
   len(re.findall(r"<div", BLOCK)) == len(re.findall(r"</div>", BLOCK)))
ok("the trading status strip is still above it",
   src.index('id="status-strip"') < src.index(
       "===================== AGENCY SALES"))
ok("it is collapsed by default, so it adds one line until opened",
   'id="sales-section" style="display:none' in BLOCK)
ok("the boot is wrapped so a throw cannot stop the trading panels",
   "must not stop the trading panels" in BLOCK)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
