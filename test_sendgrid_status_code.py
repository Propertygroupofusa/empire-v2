"""106 alerts held, one word of diagnosis: "sendgrid: HTTPError".

urllib RAISES HTTPError for every 4xx and 5xx, so the branch reporting
r.status was unreachable and this fell to the generic handler, which prints
the exception TYPE only. True, and useless - the codes need opposite
actions:

    401  the key is not one SendGrid accepts
    403  the from-address is not a verified sender
    400  the payload is malformed

SendGrid's error body is JSON from an HTTPS API and never echoes the
Authorization header, so its own `errors[].message` fields are safe to
surface. The SMTP handlers keep reporting the type alone, because their text
can echo the login line.
"""
import io
import json
import os
import pathlib
import sys
import urllib.error

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import alert_sender as A

PASS, FAIL = [], []


def ok(label, cond):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


def run(code, body, key_name="SENDGRID_API_KEY"):
    """send_email with urlopen raising the given HTTPError."""
    import urllib.request
    for k in list(os.environ):
        if "SENDGRID" in k.upper() or "GMAIL" in k.upper():
            os.environ.pop(k)
    os.environ[key_name] = "SG.test"
    os.environ["TRADE_ALERT_EMAIL"] = "someone@example.com"
    real = urllib.request.urlopen

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.sendgrid.com/v3/mail/send", code, "err", {},
            io.BytesIO(body.encode()))

    urllib.request.urlopen = boom
    try:
        return A.send_email("s", "b")
    finally:
        urllib.request.urlopen = real
        os.environ.pop("TRADE_ALERT_EMAIL", None)
        os.environ.pop(key_name, None)


BODY401 = json.dumps({"errors": [
    {"message": "The provided authorization grant is invalid, expired, or revoked"}]})
BODY403 = json.dumps({"errors": [
    {"message": "The from address does not match a verified Sender Identity."}]})

_, e401 = run(401, BODY401)
_, e403 = run(403, BODY403)
_, e400 = run(400, json.dumps({"errors": [{"message": "Bad Request"}]}))

ok(f"a 401 reports its CODE, not just a type ({'HTTP 401' in e401})",
   "HTTP 401" in e401 and "HTTPError" not in e401)
ok("and carries SendGrid's own reason",
   "invalid, expired, or revoked" in e401)
ok("a 403 is distinguishable from a 401 - they need opposite fixes",
   "HTTP 403" in e403 and "verified Sender Identity" in e403)
ok("a 400 is distinguishable from both", "HTTP 400" in e400)
ok("the no-route marker still wraps it, so the worker does not spend a "
   "retry on a pass with nowhere to go",
   A.is_infrastructure_failure(e401))

# NEVER THE RAW BODY. Only errors[].message, and bounded.
_, noisy = run(401, json.dumps({
    "errors": [{"message": "m", "field": "from.email", "help": "SECRET-ISH"}],
    "raw_request_echo": "Authorization: Bearer SG.should-never-appear"}))
ok("the raw body never reaches the error string",
   "should-never-appear" not in noisy and "raw_request_echo" not in noisy)
ok("nor any field other than the message",
   "SECRET-ISH" not in noisy and "from.email" not in noisy)

_, longm = run(401, json.dumps({"errors": [{"message": "x" * 5000}]}))
ok(f"a very long message is bounded (len {len(longm)})", len(longm) < 1200)

_, nomsg = run(500, "<html>upstream exploded</html>")
ok("a non-JSON body degrades to the code alone rather than throwing",
   "HTTP 500" in nomsg and "upstream exploded" not in nomsg)

# The padded-name note must survive onto the real failure path.
_, padded = run(401, BODY401, key_name="SENDGRID_API_KEY ")
ok("a key found under a padded name still says so on an HTTP failure",
   "stray whitespace" in padded)

# SMTP's handlers must NOT have been loosened by this.
src = (REPO / "alert_sender.py").read_text()
i = src.index("def send_email")
after = src[i:]
for h in after.split("except Exception as e:")[1:]:
    seg = h[:200]
    if "{e}" in seg.replace("{type(e).__name__}", ""):
        FAIL.append("a generic handler now returns exception TEXT")
ok("every generic handler still reports the type alone",
   not any("generic handler now returns" in f for f in FAIL))
ok("and the reason SMTP is treated differently is written down",
   "can echo the login line" in src)

if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
