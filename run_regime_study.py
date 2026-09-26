"""Run the horizon study across market regimes, not just the last three weeks.

Every horizon study this fleet has produced ended TODAY, and every one said
so in its own output: all instruments rose over the window, so a positive
number is not evidence of an edge. The strategy is long-only resting rungs.
In a rising market that makes money by construction. The only measurement
that separates an edge from a tailwind is the identical one, pointed at a
window where price fell.

This runs horizon_study.run_study unchanged over several windows and prints
them side by side. It does not trade, does not write a threshold, and does
not reimplement the measurement - if the falling-market answer came from
different code than the rising-market answer, the comparison would be
worthless.

    python3 run_regime_study.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import aiohttp

import horizon_study as HS

# Windows chosen by find_falling_windows.py from 654 comparable 21-day
# stretches over two years of daily closes. The two bear windows are the
# worst the fleet's own coins have actually lived through; the bull window
# is the counterweight, and "now" is what every previous study measured.
WINDOWS = [
    ("2026-02-05 crash", "2026-02-05", "all six fell, median -42.8%"),
    ("2025-11-22 slide", "2025-11-22", "all six fell, median -34.2%"),
    ("2025-07-22 melt-up", "2025-07-22", "all six rose, median +63.3%"),
    ("now", None, "the window every previous study used"),
]


def _ts(date_str):
    if date_str is None:
        return None
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


async def main():
    days = int(os.getenv("REGIME_STUDY_DAYS", "21"))
    out = {}
    async with aiohttp.ClientSession() as session:
        for label, date_str, why in WINDOWS:
            print(f"  running {label} ({why})...", file=sys.stderr, flush=True)
            study = await HS.run_study(session, days=days, end_ts=_ts(date_str))
            out[label] = {"why": why, "end_date": date_str, "study": study}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
