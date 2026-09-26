"""The three new panels on the dashboard that is actually being read.

The newsroom at /newsroom had the stops, the league and the alarm. The page
the account owner actually opens - /family-tree-dashboard - did not, and a
safety feature on a page nobody visits is not a safety feature.

What these guard:

  * the "these are not orders" warning surviving into the markup. The panel
    shows stop LEVELS. A reader who thinks something is resting at the
    exchange is unprotected and believes they are covered.
  * the panels reading the endpoints rather than recomputing. The money
    trace panel did its own arithmetic in JavaScript and went on showing a
    retracted 74% for hours after every other surface was corrected.
  * what cannot be measured staying visible. The census that reported
    $79.30 for an $11,292 account did it by dropping what it could not
    price.
"""
import re
from pathlib import Path

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


def section(start_marker, end_marker):
    i = HTML.index(start_marker)
    return HTML[i:HTML.index(end_marker, i)]


print("\nall three sections exist on the page people actually open")

for wrap in ("holdings-watch-wrap", "league-wrap", "alert-queue-wrap"):
    ok(f"  #{wrap} is in the markup", f'id="{wrap}"' in HTML)
for fn in ("runHoldingsWatch", "renderHoldingsWatch", "runLeague",
           "renderLeague", "loadAlertQueue"):
    ok(f"  {fn}() is defined", f"function {fn}(" in HTML)

print("\nTHE NOT-AN-ORDER WARNING SURVIVES INTO THE MARKUP")

ok("the section copy says they are alert levels, not stop-loss orders",
   "ALERT LEVELS, not stop-loss orders" in HTML)
ok("and that nothing rests at the exchange",
   "Nothing rests at the exchange" in HTML)
ok("and that a real stop needs an order",
   "a real stop needs an order" in HTML)
ok("the renderer also prints the endpoint's own disclaimer",
   "escText(d.disclaimer" in HTML,
   "the copy above can be edited; the endpoint's disclaimer cannot be lost silently")

print("\nthe panels READ the endpoints, they do not recompute")

watch = section("function renderHoldingsWatch", "async function runHoldingsWatch")
ok("the watch panel does no stop sizing of its own",
   "2.5" not in watch.replace("2.5&times;", "") and "* 2.5" not in watch,
   "one sizing rule, in adaptive_stop, or there are two answers to one question")
ok("it reads stop_level from the payload", "r.stop_level" in watch)
ok("it reads covered_share_pct rather than deriving coverage",
   "d.covered_share_pct" in watch and "covered_usd /" not in watch)

league = section("function renderLeague", "async function runLeague")
ok("the league panel computes no score", "score" in league and "0.55" not in league)
ok("it reads the division bands from the payload", "division_bands" in league)
ok("and the movement, rather than diffing anything itself",
   "r.score_change" in league and "previous_score" not in league.split("mi.from_score")[0])

print("\nWHAT CANNOT BE MEASURED STAYS ON SCREEN")

ok("unpriced holdings get their own group", "d.unpriced" in watch)
ok("so do the ones with no volatility", "d.no_vol" in watch)
ok("and they are labelled unknown, not small",
   "unknown, not small" in HTML)
ok("dust is shown separately, with the reason",
   "d.dust" in watch and "no exit exists at this size" in HTML)
ok("the league shows UNRATED as its own division",
   "'UNRATED'" in league)
ok("with the reason it could not be rated", "r.unrated_reason" in league)

print("\nthe concentration rule shown is the owner's own")

ok("the 20% rule is named in the copy", "20% rule you set" in HTML)
ok("and flagged per row from the payload flag",
   "r.over_concentration_limit" in watch,
   "read from the endpoint, not re-derived against a different denominator")

print("\nthe alarm panel leads with whether anything is delivered")

alarm = section("async function loadAlertQueue", "async function runMoneyTrace")
ok("it branches on channel_configured first", "d.channel_configured" in alarm)
ok("an unconfigured channel says nothing is being delivered",
   "Nothing is being delivered" in alarm)
ok("and that alerts are HELD, not lost", "held" in alarm.lower())
ok("and explains why they are not marked sent",
   "worse than an empty one" in alarm)
ok("it surfaces the diagnosis when there is one", "channel_diagnosis" in alarm)
ok("including similar variable names, to catch a typo",
   "similar_variables_seen" in alarm)
ok("it never prints a webhook value",
   "webhook_url" not in alarm and "g.value" not in alarm,
   "this page is behind no auth worth the name")
ok("a queue it cannot read is reported as unknown, not healthy",
   "is unknown" in alarm and "not the same as healthy" in alarm)

print("\nfailures are loud, not silent")

for fn, wrapname in (("runHoldingsWatch", "holdings-watch-wrap"),
                     ("runLeague", "league-wrap")):
    blk = HTML[HTML.index(f"async function {fn}("):]
    blk = blk[:blk.index("\n}\n") + 3]
    ok(f"  {fn} catches and shows the error", "catch (e)" in blk and "escText(e.message)" in blk)
    ok(f"  {fn} re-enables its button in finally", "finally" in blk and "btn.disabled = false" in blk)

ok("the watch says it shows nothing rather than a partial book",
   "rather than" in watch or "partial book" in HTML)

print("\neverything printed is escaped")

for expr in ("escText(r.asset)", "escText(a.message)", "escText(mi.asset)",
             "escText(L.basis", "escText(e.message)"):
    ok(f"  {expr}", expr in HTML)

print("\nthe alarm loads itself; the two expensive panels do not")

ok("loadAlertQueue runs on page load", re.search(r"^loadAlertQueue\(\);", HTML, re.M) is not None)
ok("and on an interval", "setInterval(loadAlertQueue" in HTML)
ok("the watch is button-triggered", 'onclick="runHoldingsWatch()"' in HTML)
ok("the league is button-triggered", 'onclick="runLeague()"' in HTML)
ok("neither expensive panel is on a timer",
   "setInterval(runHoldingsWatch" not in HTML and "setInterval(runLeague" not in HTML,
   "each fetches history for dozens of coins; on a timer that is a rate limit")

print("\nthe copy desk's corrections reach this page too")

ok("a correction count is surfaced", "c.correction_count" in HTML)
ok("with the first correction's note", "corrections[0]" in HTML)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
