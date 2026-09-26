"""Locate windows where the fleet's coins FELL, so a study can be run on one.

Every horizon study so far ended today, and every one carried the same
caveat in its own output: "ALL instruments ROSE over this window. A result
that is POSITIVE here is not yet evidence of an edge - it has not been
shown a falling market."

That caveat is not a footnote, it is the whole question. A long-only
resting-rung strategy makes money in a rising market by construction. The
only test that can distinguish an edge from a tailwind is the same
measurement pointed at a window where price went down.

This finds those windows. It does not measure anything about the strategy -
it only answers "when did these coins fall, and by how much", so the study
has somewhere honest to aim.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

import aiohttp

DAY = 86400
PRODUCTS = [p.strip() for p in os.getenv(
    "FALLING_PRODUCTS",
    "BONK-USD,BTC-USD,FLOKI-USD,NEAR-USD,ONDO-USD,TIA-USD").split(",") if p.strip()]
WINDOW_DAYS = int(os.getenv("FALLING_WINDOW_DAYS", "21"))


async def daily_closes(session, product_id: str, days: int = 730):
    """Daily closes, oldest first, as {epoch_day_start: close}. Never raises."""
    rows, now = {}, int(time.time())
    end, floor = now, now - days * DAY
    try:
        while end > floor:
            start = max(floor, end - 300 * DAY)
            url = (f"https://api.exchange.coinbase.com/products/{product_id}/candles"
                   f"?granularity={DAY}"
                   f"&start={datetime.utcfromtimestamp(start).isoformat()}Z"
                   f"&end={datetime.utcfromtimestamp(end).isoformat()}Z")
            async with session.get(url, headers={"Accept": "application/json"},
                                   timeout=30) as r:
                if r.status != 200:
                    break
                data = await r.json()
            if not data:
                break
            for x in data:
                rows[int(x[0])] = float(x[4])
            end = start
            await asyncio.sleep(0.35)
    except Exception:
        pass
    return rows


def windows(closes: dict, window_days: int):
    """Every window_days-long stretch, with the % change across it."""
    ts = sorted(closes)
    out = []
    for i in range(len(ts) - window_days):
        a, b = closes[ts[i]], closes[ts[i + window_days]]
        if not a:
            continue
        out.append({"start_ts": ts[i], "end_ts": ts[i + window_days],
                    "start": a, "end": b,
                    "change_pct": round((b - a) / a * 100, 2)})
    return out


async def main():
    async with aiohttp.ClientSession() as session:
        per = {}
        for p in PRODUCTS:
            c = await daily_closes(session, p)
            if len(c) < WINDOW_DAYS + 5:
                print(f"  {p:10s} only {len(c)} daily bars - skipped", file=sys.stderr)
                continue
            per[p] = windows(c, WINDOW_DAYS)
            first = datetime.utcfromtimestamp(min(c)).date()
            print(f"  {p:10s} {len(c):4d} daily bars from {first}", file=sys.stderr)

    if not per:
        print("no history", file=sys.stderr)
        return 1

    # Align on end dates every coin has, so one window is comparable across
    # the fleet rather than each coin being scored on its own worst stretch.
    common = set.intersection(*[{w["end_ts"] for w in ws} for ws in per.values()])
    rows = []
    for end_ts in sorted(common):
        chg = {p: next(w["change_pct"] for w in ws if w["end_ts"] == end_ts)
               for p, ws in per.items()}
        rows.append({"end_ts": end_ts,
                     "end_date": datetime.utcfromtimestamp(end_ts).date().isoformat(),
                     "changes": chg,
                     "median_pct": sorted(chg.values())[len(chg) // 2],
                     "all_fell": all(v < 0 for v in chg.values()),
                     "n_fell": sum(1 for v in chg.values() if v < 0)})
    rows.sort(key=lambda r: r["median_pct"])
    print(json.dumps({"window_days": WINDOW_DAYS, "products": list(per),
                      "common_windows": len(rows),
                      "worst": rows[:12],
                      "best": rows[-3:]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
