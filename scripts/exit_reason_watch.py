#!/usr/bin/env python3
"""Re-read the closed-trade exit reasons at a raised limit, and say whether
the four-way split has become readable yet.

WHY THIS IS A SCRIPT AND NOT A HABIT. The analysis has three traps, and each
one has already been walked into once:

  1. THE CAP DECIDES WHAT THE DATA SAYS. trade-history served 50 of 132
     trades with nothing admitting it. A distribution off that window is a
     distribution of whatever happens to be recent.
  2. LEGACY ROWS DROWN THE NEW ONES. The four-way split (profit_target /
     parked_sell / stop_loss / close_all) shipped at a known moment. Rows
     closed before it carry the OLD binary label, where "profit_target"
     merely meant "not a stop". Counting all rows together reports the old
     labels as if they were the new ones - at the first reading, 131 of 132
     rows were legacy and the naive count said "100% profit_target".
  3. AN UNREADABLE FETCH IS NOT AN EMPTY BOOK. A failed request must never
     print a zero.

So the split is computed the same way every time, and the script says plainly
whether the answer means anything yet.

Exit codes: 0 quiet (nothing new worth reporting), 1 UNREADABLE (a gap, not a
zero), 2 REPORTABLE (something changed that a human should see).

Usage:  python3 scripts/exit_reason_watch.py [--limit N] [--json]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

BASE = os.getenv("EMPIRE_BASE_URL",
                 "https://empire-v2-production.up.railway.app")
PATH = "/api/trading-dashboard/grid-status/trade-history"

# WHEN THE FOUR-WAY LABEL WENT LIVE. 4dd6867 introduced it; 5cdf245 was
# confirmed serving at 00:16Z on 2026-09-29, so any trade closed after this
# instant carries a label written by the new logic. Rows at or before it
# carry the old binary one and are NOT comparable.
CUTOVER = os.getenv("EXIT_REASON_CUTOVER", "2026-09-29T00:16:00")

# The four the new logic can emit. Anything else is either a legacy value or
# something unaccounted for, and both are worth surfacing.
KNOWN = ("profit_target", "parked_sell", "stop_loss", "close_all")

# Below this the post-cutover sample says nothing. Not derived - chosen so a
# handful of rows cannot be read as a finding, the same reason the expiry
# study uses a floor.
MIN_ROWS_TO_READ = int(os.getenv("EXIT_REASON_MIN_ROWS", "20"))

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", ".claude", "exit-reason-watch.json")


def fetch(limit):
    """(payload, None) or (None, reason). Never returns a fabricated book."""
    url = f"{BASE}{PATH}?limit={int(limit)}"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            if r.status != 200:
                return None, f"HTTP {r.status}"
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        # No previous run, or an unreadable one. Both mean "nothing to
        # compare against", which is not the same as "nothing changed" -
        # the caller treats a missing baseline as reportable.
        return None


def save_state(d):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2, sort_keys=True)
    except Exception as e:
        print(f"  (state not saved: {type(e).__name__}: {e})")


def split(rows, cutover=CUTOVER):
    """(post_cutover, legacy). A row with no closed_at cannot be placed on
    either side of the cutover, so it counts as legacy rather than being
    silently credited to the new logic."""
    post, legacy = [], []
    for r in rows:
        stamp = r.get("closed_at") or ""
        (post if stamp > cutover else legacy).append(r)
    return post, legacy


def analyse(payload):
    rows = payload.get("recent_trades") or []
    post, legacy = split(rows)
    post_counts = Counter(r.get("exit_reason") for r in post)
    return {
        "total_in_book": payload.get("total_trade_count"),
        "returned": payload.get("recent_trades_returned", len(rows)),
        "truncated": bool(payload.get("recent_trades_truncated")),
        "omitted": payload.get("recent_trades_omitted"),
        "post_cutover": len(post),
        "legacy": len(legacy),
        "legacy_unlabelled": sum(1 for r in legacy
                                 if r.get("exit_reason") is None),
        "post_counts": {str(k): v for k, v in post_counts.items()},
        "labels_seen": sorted(str(k) for k in post_counts if k),
        "readable": len(post) >= MIN_ROWS_TO_READ,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    payload, err = fetch(a.limit)
    if payload is None:
        # A GAP, NOT A ZERO. Loudest possible, and a distinct exit code, so a
        # caller cannot mistake a failed fetch for "no new labels".
        print(f"UNREADABLE: trade-history could not be read ({err}). This is "
              f"a GAP, not an empty book - do not read it as 'no change'.")
        return 1

    r = analyse(payload)
    if a.json:
        print(json.dumps(r, indent=2, sort_keys=True))

    print(f"book: {r['total_in_book']} closed | returned {r['returned']}"
          f"{' (TRUNCATED - raise --limit)' if r['truncated'] else ''}")
    print(f"legacy rows (pre {CUTOVER}): {r['legacy']}"
          f"  of which unlabelled: {r['legacy_unlabelled']}")
    print(f"rows under the four-way logic: {r['post_cutover']}")
    for k, v in sorted(r["post_counts"].items(), key=lambda kv: -kv[1]):
        print(f"    {k:<16}{v}")

    if r["truncated"]:
        print("VERDICT: the window is capped, so this is a distribution of "
              "whatever is recent. Raise --limit before reading anything.")
    elif not r["readable"]:
        print(f"VERDICT: NOT YET READABLE - {r['post_cutover']} of "
              f"{MIN_ROWS_TO_READ} rows needed. The legacy rows carry the old "
              f"binary label and are not comparable; counting them together "
              f"would report the old labels as if they were the new ones.")
    else:
        print(f"VERDICT: READABLE - {r['post_cutover']} rows under the new "
              f"logic. Labels seen: {', '.join(r['labels_seen']) or 'none'}.")

    prev = load_state()
    new_labels = sorted(set(r["labels_seen"]) -
                        set((prev or {}).get("labels_seen") or []))
    unknown = sorted(set(r["labels_seen"]) - set(KNOWN))
    became_readable = r["readable"] and not (prev or {}).get("readable")

    reasons = []
    if prev is None:
        reasons.append("no previous run to compare against")
    if new_labels:
        reasons.append(f"label(s) seen for the first time: "
                       f"{', '.join(new_labels)}")
    if unknown:
        reasons.append(f"label(s) the four-way logic cannot emit: "
                       f"{', '.join(unknown)}")
    if became_readable:
        reasons.append("the sample crossed the readability floor")

    save_state(r)

    if reasons:
        print("REPORTABLE: " + "; ".join(reasons))
        return 2
    print("quiet: nothing new since the last check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
