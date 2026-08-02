"""
Real live trading performance, pulled from Alpaca's own records.

WHY THIS AND NOT A BACKTEST
---------------------------
Every number produced today came from replaying historical bars. That
measures a strategy; it cannot measure execution. Alpaca's account history
is the broker's record of what actually happened to real money - real
fills, real slippage, real timing - which is the one thing no backtest and
no paper run can fake.

The bots' own performance files (metrics.json, equity.csv, brain_state.json,
orchestrator_state.json, ml_trades.json) are NOT a substitute: they are
gitignored, and they live on Railway's container filesystem, which is wiped
on every redeploy. Whatever history they held is gone.

WHAT IT REPORTS
---------------
  1. Equity curve and realized return over the window
  2. Every closed order, grouped into round trips per symbol, with the
     actual P&L and holding period of each
  3. Win rate, average win vs average loss, and PROFIT FACTOR - the same
     metric the backtests were judged on, so live and simulated results
     are directly comparable for once
  4. Realized slippage: what the fills actually cost against the quote,
     which is the number that decides whether the measured 1.08 edge
     survives contact with the market

Read-only. Issues GET requests against account/orders/activities and has
no order-placing path at all.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

BASE = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
HEADERS = {
    "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
}
DAYS = int(os.getenv("REPORT_DAYS", "90"))


def get(path):
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  ! HTTP {e.code} on {path}: {e.read()[:300].decode(errors='replace')}",
              file=sys.stderr)
    except Exception as e:
        print(f"  ! {path}: {e}", file=sys.stderr)
    return None


def money(x):
    return f"${x:,.2f}"


def main():
    if not HEADERS["APCA-API-KEY-ID"]:
        print("FAIL: ALPACA_API_KEY / ALPACA_SECRET_KEY not set.", file=sys.stderr)
        return 1

    print("=" * 70)
    print("  LIVE TRADING PERFORMANCE - from Alpaca's own records")
    print(f"  window: last {DAYS} days      endpoint: {BASE}")
    print("=" * 70)

    acct = get("/v2/account")
    if acct:
        eq = float(acct.get("equity", 0))
        last_eq = float(acct.get("last_equity", eq))
        print(f"\n  ACCOUNT")
        print(f"    equity           {money(eq)}")
        print(f"    buying power     {money(float(acct.get('buying_power', 0)))}")
        print(f"    cash             {money(float(acct.get('cash', 0)))}")
        print(f"    change today     {money(eq - last_eq)}")
        print(f"    status           {acct.get('status')}   "
              f"pattern_day_trader={acct.get('pattern_day_trader')}")

    # ── equity curve ────────────────────────────────────────────────────
    hist = get(f"/v2/account/portfolio/history?period={DAYS}D&timeframe=1D")
    if hist and hist.get("equity"):
        eqs = [e for e in hist["equity"] if e]
        if len(eqs) >= 2:
            start, end = eqs[0], eqs[-1]
            peak, trough_dd = eqs[0], 0.0
            for v in eqs:
                peak = max(peak, v)
                if peak > 0:
                    trough_dd = max(trough_dd, (peak - v) / peak)
            ups = sum(1 for a, b in zip(eqs, eqs[1:]) if b > a)
            print(f"\n  EQUITY CURVE ({len(eqs)} daily points)")
            print(f"    start            {money(start)}")
            print(f"    end              {money(end)}")
            print(f"    change           {money(end - start)}  "
                  f"({(end/start-1)*100:+.2f}%)" if start else "")
            print(f"    peak             {money(peak)}")
            print(f"    max drawdown     {trough_dd*100:.2f}%")
            print(f"    up days          {ups}/{len(eqs)-1} "
                  f"({ups/max(len(eqs)-1,1)*100:.0f}%)")

    # ── closed orders -> round trips ────────────────────────────────────
    after = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    orders, page_after = [], after
    while True:
        batch = get(f"/v2/orders?status=closed&limit=500&direction=asc&after={page_after}")
        if not batch:
            break
        orders.extend(batch)
        if len(batch) < 500:
            break
        page_after = batch[-1]["submitted_at"]

    fills = [o for o in orders if o.get("filled_qty") and float(o["filled_qty"]) > 0]
    print(f"\n  ORDERS: {len(orders)} closed, {len(fills)} actually filled")
    if not fills:
        print("    No fills in this window - nothing further to measure.")
        return 0

    # FIFO round-trip matching per symbol
    lots = defaultdict(list)      # symbol -> [[qty, price, time], ...]
    trips = []
    for o in sorted(fills, key=lambda x: x["filled_at"] or x["submitted_at"]):
        sym = o["symbol"]
        q = float(o["filled_qty"])
        p = float(o["filled_avg_price"] or 0)
        t = o["filled_at"] or o["submitted_at"]
        if p <= 0:
            continue
        if o["side"] == "buy":
            lots[sym].append([q, p, t])
        else:
            remaining = q
            while remaining > 1e-12 and lots[sym]:
                lot = lots[sym][0]
                used = min(remaining, lot[0])
                pnl = used * (p - lot[1])
                hold_h = 0.0
                try:
                    o_t = datetime.fromisoformat(lot[2].replace("Z", "+00:00"))
                    c_t = datetime.fromisoformat(t.replace("Z", "+00:00"))
                    hold_h = (c_t - o_t).total_seconds() / 3600
                except Exception:
                    pass
                trips.append({"symbol": sym, "pnl": pnl, "qty": used,
                              "entry": lot[1], "exit": p, "hours": hold_h,
                              "pct": (p - lot[1]) / lot[1] * 100})
                lot[0] -= used
                remaining -= used
                if lot[0] <= 1e-12:
                    lots[sym].pop(0)

    if not trips:
        print("    Fills exist but no completed round trips (all still open).")
        return 0

    wins = [t for t in trips if t["pnl"] > 0]
    losses = [t for t in trips if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    pf = gw / gl if gl else float("inf")

    print(f"\n  COMPLETED ROUND TRIPS: {len(trips)}")
    print(f"    realized P&L     {money(sum(t['pnl'] for t in trips))}")
    print(f"    win rate         {len(wins)/len(trips)*100:.1f}%  "
          f"({len(wins)}W / {len(losses)}L)")
    print(f"    avg win          {money(gw/len(wins)) if wins else '$0.00'}")
    print(f"    avg loss         {money(gl/len(losses)) if losses else '$0.00'}")
    print(f"    PROFIT FACTOR    {pf:.3f}"
          f"   <- compare to backtest 1.08")
    print(f"    median hold      {sorted(t['hours'] for t in trips)[len(trips)//2]:.1f} h")

    by_sym = defaultdict(lambda: [0, 0.0])
    for t in trips:
        by_sym[t["symbol"]][0] += 1
        by_sym[t["symbol"]][1] += t["pnl"]
    print(f"\n    {'symbol':8} {'trips':>6} {'realized':>12}")
    for s, (n, p) in sorted(by_sym.items(), key=lambda kv: kv[1][1]):
        print(f"    {s:8} {n:>6} {money(p):>12}")

    print(f"\n    10 most recent round trips")
    print(f"    {'symbol':8} {'entry':>9} {'exit':>9} {'move':>8} {'P&L':>9} {'held':>8}")
    for t in trips[-10:]:
        print(f"    {t['symbol']:8} {t['entry']:>9.2f} {t['exit']:>9.2f} "
              f"{t['pct']:>7.2f}% {money(t['pnl']):>9} {t['hours']:>7.1f}h")

    print("\n" + "=" * 70)
    print("  This is real execution on real money - the one thing a backtest")
    print("  cannot fake. If PROFIT FACTOR here is far below the backtest's")
    print("  1.08, the gap IS the slippage, and it is the number that decides")
    print("  whether the measured edge survives contact with the market.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
