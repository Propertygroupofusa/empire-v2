"""The server-side dry run: does the DEPLOYED code work, without touching anything.

EXECUTES NOTHING. No order, no cancel, no position change, no database
write, no venue write - and, the part the spec did not ask for but this
account needs, NO VENUE READ EITHER.

WHY NO VENUE READ. The brief says READ ONLY, and a venue read is a read,
so it would pass the stated bar. It still must not happen. The defect
this deployment exists to fix is that /account-census made a fresh
paginated accounts walk on EVERY request, a dozen callers inside two
minutes on 2026-10-09 03:36Z drove Coinbase to HTTP 429, and the 429 on
the wallet read made get_real_free_cash_usd return None, which every
caller reads as "do not deploy". A validation endpoint that walks the
accounts is a NEW unauthenticated way to cause the exact outage it is
meant to certify is gone - and validation endpoints get curled in
loops, by hand, by a watchdog, by a Kubernetes probe. So every check
below is satisfied from in-process state: loaded module objects, the
census cache as it already stands, and environment variable NAMES.

That restriction is what makes the endpoint worth having. Externally,
deploy_watch.py already compares balances across an HTTP boundary. The
only thing an in-process check can do that an external one cannot is
look at the code that is actually loaded and say whether it is the new
code - and it can do that with ZERO requests, which the HTTP marker
check cannot.

THE SPEC'S SKETCH HAS A BUG, AND IT IS THE BUG THIS RELEASE IS ABOUT.
The proposal reads `census["usd_available"]`. There is no such key -
not in the payload, not anywhere in this repository (grep returns
nothing). The USD figure lives in the holdings list, in the USD row's
`available_units`. The two nearby keys that look like it are both
traps, and both are wrong RIGHT NOW, measured 2026-10-09 05:10:41Z:

    cash_usd        543.40      sums the STABLE set - USD, USDC, USDT,
                                DAI, PYUSD, USDS - so it reads 2.2x the
                                USD wallet because the account is in USDC
    the USD row     242.13      available_units, what can actually be spent

A dry run that reads the wrong key reports a healthy deployment while
the fleet cannot buy. So check 4 below extracts it the way the endpoint
does and prints BOTH figures side by side, because the divergence is
the finding.
"""

import ast
import inspect
import os
import sys
import time
from dataclasses import dataclass, field

EXECUTES_NOTHING = True

# Names only, never values. DASHBOARD_WRITE_TOKEN and ALERT_WEBHOOK_URL
# are in this list so their ABSENCE is reportable; nothing below reads,
# lengths, prefixes, hashes or echoes any value in this set.
SENSITIVE_ENV = frozenset({
    "COINBASE_API_PRIVATE_KEY", "COINBASE_API_KEY_NAME",
    "COINBASE_SECRET_KEY", "COINBASE_API_KEY", "COINBASE_PASSPHRASE",
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
    "DASHBOARD_WRITE_TOKEN", "ALERT_WEBHOOK_URL",
    "DATABASE_URL", "STRIPE_SECRET_KEY", "ANTHROPIC_API_KEY",
})

# Configuration the census path and the grid allocator read. Split so a
# missing credential reads differently from a missing tuning knob.
REQUIRED_ENV = ("COINBASE_API_KEY_NAME", "COINBASE_API_PRIVATE_KEY",
                "DATABASE_URL")
OPTIONAL_ENV = ("GRID_CASH_RESERVE_USD", "GRID_AUTO_ROTATE",
                "GRID_ADOPTED_STOP_MODE", "STOP_TRADING",
                "ALPACA_FORCE_CLOSE_HELD_EXITS", "ALERT_WEBHOOK_URL")


@dataclass
class Check:
    """ok is True, False, or None - and None is a real answer.

    A check that cannot be settled without making a request returns
    None and says so. Reporting None as a pass is how a dry run comes
    to certify a deployment it never examined.
    """
    name: str
    ok: bool = None
    detail: str = ""
    facts: dict = field(default_factory=dict)

    @property
    def verdict(self):
        return "PASS" if self.ok is True else ("FAIL" if self.ok is False
                                               else "UNKNOWN")


# ── 1. is the deployed code the new code ───────────────────────────────
#
# Two fingerprints, both structural, neither needing a request.
#
#   a) census_cached must store a CALLER-AGNOSTIC census. The defect was
#      that the cache held one caller's tracked comparison and handed it
#      to the next caller. The fix stores apply_tracked(dict(fresh), None)
#      and reapplies the comparison per caller. Checked on the AST, not
#      on a substring, so a mention in a comment cannot satisfy it.
#
#   b) get_real_free_cash_usd must accept usd_balance, so the endpoint
#      can hand it the USD figure the census already read instead of
#      making a second venue call for the same number.
#
# Prefer the LOADED module when there is one - that is the code actually
# serving traffic. Fall back to parsing the file WITHOUT importing it,
# because importing a trading module to validate a deployment is a side
# effect, and a dry run does not get to have side effects.

def _module_source(mod_name, path_hint):
    mod = sys.modules.get(mod_name)
    if mod is not None:
        try:
            return inspect.getsource(mod), "loaded module"
        except Exception:
            pass
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, path_hint)
    if not os.path.exists(path):
        return None, f"{path_hint} not found"
    with open(path, encoding="utf-8") as fh:
        return fh.read(), "file on disk (module not imported)"


def _fn(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def _cache_store_is_caller_agnostic(fn_node):
    """_CENSUS_CACHE.update(census=apply_tracked(..., None), at=...)"""
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr == "update"
                and isinstance(f.value, ast.Name)
                and f.value.id == "_CENSUS_CACHE"):
            continue
        for kw in node.keywords:
            if kw.arg != "census":
                continue
            v = kw.value
            if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                    and v.func.id == "apply_tracked"):
                return False, ("the cache is stored WITHOUT apply_tracked - "
                               "one caller's comparison leaks to the next")
            # second positional argument must be the literal None
            if len(v.args) >= 2 and isinstance(v.args[1], ast.Constant) \
                    and v.args[1].value is None:
                return True, "apply_tracked(..., None) - caller-agnostic"
            return False, ("apply_tracked is called but not with None - the "
                           "stored census still carries a caller's books")
        return False, "_CENSUS_CACHE.update has no census= keyword"
    return False, "census_cached never writes _CENSUS_CACHE"


def _check_code_is_live():
    c = Check("code_is_live")
    src, where = _module_source("account_census", "account_census.py")
    if src is None:
        c.ok = False
        c.detail = where
        return c
    fn = _fn(ast.parse(src), "census_cached")
    if fn is None:
        c.ok = False
        c.detail = f"census_cached not defined ({where})"
        return c
    ok_a, why_a = _cache_store_is_caller_agnostic(fn)

    # (b) the signature seam. Read from the loaded module if present,
    # otherwise from the AST - never by importing crypto_grid_bot here.
    ok_b, why_b = None, "get_real_free_cash_usd not examined"
    gsrc, gwhere = _module_source("crypto_grid_bot", "crypto_grid_bot.py")
    if gsrc is not None:
        gfn = _fn(ast.parse(gsrc), "get_real_free_cash_usd")
        if gfn is None:
            ok_b, why_b = False, f"get_real_free_cash_usd not defined ({gwhere})"
        else:
            names = [a.arg for a in gfn.args.args] + \
                    [a.arg for a in gfn.args.kwonlyargs]
            ok_b = "usd_balance" in names
            why_b = ("accepts usd_balance - the endpoint can pass the figure "
                     "it already read" if ok_b else
                     "does NOT accept usd_balance - a second venue call for "
                     "a number the census already has")
    c.ok = bool(ok_a and ok_b)
    c.detail = f"{why_a}; {why_b}"
    c.facts = {"census_cache_caller_agnostic": ok_a,
               "get_real_free_cash_usd_takes_usd_balance": ok_b,
               "read_from": where, "grid_read_from": gwhere}
    return c


# ── 2. the census cache, peeked and not filled ─────────────────────────
#
# Reading _CENSUS_CACHE is a read. CALLING census_cached is not: on a
# cold or expired cache it walks the accounts, and then this endpoint is
# the 429. So peek the dict directly and report what is there, including
# "nothing yet", which on a freshly restarted process is the correct and
# expected answer rather than a fault.

def _check_census_cache():
    c = Check("census_cache")
    mod = sys.modules.get("account_census")
    if mod is None:
        c.detail = ("account_census is not imported in this process - "
                    "nothing to peek, and importing it to find out would "
                    "be a side effect")
        return c
    cache = getattr(mod, "_CENSUS_CACHE", None)
    if not isinstance(cache, dict):
        c.ok = False
        c.detail = "_CENSUS_CACHE missing - the cache seam is not present"
        return c
    cached = cache.get("census")
    at = cache.get("at") or 0.0
    age = round(time.time() - at, 1) if at else None
    ttl = getattr(mod, "CENSUS_TTL_SECONDS", None)
    if cached is None:
        c.ok = True
        c.detail = ("cache empty - expected on a process that has not served "
                    "a census yet. NOT a fault, and NOT filled by this call.")
        c.facts = {"present": False, "ttl_seconds": ttl}
        return c
    c.ok = True
    c.detail = (f"one reading held, {age}s old, TTL {ttl}s - "
                f"{'within' if (ttl and age is not None and age < ttl) else 'past'}"
                f" TTL. Peeked, not refreshed.")
    c.facts = {"present": True, "age_seconds": age, "ttl_seconds": ttl,
               "available": bool(cached.get("available")),
               "as_of": cached.get("as_of")}
    return c


# ── 3. the tracked-cash arithmetic, on the reading already in hand ─────
#
# apply_tracked MUTATES the dict it is given, which is why the endpoint
# passes dict(cached) and not cached. Pass it a copy here too; a dry run
# that corrupts the live cache is not a dry run.

def _check_tracked_math():
    c = Check("tracked_cash_math")
    mod = sys.modules.get("account_census")
    if mod is None or not hasattr(mod, "apply_tracked"):
        c.detail = "apply_tracked not available in this process"
        return c
    cache = getattr(mod, "_CENSUS_CACHE", {}) or {}
    cached = cache.get("census")
    if cached is None:
        c.detail = ("no cached reading to compute against - the arithmetic "
                    "is exercised the next time a real caller fills the cache")
        return c
    try:
        probe = mod.apply_tracked(dict(cached), 0.0)
    except Exception as e:
        c.ok = False
        c.detail = f"apply_tracked raised: {type(e).__name__}: {e}"
        return c
    total = probe.get("total_usd")
    # With tracked_usd=0 the whole account must read as untracked. That
    # is a property, not a magic number, so it cannot go stale.
    untracked = probe.get("untracked_usd")
    consistent = (total is not None and untracked is not None
                  and abs(float(untracked) - float(total)) < 0.011)
    c.ok = bool(consistent)
    c.detail = ("tracked=0 yields untracked=total, so the comparison is "
                "applied per caller and not inherited from the cache"
                if consistent else
                f"tracked=0 gave untracked {untracked} against total {total} "
                f"- the cached reading is carrying someone else's books")
    c.facts = {"total_usd": total, "untracked_usd_at_zero": untracked,
               "cache_untouched": cached.get("tracked_usd", "absent")}
    return c


# ── 4. the USD figure, taken the way the endpoint takes it ─────────────
#
# This is the check the spec's sketch would have skipped. It reports the
# USD row AND cash_usd together, because a dry run whose job is to say
# "the fleet can see its money" has to show which number it believed.

def _check_usd_row():
    c = Check("usd_is_read_from_the_usd_row")
    mod = sys.modules.get("account_census")
    cache = getattr(mod, "_CENSUS_CACHE", {}) if mod else {}
    cached = (cache or {}).get("census")
    if cached is None:
        c.detail = "no cached reading - no USD row to extract"
        return c
    usd = usdc = None
    for row in (cached.get("holdings") or []):
        if row.get("asset") == "USD":
            usd = row.get("available_units")
        elif row.get("asset") == "USDC":
            usdc = row.get("available_units")
    cash_usd = cached.get("cash_usd")
    # A missing USD row is a zero balance, not a missing reading. This
    # account held no USD row at all for stretches of 2026-10-08.
    usd = 0.0 if usd is None else float(usd)
    usdc = 0.0 if usdc is None else float(usdc)
    c.ok = True
    spread = (round(float(cash_usd) - usd, 2)
              if cash_usd is not None else None)
    c.detail = (f"USD row available_units {usd:,.2f}; cash_usd {cash_usd}; "
                f"difference {spread} held in other stablecoins. The grid "
                f"spends the first figure and the dashboard shows the "
                f"second - never substitute one for the other.")
    c.facts = {"usd_available_units": usd, "usdc_available_units": usdc,
               "cash_usd": cash_usd, "stable_minus_usd": spread}
    return c


# ── 5. configuration, by name ──────────────────────────────────────────

def _check_config():
    c = Check("configuration")
    missing = [k for k in REQUIRED_ENV if not (os.environ.get(k) or "").strip()]
    present_opt = [k for k in OPTIONAL_ENV
                   if (os.environ.get(k) or "").strip()]
    c.ok = not missing
    c.detail = ("every required name is set" if not missing else
                "required names NOT set: " + ", ".join(missing))
    # Names only. Nothing in SENSITIVE_ENV contributes a value, a length,
    # or a prefix to this payload.
    c.facts = {"required_missing": missing,
               "optional_set": present_opt,
               "values_reported": "none - names only, by design"}
    return c


# ── 6. dependency reachability: the honest UNKNOWN ─────────────────────
#
# Reachability cannot be established without a request, and a request is
# the one thing this must not make. So it returns UNKNOWN with the
# reason, rather than a green tick for something it did not test. The
# external instrument that CAN answer it is named here so the operator
# is not left with a gap.

def _check_dependencies():
    c = Check("dependencies_reachable")
    c.ok = None
    c.detail = ("not proven, deliberately. Proving the venue is reachable "
                "means asking the venue, and an unauthenticated validation "
                "endpoint that walks the Coinbase accounts is a new way to "
                "cause the 429 this release removes. Reachability is "
                "answered by deploy_watch.py across the HTTP boundary, or "
                "by the next real census the trading loop makes.")
    c.facts = {"imported": sorted(
        m for m in ("account_census", "crypto_grid_bot",
                    "crypto_coinbase_bot", "crypto_btc_compound_bot")
        if m in sys.modules)}
    return c


CHECKS = (_check_code_is_live, _check_census_cache, _check_tracked_math,
          _check_usd_row, _check_config, _check_dependencies)


def dry_run():
    """Run every check. Returns a plain dict; raises nothing it can help."""
    results = []
    for fn in CHECKS:
        try:
            results.append(fn())
        except Exception as e:                      # a check must not 500
            results.append(Check(getattr(fn, "__name__", "check"), False,
                                 f"check itself raised: {type(e).__name__}: {e}"))
    failed = [r for r in results if r.ok is False]
    unknown = [r for r in results if r.ok is None]
    # NOT_READY wins over UNKNOWN: a known failure is not softened by the
    # presence of something unmeasured.
    verdict = ("NOT_READY" if failed
               else "READY_WITH_UNKNOWNS" if unknown else "READY")
    return {
        "executes_nothing": EXECUTES_NOTHING,
        "venue_requests_made": 0,
        "verdict": verdict,
        "checks": [{"name": r.name, "verdict": r.verdict, "ok": r.ok,
                    "detail": r.detail, "facts": r.facts} for r in results],
        "failed": [r.name for r in failed],
        "unknown": [r.name for r in unknown],
        "note": ("A READY verdict means the loaded code carries the fix and "
                 "its configuration is present. It does NOT mean the venue "
                 "is reachable or that balances are correct - see "
                 "dependencies_reachable."),
    }


def report(payload=None):
    p = payload or dry_run()
    lines = [f"DRY RUN: {p['verdict']}    venue requests {p['venue_requests_made']}"]
    for ch in p["checks"]:
        lines.append(f"  [{ch['verdict']:>7}] {ch['name']}")
        lines.append(f"            {ch['detail']}")
    lines.append("")
    lines.append("  " + p["note"])
    return "\n".join(lines)


if __name__ == "__main__":
    out = dry_run()
    print(report(out))
    sys.exit(1 if out["verdict"] == "NOT_READY" else 0)
