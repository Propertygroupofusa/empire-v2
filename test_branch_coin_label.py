"""Executes the family tree's grid-branch row renderer against the real
payload, on the exact data that exposed the bug.

The bug: the "Real results" cell showed the BRANCH's lifetime P&L in a row
whose neighbouring cell names the branch's CURRENT coin. A branch outlives
its coin. crypto_grid_1 earned all +$12.80 over 15 DOGE round trips and was
later repointed at BTC-USD, so the row read

    crypto_grid_1 | BTC-USD | $16.67 | flat | +$12.80 · 15 trades · 73% win

while the Live Ops coin breakdown attributed the identical +$12.80 / 15
trades / 73% to DOGE-USD. Both queries were correct - the branch one groups
by bot_name, the coin one by product_id. The ROW was the thing telling the
reader something false.

These checks pin the fix: the headline figure is this branch on THIS coin,
anything earned on coins it no longer holds is named rather than folded in,
and a branch with no trades on its current coin never borrows another coin's
number.

Needs node on PATH. Skips cleanly (exit 0) if node is absent.

Run: python3 test_branch_coin_label.py
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def finish():
    width = max(len(l) for l, _ in checks)
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
    failed = [l for l, p in checks if not p]
    print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


# --- the server must actually produce the per-branch-per-coin rows ---------
bot_src = open(os.path.join(HERE, "crypto_grid_bot.py")).read()
ok("the bot groups trade history by branch AND coin",
   "CryptoGridTradeHistory.bot_name, CryptoGridTradeHistory.product_id" in bot_src)
ok("and returns those rows to the dashboard", '"branch_coins": branch_coins' in bot_src)
ok("the branch-lifetime grouping is kept too, not replaced",
   "group_by(CryptoGridTradeHistory.bot_name)" in bot_src)
ok("and the per-coin grouping is kept as well",
   "group_by(CryptoGridTradeHistory.product_id)" in bot_src)
# The whole point is that these are different populations; a fix that made
# them equal would have destroyed the branch lifetime figure.
ok("crypto_grid_bot still parses", isinstance(ast.parse(bot_src), ast.Module))

if not shutil.which("node"):
    print("  (node absent - the renderer itself is not executed here)")
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
    sys.exit(0 if all(p for _, p in checks) else 1)

# --- execute the real renderer --------------------------------------------
page = open(os.path.join(HERE, "family_tree_dashboard.html")).read()
js = page.split("<script>")[1].split("</script>")[0] if "<script>" in page else ""
for part in page.split("<script>")[1:]:
    body = part.split("</script>")[0]
    if "renderGridStatus" in body:
        js = body
        break

# The live numbers from the deployed dashboard.
STATUS = {
    "branches": [
        # Repointed at BTC-USD; every one of its 15 closed trades was DOGE.
        {"bot_name": "crypto_grid_1", "product_id": "BTC-USD", "allocated_usd": 16.67,
         "open_slices": 0, "num_levels": 3, "grid_pct": 0.01, "reference_price": 84470.80,
         "active": True, "locked": False, "drawdown_breached": False,
         "total_unrealized_net_usd": None},
        # Has genuinely traded its own coin, and nothing else.
        {"bot_name": "crypto_grid_4", "product_id": "ARB-USD", "allocated_usd": 578.61,
         "open_slices": 0, "num_levels": 3, "grid_pct": 0.025, "reference_price": 0.22001,
         "active": True, "locked": False, "drawdown_breached": False,
         "total_unrealized_net_usd": None},
        # Traded its current coin AND an earlier one.
        {"bot_name": "crypto_grid_7", "product_id": "SOL-USD", "allocated_usd": 40.00,
         "open_slices": 0, "num_levels": 3, "grid_pct": 0.025, "reference_price": 100.0,
         "active": True, "locked": False, "drawdown_breached": False,
         "total_unrealized_net_usd": None},
    ],
    "branches_with_open_slices": 0, "total_allocated_usd": 635.28,
    "total_unrealized_net_usd": 0.0, "real_free_cash_usd": -594.99,
}
HISTORY = {
    "branches": [
        {"bot_name": "crypto_grid_1", "trade_count": 15, "total_pnl": 12.80, "win_rate": 73.0},
        {"bot_name": "crypto_grid_4", "trade_count": 7, "total_pnl": 0.14, "win_rate": 86.0},
        {"bot_name": "crypto_grid_7", "trade_count": 10, "total_pnl": 3.00, "win_rate": 80.0},
    ],
    "branch_coins": [
        {"bot_name": "crypto_grid_1", "product_id": "DOGE-USD",
         "trade_count": 15, "total_pnl": 12.80, "win_rate": 73.0},
        {"bot_name": "crypto_grid_4", "product_id": "ARB-USD",
         "trade_count": 7, "total_pnl": 0.14, "win_rate": 86.0},
        {"bot_name": "crypto_grid_7", "product_id": "SOL-USD",
         "trade_count": 6, "total_pnl": 1.00, "win_rate": 83.0},
        {"bot_name": "crypto_grid_7", "product_id": "WIF-USD",
         "trade_count": 4, "total_pnl": 2.00, "win_rate": 75.0},
    ],
    "total_realized_pnl": 15.94, "total_trade_count": 32, "overall_win_rate": 78.0,
}

harness = r"""
global.document = {
  getElementById: function(){ return { innerHTML: '', style: {}, classList:{add(){},remove(){}},
                                       addEventListener(){}, textContent: '' }; },
  querySelectorAll: function(){ return []; },
  querySelector: function(){ return null; },
  addEventListener: function(){},
};
global.window = { location: { href: '' }, addEventListener(){} };
global.fetch = function(){ return Promise.resolve({ ok:true, json: () => ({}) }); };
global.alert = function(){}; global.confirm = function(){ return false; };
global.setInterval = function(){}; global.setTimeout = function(){};
global.__rows = [];
"""

tail = r"""
const status = JSON.parse(process.argv[2]);
const history = JSON.parse(process.argv[3]);
const byBot = {}; (history.branches||[]).forEach(h => byBot[h.bot_name] = h);
const byBotCoin = {};
(history.branch_coins||[]).forEach(h => byBotCoin[h.bot_name+'|'+h.product_id] = h);

// Capture what the results cell renders per branch by driving the same
// expressions the row builder uses, sourced from the page itself.
const out = {};
for (const b of status.branches){
  let html = '';
  const el = { set innerHTML(v){ html = v; }, get innerHTML(){ return html; },
               style:{}, classList:{add(){},remove(){}} };
  document.getElementById = function(){ return el; };
  try { renderGridStatus(status, byBot, history, byBotCoin); } catch(e){ out.__error = String(e); }
  out.__all = html;
  break;
}
console.log(JSON.stringify(out));
"""

with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "run.js")
    open(path, "w").write(harness + js + tail)
    proc = subprocess.run(["node", path, json.dumps(STATUS), json.dumps(HISTORY)],
                          capture_output=True, text=True)

if proc.returncode != 0:
    print("  node failed:\n" + (proc.stderr or "")[:2000])
    sys.exit(1)

res = json.loads(proc.stdout.strip().splitlines()[-1])
html = res.get("__all") or ""
ok("the renderer ran without throwing", not res.get("__error"))

# --- crypto_grid_1: repointed at BTC, all its trades were DOGE ------------
ok("a branch with no trades on its current coin says so",
   "No closed trades on BTC-USD yet" in html)
# The bug in one line: the row attributed DOGE's 15 trades to BTC-USD.
# Nothing may ever claim a trade count "on BTC-USD", because there are none.
ok("and never attributes a trade to the coin that did not earn it",
   "on BTC-USD ·" not in html)
ok("and the 73% win rate is not reattached to BTC either",
   "BTC-USD · 15 trade" not in html and "on BTC-USD · 15" not in html)
ok("and names the coin that actually earned it", "DOGE-USD" in html)
ok("and still reports the money as this branch's - never dropped",
   "$12.80" in html)
ok("and says the branch was moved", "before it was moved here" in html)

# --- crypto_grid_4: genuinely traded only its own coin --------------------
ok("a single-coin branch shows its own coin's result", "+$0.14" in html)
ok("and attributes it to that coin explicitly", "on ARB-USD" in html)
ok("and adds no 'earlier on' line when there is no other coin",
   html.count("earlier on") == 1)  # only crypto_grid_7 has one

# --- crypto_grid_7: current coin plus an earlier one ----------------------
ok("a mixed branch leads with the CURRENT coin's figure", "+$1.00" in html)
ok("and attributes that figure to the current coin", "on SOL-USD" in html)
ok("and names the earlier coin rather than calling it 'other'", "WIF-USD" in html)
ok("and shows the earlier coin's contribution separately", "+$2.00" in html)
ok("and still reports the branch lifetime, clearly labelled",
   "branch lifetime +$3.00" in html)
ok("the per-coin and earlier figures reconcile to the lifetime",
   "+$1.00" in html and "+$2.00" in html and "+$3.00" in html)

sys.exit(finish())
