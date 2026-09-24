#!/usr/bin/env bash
# Live view of the grid fleet: what it is deciding, what it holds, what it has
# actually made. Polls /api/trading-dashboard/live-ops and redraws.
#
#   ./watch_fleet.sh                 # every 20s against production
#   ./watch_fleet.sh 10              # every 10s
#   ./watch_fleet.sh 10 http://localhost:8000
#
# Needs bash, curl and python3. No jq - it is not installed everywhere.
#
# It shows realized profit from CLOSED round trips and nothing else as
# "made". Unrealized value moves with the market and is not money until a
# slice is sold back.

set -uo pipefail

INTERVAL="${1:-20}"
BASE="${2:-https://empire-v2-production.up.railway.app}"
URL="$BASE/api/trading-dashboard/live-ops"
STATE_DIR="${TMPDIR:-/tmp}/watch_fleet.$$"
mkdir -p "$STATE_DIR"
trap 'rm -rf "$STATE_DIR"; printf "\n"; exit 0' INT TERM

# The response is passed as a FILE, not on stdin: `python3 - <<EOF` already
# uses stdin for the script itself, so sys.stdin.read() inside it returns
# nothing. Caught by rendering a fixture before shipping this.
render() {
  python3 - "$STATE_DIR" "$1" <<'PY'
import json, os, sys, datetime

state_dir = sys.argv[1]
raw = open(sys.argv[2]).read()

B="\033[1m"; D="\033[2m"; R="\033[0m"
GRN="\033[32m"; RED="\033[31m"; YEL="\033[33m"; CYN="\033[36m"

def money(v, dp=2):
    return "  --  " if v is None else f"${v:,.{dp}f}"

def signed(v):
    if v is None: return f"{D}  --  {R}"
    c = GRN if v > 0 else RED if v < 0 else ""
    s = "+" if v > 0 else "-" if v < 0 else ""
    return f"{c}{s}${abs(v):,.2f}{R}"

try:
    d = json.loads(raw)
except Exception:
    print(f"{RED}Could not parse the response.{R}")
    print(f"{D}{raw[:300]}{R}")
    sys.exit(0)

def sec(name):
    s = d.get(name) or {}
    return (s.get("ok"), s.get("data"), s.get("error"))

print(f"{B}GRID FLEET{R}  {D}{datetime.datetime.now().strftime('%H:%M:%S')}{R}")
print("=" * 62)

# ---- can it trade -------------------------------------------------------
ok, run, err = sec("runner")
if not ok:
    print(f"{RED}runner: {err}{R}")
else:
    for g in run.get("gates", []):
        mark = f"{GRN}ok {R}" if g["ok"] else f"{RED}BLOCKED{R}"
        print(f"  {mark} {g['name']}: {g['detail']}")
        if not g["ok"]:
            print(f"        {YEL}fix: {g['fix']}{R}")
    age = run.get("last_activity_age_seconds")
    hb = "never" if age is None else (f"{age:.0f}s ago" if age < 120 else f"{age/60:.0f}m ago")
    print(f"  {D}bot last wrote to the log: {hb}{R}")

# ---- money --------------------------------------------------------------
print()
ok, tr, err = sec("trades")
if not ok:
    print(f"{RED}trades: {err}{R}")
else:
    n = tr.get("total_trade_count") or 0
    pnl = tr.get("total_realized_pnl")
    wr = tr.get("overall_win_rate")
    print(f"{B}REALIZED{R}  {signed(pnl)}  from {n} closed round trip(s)"
          + (f"  ·  {wr:.0f}% won" if wr is not None else ""))
    if n == 0:
        print(f"  {D}nothing has been bought AND sold back yet - this is the only{R}")
        print(f"  {D}number that counts as money made{R}")
    for c in (tr.get("coins") or [])[:8]:
        print(f"    {c['product_id']:<10} {signed(c['total_pnl'])}  "
              f"{D}{c['trade_count']} trades · {c['win_rate']:.0f}% won{R}")

# ---- branches -----------------------------------------------------------
print()
ok, gr, err = sec("grid")
if not ok:
    print(f"{RED}branches: {err}{R}")
else:
    bs = gr.get("branches") or []
    holding = sum(1 for b in bs if b.get("open_slices"))
    print(f"{B}BRANCHES{R}  {len(bs)} total · {holding} holding · "
          f"allocated {money(gr.get('total_allocated_usd'))} · "
          f"free {money(gr.get('real_free_cash_usd'))}")
    print(f"  {D}unrealized if sold now: {R}{signed(gr.get('total_unrealized_net_usd'))}")
    for b in sorted(bs, key=lambda x: -(x.get("open_slices") or 0)):
        slices = b.get("open_slices") or 0
        mark = f"{CYN}●{R}" if slices else f"{D}○{R}"
        state = f"{slices}/{b.get('num_levels')} slices" if slices else "flat, waiting for a dip"
        print(f"  {mark} {b['product_id']:<10} {money(b.get('allocated_usd')):>10}  "
              f"{state:<24} {signed(b.get('total_unrealized_net_usd'))}")

# ---- what the gate is deciding -----------------------------------------
print()
ok, ga, err = sec("gate")
if not ok:
    print(f"{RED}gate: {err}{R}")
else:
    t = ga.get("tally_24h") or {}
    print(f"{B}GATE (24h){R}  {GRN}{t.get('GATE_PASS',0)} bought{R} · "
          f"{RED}{t.get('GATE_BLOCK',0)} blocked{R} · "
          f"{CYN}{t.get('GATE_OBSERVE',0)} would-block{R} · "
          f"{t.get('GATE_ERROR',0)} errors")
    evs = ga.get("events") or []
    # Only print decisions not seen on a previous pass, so the screen shows
    # what just happened rather than the same backlog every refresh.
    seen_path = os.path.join(state_dir, "seen")
    seen = set()
    if os.path.exists(seen_path):
        seen = set(open(seen_path).read().split())
    fresh = [e for e in evs if str(e.get("id")) not in seen]
    for e in evs[:40]:
        seen.add(str(e.get("id")))
    open(seen_path, "w").write(" ".join(sorted(seen)))

    show = fresh if fresh else evs[:4]
    if fresh:
        print(f"  {B}new since last refresh:{R}")
    for e in show[:6]:
        kind = e.get("event_type", "")
        col = {"GATE_PASS": GRN, "GATE_BLOCK": RED,
               "GATE_OBSERVE": CYN, "GATE_ERROR": YEL}.get(kind, "")
        label = {"GATE_PASS": "BOUGHT", "GATE_BLOCK": "blocked",
                 "GATE_OBSERVE": "would-block", "GATE_ERROR": "error"}.get(kind, kind)
        msg = (e.get("message") or "")[:74]
        print(f"  {col}{label:<12}{R} {e.get('product_id','?'):<10} {D}{msg}{R}")
    if not evs:
        print(f"  {D}no decisions recorded yet - one is written every time a coin{R}")
        print(f"  {D}dips far enough for the gate to rule on it{R}")

# ---- measurement --------------------------------------------------------
mt = d.get("_metrics") or {}
if mt:
    pnl = mt.get("pnl") or {}
    cap = mt.get("capital") or {}
    print()
    print(f"{B}MEASURED{R}  {D}last {mt.get('window_days')}d · fee {mt.get('fee_round_trip_pct')}% round trip{R}")
    print(f"  gross {signed(pnl.get('gross_pnl'))}   fees {signed(-(pnl.get('fees') or 0)) if pnl.get('fees') is not None else money(None)}"
          f"   net {signed(pnl.get('net_pnl'))}")
    pf = pnl.get("profit_factor")
    pf_s = f"{pf:.2f}" if pf is not None else f"{D}undefined{R}"
    print(f"  round trips {pnl.get('round_trips')}   win {pnl.get('win_rate_pct') if pnl.get('win_rate_pct') is not None else '--'}%"
          f"   profit factor {pf_s}   avg/trade {signed(pnl.get('avg_net_per_trade'))}")
    print(f"  utilization {cap.get('utilization_pct') if cap.get('utilization_pct') is not None else '--'}%"
          f"   velocity {cap.get('velocity_per_day') if cap.get('velocity_per_day') is not None else '--'}x/day"
          f"   {D}{cap.get('reading','')}{R}")
    if pnl.get("note"):
        print(f"  {YEL}{pnl['note']}{R}")
    if pnl.get("profit_factor_note"):
        print(f"  {D}profit factor {pnl['profit_factor_note']}{R}")

# ---- cash ceilings ------------------------------------------------------
ok, ca, err = sec("cash")
if ok and ca:
    print()
    print(f"{B}WALLET{R}  free {money(ca.get('free_cash_usd'))} · "
          f"reserve {money(ca.get('global_reserve_usd'))} · "
          f"allocatable {money(ca.get('allocatable_usd'))}")
    for b in ca.get("bots", []):
        print(f"    {b['bot']:<10} {b['share_pct']:>5.0f}%  may spend {money(b.get('ceiling_usd'))}")
PY
}

printf '\033[?25l'                      # hide cursor while redrawing
trap 'printf "\033[?25h"; rm -rf "$STATE_DIR"; printf "\n"; exit 0' INT TERM

while true; do
  body_file="$STATE_DIR/body.json"
  if curl -sS --max-time 25 -o "$body_file" "$URL" 2>"$STATE_DIR/err"; then
    # The measurement endpoint reads a live price per branch, so it is
    # fetched on a slower beat than the status view and merged in. A
    # failure here costs the MEASURED block, never the whole screen.
    if curl -sS --max-time 30 -o "$STATE_DIR/metrics.json" \
         "$BASE/api/trading-dashboard/live-ops/metrics?window_days=1" 2>/dev/null; then
      python3 - "$body_file" "$STATE_DIR/metrics.json" <<'MERGE' 2>/dev/null || true
import json, sys
try:
    body = json.load(open(sys.argv[1])); m = json.load(open(sys.argv[2]))
    body["_metrics"] = m
    json.dump(body, open(sys.argv[1], "w"))
except Exception:
    pass
MERGE
    fi
    clear
    render "$body_file"
  else
    clear
    printf '\033[31mCould not reach %s\033[0m\n' "$URL"
    printf '\033[2m%s\033[0m\n' "$(cat "$STATE_DIR/err" 2>/dev/null)"
    printf '\033[2mretrying in %ss\033[0m\n' "$INTERVAL"
  fi
  printf '\n\033[2mrefreshing every %ss · ctrl-c to stop\033[0m\n' "$INTERVAL"
  sleep "$INTERVAL"
done
