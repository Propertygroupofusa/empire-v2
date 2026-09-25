"""Collect Kalshi and Polymarket prices on the same 15-minute BTC window.

READ-ONLY BY CONSTRUCTION. This file imports no trading module, holds no
credentials that can place an order, and issues only HTTP GETs. It cannot
buy or sell anything. That is deliberate: the question it exists to answer
is "is there an edge here", and answering it must cost zero capital.

WHY IT EXISTS (2026-09-25): a strategy video proposed trading the spread
between Kalshi and Polymarket 15-minute BTC up/down markets, claiming the
spread is mean-reverting in the first ~10 minutes. The claim is plausible
and the maths presented was correct, but the evidence was charts plus three
days of data, and the author said outright he had not reached statistical
significance. Two terms that decide the whole strategy were never measured:

    * how often the spread DIVERGES instead of converging (the venues
      settle on different rules, so the same window can go YES on one and
      NO on the other - and a "market neutral" spread then loses on BOTH
      legs at once)
    * what a divergence costs when it happens

This collects the data those questions need. cointegration.py does the
testing. Nothing here decides to trade.

USAGE
    python3 kalshi_polymarket_collector.py --minutes 60
    python3 kalshi_polymarket_collector.py --minutes 0        # run forever
    python3 kalshi_polymarket_collector.py --analyze          # test what's collected

Data lands in JSONL, one observation per line, append-only, so an
interrupted run loses nothing and a long run can be analysed while it is
still going.
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DATA_FILE = os.getenv("KP_DATA_FILE", "kalshi_polymarket_data.jsonl")
POLL_SECONDS = int(os.getenv("KP_POLL_SECONDS", "5"))
HTTP_TIMEOUT = 15

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"


def _get(url, timeout=HTTP_TIMEOUT):
    """Plain GET returning parsed JSON, or None with a printed reason.

    Never raises: a collector that dies on one bad response loses the run.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "kp-collector/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"    HTTP {e.code} for {url.split('?')[0]}")
    except Exception as e:
        print(f"    {type(e).__name__} for {url.split('?')[0]}: {e}")
    return None


# --- venue readers --------------------------------------------------------
# Both are best-effort and shape-tolerant. Venue APIs change; a collector
# that hard-codes one response shape stops working silently, which is worse
# than collecting nothing, because the file keeps growing and looks fine.

def kalshi_btc_markets(series_ticker="KXBTCD"):
    """Open Kalshi BTC markets. Returns a list of normalized dicts."""
    data = _get(f"{KALSHI_BASE}/markets?series_ticker={series_ticker}&status=open&limit=100")
    if not data:
        return []
    out = []
    for m in data.get("markets", []):
        yes_bid, yes_ask = m.get("yes_bid"), m.get("yes_ask")
        out.append({
            "venue": "kalshi",
            "ticker": m.get("ticker"),
            "title": m.get("title"),
            "strike": m.get("floor_strike") or m.get("cap_strike"),
            "close_time": m.get("close_time"),
            # Kalshi quotes in cents; normalise to probability 0-1.
            "yes_bid": (yes_bid / 100.0) if isinstance(yes_bid, (int, float)) else None,
            "yes_ask": (yes_ask / 100.0) if isinstance(yes_ask, (int, float)) else None,
        })
    return out


def polymarket_btc_markets(slug_contains="bitcoin"):
    """Open Polymarket markets matching a slug. Returns normalized dicts."""
    data = _get(f"{POLYMARKET_GAMMA}/markets?closed=false&limit=100&order=volume&ascending=false")
    if not data:
        return []
    out = []
    rows = data if isinstance(data, list) else data.get("data", [])
    for m in rows:
        slug = (m.get("slug") or "").lower()
        question = (m.get("question") or "").lower()
        if slug_contains not in slug and slug_contains not in question:
            continue
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except Exception:
                prices = None
        yes = None
        if isinstance(prices, list) and prices:
            try:
                yes = float(prices[0])
            except Exception:
                yes = None
        out.append({
            "venue": "polymarket",
            "ticker": m.get("slug"),
            "title": m.get("question"),
            "close_time": m.get("endDate"),
            "yes_bid": yes,
            "yes_ask": yes,
        })
    return out


def mid(row):
    """Mid price, or the single side if only one is quoted."""
    b, a = row.get("yes_bid"), row.get("yes_ask")
    if b is not None and a is not None:
        return (b + a) / 2.0
    return b if b is not None else a


def observe():
    """One sample from both venues. Returns a list of observation dicts."""
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in kalshi_btc_markets() + polymarket_btc_markets():
        m = mid(r)
        if m is None:
            continue
        rows.append({
            "ts": now,
            "venue": r["venue"],
            "ticker": r.get("ticker"),
            "title": r.get("title"),
            "close_time": r.get("close_time"),
            "yes_bid": r.get("yes_bid"),
            "yes_ask": r.get("yes_ask"),
            "mid": round(m, 6),
        })
    return rows


def append(rows, path=DATA_FILE):
    if not rows:
        return 0
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return len(rows)


def load(path=DATA_FILE):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue  # a torn final line from an interrupted write
    return out


def pair_by_timestamp(rows):
    """Align the two venues into one spread series per timestamp.

    Deliberately simple: one Kalshi mid and one Polymarket mid per instant,
    differenced. Matching SPECIFIC markets across venues needs the strike
    and window, which only real collected data can tell us the shape of -
    guessing that mapping now would bake in an assumption nobody has
    checked. Refine once there is a file to look at.
    """
    by_ts = {}
    for r in rows:
        by_ts.setdefault(r["ts"], {})[r["venue"]] = r["mid"]
    ts_sorted = sorted(t for t, v in by_ts.items() if "kalshi" in v and "polymarket" in v)
    return ts_sorted, [by_ts[t]["kalshi"] - by_ts[t]["polymarket"] for t in ts_sorted]


def analyze(path=DATA_FILE):
    from cointegration import adf_test

    rows = load(path)
    print("=" * 72)
    print("  KALSHI / POLYMARKET SPREAD - COINTEGRATION CHECK")
    print("=" * 72)
    if not rows:
        print(f"\n  No data yet at {path}. Run the collector first.\n")
        return
    venues = {}
    for r in rows:
        venues[r["venue"]] = venues.get(r["venue"], 0) + 1
    print(f"\n  observations : {len(rows)}")
    for v, n in sorted(venues.items()):
        print(f"    {v:12} {n}")

    ts, spread = pair_by_timestamp(rows)
    print(f"  paired points: {len(spread)}")
    if len(spread) < 20:
        print("\n  Not enough PAIRED observations to test yet. Keep collecting.")
        print("  A verdict on a few points would be noise wearing a statistic.\n")
        return
    print(f"  window       : {ts[0]} -> {ts[-1]}")

    r = adf_test(spread)
    print("\n  " + "-" * 68)
    if not r["ok"]:
        print(f"  {r['reason']}")
    else:
        print(f"  lambda        {r['lambda']}")
        print(f"  t-statistic   {r['t_stat']}   (Dickey-Fuller critical {r['critical_value']})")
        print(f"  half-life     {r['half_life_obs']} observations")
        print(f"  mean spread   {r['mean']}")
        print(f"\n  VERDICT: {r['verdict']}")
        if not r["mean_reverting"]:
            print("\n  Do NOT trade this yet. 'Not shown' usually means not enough")
            print("  data - it is not the same as proven random. Keep collecting.")
        else:
            print("\n  Mean reversion is supported. Still measure the divergence")
            print("  rate and subtract real spreads and fees before trading -")
            print("  see divergence_stats() and expected_value().")
    print()


def main():
    p = argparse.ArgumentParser(description="Read-only Kalshi/Polymarket spread collector")
    p.add_argument("--minutes", type=int, default=60, help="0 runs until interrupted")
    p.add_argument("--poll", type=int, default=POLL_SECONDS)
    p.add_argument("--file", default=DATA_FILE)
    p.add_argument("--analyze", action="store_true", help="analyse existing data and exit")
    args = p.parse_args()

    if args.analyze:
        analyze(args.file)
        return

    deadline = None if args.minutes == 0 else time.time() + args.minutes * 60
    print(f"Collecting to {args.file} every {args.poll}s "
          f"({'until interrupted' if deadline is None else f'for {args.minutes} min'})")
    print("READ-ONLY: this process cannot place an order.\n")
    total = 0
    try:
        while deadline is None or time.time() < deadline:
            n = append(observe(), args.file)
            total += n
            print(f"  {datetime.now(timezone.utc).strftime('%H:%M:%S')}  +{n} rows  ({total} total)")
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\nStopped.")
    print(f"\n{total} observations written to {args.file}")
    print(f"Analyse with: python3 {os.path.basename(__file__)} --analyze")


if __name__ == "__main__":
    main()
