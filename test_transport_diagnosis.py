#!/usr/bin/env python3
"""The remedy must be DERIVED from what the transports did, not asserted.

WHY THIS FILE EXISTS. alert_sender.send_email ended every failure with the
hardcoded sentence "SMTP is blocked outbound on this host; an HTTPS route is
the one that can work here." It was written when both ports returned
SMTPServerDisconnected, and it was a reasonable reading of that evidence.

Then the live queue returned this, on all 79 rows:

    smtp:465: SMTPAuthenticationError, smtp:587: SMTPAuthenticationError

SMTPAuthenticationError is raised by smtplib only after connect, TLS, banner,
EHLO and an actual AUTH exchange. Reaching it PROVES the port is open. The
hardcoded sentence was therefore printing the opposite of the truth, and
pointing the owner at a new mail provider when the fix was one 16-character
Gmail App Password.

So the thing under test is not "does it produce a string" - it is "does the
string change when the evidence changes, and does it name the right variable".
"""
import os, re, sys

FAILS = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n        {detail}" if detail else ""))
        FAILS.append(label)


import alert_sender as A

SRC = open("alert_sender.py").read()

# The exact rows the live queue returned, copied verbatim.
LIVE_AUTH = ["sendgrid: no SENDGRID_API_KEY",
             "smtp:465: SMTPAuthenticationError",
             "smtp:587: SMTPAuthenticationError"]
# What it returned BEFORE, also verbatim.
LIVE_BLOCKED = ["sendgrid: no SENDGRID_API_KEY",
                "smtp:465: SMTPServerDisconnected",
                "smtp:587: SMTPServerDisconnected"]

print("\n[1] the two evidence sets must not produce the same remedy")
r_auth = A.remedy_for(LIVE_AUTH)
r_block = A.remedy_for(LIVE_BLOCKED)
ok("authenticated-and-refused differs from never-connected",
   r_auth != r_block,
   f"both produced: {r_auth[:120]!r}")

print("\n[2] an auth refusal must say SMTP is NOT blocked, and name the fix")
ok("does not claim the host blocks SMTP",
   "blocked outbound" not in r_auth.lower(), r_auth)
ok("states SMTP is not blocked", "not blocked" in r_auth.lower(), r_auth)
ok("names an App Password", "app password" in r_auth.lower(), r_auth)
ok("names GMAIL_PASSWORD as the variable to change",
   "GMAIL_PASSWORD" in r_auth, r_auth)
ok("says no new service is needed",
   "no new service" in r_auth.lower(), r_auth)
ok("does NOT send him to SendGrid for an auth failure",
   "SENDGRID_API_KEY" not in r_auth, r_auth)

print("\n[3] a real block must still point at the HTTPS route")
ok("says the port never answered", "never answered" in r_block.lower(), r_block)
ok("names SENDGRID_API_KEY", "SENDGRID_API_KEY" in r_block, r_block)
ok("does not demand an App Password",
   "app password" not in r_block.lower(), r_block)

print("\n[4] mixed ports report BOTH facts - they need opposite actions")
mixed = A.remedy_for(["smtp:465: SMTPAuthenticationError",
                      "smtp:587: SMTPServerDisconnected"])
ok("names the port that authenticated", "465" in mixed, mixed)
ok("names the port that never answered", "587" in mixed, mixed)

print("\n[5] 'smtp: no GMAIL_EMAIL/...' is not a port outcome")
# This line has a colon but no port. Parsed as one it would read as
# port 'no' and silently poison the classification.
outc = A._smtp_outcomes(["smtp: no GMAIL_EMAIL/GMAIL_PASSWORD"])
ok("credential-missing line yields no outcome", outc == {}, repr(outc))
r_nocred = A.remedy_for(["sendgrid: no SENDGRID_API_KEY",
                         "smtp: no GMAIL_EMAIL/GMAIL_PASSWORD"])
ok("names the missing mail variables",
   "GMAIL_EMAIL" in r_nocred and "GMAIL_PASSWORD" in r_nocred, r_nocred)
ok("does not assert a block it never tested",
   "never answered" not in r_nocred.lower(), r_nocred)

print("\n[6] parses the real port numbers, not positions")
ok("465 parsed", A._smtp_outcomes(LIVE_AUTH).get("465") == "SMTPAuthenticationError",
   repr(A._smtp_outcomes(LIVE_AUTH)))
ok("587 parsed", A._smtp_outcomes(LIVE_AUTH).get("587") == "SMTPAuthenticationError")
ok("sendgrid leg excluded from smtp outcomes",
   all(not k.startswith("send") for k in A._smtp_outcomes(LIVE_AUTH)))

print("\n[7] the retired sentence must be GONE from the source")
# Not just unreachable - absent. A string that still exists gets copied.
ok("no hardcoded 'SMTP is blocked outbound on this host'",
   "SMTP is blocked outbound on this host" not in SRC)

print("\n[8] the marker survives, so the retry budget stays protected")
# is_infrastructure_failure gates whether a failure spends one of the six
# attempts. If the tail stopped carrying the marker, 79 queued alerts would
# march to 'failed' permanently while the owner was still setting the
# password.
tail = SRC[SRC.rindex("def send_email"):]
ok("send_email's failure return uses NO_ROUTE_MARKER",
   "NO_ROUTE_MARKER" in tail and "remedy_for(tried, legs)" in tail)
sample = A.NO_ROUTE_MARKER + " (" + ", ".join(LIVE_AUTH) + "). " + r_auth
ok("a real auth-failure string is classed as infrastructure",
   A.is_infrastructure_failure(sample), sample[:140])

print("\n[9] no secret can ride out in the message")
# Only exception TYPE names are ever appended to `tried`; an SMTP exception's
# text can echo the AUTH line.
body = SRC[SRC.index("def send_email"):]
ok("tried only ever gets type(e).__name__, never str(e)",
   "tried.append(f\"smtp:{port}: {type(e).__name__}\")" in body
   and not re.search(r"tried\.append\([^)]*\bstr\(e\)", body))
fake = "hunter2-the-actual-password"
os.environ["GMAIL_EMAIL"] = "x@example.com"
os.environ["GMAIL_PASSWORD"] = fake
os.environ.pop("SENDGRID_API_KEY", None)
os.environ["TRADE_ALERT_EMAIL"] = "x@example.com"
import smtplib
class _Boom(smtplib.SMTPAuthenticationError):
    def __init__(self): super().__init__(535, b"wrong " + fake.encode())
_orig_ssl, _orig_plain = smtplib.SMTP_SSL, smtplib.SMTP
class _Fake:
    def __init__(self, *a, **k): pass
    def ehlo(self): pass
    def starttls(self): pass
    def login(self, *a): raise _Boom()
    def sendmail(self, *a): pass
    def quit(self): pass
smtplib.SMTP_SSL = _Fake
smtplib.SMTP = _Fake
try:
    sent, err = A.send_email("s", "b")
finally:
    smtplib.SMTP_SSL, smtplib.SMTP = _orig_ssl, _orig_plain
ok("the send did not report success", sent is False, repr(sent))
ok("the password does not appear in the error", fake not in str(err), str(err))
ok("the live run produced the App-Password remedy, end to end",
   "app password" in str(err).lower(), str(err))
ok("the live run names both ports it tried",
   "465" in str(err) and "587" in str(err), str(err))

print("\n[10] mutation: hardcoding one remedy must break a test")
# If remedy_for is replaced by a constant, [1] fails. Prove it.
_real = A.remedy_for
A.remedy_for = lambda tried: "set SENDGRID_API_KEY"
mutant_caught = A.remedy_for(LIVE_AUTH) == A.remedy_for(LIVE_BLOCKED)
A.remedy_for = _real
ok("a constant remedy makes the two evidence sets identical (so [1] catches it)",
   mutant_caught)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
