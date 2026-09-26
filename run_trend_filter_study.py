"""Run every trend filter over every regime window, and report all of it.

Uses horizon_study.rung_profile unchanged, passing an `allow` callback - so
the filtered and unfiltered numbers come from ONE implementation of the
arithmetic, and any difference between them is the filter and nothing else.

    python3 run_trend_filter_study.py > out.json
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import aiohttp

import horizon_study as HS
import trend_filter as TF

# Chosen from 654 comparable 21-day windows by find_falling_windows.py, and
# deliberately including the MODAL case as well as the tails. The frequency
# distribution over those 654 windows, for the fleet's own six coins:
#
#     crash  < -30%      3.2%
#     bad    -30..-15%  23.4%
#     soft   -15..0%    37.6%     <- the most common thing that happens
#     mild     0..+15%  20.2%
#     good   +15..+40%  14.1%
#     boom     > +40%    1.5%
#
# 64.2% of windows had the median coin FALLING; the median window itself was
# -5.1%. Testing only crashes and melt-ups would have flattered the strategy
# by leaving out the case that occurs more than any other.
WINDOWS = [
    ("2026-02-05 crash", "2026-02-05"),      # 3.2% of windows
    ("2025-11-22 slide", "2025-11-22"),      # 23.4% bucket
    ("2025-11-11 typical", "2025-11-11"),    # 37.6% bucket - the modal case
    ("2025-07-22 melt-up", "2025-07-22"),    # 1.5% of windows
    ("now", None),
]
# A longer fetch than the study window, so a 7-day average EXISTS at the
# start of the window instead of the filter being blind for its first third.
WARMUP_DAYS = 10


def _ts(d):
    return None if d is None else int(
        datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


async def main():
    days = int(os.getenv("REGIME_STUDY_DAYS", "21"))
    out = {}
    async with aiohttp.ClientSession() as session:
        for label, date_str in WINDOWS:
            print(f"  {label}...", file=sys.stderr, flush=True)
            per = {}
            for p in HS.DEFAULT_PRODUCTS:
                hist = await HS.fetch_history(session, p, days=days + WARMUP_DAYS,
                                              end_ts=_ts(date_str))
                if not hist:
                    continue
                times, lows, highs, closes = hist
                filters = TF.build(closes)
                per[p] = {
                    "bars": len(times),
                    "window_return_pct": round(HS._pct(closes[-1], closes[0]), 3),
                    "by_filter": {name: HS.rung_profile(times, highs, lows, closes,
                                                        allow=fn)
                                  for name, fn in filters.items()},
                }
            out[label] = {"end_date": date_str, "per_coin": per}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
