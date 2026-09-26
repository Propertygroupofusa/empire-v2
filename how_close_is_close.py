"""How alarming is "1.6% from its level", really?

I reported XRP 1.60% and BTC 0.74% from their stop levels as the urgent
story. Against those coins' own daily volatility - 3.56% and 1.81% - those
distances are a fraction of one ordinary day's movement. A trailing stop
set 2.5x daily vol below the PEAK will sit near price most of the time by
construction, so "close to the level" may be the normal state of affairs
rather than a warning.

This measures it: over real history, what fraction of days did each coin
sit this close to its own trailing level, and how often did being that
close actually lead to a breach?

It changes nothing and trades nothing. It exists because I put a number on
screen that read as urgent, and I owe it a check.
"""
from __future__ import annotations
import asyncio, json, sys
import aiohttp
import adaptive_stop, holdings_watch, horizon_study

COINS = ["XRP-USD", "BTC-USD", "ZEC-USD", "ETH-USD", "HBAR-USD"]
DAYS = 120
WIN = 30 * 24          # trailing peak window, in hourly bars


async def main():
    out = {}
    async with aiohttp.ClientSession() as s:
        for pid in COINS:
            hist = await horizon_study.fetch_history(s, pid, days=DAYS, granularity=3600)
            if not hist:
                print(f"  {pid}: no history", file=sys.stderr); continue
            times, lows, highs, closes = hist
            vol = adaptive_stop.daily_vol_pct_from_closes(closes)
            stop = adaptive_stop.scaled_stop(vol)
            if stop is None:
                continue
            near = breach = total = 0
            near_then_breached = near_events = 0
            was_near = False
            for i in range(WIN, len(times)):
                peak = max(highs[i - WIN:i + 1])
                level = peak * (1 - stop)
                price = closes[i]
                if price <= level:
                    breach += 1
                    if was_near:
                        near_then_breached += 1
                    was_near = False
                else:
                    dist = (price / level - 1) * 100
                    # "close" = within one day's normal move of the level
                    if dist <= vol:
                        near += 1
                        if not was_near:
                            near_events += 1
                        was_near = True
                    else:
                        was_near = False
                total += 1
            out[pid] = {
                "daily_vol_pct": round(vol, 2),
                "stop_pct": round(stop * 100, 2),
                "hours": total,
                "pct_of_time_within_one_day_move": round(100 * near / total, 1),
                "pct_of_time_breached": round(100 * breach / total, 1),
                "near_episodes": near_events,
                "episodes_that_became_breaches": near_then_breached,
                "conversion_pct": (round(100 * near_then_breached / near_events, 1)
                                   if near_events else None),
            }
            print(f"  {pid} done", file=sys.stderr, flush=True)
    print(json.dumps(out, indent=1))

asyncio.run(main())
