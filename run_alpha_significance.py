"""Bootstrap the entry alpha at the settings where it looked positive."""
from __future__ import annotations
import asyncio, json, os, random, sys
from collections import defaultdict
from datetime import datetime
import aiohttp
import entry_alpha as EA, horizon_study as HS
from run_entry_alpha import LEDGER_URL, CONTROLS_PER_ENTRY, SEED, _ts

CASES = [(3.0, "6h"), (3.0, "12h"), (2.0, "6h"), (2.0, "2h"),
         (3.0, "72h"), (1.0, "24h")]

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
        series = {}
        for pid, rows in entries.items():
            opens = [_ts(t["opened_at"]) for t in rows]
            days = int((max(opens) - min(opens)) / 86400.0) + 6
            h = await HS.fetch_history(session, pid, days=days,
                                       end_ts=max(opens) + 4 * 86400)
            if h:
                series[pid] = h
    out = {}
    for target, hname in CASES:
        hsec = EA.HORIZON_SECONDS[hname]
        real, control = [], []
        for pid, rows in entries.items():
            if pid not in series:
                continue
            times, lows, highs, closes = series[pid]
            for t in rows:
                i = EA.nearest_index(times, _ts(t["opened_at"]))
                if i is None:
                    continue
                o = EA.resolve_entry(times, highs, lows, closes, i, target, hsec)
                if not o:
                    continue
                real.append(o)
                for j in EA.control_indices(times, i, hsec, CONTROLS_PER_ENTRY, rng):
                    c = EA.resolve_entry(times, highs, lows, closes, j, target, hsec)
                    if c:
                        control.append(c)
        out[f"{target}%@{hname}"] = {
            "n_real": len(real), "n_control": len(control),
            "bootstrap": EA.bootstrap_alpha(real, control),
        }
        print(f"  {target}%@{hname} done", file=sys.stderr, flush=True)
    print(json.dumps(out, indent=1))
    return 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
