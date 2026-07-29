#!/usr/bin/env python3
"""
TRADING EMPIRE — FUND-GRADE DASHBOARD v3
Real-time monitoring with bracket order status

Shows:
- Live balance and P&L
- Open positions with TP and SL levels
- Bracket order status per position
- Bot performance
- Control panel
- Equity curve
"""

import os
import json
import time
import math
import urllib.request
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
    FASTAPI_OK = True
except ImportError:
    FASTAPI_OK = False

try:
    from bracket_orders import get_positions
    BRACKET_OK = True
except ImportError:
    BRACKET_OK = False
    def get_positions(): return []

API_KEY          = os.getenv("ALPACA_API_KEY",    "")
SECRET_KEY       = os.getenv("ALPACA_SECRET_KEY", "")
BASE_URL         = os.getenv("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
STARTING_CAPITAL = 500.0


def load_control():
    try:
        return json.load(open("control.json"))
    except Exception:
        return {"live_mode": False, "kill_switch": False,
                "risk_multiplier": 1.0, "max_trades": 6}


def save_control(data):
    try:
        json.dump(data, open("control.json", "w"), indent=2)
        return True
    except Exception:
        return False


def load_metrics():
    try:
        return json.load(open("metrics.json"))
    except Exception:
        return {"balance": STARTING_CAPITAL, "peak": STARTING_CAPITAL,
                "drawdown": 0, "win_rate": 0, "trades": 0,
                "total_pnl": 0, "daily_pnl": 0, "regime": "UNKNOWN",
                "mode": "PAPER", "last_updated": "", "bots": {}}


def get_alpaca_balance():
    try:
        url  = BASE_URL + "/v2/account"
        req  = urllib.request.Request(url, headers={
            "APCA-API-KEY-ID":     API_KEY,
            "APCA-API-SECRET-KEY": SECRET_KEY,
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
        raw_pf      = float(d.get("portfolio_value", STARTING_CAPITAL))
        last_equity = float(d.get("last_equity",     raw_pf))

        paper_baseline = 0.0
        for fname in ["brain_state.json", "orchestrator_state.json"]:
            try:
                s  = json.load(open(fname))
                pb = float(s.get("paper_baseline", 0))
                if pb > 0:
                    paper_baseline = pb
                    break
            except Exception:
                pass

        if paper_baseline > 0:
            profit_made  = raw_pf - paper_baseline
            real_balance = STARTING_CAPITAL + profit_made
            real_balance = max(real_balance, STARTING_CAPITAL * 0.5)
        else:
            real_balance = raw_pf

        return {
            "balance":   round(real_balance, 2),
            "raw":       round(raw_pf, 2),
            "pnl_today": round(raw_pf - last_equity, 2),
        }
    except Exception:
        return {"balance": STARTING_CAPITAL, "raw": 0, "pnl_today": 0}


def load_equity_curve():
    try:
        lines  = open("equity.csv").readlines()[-50:]
        points = []
        for line in lines:
            parts = line.strip().split(",")
            if len(parts) >= 2:
                dt = datetime.fromtimestamp(float(parts[0])).strftime("%m/%d %H:%M")
                points.append({"t": dt, "v": float(parts[1])})
        return points
    except Exception:
        return []


def days_to_100k(balance, rate=2.0):
    try:
        return int(math.ceil(math.log(100000/balance)/math.log(1+rate/100)))
    except Exception:
        return 999


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Trading Empire</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a12;color:#e0e0e0;padding:12px;max-width:480px;margin:0 auto}}
h1{{color:#00ff88;font-size:1.3em;margin-bottom:2px}}
.sub{{color:#555;font-size:.75em;margin-bottom:16px}}
.card{{background:#111118;border:1px solid #1e1e2e;border-radius:14px;padding:16px;margin-bottom:12px}}
.balance{{font-size:2.4em;font-weight:700;color:#00ff88;text-align:center}}
.bal-label{{color:#555;font-size:.8em;text-align:center;margin-top:4px}}
.row{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px}}
.stat{{background:#111118;border:1px solid #1e1e2e;border-radius:12px;padding:12px;text-align:center}}
.stat-val{{font-size:1.2em;font-weight:700}}
.stat-lbl{{font-size:.65em;color:#555;margin-top:3px}}
.green{{color:#00ff88}}.red{{color:#ff4444}}.yellow{{color:#ffcc00}}
.section-title{{color:#555;font-size:.7em;text-transform:uppercase;letter-spacing:1px;margin:16px 0 8px}}
.pos-card{{background:#0d0d1a;border:1px solid #1e1e2e;border-radius:10px;padding:12px;margin-bottom:8px}}
.pos-symbol{{font-size:1em;font-weight:700;color:#fff}}
.pos-pnl{{font-size:.9em;font-weight:700}}
.pos-detail{{font-size:.7em;color:#666;margin-top:4px}}
.bracket-badge{{font-size:.6em;padding:2px 6px;border-radius:10px;background:#1a3a1a;color:#00ff88;margin-left:6px}}
.no-bracket{{background:#3a1a1a;color:#ff4444}}
.progress{{background:#1a1a1a;border-radius:6px;height:4px;margin-top:6px}}
.progress-tp{{height:100%;background:#00ff88;border-radius:6px}}
.progress-sl{{height:100%;background:#ff4444;border-radius:6px}}
.equity-chart{{height:80px;background:#0d0d1a;border-radius:8px;margin-top:8px;display:flex;align-items:flex-end;padding:4px;gap:2px}}
.eq-bar{{flex:1;background:#00ff8844;border-radius:2px 2px 0 0;min-height:2px}}
.control-panel{{background:#111118;border:1px solid #2a1a1a;border-radius:14px;padding:16px;margin-bottom:12px}}
.btn{{width:100%;padding:12px;border-radius:10px;border:none;font-size:.9em;font-weight:600;cursor:pointer;margin-bottom:8px}}
.btn-kill{{background:#ff2222;color:white}}
.btn-resume{{background:#00aa44;color:white}}
.risk-row{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.risk-val{{color:#00ff88;font-weight:700}}
input[type=range]{{width:100%;accent-color:#00ff88;margin:6px 0}}
.timestamp{{color:#333;font-size:.7em;text-align:center;margin-top:12px}}
.halt-banner{{background:#ff2222;color:white;text-align:center;padding:10px;border-radius:10px;font-weight:700;margin-bottom:12px}}
.regime-badge{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:.75em;font-weight:600}}
.regime-TRENDING{{background:#1a3a1a;color:#00ff88}}
.regime-SIDEWAYS{{background:#2a2a1a;color:#ffcc00}}
.regime-VOLATILE{{background:#3a1a1a;color:#ff4444}}
.regime-UNKNOWN{{background:#1a1a2a;color:#666}}
.regime-NEUTRAL{{background:#1a1a2a;color:#888}}
</style>
</head>
<body>
<h1>Trading Empire</h1>
<p class="sub">Refreshes every 30s · {mode}</p>

{kill_banner}

<div class="card">
<div class="balance">${balance:,.2f}</div>
<div class="bal-label">Portfolio · ~{days} days to $100k</div>
<div class="bal-label" style="margin-top:6px">
  Today: <span class="{pnl_class}">${pnl_today:+,.2f}</span>
  &nbsp;|&nbsp;
  Total: <span class="{total_class}">${total_pnl:+,.2f}</span>
</div>
{equity_html}
</div>

<div class="row">
<div class="stat"><div class="stat-val green">{wins}</div><div class="stat-lbl">Wins</div></div>
<div class="stat"><div class="stat-val red">{losses}</div><div class="stat-lbl">Losses</div></div>
<div class="stat"><div class="stat-val">{win_rate:.0f}%</div><div class="stat-lbl">Win Rate</div></div>
</div>

<div class="row">
<div class="stat"><div class="stat-val yellow">{drawdown:.1f}%</div><div class="stat-lbl">Drawdown</div></div>
<div class="stat"><div class="stat-val">{risk_mult:.0f}%</div><div class="stat-lbl">Risk</div></div>
<div class="stat">
  <div class="regime-badge regime-{regime}">{regime}</div>
  <div class="stat-lbl" style="margin-top:4px">Regime</div>
</div>
</div>

<p class="section-title">Open Positions ({pos_count})</p>
{positions_html}

<p class="section-title">Control Panel</p>
<div class="control-panel">
<button class="btn {kill_btn_class}" onclick="toggleKill()">{kill_btn_text}</button>
<div class="risk-row">
  <span style="font-size:.8em;color:#888">Risk Multiplier</span>
  <span class="risk-val" id="riskVal">{risk_display}x</span>
</div>
<input type="range" min="0.1" max="1.5" step="0.1"
  value="{risk_val}" oninput="updateRisk(this.value)"
  onchange="saveRisk(this.value)">
<div style="font-size:.7em;color:#555;margin-top:4px">
  Max Trades: {max_trades} · Mode: {mode}
</div>
</div>

<p class="timestamp">Updated: {timestamp}</p>

<script>
async function toggleKill() {{
  const c = await fetch('/api/control').then(r=>r.json());
  c.kill_switch = !c.kill_switch;
  await fetch('/api/control',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(c)}});
  location.reload();
}}
function updateRisk(val) {{
  document.getElementById('riskVal').textContent = parseFloat(val).toFixed(1)+'x';
}}
async function saveRisk(val) {{
  const c = await fetch('/api/control').then(r=>r.json());
  c.risk_multiplier = parseFloat(val);
  await fetch('/api/control',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(c)}});
}}
</script>
</body>
</html>"""


def build_positions_html(positions):
    if not positions:
        return '<div class="card" style="text-align:center;color:#555;font-size:.85em">No open positions</div>'

    html = ""
    for pos in positions:
        sym         = pos["symbol"]
        pnl         = pos["pnl"]
        pnl_pct     = pos["pnl_pct"]
        entry       = pos["entry"]
        current     = pos["current"]
        has_bracket = pos["has_bracket"]
        mv          = pos["market_value"]

        pnl_color   = "green" if pnl >= 0 else "red"
        bracket_cls = "bracket-badge" if has_bracket else "bracket-badge no-bracket"
        bracket_txt = "BRACKET ✓" if has_bracket else "NO EXIT !"

        # Progress toward TP (1.5%) or SL (-1.0%)
        tp_target = entry * 1.015
        sl_target = entry * 0.990
        tp_range  = tp_target - entry
        progress  = ((current - entry) / tp_range * 100) if tp_range > 0 else 0
        progress  = max(0, min(100, progress))

        html += f'''
<div class="pos-card">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <span class="pos-symbol">{sym}
      <span class="{bracket_cls}">{bracket_txt}</span>
    </span>
    <span class="pos-pnl {pnl_color}">{pnl_pct:+.2f}%</span>
  </div>
  <div class="pos-detail">
    Entry: ${entry:.4f} → Current: ${current:.4f} | P&L: ${pnl:+.2f}
  </div>
  <div class="pos-detail">
    TP: ${entry*1.015:.4f} · SL: ${entry*0.990:.4f} · Value: ${mv:.2f}
  </div>
  <div class="progress">
    <div class="progress-tp" style="width:{progress:.0f}%"></div>
  </div>
</div>'''

    return html


def build_html(acct, metrics, positions, control):
    kill      = control.get("kill_switch",     False)
    live      = control.get("live_mode",       False)
    risk      = control.get("risk_multiplier", 1.0)
    max_t     = control.get("max_trades",      6)
    regime    = metrics.get("regime",          "UNKNOWN")
    balance   = acct["balance"]
    pnl_today = acct["pnl_today"]
    total_pnl = balance - STARTING_CAPITAL
    drawdown  = metrics.get("drawdown", 0) * 100
    wins      = sum(1 for b in metrics.get("bots", {}).values() for _ in range(b.get("wins", 0))) if metrics.get("bots") else 0
    losses    = sum(1 for b in metrics.get("bots", {}).values() for _ in range(b.get("losses", 0) if hasattr(b, 'get') else 0)) if metrics.get("bots") else 0
    total     = wins + losses
    win_rate  = (wins / total * 100) if total > 0 else 0
    equity    = load_equity_curve()

    kill_banner  = '<div class="halt-banner">KILL SWITCH ACTIVE — TRADING HALTED</div>' if kill else ''
    equity_html  = ""
    if equity:
        values = [p["v"] for p in equity]
        mn, mx = min(values), max(values)
        rng    = mx - mn if mx > mn else 1
        bars   = ""
        for v in values[-30:]:
            h = max(4, int((v-mn)/rng*72))
            bars += f'<div class="eq-bar" style="height:{h}px"></div>'
        equity_html = f'<div class="equity-chart">{bars}</div>'

    return HTML.format(
        mode            = "LIVE" if live else "PAPER",
        kill_banner     = kill_banner,
        balance         = balance,
        days            = days_to_100k(balance),
        pnl_today       = pnl_today,
        pnl_class       = "green" if pnl_today >= 0 else "red",
        total_pnl       = total_pnl,
        total_class     = "green" if total_pnl >= 0 else "red",
        equity_html     = equity_html,
        wins            = wins,
        losses          = losses,
        win_rate        = win_rate,
        drawdown        = drawdown,
        risk_mult       = risk * 100,
        regime          = regime,
        pos_count       = len(positions),
        positions_html  = build_positions_html(positions),
        kill_btn_class  = "btn-resume" if kill else "btn-kill",
        kill_btn_text   = "RESUME TRADING" if kill else "EMERGENCY STOP",
        risk_val        = risk,
        risk_display    = f"{risk:.1f}",
        max_trades      = max_t,
        timestamp       = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


if FASTAPI_OK:
    app = FastAPI(title="Trading Empire Dashboard")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        acct      = get_alpaca_balance()
        metrics   = load_metrics()
        positions = get_positions()
        control   = load_control()
        return HTMLResponse(build_html(acct, metrics, positions, control))

    @app.get("/api/status")
    async def api_status():
        return JSONResponse({
            "account":   get_alpaca_balance(),
            "metrics":   load_metrics(),
            "positions": get_positions(),
            "control":   load_control(),
            "timestamp": datetime.now().isoformat(),
        })

    @app.get("/api/control")
    async def get_control():
        return JSONResponse(load_control())

    @app.post("/api/control")
    async def set_control(request: Request):
        data = await request.json()
        return JSONResponse({"ok": save_control(data)})

    @app.get("/api/positions")
    async def api_positions():
        return JSONResponse(get_positions())


def start():
    print("\n" + "="*55)
    print("  TRADING EMPIRE DASHBOARD v3")
    print("  Bracket orders + live position monitoring")
    print("="*55)
    if not FASTAPI_OK:
        print("  Install: pip install fastapi uvicorn")
        return
    port = int(os.getenv("PORT", 8000))
    print(f"  Dashboard: http://0.0.0.0:{port}")
    print(f"  Bracket orders: {'enabled' if BRACKET_OK else 'disabled'}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    start()
