"""Grid step study at the MEASURED fee, on real hourly candles.

Mirrors the live rules as closely as OHLC allows:
  BUY  when price dips `step` below the branch anchor and a rung is free
  SELL when price rises `step` above the anchor AND some open slice
       clears the round-trip fee - the live bot's sell path picks a
       PROFITABLE slice, so a rise that cannot pay for itself is not a
       trade here either.
  The anchor re-sets to the price of whatever just executed, which is
  what reference_price does on the live branches (ZEC's reference is
  exactly its last real buy).

Conservative where OHLC is ambiguous: within one candle the sell is
tested before the buy, so a bar that could have done both books the
exit rather than inventing an extra round trip.

FEE is the measured 0.9280% round trip from /grid-status/fee-reality -
not the 1.5% the config assumes and not a maker-only ideal.
"""
import json, sys, time, urllib.request, datetime as dt

FEE = 0.009280
LEVELS = 3
ALLOC = 100.0

def run(candles, step):
    slice_usd = ALLOC / LEVELS
    anchor = candles[0][4]          # first close
    slices = []                      # entry prices
    trades, net = 0, 0.0
    wins = 0
    for t, low, high, op, close, vol in candles:
        # --- sell first (conservative) ---
        if slices and high >= anchor * (1 + step):
            px = anchor * (1 + step)
            gains = [(px / e - 1) - FEE for e in slices]
            best = max(range(len(slices)), key=lambda i: gains[i])
            if gains[best] > 0:
                net += slice_usd * gains[best]
                trades += 1
                wins += 1
                slices.pop(best)
                anchor = px
                continue
        # --- then buy ---
        if len(slices) < LEVELS and low <= anchor * (1 - step):
            px = anchor * (1 - step)
            slices.append(px)
            anchor = px
    return {"trades": trades, "net": net, "open": len(slices), "wins": wins}

COINS = ["DOGE-USD", "LINK-USD", "ETH-USD", "XLM-USD",
         "HBAR-USD", "NEAR-USD", "SHIB-USD", "SOL-USD"]
# The PUBLIC exchange host, and a browser User-Agent because Cloudflare
# answers 403 "error code: 1010" to the default urllib agent. Deliberately
# not api.coinbase.com: the trading loop's rate limit lives on the
# authenticated host and starving it is the one cost this study must not
# impose.
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}


def fetch(days=100, gran=3600, path="candles.json"):
    out = {}
    for c in COINS:
        rows, end = [], dt.datetime.now(dt.timezone.utc)
        for _ in range(int(days * 24 / 300) + 1):
            start = end - dt.timedelta(seconds=gran * 300)
            u = (f"https://api.exchange.coinbase.com/products/{c}/candles"
                 f"?granularity={gran}&start={start.isoformat()}&end={end.isoformat()}")
            try:
                with urllib.request.urlopen(
                        urllib.request.Request(u, headers=UA), timeout=25) as r:
                    b = json.loads(r.read().decode())
            except Exception as e:
                print(f"  {c}: {type(e).__name__}", file=sys.stderr)
                break
            if not b:
                break
            rows.extend(b)
            end = start
            time.sleep(0.35)
        out[c] = sorted({r[0]: r for r in rows}.values(), key=lambda r: r[0])
        print(f"  {c:<10} {len(out[c]):>5} candles", file=sys.stderr)
    json.dump(out, open(path, "w"))
    return out


try:
    data = json.load(open("candles.json"))
except FileNotFoundError:
    data = fetch()
steps = [0.015, 0.02, 0.025, 0.03, 0.035, 0.04]
days = 100.0

print(f"{'STEP':>6} | {'TRADES':>7} {'TRADES/DAY':>11} | {'NET $ per $100':>15} {'$/DAY':>8} | {'$/TRADE':>8} | {'STILL OPEN':>10}")
print("-" * 78)
rows = []
for s in steps:
    T = N = O = 0
    for c, rows_c in data.items():
        if len(rows_c) < 100:
            continue
        r = run(rows_c, s)
        T += r["trades"]; N += r["net"]; O += r["open"]
    n_coins = sum(1 for c in data.values() if len(c) >= 100)
    per100 = N / n_coins                       # average branch, $100 each
    rows.append((s, T, per100))
    print(f"{s*100:>5.1f}% | {T:>7} {T/days:>11.2f} | {per100:>15.2f} {per100/days:>8.3f} | "
          f"{(N/T if T else 0):>8.3f} | {O:>10}")
print()
best = max(rows, key=lambda r: r[2])
cur = [r for r in rows if abs(r[0] - 0.03) < 1e-9][0]
print(f"BEST NET: {best[0]*100:.1f}%  (${best[2]:.2f} per $100 over {days:.0f} days)")
print(f"CURRENT : {cur[0]*100:.1f}%  (${cur[2]:.2f} per $100)")
d = best[2] - cur[2]
print(f"DIFFERENCE: ${d:+.2f} per $100 over {days:.0f} days "
      f"({d/cur[2]*100:+.1f}%)" if cur[2] else "")
print(f"TRADE COUNT: {cur[1]} at 3.0% vs {best[1]} at {best[0]*100:.1f}%")
