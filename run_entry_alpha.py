"""Score every REAL ledger entry against matched random entries.

    python3 run_entry_alpha.py > out.json

Reads the live coin-history ledger, fetches the candles those trades
actually happened in, and runs both the real entries and the controls
through one arithmetic.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from collections import defaultdict
from datetime import datetime

import aiohttp

import entry_alpha as EA
import horizon_study as HS

LEDGER_URL = os.getenv(
    "LEDGER_URL",
    "https://empire-v2-production.up.railway.app/api/trading-dashboard/family-tree-status/coin-history")
CONTROLS_PER_ENTRY = int(os.getenv("ENTRY_ALPHA_CONTROLS", "20"))
SEED = int(os.getenv("ENTRY_ALPHA_SEED", "20260926"))
TARGETS = [float(x) for x in os.getenv("ENTRY_ALPHA_TARGETS", "1.0,2.0,3.0").split(",")]


def _ts(s):
    return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())


async def main():
    rng = random.Random(SEED)
    async with aiohttp.ClientSession() as session:
        async with session.get(LEDGER_URL, timeout=120) as r:
            ledger = await r.json()
        entries = defaultdict(list)
        for coin in ledger.get("coins", []):
            for t in coin.get("trades", []):
                if t.get("opened_at") and t.get("entry_price"):
                    entries[t["product_id"]].append(t)

        # One fetch per coin, spanning that coin's whole entry range plus
        # the longest horizon, so every entry can be resolved from one
        # series rather than a per-trade fetch that would hammer the API
        # and stitch together windows with different gaps.
        series, per_coin_meta = {}, {}
        for pid, rows in entries.items():
            opens = [_ts(t["opened_at"]) for t in rows]
            span_days = (max(opens) - min(opens)) / 86400.0
            days = int(span_days) + 6            # + the 72h horizon, + slack
            hist = await HS.fetch_history(session, pid, days=days,
                                          end_ts=max(opens) + 4 * 86400)
            if hist:
                series[pid] = hist
                per_coin_meta[pid] = {"entries": len(rows), "bars": len(hist[0]),
                                      "fetch_days": days}
            print(f"  {pid}: {len(rows)} entries, "
                  f"{len(hist[0]) if hist else 0} bars", file=sys.stderr, flush=True)

    out = {"seed": SEED, "controls_per_entry": CONTROLS_PER_ENTRY,
           "fee_pct": EA.MAKER_ROUND_TRIP_PCT, "coins": per_coin_meta,
           "by_target": {}}
    for target in TARGETS:
        by_h = {}
        for hname, hsec in EA.HORIZON_SECONDS.items():
            real, control, unresolved = [], [], 0
            for pid, rows in entries.items():
                if pid not in series:
                    unresolved += len(rows)
                    continue
                times, lows, highs, closes = series[pid]
                for t in rows:
                    i = EA.nearest_index(times, _ts(t["opened_at"]))
                    if i is None:
                        unresolved += 1
                        continue
                    o = EA.resolve_entry(times, highs, lows, closes, i, target, hsec)
                    if o is None:
                        unresolved += 1
                        continue
                    real.append(o)
                    for j in EA.control_indices(times, i, hsec,
                                                CONTROLS_PER_ENTRY, rng):
                        c = EA.resolve_entry(times, highs, lows, closes, j,
                                             target, hsec)
                        if c:
                            control.append(c)
            res = EA.alpha(real, control)
            res["unresolved_entries"] = unresolved
            by_h[hname] = res
        out["by_target"][f"{target}%"] = by_h
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
