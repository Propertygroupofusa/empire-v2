"""A ceiling on how many positions the stock bot may OPEN in one trading day.

THE OWNER, 2026-10-14: "It was making money then ... go back to this date."
The numbers behind that hunch (Alpaca realised P&L, by week, from the orders
the broker still holds):

    week of 7 Sep    8 sales   +$6.44
    week of 14 Sep   4 sales   +$1.84
    week of 28 Sep   128 sales -$27.37
    week of 5 Oct    88 sales  -$7.28

The account made money while it traded a handful of times a week and lost it
once it traded a hundred. Each round trip on a ~$125 position pays the spread
and slippage twice, and the average loser was more than twice the average
winner, so more trades is more of the loss. Rolling the code back would also
roll back the no-loss protections, so the fix is narrower: a daily ceiling on
ENTRIES. Exits are never counted and never refused.

What it counts: orders that open a position (entry_pass, branch_entry,
idle_cash_sweep, opening_bar_entry), submitted, filled or not, because the
limit exists to stop a burst and a burst is a number of orders sent.

The count is kept in the database under the US/Eastern trading date, so a
restart or a deploy mid-day does not hand the bot a fresh allowance.

Fails CLOSED when the count cannot be read: a ceiling that cannot evaluate
itself must not wave an order through (same stance as position_caps).
"""
import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

MAX_ENTRIES_ENV = "ALPACA_MAX_ENTRIES_PER_DAY"
DEFAULT_MAX_ENTRIES = 3
KEY_PREFIX = "alpaca_entries_"

# The four callers that open a position. Everything else (the three exits)
# is out of scope by name, not by inference.
ENTRY_SOURCES = frozenset({"entry_pass", "branch_entry", "idle_cash_sweep", "opening_bar_entry"})

_OFF_WORDS = ("off", "none", "unlimited", "no", "false")


def max_entries_per_day():
    """The daily ceiling, or None when the owner switched it off.

    "off"/"none"/"unlimited" switch it off. A positive whole number sets it.
    Unset, empty, zero, negative, a typo or a quoted value all fall back to the
    default, never to "no limit": 0 would be a second, silent way to pause the
    bot (there is a pause switch for that) and a typo must not remove a guard.
    """
    raw = os.getenv(MAX_ENTRIES_ENV)
    if raw is None:
        return DEFAULT_MAX_ENTRIES
    v = raw.strip().strip("\"'").strip().lower()
    if v in _OFF_WORDS:
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ENTRIES
    return n if n >= 1 else DEFAULT_MAX_ENTRIES


def trading_day(now=None):
    """The US/Eastern calendar date an order belongs to."""
    now = now or datetime.now(ET)
    return now.astimezone(ET).date().isoformat()


def day_key(now=None):
    return f"{KEY_PREFIX}{trading_day(now)}"


class EntryCounter:
    """Counts entries per trading day against a store it is handed.

    load(key) -> float | None and save(key, value) are async, so the tests can
    hand it a dict and prop_bot can hand it the database. One lock covers
    every reserve and release: the opening-bar symbols run as separate tasks,
    and two of them reading "2 of 3" at the same moment must not both be
    allowed.
    """

    def __init__(self, load, save):
        self._load, self._save = load, save
        self._lock = asyncio.Lock()

    async def reserve(self, now=None):
        """Take one entry slot. (allowed, used_after, limit, reason)."""
        limit = max_entries_per_day()
        if limit is None:
            return True, None, None, "daily entry ceiling is off"
        async with self._lock:
            key = day_key(now)
            try:
                used = int(round(float(await self._load(key) or 0.0)))
            except Exception as exc:
                return False, None, limit, (
                    f"today's entry count is unreadable ({type(exc).__name__}: {exc}), "
                    f"so no ceiling can be applied")
            if used >= limit:
                return False, used, limit, (
                    f"{used} of {limit} entries already used today - the ceiling is "
                    f"{limit} new positions per trading day")
            try:
                await self._save(key, float(used + 1))
            except Exception as exc:
                return False, used, limit, (
                    f"today's entry count could not be saved ({type(exc).__name__}: {exc})")
            return True, used + 1, limit, "ok"

    async def release(self, now=None):
        """Give a slot back: the order was refused by the venue, so nothing opened."""
        if max_entries_per_day() is None:
            return
        async with self._lock:
            key = day_key(now)
            try:
                used = int(round(float(await self._load(key) or 0.0)))
                await self._save(key, float(max(0, used - 1)))
            except Exception:
                # A slot that is not given back only makes the ceiling stricter.
                pass

    async def used_today(self, now=None):
        try:
            return int(round(float(await self._load(day_key(now)) or 0.0)))
        except Exception:
            return None
