"""
Target/stop geometry sweep for crypto_coinbase_bot.

WHY
---
The live config is a 1.80% target against a 5% stop. Net of the REAL
round-trip fee (taker 0.40% x2 = 0.80%, not the 0.60% x2 the code assumes)
that is +1.00% per win against -5.80% per loss: a breakeven win rate of
85.3%. Even at zero fees the same geometry needs 73.5%. So the fee is the
smaller half of the problem and the target/stop pair is the larger one -
which makes the pair, not the order type, the thing worth measuring.

The bot's own comments record that TIGHTER stops were already tested and
did worse (2% -> -$35/-$45 vs 5% -> -$3/-$11), because this is
mean-reversion on 5-minute bars and ordinary noise trips a tight stop
before the thesis plays out. So the untested direction is a BIGGER target,
or levels scaled to what BTC/ETH actually do (ATR) rather than fixed
constants.

ANTI-OVERFIT
------------
Same guard as the equity sweep. ~70 configs against one 30-day sample will
produce winners by luck. Selection runs on the first 60% only; the last
40% is untouched until a winner is locked in, then re-run on it. Also
reports how many configs cleared PF 1.0 in-sample and how many of those
survived out-of-sample - a real edge persists far more often than the ~50%
chance would give.

FIDELITY
--------
Imports the real crypto_coinbase_bot for its RSI function and thresholds,
so entries cannot drift from production. Exits mirror run_crypto_cycle:
  target  unrealized_pct >= target
  stop    unrealized_pct <= -stop
  rsi     rsi > RSI_SELL_ABOVE AND unrealized_pct > round-trip fee
          (the real one gates on fees so it can't "take profit" into a loss)

Fees are charged on BOTH legs at the rate passed in, so a "win" that does
not clear costs is recorded as the loss it actually is.

Uses Coinbase's free public candle endpoint - same source the bot itself
uses, no API keys required.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import crypto_coinbase_bot as bot

PAIRS = ["BTC-USD", "ETH-USD"]
GRANULARITY = 300                      # 5-minute, as the bot uses
LOOKBACK_DAYS = int(os.getenv("SWEEP_DAYS", "30"))
STARTING_CASH = float(os.getenv("SWEEP_CASH", "130"))
IN_SAMPLE_FRAC = 0.60

# Real fees from the account's fee tier, not the code's stale default.
TAKER = float(os.getenv("SWEEP_TAKER", "0.004"))
MAKER = float(os.getenv("SWEEP_MAKER", "0.0025"))

TARGET_GRID = [0.010, 0.015, 0.018, 0.025, 0.035, 0.050, 0.070]
STOP_GRID = [0.02, 0.03, 0.05, 0.08, 0.12]
ATR_MULTS = [(1.5, 2.5), (2.0, 3.0), (1.0, 2.0)]   # (stop x ATR, target x ATR)


def fetch_candles(product_id, days):
    """[time, low, high, open, close, volume], oldest first."""
    end = int(time.time())
    start = end - days * 86400
    rows, cursor = [], end
    step = GRANULARITY * 300
    while cursor > start:
        lo = max(start, cursor - step)
        url = (f"https://api.exchange.coinbase.com/products/{product_id}/candles"
               f"?granularity={GRANULARITY}&start={lo}&end={cursor}")
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "empire-v2-sweep"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                rows.extend(json.loads(r.read().decode()))
        except Exception as e:
            print(f"  ! {product_id}: {e}", file=sys.stderr)
        cursor = lo
        time.sleep(0.35)
    dedup = {r[0]: r for r in rows}
    return [dedup[k] for k in sorted(dedup)]


def rsi_series(closes):
    """bot._compute_rsi over a trailing window - no lookahead: index i is
    computed from closes[:i+1] only."""
    out = [None] * len(closes)
    for i in range(len(closes)):
        w = closes[max(0, i - 19):i + 1]
        if len(w) >= 15:
            out[i] = bot._compute_rsi(w)
    return out


def atr_pct_series(candles, period=14):
    """True range as a fraction of close. candle = [t, low, high, open, close, vol]."""
    out = [None] * len(candles)
    trs = []
    for i in range(len(candles)):
        if i == 0:
            trs.append(candles[i][2] - candles[i][1])
            continue
        hi, lo, pc = candles[i][2], candles[i][1], candles[i - 1][4]
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
        if i >= period and candles[i][4] > 0:
            out[i] = (sum(trs[-period:]) / period) / candles[i][4]
    return out


def simulate(data, target_pct, stop_pct, atr_mult, entry_fee, exit_fee):
    """One position at a time per pair, mirroring run_crypto_cycle's gates.
    atr_mult, when given, is (stop_x, target_x) and overrides the fixed pair."""
    cash = STARTING_CASH
    trades = []
    unclosed = 0
    round_trip = entry_fee + exit_fee

    for pair, (candles, rsis, atrs) in data.items():
        pos = None
        for i, c in enumerate(candles):
            price, rsi = c[4], rsis[i]
            if rsi is None:
                continue

            if pos is None:
                if rsi < bot.RSI_BUY_BELOW:
                    size = cash / len(data)
                    if size >= bot.MIN_POSITION_NOTIONAL:
                        pos = {"entry": price, "size": size,
                               "atr": atrs[i] if atrs[i] else None}
                continue

            pct = (price - pos["entry"]) / pos["entry"]

            if atr_mult and pos["atr"]:
                stop_x, tgt_x = atr_mult
                stop_now = max(0.005, min(pos["atr"] * stop_x, 0.20))
                tgt_now = stop_now * (tgt_x / stop_x)
            else:
                stop_now, tgt_now = stop_pct, target_pct

            target_hit = pct >= tgt_now
            stop_hit = pct <= -stop_now
            # production gates the RSI exit on clearing fees, so it can
            # never "take profit" into a net loss
            rsi_exit = rsi > bot.RSI_SELL_ABOVE and pct > round_trip

            if target_hit or stop_hit or rsi_exit:
                net = pos["size"] * (pct - round_trip)
                trades.append(net)
                cash += net
                pos = None

        # CRITICAL: mark any still-open position to the final close.
        #
        # Without this the sweep reports profit factor = infinity on wide
        # stops, and it is pure survivorship bias: with a 8-12% stop that
        # 30 days never reaches, the ONLY exits that can fire are the
        # profit target and the fee-gated RSI exit - both of which require
        # a gain. Losers simply stay open and never enter the books, so
        # every "closed" trade is a winner by construction. The first run
        # of this script produced 14 configs at PF inf with 100%
        # out-of-sample survival, which is what a broken metric looks like,
        # not an edge.
        if pos is not None:
            final = candles[-1][4]
            pct = (final - pos["entry"]) / pos["entry"]
            trades.append(pos["size"] * (pct - round_trip))
            unclosed += 1

    wins = sum(t for t in trades if t > 0)
    losses = abs(sum(t for t in trades if t <= 0))
    pf = (wins / losses) if losses else (float("inf") if wins else 0.0)
    return {"pf": pf, "n": len(trades), "net": sum(trades), "open_at_end": unclosed,
            "ret": sum(trades) / STARTING_CASH * 100}


def main():
    print("crypto_coinbase_bot GEOMETRY sweep")
    print(f"  pairs      {', '.join(PAIRS)}   {LOOKBACK_DAYS}d of 5-min candles")
    print(f"  fees       taker {TAKER*100:.2f}%  maker {MAKER*100:.2f}%")
    print(f"  live now   target {bot.PROFIT_TARGET_PCT*100:.2f}%  "
          f"stop {bot.STOP_LOSS_PCT*100:.2f}%")
    print(f"  entry      RSI < {bot.RSI_BUY_BELOW} (unchanged - this sweeps EXITS)\n")

    raw = {}
    for p in PAIRS:
        cs = fetch_candles(p, LOOKBACK_DAYS)
        if len(cs) < 500:
            print(f"  ! {p}: only {len(cs)} candles", file=sys.stderr)
            continue
        raw[p] = cs
        print(f"  {p}: {len(cs)} candles")
    if not raw:
        print("FAIL: no candles fetched.", file=sys.stderr)
        return 1

    def prep(lo_f, hi_f):
        out = {}
        for p, cs in raw.items():
            lo, hi = int(len(cs) * lo_f), int(len(cs) * hi_f)
            seg = cs[lo:hi]
            out[p] = (seg, rsi_series([c[4] for c in seg]), atr_pct_series(seg))
        return out

    ins, oos = prep(0.0, IN_SAMPLE_FRAC), prep(IN_SAMPLE_FRAC, 1.0)

    configs = [("fixed", t, s, None) for t in TARGET_GRID for s in STOP_GRID]
    configs += [("atr", None, None, m) for m in ATR_MULTS]

    rows = []
    for kind, t, s, m in configs:
        r = simulate(ins, t, s, m, TAKER, TAKER)
        r.update(kind=kind, t=t, s=s, m=m)
        rows.append(r)
    rows.sort(key=lambda r: -r["pf"])

    def label(r):
        if r["kind"] == "atr":
            return f"ATR stop {r['m'][0]}x / tgt {r['m'][1]}x"
        return f"target {r['t']*100:.1f}% / stop {r['s']*100:.0f}%"

    print(f"\n  IN-SAMPLE top 10 of {len(rows)}")
    print(f"  {'config':32} {'PF':>7} {'trades':>7} {'open':>5} {'return':>9}")
    for r in rows[:10]:
        print(f"  {label(r):32} {r['pf']:>7.3f} {r['n']:>7} "
              f"{r['open_at_end']:>5} {r['ret']:>8.2f}%")

    cleared = [r for r in rows if r["pf"] > 1.0 and r["n"] >= 10]
    print(f"\n  cleared PF 1.0 in-sample (>=10 trades): {len(cleared)} / {len(rows)}")

    best = rows[0]
    o = simulate(oos, best["t"], best["s"], best["m"], TAKER, TAKER)
    print("\n  " + "=" * 62)
    print(f"  IN-SAMPLE WINNER: {label(best)}")
    print(f"    in-sample      PF {best['pf']:.3f}  {best['n']:>4} trades  {best['ret']:+.2f}%")
    print(f"    OUT-OF-SAMPLE  PF {o['pf']:.3f}  {o['n']:>4} trades  {o['ret']:+.2f}%")
    if o['pf'] == float('inf') or best['pf'] == float('inf'):
        print("    !! PF inf means zero losing trades - treat as a broken")
        print("       metric, not an edge, and investigate before believing it.")
    print(f"    -> {'HELD UP' if o['pf'] > 1.0 else 'COLLAPSED'}")
    print("  " + "=" * 62)

    if cleared:
        surv = sum(1 for r in cleared
                   if simulate(oos, r["t"], r["s"], r["m"], TAKER, TAKER)["pf"] > 1.0)
        print(f"\n  of {len(cleared)} in-sample winners, {surv} survived out-of-sample "
              f"({surv/len(cleared)*100:.0f}%)")
    else:
        print("\n  NOTHING cleared profit factor 1.0 in-sample.")
        print("  On this data no target/stop pair makes this signal profitable,")
        print("  and the problem is the RSI entry rather than the geometry.")

    # what maker execution would add ON TOP of the best geometry
    mk = simulate(oos, best["t"], best["s"], best["m"], MAKER, MAKER)
    print(f"\n  maker/maker on the same winner (out-of-sample): "
          f"PF {mk['pf']:.3f} vs {o['pf']:.3f} taker")
    print("  Order-type work is only worth doing if a profitable geometry")
    print("  exists first - it adds a few points, it does not create an edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
