#!/usr/bin/env python3
"""Which Alpaca account are these figures from. Now published, not inferred.

WHAT WENT WRONG. On 2026-10-09 the account owner was shown an equity of
$974.49, a realised -$29.70 and an open SLV position - and had to tell
Claude the account was not real. He was right. Three sources all say paper
and none of them is an endpoint:

  README.md L38   "| Alpaca | Paper account | Paper -> Live |"
  README.md L125  ALPACA_BASE_URL=https://paper-api.alpaca.markets
  the code        os.getenv("ALPACA_BASE_URL", "https://paper-api...")
                  defaults to the PAPER host, in three separate files

and the GO LIVE CHECKLIST's "Change ALPACA_BASE_URL to api.alpaca.markets"
is unticked. No endpoint published the host, so an hourly guard reported
those figures for days and nothing in the system ever said what they were.

THE FIELD THAT LOOKED LIKE AN ANSWER AND WAS NOT.
equity_curve.current_is_live_account means "this equity came from a live
fetch rather than the last stored point". It is True on a paper account.
Claude read it as evidence the account was live. It is not evidence of
anything about the account.

THE TWO FLAGS CAN DISAGREE, and README.md says so in those words: "Both
Alpaca flags required - one alone does nothing." Live measurement
2026-10-09: /status publishes live_trading TRUE, so ALPACA_LIVE_TRADE is
set - while ALPACA_BASE_URL may still be the paper host. That exact
combination sends live-intent orders to a simulator, and no single boolean
can express it. Hence a verdict, not a flag.

WHAT IS ASSERTED HERE: every combination of the two variables produces a
verdict a reader cannot mistake, UNKNOWN stays a real third answer, and
no secret is ever published.

Run: python3 test_alpaca_venue_is_published.py
"""
import importlib
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

_failures = []
_passes = 0


def ok(label, condition, detail=""):
    global _passes
    if condition:
        _passes += 1
        print(f"  ok   {label}")
    else:
        _failures.append(f"{label}{(' - ' + detail) if detail else ''}")
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")


def venue(base_url, live_flag):
    """Resolve the verdict under one (host, flag) pair."""
    import routers.trading_dashboard as td
    if base_url is None:
        os.environ.pop("ALPACA_BASE_URL", None)
    else:
        os.environ["ALPACA_BASE_URL"] = base_url
    os.environ["ALPACA_LIVE_TRADE"] = "true" if live_flag else "false"
    # The module reads ALPACA_BASE_URL at import time, as every other
    # constant in that file does; reload so the test drives the real path
    # rather than a copy of it.
    importlib.reload(td)
    return td.alpaca_venue()


PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"


def test_the_default_is_paper_and_says_so_plainly():
    v = venue(None, False)
    ok("an UNSET variable resolves to the paper host", v["is_paper"] is True,
       str(v["host"]))
    ok("verdict PAPER", v["verdict"] == "PAPER", v["verdict"])
    ok("the detail says SIMULATED in a word nobody misreads",
       "SIMULATED" in v["detail"], v["detail"])
    ok("and says no real money was made or lost",
       "no real money" in v["detail"], v["detail"])


def test_the_conflict_case_is_its_own_verdict():
    # The live measurement: ALPACA_LIVE_TRADE=true, host still paper.
    v = venue(PAPER, True)
    ok("verdict PAPER_WITH_LIVE_FLAG, not PAPER and not LIVE",
       v["verdict"] == "PAPER_WITH_LIVE_FLAG", v["verdict"])
    ok("it is named a CONFLICT", "CONFLICT" in v["detail"], v["detail"])
    ok("it quotes the README rule that makes it a conflict",
       "one alone does nothing" in v["detail"], v["detail"])
    ok("it still says the figures are simulated, which is what matters",
       "simulated" in v["detail"].lower(), v["detail"])
    ok("both inputs are reported, not just the conclusion",
       v["is_paper"] is True and v["live_trade_flag"] is True, str(v))


def test_a_real_live_account_reads_as_real_money():
    v = venue(LIVE, True)
    ok("verdict LIVE", v["verdict"] == "LIVE", v["verdict"])
    ok("says REAL money", "REAL money" in v["detail"], v["detail"])
    ok("is_paper is false", v["is_paper"] is False, str(v))


def test_a_live_host_with_the_flag_off_is_not_called_live():
    v = venue(LIVE, False)
    ok("verdict LIVE_HOST_FLAG_OFF", v["verdict"] == "LIVE_HOST_FLAG_OFF",
       v["verdict"])
    ok("it tells the reader to check both before trusting a figure",
       "read both" in v["detail"], v["detail"])


def test_unknown_stays_a_third_verdict():
    # This repository has been bitten four times by an unreadable value
    # collapsing into a confident one (ITEM 0, ITEM 8, condition 6, and
    # the paper-account call itself). A host that is neither endpoint is
    # not quietly sorted into whichever is nearer.
    for weird in ("https://example.invalid", "", "http://localhost:9999"):
        v = venue(weird, True)
        ok(f"{weird!r} -> UNKNOWN", v["verdict"] == "UNKNOWN", v["verdict"])
        ok(f"{weird!r} is not claimed as either one",
           "do not read it as either" in v["detail"], v["detail"])


def test_it_names_the_field_that_misled_and_why():
    v = venue(PAPER, False)
    ok("it points at current_is_live_account by name",
       "current_is_live_account" in v["not_the_same_as"], str(v))
    ok("and explains that field is True on a paper account",
       "True on a paper" in v["not_the_same_as"], str(v))


def test_no_secret_is_ever_published():
    os.environ["ALPACA_API_KEY"] = "SHOULD-NEVER-APPEAR"
    os.environ["ALPACA_SECRET_KEY"] = "ALSO-NEVER"
    for host, flag in ((PAPER, False), (LIVE, True), ("https://x.invalid", True)):
        v = venue(host, flag)
        blob = repr(v)
        ok(f"{v['verdict']}: no key material in the payload",
           "SHOULD-NEVER-APPEAR" not in blob and "ALSO-NEVER" not in blob)
        ok(f"{v['verdict']}: the only URL published is the host itself",
           v["host"] in (host or None, None) or host == "",
           str(v["host"]))


def test_both_endpoints_carry_it():
    # /alpaca-overview is the guard's primary check and /status is what it
    # cross-checks against. A verdict on one and not the other is how the
    # two disagree again.
    import ast
    with open("routers/trading_dashboard.py", encoding="utf-8") as fh:
        src = fh.read()
    ok("the resolver is called more than once in the router",
       src.count("alpaca_venue()") >= 2, str(src.count("alpaca_venue()")))
    ok('it is published under the key "venue"', '"venue": alpaca_venue()' in src)
    ast.parse(src)
    ok("the router still parses", True)


def test_zz_nothing_above_failed():
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"ALL {_passes} ASSERTIONS PASS")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
