#!/usr/bin/env python3
"""Two files, one name, two numbers — this repo's most expensive habit.

THE PATTERN, IN ITS OWN WORDS. From auto_trim.plan_trims:

    "Two subsystems enforcing the same 20% ceiling against different books,
     and neither telling the other."

It has cost real money here. And on 2026-09-29 a sweep found
MAX_POSITION_SHARE_PCT defined as 20.0 in coin_adoption.py and
capital_placement.py — the concentration CEILING, which the owner lists as a
hard limit — and as 35.0 in auto_trim.py, where it meant something else
entirely: the most of one holding a single trim may sell. One name, two
meanings, fifteen points apart, in three files that all decide how much of a
coin to hold. Renamed; this file stops the next one.

WHAT THIS DOES NOT DO. It does not demand every repeated constant be unified.
Plenty are legitimately per-module — each bot has its own BOT_NAME, each worker
its own MODE_ENV — and forcing those together would be worse than the problem.
It fails only on names that carry a MONEY OR SAFETY meaning and disagree, and
every exemption below is named with a reason rather than waved through.

Run: python3 test_no_confusing_duplicates.py
"""
import ast
import collections
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
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


# Substrings that mark a constant as deciding money or safety. A name matching
# one of these MUST NOT disagree across files without an entry below.
MONEY_MARKERS = (
    "USD", "PCT", "PERCENT", "FEE", "TRADE", "TRIM", "SHARE", "STOP", "LIMIT",
    "CAPITAL", "MIN_TRADES", "SPAN_DAYS", "WINDOW_DAYS", "BARS",
)

# EXEMPTIONS, each with the reason it is not a bug. An entry here is a claim
# that the two values mean different things ON PURPOSE. Adding one is a
# decision; leaving it undocumented is not an option the test allows.
ALLOWED = {
    "BOT_NAME": "each bot names itself; sharing one would merge their state rows",
    "MODE_ENV": "each worker reads its own arming variable",
    "EQUITY_FLOOR_STATE_KEY": "one state row per bot, by design",
    "LOCKED_PROFIT_STATE_KEY": "one state row per bot, by design",
    "PRODUCT_ID": "each single-coin bot names its own market",
    "BASE_URL": "different services entirely (local API vs Alpaca)",
    "DATA_URL": "different services entirely",
    "BOT_VERSION": "independent version numbers",
    "MAX_WORKERS": "independent pool sizes for unrelated workloads",
    "MIN_DELAY": "independent pacing for unrelated workloads",
    "CACHE_TTL_SECONDS": "unrelated caches with different staleness tolerance",
    "GRANULARITY_SECONDS": "different studies sample at different resolutions",
    "STARTING_CAPITAL": "two different accounts",
    "SENDER_NAME": "unrelated email campaigns",
    "REFERRAL_EMAIL_TEMPLATE": "two different campaigns' copy",
    "TOKEN_EXPIRE_DAYS": "study tokens and worker tokens have different lifetimes",
    "PROFIT_TARGET_PCT": "two different strategies with separately measured targets",
    "STOP_LOSS_PCT": "per-strategy stops, measured separately per backtest",
    "MIN_SPAN_DAYS": ("each metric names the shortest window it will report from; "
                      "the /edge-rate retraction is why they are NOT one number"),
    "MIN_TRADES": "different confidence bars for different questions",
    "MIN_TRADES_FOR_RATE": "different confidence bars for different questions",
    "MIN_BARS": "different studies need different history depths",
    "WINDOW_DAYS": "different studies look back over different spans",
    "MIN_TRADE_USD": ("the grid fleet shares 5.0 by import or copy; "
                      "crypto_mean_reversion_bot is a separate bot at 10.0"),
    "MAX_RETRIES": "unrelated pipelines",
    "RETRY_DELAY": "unrelated pipelines",
    "DB_PATH": "unrelated youtube pipeline files",
}


def module_constants():
    """{NAME: {file: value}} for every module-level literal constant."""
    out = collections.defaultdict(dict)
    files = sorted(HERE.glob("*.py")) + sorted((HERE / "routers").glob("*.py"))
    for p in files:
        if p.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(p.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in tree.body:
            if not isinstance(n, ast.Assign) or not isinstance(n.value, ast.Constant):
                continue
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) > 4:
                    out[t.id][p.name] = n.value.value
    return out


CONSTS = module_constants()


def test_no_money_constant_disagrees_across_files_unexplained():
    offenders = []
    for name, places in sorted(CONSTS.items()):
        if len(places) < 2 or name in ALLOWED:
            continue
        if not any(m in name for m in MONEY_MARKERS):
            continue
        if len({repr(v) for v in places.values()}) > 1:
            offenders.append((name, places))
    ok("no money or safety constant disagrees across files without a reason",
       not offenders,
       "; ".join(f"{n} {dict(p)}" for n, p in offenders)
       + "  -- either make them one value, give the different concept its own "
         "NAME, or add it to ALLOWED with the reason")


def test_the_collision_that_started_this_is_gone():
    """MAX_POSITION_SHARE_PCT must mean exactly one thing."""
    places = CONSTS.get("MAX_POSITION_SHARE_PCT", {})
    ok("MAX_POSITION_SHARE_PCT is defined somewhere", places, str(places))
    ok("and every definition of it agrees",
       len({repr(v) for v in places.values()}) <= 1, str(places))
    ok("it is the 20% ceiling, not a trim cap",
       set(places.values()) in ({20.0}, set()), str(places))
    trim = CONSTS.get("MAX_TRIM_SHARE_OF_POSITION_PCT", {})
    ok("the trim cap has its own name now", trim, str(trim))
    ok("auto_trim owns it", "auto_trim.py" in trim, str(trim))
    ok("and it is still 35, so nothing about trimming changed",
       set(trim.values()) == {35.0}, str(trim))


def test_every_exemption_is_real():
    """An allowlist that outlives what it excused becomes a place to hide
    things. Each entry must still name a constant that is actually repeated."""
    stale = [n for n in ALLOWED if len(CONSTS.get(n, {})) < 2]
    ok("no exemption is stale", not stale,
       f"{stale} are no longer defined in two files - drop them from ALLOWED")
    blank = [n for n, why in ALLOWED.items() if not str(why).strip()]
    ok("every exemption carries a reason", not blank, str(blank))


def test_the_hard_limits_are_each_one_number():
    """The owner's stated hard limits, checked by value rather than by name,
    because the whole failure mode is one concept wearing two names."""
    import concentration_gate
    ok("the concentration ceiling is 20%",
       concentration_gate.MAX_SINGLE_COIN_SHARE == 0.2,
       repr(concentration_gate.MAX_SINGLE_COIN_SHARE))
    import auto_trim
    ok("auto_trim measures against THAT, not its own copy",
       auto_trim.LIMIT_PCT == concentration_gate.MAX_SINGLE_COIN_SHARE * 100,
       f"{auto_trim.LIMIT_PCT} vs {concentration_gate.MAX_SINGLE_COIN_SHARE * 100}")
    ok("and the trim cap is a separate, larger number",
       auto_trim.MAX_TRIM_SHARE_OF_POSITION_PCT > auto_trim.LIMIT_PCT,
       "a trim cap below the ceiling could never bring a position back under it")


def test_zz_nothing_above_failed():
    assert not _failures, f"{len(_failures)} checks failed: {_failures}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for t in TESTS:
        print(f"\n== {t.__name__}")
        try:
            t()
        except BaseException as e:
            ok(f"{t.__name__} ran to completion", False,
               f"raised {type(e).__name__}: {e}")
    print()
    if _failures:
        print(f"{len(_failures)} FAILED, {_passes} passed:")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"all {_passes} duplicate-constant checks passed")
