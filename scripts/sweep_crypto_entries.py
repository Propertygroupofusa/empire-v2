"""
Entry-signal sweep for crypto_coinbase_bot.

WHY THIS EXISTS
---------------
The geometry sweep tested 38 target/stop pairs and none survived
out-of-sample. But every one of those runs held the entry fixed at
RSI < 45, so what it actually established is "no exit rule rescues THIS
entry" - not "this asset has no edge". The entry is the untested half.

THE QUESTION THIS ANSWERS
-------------------------
Does any entry signal on BTC/ETH 5-minute bars predict the next few hours
better than not having a signal at all?

That is deliberately not "which backtest scores highest". Ranking
backtests is how you find the luckiest draw. Instead the primary
measurement is exit-independent:

  for each signal, take the forward return at fixed horizons, net of a
  round-trip fee, and compare it against the UNCONDITIONAL forward return
  over the same bars

If a signal has no predictive power, its conditional mean is the
unconditional mean. Any edge has to show up as a gap between the two,
before any exit rule is chosen. This cannot be tuned away or tuned into
existence by fiddling with targets and stops, which is exactly the trap
the geometry sweep fell into.

CONTROLS, BECAUSE THE OBVIOUS MISTAKES ARE EASY TO MAKE
-------------------------------------------------------
buy-and-hold   If BTC rallied 20% across the window, EVERY long signal
               looks profitable. Without this baseline a bull market
               reads as a strategy.
random entry   Same trade count, same horizons, entries drawn at random.
               Isolates whether the signal contributes anything over
               noise. A signal that cannot beat coin flips is not a
               signal.
shuffled       The signal's own returns re-drawn from the unconditional
               pool, repeated, to get a null distribution and a
               bootstrap p-value. Tells us whether a gap that looks real
               is inside what chance produces.

Split-sample throughout: the first 60% is where anything gets chosen, the
last 40% is untouched until a winner is locked in.

Both mean-reversion (buy dips) and momentum (buy strength) families are
tested. If mean reversion has a negative edge, its mirror may have a
positive one, and testing only the direction the bot already uses would
miss that.

Public Coinbase candles, no API keys, places no orders.
"""
import json
import os
import random
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

PAIRS = ["BTC-USD", "ETH-USD"]
GRANULARITY = 300                       # 5-minute, as the bot uses
LOOKBACK_DAYS = int(os.getenv("SWEEP_DAYS", "30"))
IN_SAMPLE_FRAC = 0.60
TAKER = float(os.getenv("SWEEP_TAKER", "0.004"))
ROUND_TRIP = TAKER * 2                  # entry + exit, real fee tier
HORIZONS = [12, 48, 288]                # bars: 1h, 4h, 24h
BOOTSTRAP = 2000
random.seed(20260802)                   # reproducible controls


# ── data ────────────────────────────────────────────────────────────────

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


# ── indicators (all strictly trailing - index i uses bars <= i only) ────

def rsi_series(closes, period=14):
    out = [None] * len(closes)
    for i in range(period, len(closes)):
        w = closes[i - period:i + 1]
        gains = [max(0.0, w[j] - w[j - 1]) for j in range(1, len(w))]
        losses = [max(0.0, w[j - 1] - w[j]) for j in range(1, len(w))]
        ag, al = sum(gains) / period, sum(losses) / period
        out[i] = 100.0 if al == 0 else 100 - (100 / (1 + ag / al))
    return out


def sma_series(closes, n):
    out, run = [None] * len(closes), 0.0
    for i, c in enumerate(closes):
        run += c
        if i >= n:
            run -= closes[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def rolling_extreme(closes, n, hi=True):
    out = [None] * len(closes)
    for i in range(n, len(closes)):
        w = closes[i - n:i]
        out[i] = max(w) if hi else min(w)
    return out


# ── entry signal families ───────────────────────────────────────────────

def build_signals(closes):
    """name -> list[bool], True where the signal fires at that bar."""
    n = len(closes)
    rsi = rsi_series(closes)
    sma50, sma200 = sma_series(closes, 50), sma_series(closes, 200)
    hi48 = rolling_extreme(closes, 48, hi=True)
    lo48 = rolling_extreme(closes, 48, hi=False)
    sigs = {}

    # mean reversion - what the bot does today
    for thr in (25, 30, 35, 40, 45):
        sigs[f"MR rsi<{thr}"] = [r is not None and r < thr for r in rsi]

    # mean reversion on distance from a moving average
    for pct in (0.01, 0.02, 0.03):
        sigs[f"MR px<sma50-{pct*100:.0f}%"] = [
            (sma50[i] is not None and closes[i] < sma50[i] * (1 - pct))
            for i in range(n)]

    # momentum - the untested mirror
    for thr in (55, 60, 65, 70):
        sigs[f"MOM rsi>{thr}"] = [r is not None and r > thr for r in rsi]
    sigs["MOM px>48bar high"] = [
        (hi48[i] is not None and closes[i] > hi48[i]) for i in range(n)]
    sigs["MOM px>sma50>sma200"] = [
        (sma50[i] is not None and sma200[i] is not None
         and closes[i] > sma50[i] > sma200[i]) for i in range(n)]

    # breakdown (short-side interest, measured long-only for comparison)
    sigs["BRK px<48bar low"] = [
        (lo48[i] is not None and closes[i] < lo48[i]) for i in range(n)]

    # trend-filtered mean reversion - dip inside an uptrend
    sigs["MR rsi<35 & px>sma200"] = [
        (rsi[i] is not None and rsi[i] < 35
         and sma200[i] is not None and closes[i] > sma200[i]) for i in range(n)]
    return sigs


# ── measurement ─────────────────────────────────────────────────────────

def forward_returns(closes, idxs, horizon):
    """Net forward return at `horizon` bars for each index, fee-adjusted."""
    out = []
    for i in idxs:
        j = i + horizon
        if j < len(closes) and closes[i] > 0:
            out.append((closes[j] - closes[i]) / closes[i] - ROUND_TRIP)
    return out


def summarise(rets):
    if len(rets) < 2:
        return None
    wins = [r for r in rets if r > 0]
    return {"n": len(rets), "mean": statistics.mean(rets),
            "median": statistics.median(rets),
            "winrate": len(wins) / len(rets)}


def bootstrap_p(signal_rets, pool, iters=BOOTSTRAP):
    """P(random sample of the same size has a mean >= the signal's mean).

    The null being tested: this signal is just a re-drawing of the same
    bars. A small p means the gap is bigger than chance usually produces;
    it does NOT by itself mean the edge is tradeable.
    """
    if not signal_rets or len(pool) < len(signal_rets):
        return None
    target, k, hits = statistics.mean(signal_rets), len(signal_rets), 0
    for _ in range(iters):
        if statistics.mean(random.sample(pool, k)) >= target:
            hits += 1
    return hits / iters


def main():
    print("crypto ENTRY-SIGNAL sweep")
    print(f"  pairs      {', '.join(PAIRS)}   {LOOKBACK_DAYS}d of 5-min candles")
    print(f"  fee        round trip {ROUND_TRIP*100:.2f}% (taker {TAKER*100:.2f}% x2)")
    print(f"  horizons   {HORIZONS} bars = 1h / 4h / 24h")
    print(f"  split      first {IN_SAMPLE_FRAC:.0%} selects, last {1-IN_SAMPLE_FRAC:.0%} validates")
    print("  measuring  forward return conditional on the signal, vs the")
    print("             unconditional forward return over the same bars\n")

    raw = {}
    for p in PAIRS:
        cs = fetch_candles(p, LOOKBACK_DAYS)
        if len(cs) < 1000:
            print(f"  ! {p}: only {len(cs)} candles", file=sys.stderr)
            continue
        raw[p] = cs
        print(f"  {p}: {len(cs)} candles")
    if not raw:
        print("FAIL: no candles fetched.", file=sys.stderr)
        return 1

    for pair, candles in raw.items():
        closes = [c[4] for c in candles]
        split = int(len(closes) * IN_SAMPLE_FRAC)
        sigs = build_signals(closes)

        print(f"\n{'='*74}\n  {pair}\n{'='*74}")

        # buy-and-hold, the baseline every long signal must beat
        bh_is = (closes[split - 1] - closes[0]) / closes[0] * 100
        bh_oos = (closes[-1] - closes[split]) / closes[split] * 100
        print(f"  buy & hold:  in-sample {bh_is:+.2f}%   out-of-sample {bh_oos:+.2f}%")
        print("  (a long signal that does not beat this is worse than doing nothing)\n")

        for horizon in HORIZONS:
            hrs = horizon * 5 / 60
            all_idx = list(range(len(closes) - horizon))
            is_pool = forward_returns(closes, [i for i in all_idx if i < split], horizon)
            base = summarise(is_pool)
            if not base:
                continue

            print(f"  ── horizon {hrs:.0f}h "
                  f"(unconditional: mean {base['mean']*100:+.3f}%, "
                  f"win {base['winrate']*100:.1f}%, n={base['n']}) ──")
            print(f"  {'signal':26} {'n':>5} {'mean':>9} {'win%':>7} "
                  f"{'edge vs base':>13} {'p':>7}")

            rows = []
            for name, flags in sigs.items():
                idxs = [i for i in all_idx if i < split and flags[i]]
                r = summarise(forward_returns(closes, idxs, horizon))
                if not r or r["n"] < 30:
                    continue
                edge = r["mean"] - base["mean"]
                rows.append((name, r, edge))

            # random-entry control, same trade count as the median signal
            if rows:
                k = int(statistics.median([r["n"] for _, r, _ in rows]))
                rnd = summarise(random.sample(is_pool, min(k, len(is_pool))))
                rows.append(("[control] random entry", rnd, rnd["mean"] - base["mean"]))

            for name, r, edge in sorted(rows, key=lambda x: -x[2]):
                p = bootstrap_p(
                    forward_returns(
                        closes,
                        [i for i in all_idx if i < split and sigs[name][i]]
                        if name in sigs else [], horizon),
                    is_pool) if name in sigs else None
                pstr = f"{p:.3f}" if p is not None else "  -  "
                print(f"  {name:26} {r['n']:>5} {r['mean']*100:>8.3f}% "
                      f"{r['winrate']*100:>6.1f}% {edge*100:>12.3f}% {pstr:>7}")

            # out-of-sample check on the best in-sample signal
            real = [x for x in rows if x[0] in sigs]
            if real:
                best_name = max(real, key=lambda x: x[2])[0]
                oos_all = list(range(split, len(closes) - horizon))
                oos_pool = forward_returns(closes, oos_all, horizon)
                oos_base = summarise(oos_pool)
                oos_sig = summarise(forward_returns(
                    closes, [i for i in oos_all if sigs[best_name][i]], horizon))
                if oos_base and oos_sig:
                    oe = oos_sig["mean"] - oos_base["mean"]
                    verdict = "HELD" if oe > 0 else "COLLAPSED"
                    print(f"    -> best in-sample '{best_name}' out-of-sample: "
                          f"edge {oe*100:+.3f}% over base, n={oos_sig['n']} [{verdict}]")
            print()

    print("=" * 74)
    print("  READING THIS")
    print("  'edge vs base' is what matters: how much better the signal does")
    print("  than entering at a random moment in the same window. A signal")
    print("  with a positive mean but a NEGATIVE edge is just riding the")
    print("  market, and buy & hold does it with one trade and one fee.")
    print("  p is a bootstrap against re-drawn samples; with ~14 signals x 3")
    print("  horizons, expect one or two below 0.05 by chance alone. The")
    print("  out-of-sample line is the one that gets a vote.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
