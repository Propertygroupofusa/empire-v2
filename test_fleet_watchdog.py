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
                # A stop well clear of the slice, and no drawdown breach.
                "stop_pct": 0.10, "stop_daily_vol_pct": 2.0,
                "drawdown_pct": 0.01, "drawdown_breached": False,
                "slices": [{"opened_at": "2026-09-29T10:00:00Z", "qty": 1.0,
                            "entry_price": 100.0, "entry_fee_rate": 0.0035,
                            "adopted": False, "slice_state": "ACCOUNTED",
                            "unrealized_net_pct": 0.001}],
            }],
            "fill_mix": {"buy": {"legs": 10}, "sell": {"legs": 5}},
            "heartbeat": {"age_seconds": 20},
            "maker_only_skipped_cycles": {"buy": 1},
            "maker_expiry_drift": {"buy": 1},
            "real_free_cash_usd": 500.0,
            "real_round_trip_fee_rate": 0.015,
            "maker_only_active": True,
            "order_refusals": {"available": True, "by_product": {},
                               "product_rules_unreadable": []},
            # A fleet closing about once an hour, last close an hour ago.
            "realized_edge": {"available": True,
                              "current": {"closes_per_day": 24.0,
                                          "days_since_last_close": 0.04,
                                          "mean_slice_usd": 42.0}},
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
        "/api/trading-dashboard/account-census": {
            "available": True, "total_usd": 1000.0, "untracked_usd": 0.0,
            "tracked_usd": 1000.0, "tracked_share_pct": 100.0, "holdings": [],
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
        # One winning close, so the loss check runs and finds nothing.
        "/api/trading-dashboard/grid-status/trade-history?limit=1000": {
            "recent_trades": [
                {"id": 1, "product_id": "AAA-USD", "pnl": 0.5,
                 "exit_reason": "profit_target"}],
            "recent_trades_truncated": False,
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
             "full_usd": 0.0, "last_trade_id": 1}

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

# THE THRESHOLD THAT GOVERNS A SLICE DEPENDS ON ITS BRANCH.
#
# The 1.0% parked-sell floor applies only to branches FULL on their rungs.
# A branch with a free rung exits on the grid rise trigger - one grid step.
# TON-USD sat at +1.33% net on a 3.0% step with 2 of 3 rungs filled: real
# profit, nothing reserved, nothing short, and nothing wrong - it was 0.94%
# short of the only trigger that applies to it. Reported against the 1.0%
# floor it read as a stuck sell path.
d = healthy()
b = d["/api/trading-dashboard/grid-status"]["branches"][0]   # 1 slice / 3 levels
sl = b["slices"][0]
sl["unrealized_net_pct"] = 0.0133          # past 1.0%, short of the 3.0% step
snap = dict(BASE_SNAP, reachable=[f"AAA-USD|{sl['opened_at']}"])
out = run(d, snap)
ok("no IDLE_PROFIT when a NON-parked slice is short of its rise trigger",
   "IDLE_PROFIT" not in out, out)

# Same number, parked branch: now the 1.0% floor really does govern it.
d = healthy()
b = d["/api/trading-dashboard/grid-status"]["branches"][0]
b["num_levels"] = 1                        # 1 slice on 1 rung == full
sl = b["slices"][0]
sl["unrealized_net_pct"] = 0.0133
snap = dict(BASE_SNAP, reachable=[f"AAA-USD|{sl['opened_at']}"], full_usd=9000.0)
out = run(d, snap)
ok("IDLE_PROFIT when a PARKED slice past the 1.0% floor sits",
   "IDLE_PROFIT" in out, out)
ok("and it names the threshold that governed it",
   "parked-sell floor at +1.0%" in out, out)

# A non-parked slice past the FULL step is still a real finding.
d = healthy()
sl = d["/api/trading-dashboard/grid-status"]["branches"][0]["slices"][0]
sl["unrealized_net_pct"] = 0.05            # past the 3.0% step
snap = dict(BASE_SNAP, reachable=[f"AAA-USD|{sl['opened_at']}"])
out = run(d, snap)
ok("IDLE_PROFIT still fires past the rise trigger on a non-parked branch",
   "IDLE_PROFIT" in out, out)
ok("and names THAT threshold instead",
   "grid rise trigger at +3.0%" in out, out)

# An unreadable step is UNKNOWN, never "not reachable".
d = healthy()
b = d["/api/trading-dashboard/grid-status"]["branches"][0]
b["grid_pct"] = None
b["slices"][0]["unrealized_net_pct"] = 0.05
ok("an unreadable exit rule is a gap, not a silent skip",
   "exit rule unreadable" in run(d, BASE_SNAP))

# STOP_NEAR - the loss that has not happened yet.
#
# Both of yesterday's stops were visible hours ahead in data the fleet already
# publishes. The threshold is the branch's OWN daily volatility: a slice within
# one average day of its stop can reach it on an ordinary day.
def _stopnear(stop_pct, vol_pct, net, **kw):
    d = healthy()
    b = d["/api/trading-dashboard/grid-status"]["branches"][0]
    b["stop_pct"] = stop_pct
    b["stop_daily_vol_pct"] = vol_pct
    b["slices"][0]["unrealized_net_pct"] = net
    b.update(kw)
    return d

# TON live: worst slice -7.42% against an 8.23% stop on 3.29% daily vol.
out = run(_stopnear(0.0823, 3.292, -0.0742), BASE_SNAP)
ok("STOP_NEAR when a slice is within one average day of its stop",
   "STOP_NEAR" in out, out)
ok("and it names the slice, the stop and the volatility behind it",
   "-7.42%" in out and "8.23% stop" in out and "3.29% daily vol" in out, out)
ok("and it says the comparison warns early rather than implying precision",
   "the real distance is a little larger" in out, out)
ok("and it leaves the stop to the owner",
   "owner's call" in out, out)

# Comfortably clear of the stop: silent.
ok("silent when the slice is more than a day's move from the stop",
   "STOP_NEAR" not in run(_stopnear(0.0823, 3.292, -0.02), BASE_SNAP))

# THE UNIT TRAP. stop_pct is a FRACTION, stop_daily_vol_pct is a PERCENT.
# Reading the volatility as a fraction makes the margin ~the stop itself and
# the check goes quiet on exactly the slices it exists to catch.
out = run(_stopnear(0.0823, 3.292, -0.06), BASE_SNAP)
ok("the volatility is read as a percent, not a fraction",
   "STOP_NEAR" in out,
   "a -6.00% slice is inside 8.23% - 3.29%; reading vol as 0.0329 would hide it")

# A branch with NO stop armed cannot be near one.
ok("no STOP_NEAR on a branch whose stop is not armed",
   "STOP_NEAR" not in run(_stopnear(0.0, 3.292, -0.50), BASE_SNAP))

# Unreadable volatility is UNKNOWN, never "not close".
ok("unreadable daily volatility is a gap",
   "daily volatility unreadable" in run(_stopnear(0.0823, None, -0.50), BASE_SNAP))

# A NEWLY near branch escalates; a standing one does not; and with NO
# baseline there is no transition to claim, so it must not invent one.
d = _stopnear(0.0823, 3.292, -0.0742)
ok("a newly near branch is CRITICAL",
   "CRITICAL STOP_NEAR" in run(d, dict(BASE_SNAP, stop_near=["ZZZ-USD"])))
ok("and a standing one stays a WARN",
   "WARN     STOP_NEAR" in run(d, dict(BASE_SNAP, stop_near=["AAA-USD"])))
ok("and with no baseline it reports the state without claiming it is new",
   "WARN     STOP_NEAR" in run(d, BASE_SNAP))

# DRAWDOWN_BREACHED - a branch that has stopped buying, and nothing said so.
d = healthy()
d["/api/trading-dashboard/grid-status"]["branches"][0].update(
    {"drawdown_pct": 0.2828, "drawdown_breached": True})
out = run(d, BASE_SNAP)
ok("DRAWDOWN_BREACHED when a branch trips its own breaker",
   "DRAWDOWN_BREACHED" in out, out)
ok("and it says buys are paused while sells still work",
   "STOPPED BUYING" in out and "still sell normally" in out, out)
ok("and it does not suggest moving the breaker",
   "is the owner's and is not touched here" in out, out)
ok("a standing breach stays a WARN",
   "WARN     DRAWDOWN_BREACHED" in run(d, dict(BASE_SNAP, breached=["AAA-USD"])), out)

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
#
# Driven off the BRANCH DATA, because that is what the check now reads. It
# used to be regexed out of the invariant's English prose, which meant a
# reworded sentence would silently disable the most valuable alarm here -
# and which reported $3,492.09 live while the branches summed to $3,647.40.
# Basis (qty x entry), never market value: a parked branch's cost does not
# move with price, so a change means a branch joined or left.
d = healthy()
b = d["/api/trading-dashboard/grid-status"]["branches"][0]
b["num_levels"] = 1                      # 1 slice on 1 rung == full
ok("PARKED_GREW when capital full on its rungs grows",
   "PARKED_GREW" in run(d, dict(BASE_SNAP, full_usd=10.0)))
ok("and it does NOT fire when that capital is flat or falling",
   "PARKED_GREW" not in run(d, dict(BASE_SNAP, full_usd=9000.0)))

# THE CROSS-CHECK IS THE BRANCH SET, NOT THE DOLLARS.
#
# The two dollar figures measure different things: no_dead_capital sums each
# branch's allocated_usd (capital committed), this check sums qty x entry over
# its slices (what they cost). Live on 2026-09-30 those ran $155.31 apart over
# the SAME ten branches, both directions per branch - and the old guard called
# that a gap on every pass. What cannot both be true is the branch COUNT.
def _dead_detail(usd, n):
    return {"name": "no_dead_capital", "status": "FAIL",
            "detail": f"${usd} across {n} branch(es) full on their rungs, which "
                      f"cannot buy at any price until a slice sells."}

d2 = healthy()
d2["/api/trading-dashboard/grid-status"]["branches"][0]["num_levels"] = 1
d2["/api/trading-dashboard/grid-status/invariants"]["checks"] = [
    _dead_detail("5,000.00", 12)]
out = run(d2, BASE_SNAP)
ok("a branch COUNT the two sources disagree on is reported as a gap",
   "parked branch COUNT disagrees" in out)
ok("and that gap names both counts, so neither is the silent default",
   "sees 1 branch(es)" in out and "invariant sees 12" in out)

# The live case the old guard got wrong: same branch count, different dollars.
d3 = healthy()
d3["/api/trading-dashboard/grid-status"]["branches"][0]["num_levels"] = 1
d3["/api/trading-dashboard/grid-status/invariants"]["checks"] = [
    _dead_detail("5,000.00", 1)]
out3 = run(d3, dict(BASE_SNAP, full_usd=9000.0))
ok("matching counts with differing dollars is NOT a gap - two measurements",
   "disagrees" not in out3)
ok("but both figures still appear, each named for what it measures",
   "slice cost (qty x entry)" in out3 and "$5,000.00 allocated" in out3)

# And the growth alarm carries both too - a reader who only ever sees
# PARKED_GREW would otherwise never meet the invariant's figure.
out4 = run(d3, dict(BASE_SNAP, full_usd=10.0))
ok("PARKED_GREW names the measurement it grew on",
   "PARKED_GREW" in out4 and "slice cost (qty x entry)" in out4)
ok("and carries the allocated figure alongside it",
   "$5,000.00 allocated" in out4)

# And a fleet with nothing parked says nothing at all about parking.
ok("no PARKED line when nothing is full",
   "PARKED " not in run(healthy(), BASE_SNAP))

# LOSS_CLOSED - the blind spot REALIZED_FELL could not see.
#
# Two stops booked -$7.29 and -$2.89 on 2026-09-29 and the running total went
# UP, because the grid's wins over the same hours covered them. The total-based
# check is blind to that by construction, so this one watches single closes.
def _with_trades(rows, truncated=False):
    d = healthy()
    d["/api/trading-dashboard/grid-status/trade-history?limit=1000"] = {
        "recent_trades": rows, "recent_trades_truncated": truncated}
    return d

WIN = {"id": 1, "product_id": "AAA-USD", "pnl": 0.5,
       "exit_reason": "profit_target"}
TON = {"id": 2, "product_id": "TON-USD", "pnl": -7.29,
       "exit_reason": "stop_loss"}

d = _with_trades([WIN, TON])
out = run(d, BASE_SNAP)
ok("LOSS_CLOSED when a new close books a loss", "LOSS_CLOSED" in out)
ok("and it is CRITICAL when one loss exceeds the mean win",
   "CRITICAL LOSS_CLOSED" in out)
ok("and it names the coin, the amount and the exit route",
   "TON-USD" in out and "-$7.29" in out and "stop_loss" in out)
ok("and it says how much of the edge that handed back",
   "of them" in out)
ok("and it states the owner's rule rather than acting on it",
   "nothing negative realized" in out and "owner's call" in out)

# A loss SMALLER than the mean win is real but not an edge-eater.
d = _with_trades([{"id": 1, "product_id": "AAA-USD", "pnl": 5.0,
                   "exit_reason": "profit_target"},
                  {"id": 2, "product_id": "BBB-USD", "pnl": -0.10,
                   "exit_reason": "stop_loss"}])
out = run(d, BASE_SNAP)
ok("a loss under the mean win is a WARN, not a CRITICAL",
   "WARN     LOSS_CLOSED" in out)

# THE TRANSITION, NOT THE LEVEL. The same loss already seen must stay quiet,
# or every pass for ever re-reports one stop from yesterday.
d = _with_trades([WIN, TON])
ok("a loss already seen last pass does NOT fire again",
   "LOSS_CLOSED" not in run(d, dict(BASE_SNAP, last_trade_id=2)))

# And the total going UP must not suppress it - that is the whole point.
d = _with_trades([WIN, TON])
d["/api/trading-dashboard/live-ops"]["headline"]["data"]["realized_usd"] = 500.0
out = run(d, dict(BASE_SNAP, last_trade_id=1))
ok("a rising realized total does not hide a losing close",
   "LOSS_CLOSED" in out and "REALIZED_FELL" not in out)

# NO BASELINE: no transition exists, so no claim - but an existing loss book
# is still said out loud rather than read as a clean run of wins.
d = _with_trades([WIN, TON])
snap = {k: v for k, v in BASE_SNAP.items() if k != "last_trade_id"}
out = run(d, snap)
ok("no baseline means no new-loss claim", "LOSS_CLOSED" not in out)
ok("but the standing loss book is reported instead", "LOSS_BOOK" in out)
ok("and it says realized P&L is a NET figure", "not a run of wins" in out)

# A CAPPED list makes the count a floor, and it has to say so.
d = _with_trades([WIN, TON], truncated=True)
out = run(d, snap)
ok("a truncated history reports its loss count as a floor",
   "at least - the list is capped" in out)

# GAPS: unreadable is never zero.
d = _with_trades([WIN, {"id": 2, "product_id": "CCC-USD", "pnl": None,
                        "exit_reason": None}])
ok("an unreadable pnl is a gap, not a clean close",
   "unreadable pnl" in run(d, BASE_SNAP))

d = healthy()
d["/api/trading-dashboard/grid-status/trade-history?limit=1000"] = {"recent_trades_truncated": False}
ok("a history with no trade list at all is a gap",
   "recent_trades missing" in run(d, BASE_SNAP))

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

# PARKED_DUST: a branch full on its rungs whose only qualifying slice is too
# small to matter. Measured live on LINK-USD: 0.01 LINK (fourteen cents) was
# the sole slice clearing the exit floor, against ~$91 locked in the branch.
# The escape hatch asked "does any slice clear the floor" and never asked
# "is selling it worth doing", so the branch reported itself escapable while
# staying locked.
d = healthy()
b = d["/api/trading-dashboard/grid-status"]["branches"][0]
b["num_levels"] = 2
b["slices"] = [
    {"opened_at": "2026-09-29T10:00:00Z", "qty": 0.01, "entry_price": 14.34,
     "entry_fee_rate": 0.0035, "adopted": False, "slice_state": "ACCOUNTED",
     "unrealized_net_pct": 0.0134},
    {"opened_at": "2026-09-29T10:00:00Z", "qty": 3.0, "entry_price": 15.21,
     "entry_fee_rate": 0.0035, "adopted": False, "slice_state": "ACCOUNTED",
     "unrealized_net_pct": -0.0447},
]
out = run(d, BASE_SNAP)
ok("PARKED_DUST when the only qualifying slice is worth cents", "PARKED_DUST" in out)
ok("and it contrasts that against what stays locked", "locked in the branch" in out, out)

# The same branch with a REAL qualifying slice is not a finding: this alarm
# must not fire on every parked branch that happens to have a way out.
d2 = healthy()
b2 = d2["/api/trading-dashboard/grid-status"]["branches"][0]
b2["num_levels"] = 2
b2["slices"] = [
    {"opened_at": "2026-09-29T10:00:00Z", "qty": 3.0, "entry_price": 14.34,
     "entry_fee_rate": 0.0035, "adopted": False, "slice_state": "ACCOUNTED",
     "unrealized_net_pct": 0.0134},
    {"opened_at": "2026-09-29T10:00:00Z", "qty": 3.0, "entry_price": 15.21,
     "entry_fee_rate": 0.0035, "adopted": False, "slice_state": "ACCOUNTED",
     "unrealized_net_pct": -0.0447},
]
ok("but NOT when that slice is a real position",
   "PARKED_DUST" not in run(d2, BASE_SNAP))

# And not on a branch that still has room to buy - it is not parked at all.
d3 = healthy()
b3 = d3["/api/trading-dashboard/grid-status"]["branches"][0]
b3["num_levels"] = 9
b3["slices"] = [
    {"opened_at": "2026-09-29T10:00:00Z", "qty": 0.01, "entry_price": 14.34,
     "entry_fee_rate": 0.0035, "adopted": False, "slice_state": "ACCOUNTED",
     "unrealized_net_pct": 0.0134},
]
ok("and not on a branch that can still buy", "PARKED_DUST" not in run(d3, BASE_SNAP))

# PARKED_RETRY_LOOP: the escape sell keeps being refused. From outside this
# looks exactly like a healthy quiet branch, which is why it is read off the
# feed rather than inferred from the branch looking unchanged.
d = healthy()
d["/api/trading-dashboard/live-ops"]["gate"]["data"]["events"] = [
    {"event_type": "PARKED_SELL_NOFILL", "product_id": "LINK-USD",
     "created_at": "2026-09-29T18:00:00", "message": "did not fill"},
    {"event_type": "PARKED_SELL_NOFILL", "product_id": "LINK-USD",
     "created_at": "2026-09-29T18:01:00", "message": "did not fill"},
]
# CANNOT and HAS NOT YET are different failures. A maker-only sell that
# nobody crossed is the mode working - SHIB-USD proved it live: three
# no-fills, then it filled twice, left the full set and realized +$1.65.
# Only a product the venue refuses outright can never fill.
d["/api/trading-dashboard/grid-status"]["order_refusals"] = {
    "available": True,
    "by_product": {"LINK-USD": "REQUEST_BELOW_BASE_INCREMENT: 0.0099 requested"},
    "product_rules_unreadable": []}
out = run(d, BASE_SNAP)
ok("PARKED_RETRY_LOOP when the venue refuses the escape sell outright",
   "PARKED_RETRY_LOOP" in out, out)
ok("and it names the coin", "LINK-USD" in out, out)
ok("and says waiting will not fix it", "does not resolve by waiting" in out, out)

# A REFUSAL ON A BRANCH THAT IS ALSO PHANTOM WANTS A DIFFERENT ANSWER.
#
# QNT-USD reads as a rounding problem: 0.00097323 held against a 0.001 venue
# increment, a shortfall of three-quarters of one cent, on a branch that still
# had a free rung. The obvious conclusion - "its next buy lifts it over the
# minimum and it clears itself" - is false. It claims 0.676 QNT and holds
# 0.14% of that, so clearing the increment only makes the order PLACEABLE; it
# would then be for units that still do not exist.
d3 = healthy()
d3["/api/trading-dashboard/live-ops"]["gate"]["data"]["events"] = [
    {"event_type": "PARKED_SELL_NOFILL", "product_id": "QNT-USD",
     "created_at": "2026-09-29T18:00:00", "message": "did not fill"},
]
d3["/api/trading-dashboard/grid-status"]["order_refusals"] = {
    "available": True,
    "by_product": {"QNT-USD": "BELOW_BASE_INCREMENT: 0.00097323 available"},
    "product_rules_unreadable": []}
d3["/api/trading-dashboard/grid-status/invariants"] = {
    "failed": 1, "headline": "1 broken",
    "checks": [{"name": "coin_tracked_is_held", "status": "FAIL",
                "detail": "short", "short_positions": [
                    {"product_id": "QNT-USD", "tracked": 0.676,
                     "held": 0.00097323, "short_usd": 190.35}]}]}
out = run(d3, BASE_SNAP)
ok("a refused branch that is ALSO phantom is still CRITICAL",
   "CRITICAL PARKED_RETRY_LOOP" in out, out)
ok("and it says raising inventory would NOT fix it",
   "would NOT fix it" in out, out)
ok("and it points at reconciliation as the owner's call",
   "Reconciliation" in out and "owner's call" in out, out)
ok("and it does NOT claim the venue rule alone is the trap",
   "not a shortfall" not in out, out)

# And the plain refusal - branch holds what it claims - keeps its own wording.
ok("a refused branch that is NOT phantom says the venue rule is the trap",
   "not a shortfall" in run(d, BASE_SNAP), out)

# The same no-fills with NO refusal are an unfilled maker order, not a trap.
d2 = healthy()
d2["/api/trading-dashboard/live-ops"]["gate"]["data"]["events"] = [
    {"event_type": "PARKED_SELL_NOFILL", "product_id": "SHIB-USD",
     "created_at": "2026-09-29T18:00:00", "message": "did not fill"},
    {"event_type": "PARKED_SELL_NOFILL", "product_id": "SHIB-USD",
     "created_at": "2026-09-29T18:01:00", "message": "did not fill"},
]
out = run(d2, BASE_SNAP)
ok("but an unrefused product is NOT a critical retry loop",
   "PARKED_RETRY_LOOP" not in out, out)
ok("it is reported as a maker sell still waiting for a taker",
   "PARKED_SELL_WAITING" in out, out)
ok("and that is not a WARN or a CRITICAL",
   not [ln for ln in out.splitlines()
        if "PARKED_SELL_WAITING" in ln and ln.strip().startswith(("WARN", "CRITICAL"))],
   out)

# ORDER_REFUSED: /grid-status publishes the venue's own refusal reasons and
# nothing was reading them. Two live passes were spent inferring from slice
# shapes what this field states outright - LINK requesting 0.009999999999998899
# against a 0.01 increment, QNT holding 0.00097323 of a claimed 0.337991.
d = healthy()
d["/api/trading-dashboard/grid-status"]["order_refusals"] = {
    "available": True,
    "by_product": {"LINK-USD": "REQUEST_BELOW_BASE_INCREMENT: 0.0099999 requested"},
    "product_rules_unreadable": []}
out = run(d, BASE_SNAP)
ok("ORDER_REFUSED when a product cannot place an order", "ORDER_REFUSED" in out)
ok("and the venue's reason is passed through verbatim, not categorised",
   "REQUEST_BELOW_BASE_INCREMENT: 0.0099999 requested" in out, out)

# A TRANSITION MUST SURVIVE A DUPLICATE RUN.
#
# The old rule escalated only when a product was missing from the PREVIOUS
# snapshot, so running the watchdog twice let the second run overwrite the
# first's evidence and report "same products as last pass". The first run
# then owned the only announcement a new refusal would ever get. TIA-USD
# went refused this afternoon and it could not afterwards be shown that its
# escalation had ever fired - and with ALARM_DEAD standing, "announced once"
# means announced to nobody.
import datetime as _dt
_now = _dt.datetime.utcnow()
_fmt = "%Y-%m-%dT%H:%M:%SZ"
_recent = (_now - _dt.timedelta(minutes=5)).strftime(_fmt)
_old = (_now - _dt.timedelta(hours=6)).strftime(_fmt)

SNAP_REF = dict(BASE_SNAP, refused_products=["LINK-USD"],
                refused_since={"LINK-USD": _recent})
out = run(d, SNAP_REF)
ok("a refusal already in the snapshot is STILL critical while it is fresh",
   "CRITICAL" in out and "refused within the last hour" in out,
   "this is the regression: a duplicate run must not consume the transition")
ok("and it shows how long it has been refused", "refused 5m ago" in out, out)

SNAP_OLD = dict(BASE_SNAP, refused_products=["LINK-USD"],
                refused_since={"LINK-USD": _old})
out = run(d, SNAP_OLD)
noisy = [ln for ln in out.splitlines()
         if "ORDER_REFUSED" in ln and ln.strip().startswith("CRITICAL")]
ok("but a refusal standing over an hour settles to a WARN", not noisy, "; ".join(noisy))
ok("and says so", "all standing over an hour" in out, out)
ok("with its age in hours", "refused 6.0h" in out, out)

d2 = healthy()
d2["/api/trading-dashboard/grid-status"]["order_refusals"] = {
    "available": True,
    "by_product": {"LINK-USD": "a", "QNT-USD": "b"},
    "product_rules_unreadable": []}
out = run(d2, SNAP_OLD)
ok("a NEWLY refused product escalates even beside an old one",
   "CRITICAL" in out and "QNT-USD refused within the last hour" in out, out)
ok("and the old one is not relabelled new", "LINK-USD, QNT-USD refused" not in out, out)

# An unreadable stamp is UNKNOWN, and UNKNOWN on a refusal fails loud.
SNAP_BAD = dict(BASE_SNAP, refused_products=["LINK-USD"],
                refused_since={"LINK-USD": "not-a-timestamp"})
ok("an unreadable refusal stamp is treated as fresh, not as settled",
   "CRITICAL" in run(d, SNAP_BAD))

# Unreadable product rules hide refusals, so they are a gap, not silence.
d3 = healthy()
d3["/api/trading-dashboard/grid-status"]["order_refusals"] = {
    "available": True, "by_product": {},
    "product_rules_unreadable": ["FOO-USD"]}
out = run(d3, BASE_SNAP)
ok("unreadable product rules are reported as a gap",
   "would be invisible" in out, out)

d4 = healthy()
d4["/api/trading-dashboard/grid-status"]["order_refusals"] = {"available": False}
out = run(d4, BASE_SNAP)
ok("an unavailable refusal feed is a gap, not a quiet pass",
   "order_refusals unavailable" in out, out)

# The invariant headline is a list of NAMES; the money sits in fields under
# it. Printing only the names hid $898 of coin claimed but not held and $892
# reserved by resting orders for a whole afternoon.
d = healthy()
d["/api/trading-dashboard/grid-status/invariants"] = {
    "failed": 2, "headline": "coin_tracked_is_held; grid_inventory_is_free",
    "checks": [
        {"name": "coin_tracked_is_held", "status": "FAIL", "short_usd": 898.96,
         "short_positions": [{"product_id": "QNT-USD", "short_usd": 178.68},
                             {"product_id": "PEPE-USD", "short_usd": 137.46}]},
        {"name": "grid_inventory_is_free", "status": "FAIL", "locked_usd": 892.22,
         "locked_positions": [{"product_id": "AAA-USD", "locked_pct": 100.0,
                               "locked_usd": 441.96}],
         "unreadable": ["ZZZ-USD"]},
    ]}
out = run(d, BASE_SNAP)
ok("INVENTORY_SHORT carries the dollar figure, not just the name",
   "INVENTORY_SHORT" in out and "898.96" in out, out)
ok("and names the worst positions", "QNT-USD" in out, out)
ok("INVENTORY_LOCKED carries its figure too",
   "INVENTORY_LOCKED" in out and "892.22" in out, out)
ok("an unreadable lock state is a gap, not silence",
   "would be invisible" in out, out)

# Profit that is BOTH reachable and locked is the one worth naming alone.
b = d["/api/trading-dashboard/grid-status"]["branches"][0]
b["slices"] = [{"opened_at": "2026-09-29T10:00:00Z", "qty": 100.0,
                "entry_price": 1.0, "entry_fee_rate": 0.0035, "adopted": False,
                "slice_state": "ACCOUNTED", "unrealized_net_pct": 0.0221}]
out = run(d, BASE_SNAP)
ok("LOCKED_PROFIT when a slice past the floor sits on reserved coin",
   "LOCKED_PROFIT" in out, out)
ok("and it says whose decision freeing it is", "the owner's call" in out, out)

# Not a finding when the coin is free, however profitable the slice is.
d2 = healthy()
d2["/api/trading-dashboard/grid-status/invariants"] = {
    "failed": 1, "headline": "grid_inventory_is_free",
    "checks": [{"name": "grid_inventory_is_free", "status": "FAIL",
                "locked_usd": 10.0,
                "locked_positions": [{"product_id": "AAA-USD", "locked_pct": 3.0,
                                      "locked_usd": 10.0}],
                "unreadable": []}]}
d2["/api/trading-dashboard/grid-status"]["branches"][0]["slices"] = [
    {"opened_at": "2026-09-29T10:00:00Z", "qty": 100.0, "entry_price": 1.0,
     "entry_fee_rate": 0.0035, "adopted": False, "slice_state": "ACCOUNTED",
     "unrealized_net_pct": 0.0221}]
ok("but NOT when only a sliver of the coin is reserved",
   "LOCKED_PROFIT" not in run(d2, BASE_SNAP))

# And not when the locked branch has nothing past the floor - that branch is
# waiting on price, which is a different problem with a different answer.
d3 = healthy()
d3["/api/trading-dashboard/grid-status/invariants"] = {
    "failed": 1, "headline": "grid_inventory_is_free",
    "checks": [{"name": "grid_inventory_is_free", "status": "FAIL",
                "locked_usd": 441.96,
                "locked_positions": [{"product_id": "AAA-USD", "locked_pct": 100.0,
                                      "locked_usd": 441.96}],
                "unreadable": []}]}
d3["/api/trading-dashboard/grid-status"]["branches"][0]["slices"] = [
    {"opened_at": "2026-09-29T10:00:00Z", "qty": 100.0, "entry_price": 1.0,
     "entry_fee_rate": 0.0035, "adopted": False, "slice_state": "ACCOUNTED",
     "unrealized_net_pct": -0.04}]
ok("and not when the locked branch has no profit to reach",
   "LOCKED_PROFIT" not in run(d3, BASE_SNAP))

# UNTRACKED: every other check asks whether a BRANCH is working. None asked
# how much of the wallet has no branch at all - so $2,417.62 of $10,094.55
# sat outside the fleet, $1,497.18 of it Bitcoin, reported by nothing.
d = healthy()
d["/api/trading-dashboard/account-census"] = {
    "available": True, "total_usd": 10094.55, "untracked_usd": 2417.62,
    "tracked_usd": 7676.93, "tracked_share_pct": 76.05, "holdings": []}
out = run(d, BASE_SNAP)
ok("UNTRACKED when a quarter of the wallet has no branch", "UNTRACKED" in out)
ok("and it gives the share, not just the dollars", "24.0%" in out, out)

# Reported by SHARE, not dollars - the figure moves on price every pass, and
# escalating on that is exactly the mistake NO_EXIT made this afternoon.
d2 = healthy()
d2["/api/trading-dashboard/account-census"] = {
    "available": True, "total_usd": 10000.0, "untracked_usd": 200.0,
    "tracked_usd": 9800.0, "tracked_share_pct": 98.0, "holdings": []}
out = run(d2, BASE_SNAP)
noisy = [ln for ln in out.splitlines()
         if "UNTRACKED" in ln and ln.strip().startswith(("WARN", "CRITICAL"))]
ok("but a small remainder is INFO, not a WARN", not noisy, "; ".join(noisy))

d3 = healthy()
d3["/api/trading-dashboard/account-census"] = {"available": False}
ok("an unavailable census is a gap, not a quiet pass",
   "account-census unavailable" in run(d3, BASE_SNAP))

# MAKER_DEPENDENT_STEP: a branch above the ACTIVE floor and below the one
# that would apply if maker-only came off. fee_floor.py exists because a
# target under the round trip means a WINNING trade still loses; the fleet's
# live floor is computed off the maker round trip, so that safety is resting
# on an environment variable. Live it finds BTC-USD at 1.38% against a 1.70%
# taker floor, while maker_only_holds is already a broken invariant.
d = healthy()
d["/api/trading-dashboard/grid-status"]["branches"][0]["grid_pct"] = 0.0138
out = run(d, BASE_SNAP)
ok("MAKER_DEPENDENT_STEP when a step clears only the maker floor",
   "MAKER_DEPENDENT_STEP" in out, out)
ok("and it names the taker floor it fails", "1.70% taker floor" in out, out)

# A step above the taker floor is not a finding - most of the fleet is here.
ok("but NOT when the step clears the taker floor too",
   "MAKER_DEPENDENT_STEP" not in run(healthy(), BASE_SNAP))

# With maker-only OFF the floor is already the taker one, so this particular
# warning has nothing to add - the fleet's own floor covers it.
d2 = healthy()
d2["/api/trading-dashboard/grid-status"]["branches"][0]["grid_pct"] = 0.0138
d2["/api/trading-dashboard/grid-status"]["maker_only_active"] = False
ok("and not when maker-only is already off",
   "MAKER_DEPENDENT_STEP" not in run(d2, BASE_SNAP))

# An unreadable fee rate is UNKNOWN, never a quiet pass.
d3 = healthy()
del d3["/api/trading-dashboard/grid-status"]["real_round_trip_fee_rate"]
ok("an unreadable taker round trip is reported as a gap",
   "cannot judge step safety" in run(d3, BASE_SNAP))

# PHANTOM_BRANCH: holds NONE is categorically different from holds LESS,
# and sorting the short list by dollars hides it. PRIME is $8.95 short and
# holds literally zero; XRP is $104 short and holds 95%. The first can never
# place an order that exists; the second trades fine. Live it finds four -
# QNT, PEPE, TIA, PRIME - which is also exactly the set whose "lock state
# unreadable" gap I had been reporting as an observability quirk for hours.
# A locked percentage cannot be computed against a zero balance.
d = healthy()
d["/api/trading-dashboard/grid-status/invariants"] = {
    "failed": 1, "headline": "coin_tracked_is_held",
    "checks": [{"name": "coin_tracked_is_held", "status": "FAIL",
                "short_usd": 200.0,
                "short_positions": [
                    {"product_id": "GHOST-USD", "tracked": 100.0, "held": 0.0,
                     "short_usd": 9.0},
                    {"product_id": "DRIFT-USD", "tracked": 100.0, "held": 95.0,
                     "short_usd": 191.0}]}]}
out = run(d, BASE_SNAP)
ok("PHANTOM_BRANCH when a branch holds none of what it claims",
   "PHANTOM_BRANCH" in out and "GHOST-USD" in out, out)
ok("and it says they cannot trade at any price",
   "cannot trade at any price" in out, out)
ok("a branch merely SHORT is not called phantom",
   "DRIFT-USD claims" not in out, out)
ok("even though the short one carries far more dollars",
   "INVENTORY_SHORT" in out, out)

# A newly phantom branch escalates; a standing one does not.
SNAP_PH = dict(BASE_SNAP, phantom=["GHOST-USD"])
out = run(d, SNAP_PH)
ok("a standing phantom stays a WARN",
   not [l for l in out.splitlines()
        if "PHANTOM_BRANCH" in l and l.strip().startswith("CRITICAL")], out)
d2 = json.loads(json.dumps(d))
d2["/api/trading-dashboard/grid-status/invariants"]["checks"][0]["short_positions"].append(
    {"product_id": "NEW-USD", "tracked": 50.0, "held": 0.0, "short_usd": 5.0})
out = run(d2, SNAP_PH)
ok("a NEWLY phantom branch escalates to CRITICAL",
   "CRITICAL" in out and "NEW-USD newly so" in out, out)

# A branch holding everything it claims is not a finding at all.
d3 = healthy()
d3["/api/trading-dashboard/grid-status/invariants"] = {
    "failed": 1, "headline": "coin_tracked_is_held",
    "checks": [{"name": "coin_tracked_is_held", "status": "FAIL",
                "short_usd": 1.0,
                "short_positions": [{"product_id": "FINE-USD", "tracked": 100.0,
                                     "held": 99.9, "short_usd": 1.0}]}]}
ok("and a branch holding what it claims is never phantom",
   "PHANTOM_BRANCH" not in run(d3, BASE_SNAP))

# STUCK at any increment, not just 0.01.
#
# grid_sell_residual's float subtraction made two slices one ULP below a
# single tradeable unit - LINK 0.009999999999998899 and PRIME
# 0.00999999999999801, both against 0.01 - and neither can ever be sold at
# any price. That CAUSE is fixed (exact Decimal arithmetic). The detector
# was not: it hardcoded 0.01, so the same defect at QNT's 0.001 increment,
# or at 1.0, would have been invisible. A check that only finds the instance
# that prompted it is a memory of one bug, not a check.
def _stuck_slice(qty):
    return {"opened_at": "2026-09-29T10:00:00Z", "qty": qty, "entry_price": 1.0,
            "entry_fee_rate": 0.0035, "adopted": False,
            "slice_state": "ACCOUNTED", "unrealized_net_pct": 0.0}


d = healthy()
d["/api/trading-dashboard/grid-status"]["branches"][0]["slices"].append(
    _stuck_slice(0.0009999999999998))          # one unit of a 0.001 increment
ok("STUCK_ROSE at an increment the old check never looked at",
   "STUCK_ROSE" in run(d, BASE_SNAP), "0.001-scale residue must be caught")

d = healthy()
d["/api/trading-dashboard/grid-status"]["branches"][0]["slices"].append(
    _stuck_slice(0.9999999999999))             # one unit of a 1.0 increment
ok("and at whole-unit scale too", "STUCK_ROSE" in run(d, BASE_SNAP))

# Residue on a LARGE quantity is harmless - ALGO at 279.4 is 27,940 units
# and misses by 3.6e-14. Only a quantity that IS one unit is fatal.
d = healthy()
d["/api/trading-dashboard/grid-status"]["branches"][0]["slices"].append(
    _stuck_slice(279.4))
ok("but harmless residue on a large quantity is NOT stuck",
   "STUCK_ROSE" not in run(d, BASE_SNAP))

d = healthy()
d["/api/trading-dashboard/grid-status"]["branches"][0]["slices"].append(
    _stuck_slice(0.01))
ok("and a clean single unit is not stuck either",
   "STUCK_ROSE" not in run(d, BASE_SNAP))

# CASH_STARVED / CASH_LOW - the silent stop nothing watched.
#
# A grid earns by buying a rung and selling it higher. When free cash runs
# out it simply stops buying: closes continue until there is nothing left to
# close, then the fleet goes quiet with a healthy heartbeat and no errors -
# the same shape as the sixteen-day deadlock, reached by a different road.
#
# Measured against the fleet's OWN mean slice, never a hardcoded dollar
# figure. GRID_CASH_RESERVE_USD is deliberately NOT baked in: the endpoint
# does not publish it, and inventing a number the system never stated is how
# the parked figure sat $155 wrong for hours.
d = healthy()
d["/api/trading-dashboard/grid-status"]["real_free_cash_usd"] = 30.0   # < 1 slice
out = run(d, BASE_SNAP)
ok("CASH_STARVED when free cash is under one more buy",
   "CASH_STARVED" in out, out)
ok("and it warns the quiet looks healthy", "healthy heartbeat" in out, out)
ok("and admits the real room is smaller than reported",
   "not published" in out, out)

d = healthy()
d["/api/trading-dashboard/grid-status"]["real_free_cash_usd"] = 84.0   # 2 slices
out = run(d, BASE_SNAP)
ok("CASH_LOW at about two more buys", "CASH_LOW" in out, out)
ok("and CASH_STARVED does not also fire", "CASH_STARVED" not in out, out)

# The live fleet has room; that must stay silent.
ok("silent when the fleet has plenty of buying room",
   "CASH_LOW" not in run(healthy(), BASE_SNAP)
   and "CASH_STARVED" not in run(healthy(), BASE_SNAP))

# Unknowns are gaps, never quiet passes.
d = healthy()
del d["/api/trading-dashboard/grid-status"]["real_free_cash_usd"]
ok("unreadable free cash is a gap",
   "cannot tell if the fleet can still buy" in run(d, BASE_SNAP))

d = healthy()
d["/api/trading-dashboard/grid-status"]["realized_edge"]["current"]["mean_slice_usd"] = 0
ok("and an unreadable slice size is a gap, not a division by zero",
   "no baseline for buying room" in run(d, BASE_SNAP))

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
