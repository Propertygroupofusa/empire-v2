"""
Test that every Alpaca bars request carries an explicit `start`.

WHAT WENT WRONG
---------------
Railway logs showed this, every cycle, for 13 of the 16 symbols
prop_bot scans:

    [WARNING] Invalid bars format for QQQ: expected list, got NoneType
    [WARNING] Invalid bars format for MSFT: expected list, got NoneType
    ...
    [APEX_589296] ... | Daily P&L: $0.00

Three of the four bars URLs in prop_bot.py omitted `start`:

    /v2/stocks/{symbol}/bars?timeframe=15Min&limit=100&feed=iex

Alpaca defaults `start` to the beginning of the CURRENT DAY. Those log
lines are timestamped 12:09 UTC - 08:09 ET, about 80 minutes before the
09:30 open - so the requested window contained no session at all and the
response came back with `bars: null`. The bot then discarded the symbol
and evaluated nothing.

It is not an authentication failure: a rejected key logs "Alpaca API
error ... HTTP 401" from the branch above, and no such line appears. The
request succeeded and returned nothing, which is a different problem
wearing the same silence.

The correct pattern was already in the same file at line ~3229, which
passes `start={start}` computed from a lookback. The three that predate
it each got written without one.

WHAT IS ASSERTED
----------------
  1. every Alpaca bars URL in the repo includes start=
  2. each lookback is long enough to cover the bars it asks for, with a
     weekend and a holiday to spare - a 3-day window on a 50-bar hourly
     request would fail the same way over a long weekend
  3. the lookback is computed, not hardcoded to a date

No network, no keys.
"""
import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BARS_URL = re.compile(r'data\.alpaca\.markets/v2/stocks/\{symbol\}/bars\?([^"\']+)')

# timeframe -> minutes of REAL SESSION each bar covers. A session is 6.5h.
TF_MINUTES = {"1Min": 1, "2Min": 2, "5Min": 5, "15Min": 15, "1Hour": 60, "1Day": 390}
SESSION_MINUTES = 390


def main():
    failures = []
    print("Alpaca bars requests carry an explicit start\n")

    found = 0
    for path in sorted(REPO.glob("*.py")):
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = src.splitlines()
        for i, line in enumerate(lines, 1):
            m = BARS_URL.search(line)
            if not m:
                continue
            found += 1
            query = m.group(1)
            rel = f"{path.name}:{i}"

            # ── 1. start present ───────────────────────────────────────
            if "start=" not in query:
                failures.append(f"{rel}: no start= in the query. Alpaca then defaults "
                                f"it to the beginning of the current day, so any call "
                                f"made before the open gets bars: null and the symbol "
                                f"is silently skipped.")
                continue

            tf = re.search(r"timeframe=(\w+)", query)
            lim = re.search(r"limit=(\d+)", query)
            if not tf or not lim:
                # A variable timeframe or limit cannot be sized statically.
                # Printed rather than skipped in silence: a line this check
                # passes over should still be visible, or the count at the
                # bottom stops matching what was actually verified.
                print(f"  {rel:<38} start= present; timeframe/limit is a "
                      f"variable, size not checked")
                continue
            tf, lim = tf.group(1), int(lim.group(1))

            # ── 3. the lookback is computed ────────────────────────────
            window = "\n".join(lines[max(0, i - 4):i])
            # days= may be a VARIABLE, not a literal - backtests and the
            # 2Min fetcher both pass it in. An earlier version of this check
            # only matched digits and flagged five correct call sites, which
            # would have taught everyone to ignore it.
            days = re.search(r"timedelta\(days=(\w+)\)", window)
            if not days:
                failures.append(f"{rel}: start= is present but no "
                                f"timedelta(days=...) precedes it - a hardcoded or "
                                f"stale start silently narrows over time")
                continue
            if not days.group(1).isdigit():
                print(f"  {rel:<38} {lim:>5} x {tf:<6} start=timedelta(days="
                      f"{days.group(1)}) - caller-supplied, size not checked")
                continue
            days = int(days.group(1))

            # ── 2. long enough for the bars requested ──────────────────
            need_session_min = lim * TF_MINUTES.get(tf, 15)
            need_days = need_session_min / SESSION_MINUTES
            # 252 trading sessions per 365 calendar days, so calendar days =
            # trading days / 0.69 = x1.45. An earlier x2 demanded 734 calendar
            # days for 365 daily bars, which is a year and a half of margin on
            # a figure that only needs ~530 - a test failing on correct code.
            safe = need_days * 1.45 + 4
            if days < safe:
                failures.append(f"{rel}: start is {days}d back but {lim} x {tf} needs "
                                f"~{need_days:.1f} trading days ({safe:.0f}d allowing "
                                f"for weekends and a holiday). A short window returns "
                                f"fewer bars than the indicators require.")
            else:
                print(f"  {rel:<28} {lim:>5} x {tf:<6} start={days}d "
                      f"(needs ~{need_days:.1f} trading days)")

    if not found:
        failures.append("no Alpaca bars URLs found at all - has the endpoint moved? "
                        "This test would silently pass forever.")
    else:
        print(f"\n  {found} bars URLs checked")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  every bars request names its own start window")
    return 0


if __name__ == "__main__":
    sys.exit(main())
