"""
One-off backtest: does expressing prop_bot's profit target as a PERCENTAGE
(matching the percentage stop-loss) actually beat the old absolute-dollar
target on real market data?

WHY THIS EXISTS
---------------
prop_bot.py used to take profit at a fixed dollar amount ($0.50, rising to
$1.00 at $10k equity) while cutting losses at STOP_LOSS_PCT (2% of the
position). Two different units on the two sides of the same trade, so
risk:reward drifted with account size and always against us - at ~$980
equity that was risking $2.80 to make $0.50, an 85% breakeven win rate.

The fix expresses the target as a percentage so R:R is constant. The
arithmetic for WHY the old rule was unwinnable is airtight. What arithmetic
alone CANNOT tell us is whether the RSI entry signal actually clears the
new, much lower 40% breakeven bar - or whether holding for a 3% move just
converts a lot of small wins into a few large losses. That is an empirical
question, and this script is how it gets answered before the change goes
anywhere near real money.

FIDELITY
--------
Imports the real, unmodified production module for its constants and pure
functions (size_position, get_profit_target_dollars) so this backtest
cannot silently drift from the bot it is testing. The RSI/trend math is
replicated inline ONLY because in production it lives inside an async
network call (get_price_rsi) that can't be reused offline - it is copied
verbatim from that function, including the same 14-period window and the
sma5/sma10 trend rule.

Replays both rules over the SAME bars, same entries, same stop, same
higher-timeframe filter. The only variable is the exit target, which is
the point.

NOT part of the app - throwaway, run under GitHub Actions because the dev
sandbox has no outbound network access to market-data APIs.

REQUIRES: ALPACA_API_KEY / ALPACA_SECRET_KEY as repo secrets (free-tier
IEX data is enough - that is the same feed the bot trades on).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
import prop_bot as bot  # the real, unmodified production module

DATA_URL = "https://data.alpaca.markets/v2/stocks"
LOOKBACK_DAYS = int(os.getenv("BACKTEST_DAYS", "30"))
STARTING_CASH = float(os.getenv("BACKTEST_CASH", "980"))

SYMBOLS = [cfg["symbol"] for cfg in bot.FUTURES.values()]

HEADERS = {
    "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""),
}


# ── the OLD rule, reproduced here because the fix deleted it ──────────────
OLD_MILESTONES = [(0, 0.50), (1000, 0.60), (5000, 0.80), (10000, 1.00)]


def old_profit_target(equity):
    if equity is None:
        return OLD_MILESTONES[0][1]
    target = OLD_MILESTONES[0][1]
    for threshold, t in OLD_MILESTONES:
        if equity >= threshold:
            target = t
    return target


def fetch_bars(symbol, timeframe, days):
    """Paginated Alpaca bars, oldest-first. Returns [] on any failure so one
    bad symbol degrades the sample instead of killing the whole run."""
    end = datetime.now(timezone.utc) - timedelta(minutes=20)  # free-tier delay
    start = end - timedelta(days=days)
    out, page_token = [], None

    while True:
        url = (f"{DATA_URL}/{symbol}/bars?timeframe={timeframe}"
               f"&start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
               f"&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
               f"&limit=10000&feed=iex&adjustment=raw")
        if page_token:
            url += f"&page_token={page_token}"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode(errors="replace")
            print(f"  ! HTTP {e.code} fetching {symbol} {timeframe}: {body}", file=sys.stderr)
            return out
        except Exception as e:
            print(f"  ! error fetching {symbol} {timeframe}: {e}", file=sys.stderr)
            return out

        out.extend(data.get("bars") or [])
        page_token = data.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.3)

    return out


def rsi_trend_series(closes):
    """Verbatim from get_price_rsi: 14-period RSI over the last 20 closes,
    trend from sma5 vs sma10. Index i uses closes[:i+1] - i.e. only bars
    the bot would actually have seen at that point, no lookahead."""
    out = [None] * len(closes)
    for i in range(len(closes)):
        window = closes[max(0, i - 19):i + 1]
        if len(window) < 14:
            continue
        gains = [max(window[j] - window[j - 1], 0) for j in range(1, len(window))]
        losses = [max(window[j - 1] - window[j], 0) for j in range(1, len(window))]
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        rsi = 100 - (100 / (1 + rs))
        sma5 = sum(window[-5:]) / 5
        sma10 = sum(window[-10:]) / 10 if len(window) >= 10 else sma5
        out[i] = {"rsi": round(rsi, 1), "trend": "bullish" if sma5 > sma10 else "bearish"}
    return out


def hourly_trend_series(bars):
    """Verbatim from get_higher_tf_trend: sma20 vs sma50, +/-1.5% bands."""
    closes = [b["c"] for b in bars]
    out = []
    for i, b in enumerate(bars):
        if i < 49:
            out.append((b["t"], "UNKNOWN"))
            continue
        window = closes[i - 49:i + 1]
        sma20, sma50 = sum(window[-20:]) / 20, sum(window) / 50
        if sma50 == 0:
            out.append((b["t"], "UNKNOWN"))
            continue
        d = (sma20 - sma50) / sma50
        out.append((b["t"], "UP" if d > 0.015 else "DOWN" if d < -0.015 else "SIDEWAYS"))
    return out


def trend_at(hourly, ts):
    """Most recent 1H trend at-or-before ts. Never looks ahead."""
    best = "UNKNOWN"
    for h_ts, t in hourly:
        if h_ts <= ts:
            best = t
        else:
            break
    return best


def simulate(bars_by_symbol, rsi_by_symbol, hourly_by_symbol, mode):
    """Replays run_prop_cycle's Pass 1 (exits) then Pass 2 (entries) over
    every 5-minute timestamp in the sample. `mode` is the ONLY difference
    between runs: 'old' = absolute-dollar target, 'new' = percentage."""
    cash = STARTING_CASH
    positions = {}          # symbol -> {"entry", "qty"}
    trades = []

    timeline = sorted({b["t"] for bars in bars_by_symbol.values() for b in bars})
    idx = {s: {b["t"]: i for i, b in enumerate(bars)} for s, bars in bars_by_symbol.items()}

    for ts in timeline:
        equity = cash + sum(
            p["qty"] * bars_by_symbol[s][idx[s][ts]]["c"]
            for s, p in positions.items() if ts in idx[s]
        )

        # ── Pass 1: exits ────────────────────────────────────────────────
        for symbol in list(positions):
            if ts not in idx[symbol]:
                continue
            i = idx[symbol][ts]
            sig = rsi_by_symbol[symbol][i]
            if not sig:
                continue
            price, rsi, trend = bars_by_symbol[symbol][i]["c"], sig["rsi"], sig["trend"]
            pos = positions[symbol]
            entry, qty = pos["entry"], pos["qty"]

            unrealized = (price - entry) * qty
            rsi_signal = rsi > bot.RSI_SELL_ABOVE or (trend == "bearish" and rsi > 50)
            stop_hit = price <= entry * (1 - bot.STOP_LOSS_PCT)
            rsi_exit = rsi_signal and unrealized > 0

            target = (old_profit_target(equity) if mode == "old"
                      else bot.get_profit_target_dollars(entry * qty))

            if unrealized >= target or rsi_exit or stop_hit:
                reason = ("PROFIT TARGET" if unrealized >= target
                          else "STOP LOSS" if stop_hit else "RSI")
                cash += qty * price
                trades.append({"symbol": symbol, "pnl": unrealized, "reason": reason,
                               "pct": (price - entry) / entry * 100})
                del positions[symbol]

        # ── Pass 2: entries ──────────────────────────────────────────────
        candidates = []
        for symbol in SYMBOLS:
            if symbol in positions or ts not in idx.get(symbol, {}):
                continue
            i = idx[symbol][ts]
            sig = rsi_by_symbol[symbol][i]
            if not sig or sig["rsi"] >= bot.RSI_BUY_BELOW:
                continue
            # higher-timeframe confluence filter (entries only, as in prod)
            if trend_at(hourly_by_symbol[symbol], ts) == "DOWN":
                continue
            candidates.append((bot.RSI_BUY_BELOW - sig["rsi"], symbol, bars_by_symbol[symbol][i]["c"]))

        candidates.sort(key=lambda c: -c[0])  # strongest signal first

        for _, symbol, price in candidates:
            if len(positions) >= bot.MAX_POSITIONS:
                break
            qty = bot.size_position(cash, bot.MAX_POSITIONS - len(positions), price)
            if not qty or qty * price > cash:
                continue
            cash -= qty * price
            positions[symbol] = {"entry": price, "qty": qty}

    # mark remaining open positions to final price
    final_equity = cash
    for symbol, p in positions.items():
        final_equity += p["qty"] * bars_by_symbol[symbol][-1]["c"]

    return {"trades": trades, "final_equity": final_equity, "open": len(positions)}


def summarize(name, res):
    trades = res["trades"]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    wr = len(wins) / len(trades) * 100 if trades else 0
    pf = gross_win / gross_loss if gross_loss else float("inf")
    ret = (res["final_equity"] - STARTING_CASH) / STARTING_CASH * 100

    by_reason = defaultdict(int)
    for t in trades:
        by_reason[t["reason"]] += 1

    print(f"\n  {name}")
    print(f"    final equity   ${res['final_equity']:,.2f}  ({ret:+.2f}%)")
    print(f"    closed trades  {len(trades)}   (still open at end: {res['open']})")
    print(f"    win rate       {wr:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"    avg win        ${(gross_win / len(wins)) if wins else 0:.2f}"
          f"   avg loss  ${(gross_loss / len(losses)) if losses else 0:.2f}")
    print(f"    profit factor  {pf:.2f}")
    print(f"    exits          {dict(by_reason)}")
    return {"return_pct": ret, "trades": len(trades), "wr": wr, "pf": pf,
            "final": res["final_equity"]}


def main():
    if not HEADERS["APCA-API-KEY-ID"] or not HEADERS["APCA-API-SECRET-KEY"]:
        print("FAIL: ALPACA_API_KEY / ALPACA_SECRET_KEY not set.\n"
              "  Add both as GitHub repo secrets (Settings -> Secrets and\n"
              "  variables -> Actions). Free-tier IEX keys are sufficient -\n"
              "  this only reads historical bars, it never places an order.",
              file=sys.stderr)
        return 1

    print(f"prop_bot profit-target backtest")
    print(f"  symbols        {', '.join(SYMBOLS)}")
    print(f"  lookback       {LOOKBACK_DAYS} days of 5-minute IEX bars")
    print(f"  starting cash  ${STARTING_CASH:,.2f}")
    print(f"  entry          RSI < {bot.RSI_BUY_BELOW}, 1H trend not DOWN")
    print(f"  stop           {bot.STOP_LOSS_PCT * 100:.1f}%")
    print(f"  OLD target     $0.50-$1.00 absolute (pre-fix)")
    print(f"  NEW target     {bot.PROFIT_TARGET_PCT * 100:.1f}% of position, "
          f"min ${bot.MIN_PROFIT_DOLLARS:.2f}")

    bars_by_symbol, rsi_by_symbol, hourly_by_symbol = {}, {}, {}
    for symbol in SYMBOLS:
        bars = fetch_bars(symbol, "5Min", LOOKBACK_DAYS)
        if len(bars) < 100:
            print(f"  ! {symbol}: only {len(bars)} bars, skipping", file=sys.stderr)
            continue
        bars_by_symbol[symbol] = bars
        rsi_by_symbol[symbol] = rsi_trend_series([b["c"] for b in bars])
        hourly_by_symbol[symbol] = hourly_trend_series(fetch_bars(symbol, "1Hour", LOOKBACK_DAYS + 10))
        print(f"  {symbol}: {len(bars)} 5m bars")
        time.sleep(0.3)

    if not bars_by_symbol:
        print("FAIL: no usable market data fetched.", file=sys.stderr)
        return 1

    old = summarize("OLD  (absolute-dollar target)",
                    simulate(bars_by_symbol, rsi_by_symbol, hourly_by_symbol, "old"))
    new = summarize(f"NEW  ({bot.PROFIT_TARGET_PCT * 100:.0f}% target)",
                    simulate(bars_by_symbol, rsi_by_symbol, hourly_by_symbol, "new"))

    print("\n  " + "=" * 60)
    delta = new["final"] - old["final"]
    print(f"  VERDICT: new rule {'BEATS' if delta > 0 else 'LOSES TO'} old by "
          f"${abs(delta):,.2f} ({new['return_pct'] - old['return_pct']:+.2f} pts)")
    print(f"  profit factor  {old['pf']:.2f} -> {new['pf']:.2f}")
    print(f"  trade count    {old['trades']} -> {new['trades']}")
    print("  " + "=" * 60)
    print("\n  NOTE: one sample over one regime. A positive result here means"
          "\n  the change is not obviously harmful - it is not proof of future"
          "\n  profit, and this window may not contain a real drawdown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
