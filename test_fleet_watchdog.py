"""Force every watchdog alarm against a mock fleet, and prove it fires.

WHY A MOCK AND NOT THE LIVE FEED. Several of these checks cannot be forced
against production without faking money: LOST_FILL needs coin bought with no
slice written, IDLE_PROFIT needs a reachable profitable slice when there are
currently zero, ALPACA_HALT needs a halted account. When the watchdog shipped,
IDLE_PROFIT went out honestly labelled UNVERIFIED for exactly that reason -
and an unverified alarm is a decoration until something makes it fire.

So this serves a fleet whose numbers are chosen to trip one alarm at a time,
and asserts each one really does. The control case matters just as much: a
healthy fleet must produce NO alarms, or every green result below is
meaningless.

The lesson underneath is one that already cost five hours: a protection whose
firing cannot be observed is indistinguishable from one that never fires. That
applies to the watchdog itself.

No network, no credentials - it binds a throwaway HTTP server on localhost.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


def healthy():
    """A fleet with nothing wrong. Every scenario below mutates a copy."""
    return {
        "/api/trading-dashboard/grid-status": {
            "branches": [{
                "product_id": "AAA-USD", "num_levels": 3, "active": True,
                "reference_price": 100.0, "current_price": 100.0, "grid_pct": 0.03,
                "total_unrealized_net_usd": 1.0, "allocated_usd": 300.0,
                "slices": [{"opened_at": "2026-09-29T10:00:00Z", "qty": 1.0,
                            "entry_price": 100.0, "entry_fee_rate": 0.0035,
                            "adopted": False, "slice_state": "ACCOUNTED",
                            "unrealized_net_pct": 0.001}],
            }],
            "fill_mix": {"buy": {"legs": 10}, "sell": {"legs": 5}},
            "heartbeat": {"age_seconds": 20},
            "maker_only_skipped_cycles": {"buy": 1},
            "maker_expiry_drift": {"buy": 1},
            # A fleet closing about once an hour, last close an hour ago.
            "realized_edge": {"available": True,
                              "current": {"closes_per_day": 24.0,
                                          "days_since_last_close": 0.04}},
        },
        "/api/trading-dashboard/live-ops": {
            "gate": {"data": {"events": []}},
            "headline": {"data": {"realized_usd": 100.0}},
        },
        "/api/trading-dashboard/grid-status/invariants": {
            "failed": 0, "headline": "all clear", "checks": [],
        },
        "/api/trading-dashboard/alert-queue": {
            "channel_configured": True, "counts": {"pending": 0, "sent": 5, "failed": 0},
        },
        "/api/trading-dashboard/resting-stops": {
            "uncovered_usd": 0, "uncovered_count": 0, "uncovered": [],
        },
        "/api/trading-dashboard/alpaca-overview": {
            "equity": 2000.0, "equity_floor": 900.0, "buying_power": 800.0,
            "buying_power_floor": 150.0, "buying_power_halted": False,
            "account_blocked": False, "trading_blocked": False,
            "trade_suspended_by_user": False,
        },
        # The real local HEAD, so the control case does not trip DEPLOY_LAG.
        # Hardcoding a commit here made the healthy fixture fail against its
        # own repo - the check was right and the fixture was wrong, which is
        # worth a comment because the instinct is to weaken the check.
        "/health": {"commit": HEAD, "uptime_human": "1h"},
    }


HEAD = (os.popen("git -C %s rev-parse --short HEAD 2>/dev/null"
                 % os.path.dirname(os.path.abspath(__file__))).read().strip()
        or "0000000")

ROUTES = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ROUTES.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "scripts", "fleet_watchdog.py")


def run(routes, prev_snapshot=None):
    """Serve `routes`, run the watchdog once, return its stdout."""
    ROUTES.clear()
    ROUTES.update(routes)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        state = f.name
        if prev_snapshot is not None:
            json.dump(prev_snapshot, f)
    try:
        env = dict(os.environ, EMPIRE_BASE=BASE)
        r = subprocess.run([sys.executable, "-B", SCRIPT, "--state", state],
                           capture_output=True, text=True, env=env, timeout=120)
        return r.stdout + r.stderr
    finally:
        os.unlink(state)


# A baseline snapshot that matches `healthy()`, so delta checks are ARMED.
# Without this every delta check skips and the scenarios below would pass
# vacuously - which is the exact failure mode the first watchdog run had.
BASE_SNAP = {"at": "2026-09-29T10:00:00Z", "buy_legs": 10, "sell_legs": 5,
             "slice_count": 1, "realized": 100.0, "stuck": 0, "inv_failed": 0,
             "uncovered_usd": 0, "cycle_errors": [], "reachable": [],
             "alpaca_equity": 2000.0, "populated": 1, "adopted_nofee_usd": 0.0,
             "full_usd": 0.0}

print("\n-- the control: a healthy fleet must raise NOTHING --")
# THE CONTROL MUST PASS FOR THE RIGHT REASON. The healthy fixture serves the
# real local HEAD so DEPLOY_LAG stays quiet - but if git is unavailable the
# fixture falls back to "0000000" AND the watchdog's own lookup returns "",
# which makes it skip that check entirely. The control would then pass
# because the check never ran, not because it was satisfied. That is a
# vacuous pass, and it is the same defect class this whole file exists to
# catch, so it is asserted rather than assumed.
ok("git resolved, so the control really exercises DEPLOY_LAG",
   HEAD != "0000000",
   "git is unavailable - the DEPLOY_LAG check is SKIPPED, not satisfied, and "
   "the control below proves nothing about it")

out = run(healthy(), BASE_SNAP)
noisy = [ln for ln in out.splitlines()
         if ln.strip().startswith(("CRITICAL", "WARN"))]
ok("a healthy fleet produces no CRITICAL or WARN", not noisy, "; ".join(noisy))
ok("and says so", "quiet" in out, out)

print("\n-- each alarm, forced one at a time --")

# LOST_FILL: buy legs moved, book did not grow.
d = healthy(); d["/api/trading-dashboard/grid-status"]["fill_mix"]["buy"]["legs"] = 13
ok("LOST_FILL when 3 buys fill and no slice appears", "LOST_FILL" in run(d, BASE_SNAP))

# SELL_NOT_RETIRED: sells filled, book did not shrink.
d = healthy(); d["/api/trading-dashboard/grid-status"]["fill_mix"]["sell"]["legs"] = 8
ok("SELL_NOT_RETIRED when 3 sells fill and the book stays", "SELL_NOT_RETIRED" in run(d, BASE_SNAP))

# REALIZED_FELL: a forced close booked a loss.
d = healthy(); d["/api/trading-dashboard/live-ops"]["headline"]["data"]["realized_usd"] = 92.0
ok("REALIZED_FELL when realized drops", "REALIZED_FELL" in run(d, BASE_SNAP))

# CYCLE_ERROR: any at all.
d = healthy()
d["/api/trading-dashboard/live-ops"]["gate"]["data"]["events"] = [
    {"event_type": "CYCLE_ERROR", "created_at": "2026-09-29T11:00:00",
     "product_id": "AAA-USD", "message": "BoomError: x"}]
ok("CYCLE_ERROR on any cycle error", "CYCLE_ERROR" in run(d, BASE_SNAP))

# ALARM_DEAD: alerts held with nowhere to go.
d = healthy()
d["/api/trading-dashboard/alert-queue"] = {
    "channel_configured": False, "counts": {"pending": 9, "sent": 0, "failed": 0},
    "channel_diagnosis": {"expected_variable": "ALERT_WEBHOOK_URL"}}
ok("ALARM_DEAD when no channel is configured", "ALARM_DEAD" in run(d, BASE_SNAP))

# STALLED: the loop stopped cycling.
d = healthy(); d["/api/trading-dashboard/grid-status"]["heartbeat"]["age_seconds"] = 999
ok("STALLED when the loop stops cycling", "STALLED" in run(d, BASE_SNAP))

# STUCK_ROSE: more inventory below one tradeable unit.
d = healthy()
d["/api/trading-dashboard/grid-status"]["branches"][0]["slices"].append(
    {"opened_at": "2026-09-29T09:00:00Z", "qty": 0.009999999999998899,
     "entry_price": 1.0, "adopted": False, "unrealized_net_pct": 0.0})
ok("STUCK_ROSE when a stuck slice appears", "STUCK_ROSE" in run(d, BASE_SNAP))

# NO_EXIT: coin with no stop from either layer.
d = healthy()
d["/api/trading-dashboard/resting-stops"] = {
    "uncovered_usd": 500.0, "uncovered_count": 2, "uncovered": ["AAA", "BBB"]}
ok("NO_EXIT when coin has no automatic exit", "NO_EXIT" in run(d, BASE_SNAP))

# This exposure is unsold inventory marked to market, so its dollar figure
# moves on price EVERY pass. Escalating on the number alone raised a CRITICAL
# over a $6.54 drift on a $6,187 book in production - noise wearing the
# costume of a trend. The structural change is a new asset joining the set.
SNAP_UNC = dict(BASE_SNAP, uncovered_usd=500.0, uncovered_names=["AAA", "BBB"])

d = healthy()
d["/api/trading-dashboard/resting-stops"] = {
    "uncovered_usd": 506.54, "uncovered_count": 2, "uncovered": ["BBB", "AAA"]}
out = run(d, SNAP_UNC)
ok("NO_EXIT stays a WARN when only the dollar figure moved",
   "WARN     NO_EXIT" in out or "WARN NO_EXIT" in out, out)
ok("and says the change is price only", "price only" in out, out)

d = healthy()
d["/api/trading-dashboard/resting-stops"] = {
    "uncovered_usd": 507.0, "uncovered_count": 3, "uncovered": ["AAA", "BBB", "CCC"]}
out = run(d, SNAP_UNC)
ok("NO_EXIT goes CRITICAL when a NEW asset joins the uncovered set",
   "CRITICAL NO_EXIT" in out.replace("CRITICAL  NO_EXIT", "CRITICAL NO_EXIT"), out)
ok("and names the asset that joined", "CCC JOINED" in out, out)

# Falling exposure with the same assets must not escalate either.
d = healthy()
d["/api/trading-dashboard/resting-stops"] = {
    "uncovered_usd": 400.0, "uncovered_count": 2, "uncovered": ["AAA", "BBB"]}
out = run(d, SNAP_UNC)
noexit_lines = [ln for ln in out.splitlines() if "NO_EXIT" in ln]
ok("and a FALLING figure on the same assets is not a CRITICAL",
   noexit_lines and not any("CRITICAL" in ln for ln in noexit_lines),
   "; ".join(noexit_lines) or "NO_EXIT never reported at all")

# IDLE_PROFIT - the one that shipped UNVERIFIED because production had no
# reachable profitable slice to force it with. Here it does.
d = healthy()
sl = d["/api/trading-dashboard/grid-status"]["branches"][0]["slices"][0]
sl["unrealized_net_pct"] = 0.05           # well clear of the +1.0% floor
snap = dict(BASE_SNAP, reachable=[f"AAA-USD|{sl['opened_at']}"])
ok("IDLE_PROFIT when reachable profit sits across two passes",
   "IDLE_PROFIT" in run(d, snap))

# ADOPTED_BASIS: a "loss" measured against a price nobody paid.
d = healthy()
b = d["/api/trading-dashboard/grid-status"]["branches"][0]
b["total_unrealized_net_usd"] = -100.0
b["slices"][0]["adopted"] = True
ok("ADOPTED_BASIS when an all-adopted branch shows a loss",
   "ADOPTED_BASIS" in run(d, BASE_SNAP))

# SECTION1_SILENT: a newly BOUGHT row with no state written.
d = healthy()
d["/api/trading-dashboard/grid-status"]["branches"][0]["slices"].append(
    {"opened_at": "2026-09-29T12:00:00Z", "qty": 1.0, "entry_price": 100.0,
     "entry_fee_rate": 0.0035, "adopted": False, "slice_state": None,
     "unrealized_net_pct": 0.0})
ok("SECTION1_SILENT when a bought slice carries no state",
   "SECTION1_SILENT" in run(d, BASE_SNAP))

# ADOPTED_GREW - real bought coin quietly rewritten as adopted. The second
# detector for the day's failure, independent of LOST_FILL: it catches the
# consequence arriving on a later pass where no buy leg moved at all.
d = healthy()
d["/api/trading-dashboard/grid-status"]["branches"][0]["slices"].append(
    {"opened_at": "2026-09-29T12:00:00Z", "qty": 10.0, "entry_price": 50.0,
     "entry_fee_rate": None, "adopted": True, "slice_state": None,
     "unrealized_net_pct": 0.0})
ok("ADOPTED_GREW when fee-less adopted book grows",
   "ADOPTED_GREW" in run(d, dict(BASE_SNAP, adopted_nofee_usd=0.0)))

# PARKED_GREW - the deadlock that produced sixteen days of zero closes.
# Parsed off the invariant's own STABLE phrase, never the underwater figure
# beside it, which that text warns swings hundreds of dollars on price alone.
d = healthy()
d["/api/trading-dashboard/grid-status/invariants"]["checks"] = [
    {"name": "no_dead_capital", "status": "FAIL",
     "detail": "$5,000.00 across 12 branch(es) full on their rungs, which "
               "cannot buy at any price until a slice sells."}]
ok("PARKED_GREW when capital full on its rungs grows",
   "PARKED_GREW" in run(d, dict(BASE_SNAP, full_usd=1000.0)))
ok("and it does NOT fire when that capital is flat or falling",
   "PARKED_GREW" not in run(d, dict(BASE_SNAP, full_usd=9000.0)))

# ALPACA_FLOOR / HALT / BP - the half of the account nothing watched.
d = healthy(); d["/api/trading-dashboard/alpaca-overview"]["equity"] = 800.0
ok("ALPACA_FLOOR when equity is under its floor", "ALPACA_FLOOR" in run(d, BASE_SNAP))

d = healthy(); d["/api/trading-dashboard/alpaca-overview"]["trading_blocked"] = True
ok("ALPACA_HALT when trading is blocked", "ALPACA_HALT" in run(d, BASE_SNAP))

d = healthy(); d["/api/trading-dashboard/alpaca-overview"]["buying_power"] = 50.0
ok("ALPACA_BP when buying power is under its floor", "ALPACA_BP" in run(d, BASE_SNAP))

# DEPLOY_LAG: a push that never went live.
d = healthy(); d["/health"] = {"commit": "0000000", "uptime_human": "1h"}
ok("DEPLOY_LAG when the served commit is not local HEAD", "DEPLOY_LAG" in run(d, BASE_SNAP))

# DRY_SPELL: the loop is alive but nothing has closed. This is the ONLY check
# that would have caught the sixteen-day dead period - the heartbeat was fresh
# throughout it, so check 7 called the fleet healthy for sixteen days.
d = healthy()
d["/api/trading-dashboard/grid-status"]["realized_edge"]["current"]["days_since_last_close"] = 0.35
ok("DRY_SPELL at 8.4h on a fleet that closes hourly",
   "DRY_SPELL" in run(d, BASE_SNAP))

d = healthy()
d["/api/trading-dashboard/grid-status"]["realized_edge"]["current"]["days_since_last_close"] = 0.6
out = run(d, BASE_SNAP)
ok("and it escalates to CRITICAL past half a day", "CRITICAL  DRY_SPELL" in out
   or "CRITICAL DRY_SPELL" in out, out)

# The threshold is the fleet's OWN rate, not a fixed number of hours. A fleet
# that closes twice a day is not broken because it went 8 hours without one,
# and an alarm that cannot tell those apart would fire on every slow fleet
# until its reader stopped reading it.
d = healthy()
e = d["/api/trading-dashboard/grid-status"]["realized_edge"]["current"]
e["closes_per_day"] = 2.0
e["days_since_last_close"] = 0.35
ok("but NOT at 8.4h on a fleet that only closes twice a day",
   "DRY_SPELL" not in run(d, BASE_SNAP))

# No rate means no baseline. A dry spell judged against a rate of zero would
# fire every pass; a gap is not a zero, so it must decline to judge and say so.
d = healthy()
d["/api/trading-dashboard/grid-status"]["realized_edge"]["current"]["closes_per_day"] = 0
out = run(d, BASE_SNAP)
ok("an unreadable close rate refuses to judge a dry spell",
   "DRY_SPELL" not in out)
ok("and reports the missing baseline as a gap", "no baseline" in out, out)

d = healthy()
d["/api/trading-dashboard/grid-status"]["realized_edge"] = {"available": False}
out = run(d, BASE_SNAP)
ok("an unavailable realized_edge is a gap, not a quiet pass",
   "realized_edge unavailable" in out, out)

print("\n-- a gap is a finding, never a pass --")
d = healthy(); del d["/api/trading-dashboard/alert-queue"]
out = run(d, BASE_SNAP)
ok("an unreadable feed is reported as a GAP", "GAP" in out, out)

d = {"/health": {"commit": "x"}}   # grid-status itself unreadable
out = run(d, BASE_SNAP)
ok("an unreadable grid-status refuses to judge anything",
   "UNREADABLE" in out, out)

print("\n-- an absent baseline skips delta checks rather than inventing them --")
d = healthy(); d["/api/trading-dashboard/grid-status"]["fill_mix"]["buy"]["legs"] = 99
out = run(d, prev_snapshot=None)
ok("no snapshot means no LOST_FILL claim", "LOST_FILL" not in out)
ok("and it says why", "no previous snapshot" in out, out)

srv.shutdown()
print()
if failures:
    print(f"FAILED: {len(failures)}")
    sys.exit(1)
print("Every watchdog alarm fires when forced, and a healthy fleet raises none.")
