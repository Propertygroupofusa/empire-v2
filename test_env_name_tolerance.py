"""A variable the dashboard shows and the process cannot see.

The account owner's Railway page showed SENDGRID_API_KEY set on the right
service. A container two minutes old still reported `sendgrid: no
SENDGRID_API_KEY`, and the alert queue read `present: []` against 105 held
alerts. He said, correctly: "I have it. You're just not pulling it because
you think I don't have it."

Both were true. Railway's variable list renders "SENDGRID_API_KEY" and
"SENDGRID_API_KEY " identically - a trailing space in the NAME is invisible
in a left-aligned list - and os.getenv() misses the padded one. This is not
a hypothesis about this project: its own process environment already carries
"STRIPE_WEBHOOK_SECRET " with a trailing space, which alert-queue's
similar_variables_seen has been printing all along.

So the lookup is tolerant, rather than a person being asked to retype a
secret to satisfy a string comparison.
"""
import os
import sys
import pathlib

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import alert_sender as A

PASS, FAIL = [], []


def ok(label, cond):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


def clear():
    for k in list(os.environ):
        if "SENDGRID" in k.upper():
            os.environ.pop(k)


# --- the lookup -----------------------------------------------------------
clear()
ok("absent stays absent - tolerance never invents a credential",
   A._env_tolerant("SENDGRID_API_KEY") == ("", None))

clear()
os.environ["SENDGRID_API_KEY"] = "exact"
v, n = A._env_tolerant("SENDGRID_API_KEY")
ok("a correctly named variable is read exactly as before",
   v == "exact" and n == "SENDGRID_API_KEY")

clear()
os.environ["SENDGRID_API_KEY "] = "padded"
v, n = A._env_tolerant("SENDGRID_API_KEY")
ok("a trailing space in the NAME is found, and the real name is reported",
   v == "padded" and n == "SENDGRID_API_KEY ")

clear()
os.environ[" SENDGRID_API_KEY"] = "lead"
ok("a leading space too", A._env_tolerant("SENDGRID_API_KEY")[0] == "lead")

clear()
os.environ[" sendgrid_api_key "] = "casefold"
ok("and a case difference, since that is the same invisible class of typo",
   A._env_tolerant("SENDGRID_API_KEY")[0] == "casefold")

# THE EXACT NAME ALWAYS WINS. If both exist, reading the padded one would
# silently prefer whichever os.environ happened to iterate first - a
# credential chosen by dict ordering.
clear()
os.environ["SENDGRID_API_KEY"] = "right"
os.environ["SENDGRID_API_KEY "] = "wrong"
v, n = A._env_tolerant("SENDGRID_API_KEY")
ok("with both present the EXACT name wins - never dict ordering",
   v == "right" and n == "SENDGRID_API_KEY")

clear()
os.environ["SENDGRID_API_KEY"] = "   "
os.environ["SENDGRID_API_KEY "] = "real"
ok("an exact-but-blank variable falls through to the one with a value",
   A._env_tolerant("SENDGRID_API_KEY")[0] == "real")

clear()
os.environ["SENDGRID_API_KEY_ORDERS"] = "different-variable"
ok("a DIFFERENT variable that merely starts the same is not mistaken for it",
   A._env_tolerant("SENDGRID_API_KEY") == ("", None))

# --- the diagnosis --------------------------------------------------------
clear()
os.environ["SENDGRID_API_KEY "] = "padded"
d = A.channel_diagnosis() if hasattr(A, "channel_diagnosis") else None
if d is None:
    # the function is named by its only caller; find it by its payload
    import inspect
    for _n, _f in vars(A).items():
        if callable(_f) and not _n.startswith("_"):
            try:
                r = _f()
            except Exception:
                continue
            if isinstance(r, dict) and "email_fallbacks" in r:
                d = r
                break
ok("the diagnosis function was found", isinstance(d, dict))
if isinstance(d, dict):
    sg = (d.get("email_fallbacks") or {}).get("sendgrid") or {}
    ok("the page no longer reports present: [] for a key the owner can see",
       sg.get("present") == ["SENDGRID_API_KEY"] and sg.get("complete") is True)
    ok("and it names the stored spelling it was actually found under",
       (sg.get("found_under_a_different_name") or {}).get("SENDGRID_API_KEY")
       == "SENDGRID_API_KEY ")
    ok("with what to do about it - rename, do not re-enter the secret",
       "value does not need to change" in (sg.get("what_to_do") or ""))

    # NO VALUE ANYWHERE. The whole module's contract.
    import json
    blob = json.dumps(d)
    ok("no credential value appears anywhere in the diagnosis",
       "padded" not in blob)

clear()
os.environ["SENDGRID_API_KEY"] = "exact"
d2 = None
import inspect
for _n, _f in vars(A).items():
    if callable(_f) and not _n.startswith("_"):
        try:
            r = _f()
        except Exception:
            continue
        if isinstance(r, dict) and "email_fallbacks" in r:
            d2 = r
            break
if isinstance(d2, dict):
    sg2 = (d2.get("email_fallbacks") or {}).get("sendgrid") or {}
    ok("a correctly named variable reports no rename advice at all",
       "found_under_a_different_name" not in sg2 and "what_to_do" not in sg2)

clear()
if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
