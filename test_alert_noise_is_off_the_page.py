"""156 copies of one fact is not an alarm. It is noise.

Run as written: python3 test_alert_noise_is_off_the_page.py

2026-10-08. The owner photographed his dashboard: page after page of red.
Every row the same four lines -

    no transport delivered (sendgrid: HTTP 401 - Maximum credits exceeded,
    smtp:465: SMTPServerDisconnected, smtp:587: SMTPServerDisconnected).
    SMTP port(s) 465, 587 never answered, so outbound SMTP looks filtered
    on this host. Set SENDGRID_API_KEY to deliver over HTTPS instead.

- under headings reading CRITICAL in red. The counter at the top: 156
waiting, 0 sent, 0 failed. Six hundred lines of identical text about ONE
fact: no mail can leave this box. He did not know what SendGrid was, read
"CRITICAL" as something wrong with his money, and said: "It's too much red
that's negative on my dashboard... causing too much noise and confusion.
I don't know what it is. Just take it away."

He was right, and the severity labels were the cruellest part: they are the
severity of the COIN EVENT, not of the delivery failure, so a routine level
crossing rendered as CRITICAL in red beside an error about email.

WHAT THIS TEST PROTECTS - that removing the panels removed NOTHING ELSE.
The alerts concern holdings_watch ALERT LEVELS, which this same page states
plainly: "nothing here rests at the exchange and nothing here will sell."
No order, no stop, no branch depends on any of it. The queue, the worker
and the /alert-queue endpoint are all untouched, so setting SENDGRID_API_KEY
restores delivery with no further change to this page.
"""
import re
import sys

SRC = open("family_tree_dashboard.html", encoding="utf-8").read()
ROUTER = open("routers/trading_dashboard.py", encoding="utf-8").read()
FAILS = []


def ok(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"  ({detail})" if not cond and detail else ""))
    if not cond:
        FAILS.append(label)


print("\n[1] both panels are gone from the page")
ok("the full alert-queue container is removed",
   not re.search(r'<div id="alert-queue-wrap"', SRC))
ok("the top-of-page alarm container is removed",
   not re.search(r'<div id="alarm-top-wrap"', SRC))
# Checked against the MARKUP, not the whole file: the string also lives
# inside renderTopAlarm's HTML template, which is dead code now (the
# function early-returns on its missing container, pinned in section [3]).
# Asserting on the whole file would fail for text that cannot render.
MARKUP = SRC[: SRC.index("<script>")]
ok("the section heading no longer renders",
   not re.search(r'<div class="section-title">🔔 The alarm', MARKUP))
ok("and no alarm section-title survives in the markup at all",
   "🔔 The alarm" not in MARKUP)
ok("no panel-sub about the queue survives",
   "This is the queue that turns it" not in SRC)

print("\n[2] nothing fetches or polls it any more")
calls = [m for m in re.findall(r"^\s*loadAlertQueue\(\);", SRC, re.M)]
ok("loadAlertQueue() is not called at boot", not calls, str(calls))
ok("and is not polled", not re.search(r"pollEvery\(\s*loadAlertQueue", SRC))

print("\n[3] the dead code that remains CANNOT throw")
# The functions survive so restoring the panel is a one-line change. They
# must early-return rather than blow up on a container that no longer exists.
for fn, ident in (("loadAlertQueue", "alert-queue-wrap"),
                  ("renderTopAlarm", "alarm-top-wrap")):
    i = SRC.index("function %s" % fn)
    head = SRC[i:i + 400]
    ok(f"{fn} null-guards its missing container",
       re.search(r"if \(!wrap\) return", head) is not None, head[:120])
# The third lookup is inside loadAlertQueue's error path.
ok("the error-path lookup is wrapped in if (top)",
   re.search(r"const top = document\.getElementById\('alarm-top-wrap'\);\s*\n\s*if \(top\)", SRC)
   is not None)

print("\n[4] THE BACKEND IS UNTOUCHED - this was a display change only")
ok("the /alert-queue endpoint still exists", '@router.get("/alert-queue")' in ROUTER)
ok("the page still contains no write to the alert system",
   not re.search(r"alert-queue[^\"']*\"\s*,\s*\{\s*method:\s*'POST'", SRC))

print("\n[5] it did not take any TRADING panel with it")
for must_stay in ('id="grid-content"', 'id="stat-profit"', 'id="stat-total"',
                  'id="fleet-readiness-wrap"', "renderGridStatus"):
    ok(f"{must_stay} is still on the page", must_stay in SRC)

print("\n[6] the removal explains itself where the next reader will look")
ok("a note sits where the panel was",
   "THE EMAIL ALERT QUEUE WAS REMOVED FROM THIS PAGE" in SRC)
ok("and says trading did not change",
   "NOTHING ABOUT TRADING CHANGED" in SRC)
ok("and says how to bring delivery back",
   "SENDGRID_API_KEY" in SRC)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
