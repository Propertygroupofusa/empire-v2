"""Runs the Live Ops page's own renderers against the REAL payload shapes
the Python side produces.

Why this exists: test_live_ops.py checks that key NAMES appear on both
sides, and that was not enough. It passed while renderCapital() iterated
`venues` as if it were a map keyed by venue name, when capital_census
returns a LIST of per-venue dicts - a panel that would have rendered
nothing on the live page and looked merely empty rather than broken.
Executing the renderer against the producer's actual output is the only
check that catches a shape mismatch.

Needs node on PATH. Skips cleanly (exit 0) if node is absent, so it never
fails a machine that simply has no JS runtime.

Run: python3 test_live_ops_render.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

if not shutil.which("node"):
    print("  SKIP - node not on PATH; the renderers cannot be executed here")
    sys.exit(0)

js = open(os.path.join(HERE, "live_ops_dashboard.html")).read()
js = js.split("<script>")[1].split("</script>")[0]
# Strip the page's own bootstrapping - the renderers are what is under test,
# and tick() would try to fetch().
js = js.replace("tick();", "").replace("setInterval(tick, REFRESH_MS);", "")

# --- payloads, copied from the producing functions' own return statements ---
# capital_census.coinbase_holdings() / alpaca_account()
COINBASE_OK = {"venue": "Coinbase", "status": "OK", "usd_cash": 0.29, "coin_usd": 512.40,
               "coin_balances": {"BTC": 0.00684381}, "unpriced": ["DOT"],
               "source": "X / Y", "note": ""}
ALPACA_OK = {"venue": "Alpaca", "status": "OK", "equity": 936.01, "usd_cash": 936.01,
             "buying_power": 1872.02, "endpoint": "https://api.alpaca.markets",
             "is_live_money": True, "source": "A / B"}
COINBASE_BAD = {"venue": "Coinbase", "status": "UNKNOWN", "reason": "HTTP 401 - bad key"}

CASES = {
    "both venues readable": {"venues": [COINBASE_OK, ALPACA_OK], "verified_usd_cash": 936.30,
                             "venues_unknown": [], "phantom_capital": []},
    "one venue unreadable": {"venues": [COINBASE_BAD, ALPACA_OK], "verified_usd_cash": 936.01,
                             "venues_unknown": ["Coinbase"], "phantom_capital": []},
    "empty census": {"venues": [], "verified_usd_cash": 0.0,
                     "venues_unknown": [], "phantom_capital": []},
}
# crypto_grid_bot.get_grid_status()
GRID = {"branches": [
            {"bot_name": "g1", "product_id": "SOL-USD", "allocated_usd": 70.0, "active": True,
             "locked": False, "grid_pct": 0.012, "num_levels": 4, "open_slices": 2,
             "current_price": 142.5, "drawdown_breached": False,
             "total_unrealized_net_usd": -1.25},
            {"bot_name": "g2", "product_id": "ETH-USD", "allocated_usd": 70.0, "active": True,
             "locked": False, "grid_pct": 0.012, "num_levels": 4, "open_slices": 1,
             "current_price": 3100.0, "drawdown_breached": False,
             "total_unrealized_net_usd": 2.50}],
        "branch_count": 2, "branches_with_open_slices": 2, "total_allocated_usd": 140.0,
        "total_unrealized_net_usd": 1.25, "real_free_cash_usd": None}
# routers.trading_dashboard._live_ops_gate_feed()
GATE = {"events": [{"id": 1, "bot_name": "g1", "product_id": "SOL-USD",
                    "event_type": "GATE_OBSERVE",
                    "message": "net edge +0.4%; WOULD-HAVE-VETOED: tape leans against this long",
                    "created_at": "2026-09-24T20:00:00"}],
        "tally_24h": {"GATE_PASS": 3, "GATE_BLOCK": 1, "GATE_OBSERVE": 2, "GATE_ERROR": 0},
        "last_decision_at": "2026-09-24T20:00:00", "last_decision_age_seconds": 42.0}


# --- runner / pipeline / money payloads ------------------------------------
RUNNER_OK = {"gates": [
    {"name": "Strategy mode is grid_fleet", "ok": True, "detail": "grid_fleet", "fix": "x"},
    {"name": "Trading not halted", "ok": True, "detail": "running", "fix": "y"},
    {"name": "Coinbase credentials present", "ok": True, "detail": "present", "fix": "z"}],
    "all_clear": True, "last_activity_at": "2026-09-24T20:00:00",
    "last_activity_age_seconds": 30.0}
RUNNER_BAD = {"gates": [
    {"name": "Strategy mode is grid_fleet", "ok": False, "detail": "(unset)",
     "fix": "set CRYPTO_STRATEGY_MODE=grid_fleet - bot_runner exits at boot without it"},
    {"name": "Trading not halted", "ok": True, "detail": "running", "fix": "y"},
    {"name": "Coinbase credentials present", "ok": True, "detail": "present", "fix": "z"}],
    "all_clear": False, "last_activity_at": None, "last_activity_age_seconds": None}
GRID_FLEET = {"branches": [], "branch_count": 7, "branches_with_open_slices": 2,
              "total_allocated_usd": 490.0, "total_unrealized_net_usd": -3.10,
              "real_free_cash_usd": 88.0}
GRID_EMPTY = {"branches": [], "branch_count": 0, "branches_with_open_slices": 0,
              "total_allocated_usd": 0.0, "total_unrealized_net_usd": None,
              "real_free_cash_usd": None}
GATE_MIX = {"events": [], "tally_24h": {"GATE_PASS": 4, "GATE_BLOCK": 9,
            "GATE_OBSERVE": 2, "GATE_ERROR": 0}, "last_decision_age_seconds": 50.0}
GATE_ALLBLOCK = {"events": [], "tally_24h": {"GATE_PASS": 0, "GATE_BLOCK": 11,
                 "GATE_OBSERVE": 0, "GATE_ERROR": 0}, "last_decision_age_seconds": 50.0}
GATE_NONE = {"events": [], "tally_24h": {"GATE_PASS": 0, "GATE_BLOCK": 0,
             "GATE_OBSERVE": 0, "GATE_ERROR": 0}, "last_decision_age_seconds": None}
TRADES = {"branches": [], "coins": [{"product_id": "SOL-USD", "trade_count": 40,
          "total_pnl": 12.30, "avg_pnl": 0.31, "wins": 31, "win_rate": 77.5}],
          "recent_trades": [{"product_id": "SOL-USD", "pnl": 0.42, "closed_at": "2026-09-24T19:55:00"},
                            {"product_id": "ETH-USD", "pnl": -0.18, "closed_at": "2026-09-24T19:40:00"}],
          "total_trade_count": 82, "total_realized_pnl": 19.55, "overall_win_rate": 76.0}
TRADES_NONE = {"branches": [], "coins": [], "recent_trades": [], "total_trade_count": 0,
               "total_realized_pnl": 0.0, "overall_win_rate": None}

HARNESS = """
const store = {};
const mk = id => (store[id] = store[id] || {innerHTML:'', textContent:'', className:'',
  classList:{ _o:null,
    add(c){ this._o.className = (this._o.className + ' ' + c).trim(); },
    remove(c){ this._o.className = this._o.className.split(' ').filter(x=>x!==c).join(' '); } }});
const get = id => { const o = mk(id); o.classList._o = o; return o; };
global.document = { getElementById: get };
%(js)s
const CASES=%(cases)s, GRID=%(grid)s, GATE=%(gate)s;
const R_OK=%(r_ok)s, R_BAD=%(r_bad)s, GF=%(gf)s, GE=%(ge)s,
      GM=%(gm)s, GB=%(gb)s, GN=%(gn)s, TR=%(tr)s, TN=%(tn)s;
const out = {};
for (const [name, data] of Object.entries(CASES)) {
  get('capital-kv').innerHTML=''; renderCapital({ok:true, data, error:null});
  out[name] = get('capital-kv').innerHTML;
}
get('capital-kv').innerHTML=''; renderCapital({ok:false, data:null, error:'boom'});
out['section failed'] = get('capital-kv').innerHTML;
get('branches').innerHTML=''; renderBranches({ok:true, data:GRID, error:null});
out['grid'] = get('branches').innerHTML;
renderGate({ok:true, data:GATE, error:null});
out['gate'] = get('gate-events').innerHTML + ' || ' + get('gate-tally').innerHTML;
renderBeat({ok:true, data:GATE, error:null});
out['beat'] = get('beat-state').textContent + ' | ' + get('beat-sub').textContent
              + ' | cls=' + get('beat').className;
renderBeat({ok:true, data:{...GATE, last_decision_age_seconds: 99999}, error:null});
out['beat_stale'] = get('beat-state').textContent + ' | cls=' + get('beat').className;
renderBeat({ok:true, data:{...GATE, last_decision_age_seconds: null}, error:null});
out['beat_never'] = get('beat-state').textContent;
renderBeat({ok:false, data:null, error:'db down'});
out['beat_down'] = get('beat-state').textContent + ' | ' + get('beat-sub').textContent
                   + ' | cls=' + get('beat').className;
get('gate-events').innerHTML='';
renderGate({ok:true, data:{events:[], tally_24h:{}, last_decision_age_seconds:null}, error:null});
out['gate_empty'] = get('gate-events').innerHTML;
get('gate-events').innerHTML='';
renderGate({ok:true, data:{events:[{product_id:'<img src=x onerror=1>',event_type:'GATE_PASS',
  message:'<script>bad()<\\/script>',created_at:'2026-09-24T20:00:00'}], tally_24h:{}}, error:null});
out['xss'] = get('gate-events').innerHTML;
const S=(ok,data,err)=>({ok,data,error:err||null});
renderRunner(S(true,R_OK)); out.runner_ok=get('runner').innerHTML;
renderRunner(S(true,R_BAD)); out.runner_bad=get('runner').innerHTML;
renderRunner(S(false,null,'db gone')); out.runner_err=get('runner').innerHTML;
renderPipeline({runner:S(true,R_OK),grid:S(true,GF),gate:S(true,GM),trades:S(true,TR)});
out.pipe_flow=get('pipe').innerHTML+' ||NOTE|| '+get('pipe-note').textContent;
renderPipeline({runner:S(true,R_BAD),grid:S(true,GF),gate:S(true,GM),trades:S(true,TR)});
out.pipe_runner_blocked=get('pipe-note').textContent;
renderPipeline({runner:S(true,R_OK),grid:S(true,GE),gate:S(true,GN),trades:S(true,TN)});
out.pipe_nobranch=get('pipe-note').textContent;
renderPipeline({runner:S(true,R_OK),grid:S(true,GF),gate:S(true,GN),trades:S(true,TN)});
out.pipe_nodips=get('pipe-note').textContent;
renderPipeline({runner:S(true,R_OK),grid:S(true,GF),gate:S(true,GB),trades:S(true,TN)});
out.pipe_allblocked=get('pipe-note').textContent;
renderPipeline({runner:S(false,null,'x'),grid:S(false,null,'x'),gate:S(false,null,'x'),trades:S(false,null,'x')});
out.pipe_alldead=get('pipe').innerHTML;
renderMoney(S(true,TR)); out.money=get('money-kv').innerHTML+' ||'+get('recent-trades').innerHTML;
renderMoney(S(true,TN)); out.money0=get('money-kv').innerHTML+' ||'+get('recent-trades').innerHTML;
renderMoney(S(false,null,'no db')); out.money_err=get('money-kv').innerHTML;
console.log(JSON.stringify(out));

"""

with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
    fh.write(HARNESS % {"js": js, "cases": json.dumps(CASES),
                        "grid": json.dumps(GRID), "gate": json.dumps(GATE),
                        "r_ok": json.dumps(RUNNER_OK), "r_bad": json.dumps(RUNNER_BAD),
                        "gf": json.dumps(GRID_FLEET), "ge": json.dumps(GRID_EMPTY),
                        "gm": json.dumps(GATE_MIX), "gb": json.dumps(GATE_ALLBLOCK),
                        "gn": json.dumps(GATE_NONE), "tr": json.dumps(TRADES),
                        "tn": json.dumps(TRADES_NONE)})
    harness_path = fh.name
try:
    proc = subprocess.run(["node", harness_path], capture_output=True, text=True)
finally:
    os.unlink(harness_path)
if proc.returncode:
    print("  node failed:\n" + proc.stderr[:1500])
    sys.exit(2)
res = json.loads(proc.stdout)


def text(html):
    return re.sub(r"<[^>]+>", " ", html)


checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


a = text(res["both venues readable"])
ok("Coinbase USD renders its real value", "$0.29" in a)
ok("Coinbase coin value renders", "$512.40" in a)
ok("unpriced coins are named, not silently dropped", "DOT" in a and "could not be priced" in a)
ok("Alpaca equity renders", "$936.01" in a)
ok("Alpaca buying power renders", "$1,872.02" in a)
ok("verified cash renders", "$936.30" in a)

b = text(res["one venue unreadable"])
ok("an unreadable venue says unreadable", "unreadable" in b)
ok("and carries the census's own reason", "401" in b)
ok("and NEVER renders as $0.00", "$0.00" not in b)
ok("the verified total names what it excludes", "excludes Coinbase" in b)

c = text(res["empty census"])
ok("no venues renders no fabricated total",
   "No venue figures returned" in c and "verified" not in c.lower())
ok("a failed section shows its error", "boom" in text(res["section failed"]))

g = text(res["grid"])
ok("a branch renders its coin", "SOL-USD" in g)
ok("slices open render", "2/4 slices" in g)
ok("step renders as a percent", "1.20%" in g)
ok("a losing branch renders -$1.25, not $-1.25", "-$1.25" in g and "$-1.25" not in g)
ok("a winning branch renders +$2.50", "+$2.50" in g)
ok("an unknown free cash renders as a dash, not $0", "—" in g)

gt = text(res["gate"])
ok("a gate event renders its verdict label", "WOULD BLOCK" in gt)
ok("and the reason text", "tape leans against this long" in gt)
ok("the 24h tally renders", "3" in gt and "last 24h" in gt)
ok("an empty feed explains itself", "as soon as a coin dips" in text(res["gate_empty"]))

ok("heartbeat reads live on a fresh decision",
   "Bot is deciding" in res["beat"] and "beat live" in res["beat"])
ok("heartbeat reads quiet on an old decision",
   "Quiet" in res["beat_stale"] and "stale" in res["beat_stale"])
ok("heartbeat distinguishes 'never decided' from 'quiet'",
   "No decisions recorded yet" in res["beat_never"])
ok("heartbeat reads down when it cannot read at all",
   "Cannot read" in res["beat_down"] and "db down" in res["beat_down"] and "down" in res["beat_down"])

ok("event text is escaped, never executed",
   "<script>" not in res["xss"] and "&lt;script&gt;" in res["xss"])
ok("attributes in event data are escaped too", "onerror=1>" not in res["xss"])

ok("all preconditions met reads clear", "Every precondition met" in text(res["runner_ok"]))
ok("a failing precondition is called out", "cannot trade" in text(res["runner_bad"]))
ok("and names the exact fix", "CRYPTO_STRATEGY_MODE=grid_fleet" in text(res["runner_bad"]))
ok("a never-heard-from bot says never, not 0s ago", "never" in text(res["runner_bad"]))
ok("runner section error surfaces", "db gone" in text(res["runner_err"]))

pf = text(res["pipe_flow"])
ok("pipeline shows branches watching", "Branches watching" in pf and " 7 " in pf)
ok("pipeline sums every verdict into dips considered", " 15 " in pf)
ok("pipeline shows buys allowed", "Buys allowed" in pf and " 4 " in pf)
ok("pipeline shows closed round trips", " 82 " in pf)
ok("a fully flowing pipeline says so", "Every stage is moving" in pf)
ok("a blocked runner is named as the stall", "runner is blocked" in res["pipe_runner_blocked"])
ok("no branches is named as the stall", "no branches exist" in res["pipe_nobranch"])
ok("no dips is distinguished from no branches", "no coin has dipped" in res["pipe_nodips"])
ok("all-rejected is explained as the gate working, not a fault",
   "rejected on cost" in res["pipe_allblocked"] and "doing its job" in res["pipe_allblocked"])
ok("unreadable stages render a dash, never 0", text(res["pipe_alldead"]).count("\u2014") >= 5)
ok("and never invent a zero", " 0 " not in text(res["pipe_alldead"]))

m = text(res["money"])
ok("realized P&L renders with its sign", "+$19.55" in m)
ok("round trips render", "82" in m)
ok("win rate renders", "76.0%" in m)
ok("per-coin P&L renders", "SOL-USD" in m and "+$12.30" in m)
ok("a losing recent trade renders negative", "-$0.18" in m)
m0 = text(res["money0"])
ok("no trades yet is explained, not shown as a win rate", "No round trip has closed yet" in m0)
ok("and win rate shows a dash rather than 0%", "\u2014" in m0)
ok("money section error surfaces", "no db" in text(res["money_err"]))

# --- the tree page's inert-controls banner ---------------------------------
tree_page = open(os.path.join(HERE, "family_tree_dashboard.html")).read()
_m = re.search(r"function renderTreeLoopBanner\(data\) \{.*?\n\}", tree_page, re.S)
if _m:
    tharness = """
const store={}; global.document={getElementById:id=>(store[id]=store[id]||{innerHTML:''})};
%s
const out={};
renderTreeLoopBanner({family_tree_loop_running:false, crypto_strategy_mode:'grid_fleet'});
out.inert = store['tree-loop-banner'].innerHTML;
renderTreeLoopBanner({family_tree_loop_running:true, crypto_strategy_mode:'family_tree'});
out.live = store['tree-loop-banner'].innerHTML;
renderTreeLoopBanner({});
out.absent = store['tree-loop-banner'].innerHTML;
renderTreeLoopBanner({family_tree_loop_running:false, crypto_strategy_mode:'<img src=x onerror=1>'});
out.xss = store['tree-loop-banner'].innerHTML;
console.log(JSON.stringify(out));
""" % _m.group(0)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(tharness)
        tpath = fh.name
    try:
        tproc = subprocess.run(["node", tpath], capture_output=True, text=True)
    finally:
        os.unlink(tpath)
    if tproc.returncode:
        ok("tree banner harness runs", False)
    else:
        t = json.loads(tproc.stdout)
        ok("under grid_fleet the tree banner warns the controls are inert",
           "inert right now" in t["inert"])
        ok("and names the live mode", "grid_fleet" in t["inert"])
        ok("and says the balances below are still real", "still real" in t["inert"])
        ok("and points at the page that IS live", "/live-ops" in t["inert"])
        ok("under family_tree the banner disappears", t["live"] == "")
        ok("an older server without the field shows nothing (fails safe)", t["absent"] == "")
        ok("the mode string is escaped, never executed",
           "<img" not in t["xss"] and "&lt;img" in t["xss"])
else:
    ok("renderTreeLoopBanner exists on the tree page", False)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
