"""The census-deploy watch, as a tool rather than a document.

READ-ONLY. Every call is a GET. It places no order, writes nothing to
the venue, and sets no variable. It reads three endpoints and prints a
verdict against the rollback matrix.

    python3 deploy_watch.py --baseline     # at T+0, before deploying
    python3 deploy_watch.py                # at T+5, T+15, T+30...

WHY A SCRIPT. The matrix is four thresholds and two markers, and every
one of them was written down at some point as an absolute dollar figure
that went stale within the hour. This account accumulates USD from the
grid's own sells with no buys to spend it - 102.50 -> 204.95 -> 234.50
-> 242.13 across 73 minutes on 2026-10-09 - so a threshold typed into a
runbook is wrong by the time the runbook is printed. A baseline captured
by the tool at the moment of deployment cannot be.

THE GAP IS READ, NOT COMPUTED. It is tempting to write
    gap = stable_cash_total - usd
and both of those move. A runbook draft did exactly that against a
stable total four readings old and produced 161.63 against a live
301.26, which is below the $225 threshold and would have ordered a
rollback of a healthy system. The gap IS the USDC balance. One reading,
no subtraction, nothing to go stale.

WHAT IT CANNOT DO. It cannot read Railway's logs, so the exception and
restart conditions are not evaluated here - they are the operator's, and
the known-noise allowlist is printed by --noise for that purpose.
"""

import argparse
import json
import os
import sys
import time
import urllib.request

BASE = ("https://empire-v2-production.up.railway.app"
        "/api/trading-dashboard")
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     ".deploy_watch_baseline.json")

# The matrix. Every threshold here is a MOVEMENT or a STEP, never a
# level - see the module docstring for what levels cost.
DRIFT_LIMIT = 100.0        # free_cash drift from the T+0 baseline
STEP_LIMIT = 225.0         # free_cash single-interval jump
GAP_FLOOR = 225.0          # USDC floor; the leak collapses this
STALE_CHAIN = 3            # consecutive stale reads before SEV-1

# Messages that are present and expected on a healthy system RIGHT NOW.
# Counting these toward "same exception 3+ times in 10 minutes" fires a
# SEV-1 rollback within two minutes of any deployment: measured in the
# 03:27:39-03:37:08 window on 2026-10-09, `[alerts] no route out`
# appeared 13 times and ADOPTED STOP ARMED fired for 13 branches on a
# ~40-second cycle, roughly 150 lines in ten minutes.
KNOWN_NOISE = [
    "[alerts] no route out",
    "ADOPTED STOP ARMED",
    "no buy - wallet",
    "real equity $",                 # the LTC breaker line
    "real rise trigger fired",
    "[CENSUS] accounts HTTP 429 - attempt",
    "Infrastructure healthy",
]


def _get(path, timeout=60):
    req = urllib.request.Request(BASE + path,
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def read_now():
    """One observation. Returns a dict, or raises."""
    census = _get("/account-census")
    money = _get("/grid-status/money-check")
    usd = usdc = None
    for row in (census.get("holdings") or []):
        if row.get("asset") == "USD":
            usd = float(row.get("available_usd") or 0.0)
        elif row.get("asset") == "USDC":
            usdc = float(row.get("available_usd") or 0.0)
    # A missing row is a ZERO balance, not a missing reading - this
    # account has held no USD row at all within the last six hours.
    if usd is None:
        usd = 0.0
    if usdc is None:
        usdc = 0.0
    return {
        "at": census.get("as_of"),
        "wall": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "free_cash_usd": float(money.get("free_cash_usd")),
        "usd": usd,
        "usdc": usdc,
        "stable_total": round(usd + usdc, 2),
        "grid_reserve": float(money.get("idle_usd") or 0.0),
        "allocated": float(money.get("allocated_usd") or 0.0),
        "deployed": float(money.get("deployed_usd") or 0.0),
        # THE DEPLOYMENT FINGERPRINT. census_cached adds both to every
        # response; bare census() adds neither. Present means the new
        # code is live - deterministic, one request, no log reading.
        "markers": sorted(k for k in ("stale", "age_seconds")
                          if k in census),
        "stale": bool(census.get("stale")),
        "age_seconds": census.get("age_seconds"),
        "stale_reason": census.get("stale_reason"),
        "census_available": bool(census.get("available")),
    }


def _fmt(obs):
    return (f"  as_of            {obs['at']}\n"
            f"  free_cash_usd    {obs['free_cash_usd']:>10.2f}\n"
            f"  USD available    {obs['usd']:>10.2f}\n"
            f"  USDC  (= gap)    {obs['usdc']:>10.2f}\n"
            f"  stable total     {obs['stable_total']:>10.2f}\n"
            f"  grid reserve     {obs['grid_reserve']:>10.2f}\n"
            f"  markers          {obs['markers'] or 'ABSENT'}")


def capture():
    obs = read_now()
    obs["stale_streak"] = 0
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(obs, fh, indent=2)
    print("T+0 BASELINE CAPTURED")
    print(_fmt(obs))
    print(f"\n  written to {STATE}")
    print("\n  Deploy now. Then run this again with no arguments.")
    if obs["markers"]:
        print("\n  NOTE: the deployment markers are ALREADY present, so the "
              "new code\n  is already live. A baseline taken now is a "
              "post-deploy reading.")
    return 0


def check():
    if not os.path.exists(STATE):
        print("NO BASELINE. Run --baseline BEFORE deploying.", file=sys.stderr)
        return 2
    with open(STATE, encoding="utf-8") as fh:
        base = json.load(fh)
    prev_fc = base.get("last_free_cash", base["free_cash_usd"])
    streak = int(base.get("stale_streak", 0))

    obs = read_now()
    drift = obs["free_cash_usd"] - base["free_cash_usd"]
    step = obs["free_cash_usd"] - prev_fc
    streak = streak + 1 if obs["stale"] else 0

    print(f"OBSERVATION  (baseline {base['at']})")
    print(_fmt(obs))
    print(f"\n  drift from T+0   {drift:>+10.2f}   limit +/-{DRIFT_LIMIT:.0f}")
    print(f"  step since last  {step:>+10.2f}   limit +{STEP_LIMIT:.0f}")
    if obs["stale"]:
        print(f"  STALE            streak {streak}, age "
              f"{obs['age_seconds']}s, reason {obs['stale_reason']!r}")

    findings = []
    if abs(drift) > DRIFT_LIMIT:
        findings.append(("SEV-1", f"free_cash drift {drift:+.2f} exceeds "
                                  f"+/-{DRIFT_LIMIT:.0f} from T+0 - confirm "
                                  f"against fills before rolling back"))
    if step > STEP_LIMIT:
        findings.append(("SEV-1", f"free_cash STEP +{step:.2f} in one "
                                  f"interval, over +{STEP_LIMIT:.0f}. This "
                                  f"is the denomination leak's shape."))
    if obs["usdc"] < GAP_FLOOR:
        findings.append(("SEV-1", f"gap (USDC) {obs['usdc']:.2f} below "
                                  f"{GAP_FLOOR:.0f} - stablecoins may be "
                                  f"counting as USD"))
    if streak >= STALE_CHAIN:
        findings.append(("SEV-1", f"{streak} consecutive stale census reads "
                                  f"- venue refusing AND cache not "
                                  f"refreshing"))
    if not obs["census_available"]:
        findings.append(("SEV-2", "census reports available=false with no "
                                  "cached reading to serve"))
    if not obs["markers"]:
        findings.append(("SEV-3", "deployment markers ABSENT - the new code "
                                  "is not active. Do not declare success."))

    print()
    if not findings:
        print("  VERDICT: clear. No rollback condition met.")
    else:
        for sev, why in findings:
            print(f"  {sev}: {why}")
        worst = min(f[0] for f in findings)
        print(f"\n  VERDICT: {worst}"
              + ("  -> ROLLBACK to d546a95" if worst == "SEV-1" else
                 "  -> escalate, do not roll back yet"))

    print("\n  Not checked here (needs Railway logs, operator's to read):")
    print("    - repeated restart cycle        - unexpected order placement")
    print("    - insufficient-funds errors     - unknown exception recurrence")
    print("    Run --noise for the exception allowlist.")

    base["last_free_cash"] = obs["free_cash_usd"]
    base["stale_streak"] = streak
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(base, fh, indent=2)
    return 1 if any(f[0] == "SEV-1" for f in findings) else 0


def noise():
    print("KNOWN NOISE - do NOT count toward 'same exception 3+ in 10 min'.")
    print("Every line below is present on a healthy system right now.\n")
    for pat in KNOWN_NOISE:
        print(f"    {pat}")
    print("\n  Measured 2026-10-09 03:27:39-03:37:08 (ten minutes):")
    print("    [alerts] no route out      13 occurrences")
    print("    ADOPTED STOP ARMED         13 branches x ~15 cycles (~150)")
    print("\n  Count only exceptions NOT matching these.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--baseline", action="store_true",
                    help="capture T+0 before deploying")
    ap.add_argument("--noise", action="store_true",
                    help="print the exception allowlist")
    a = ap.parse_args()
    if a.noise:
        sys.exit(noise())
    sys.exit(capture() if a.baseline else check())
