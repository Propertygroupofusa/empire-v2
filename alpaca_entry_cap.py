"""When the stock bot may open a position, and how many per trading day.

THE OWNER, 2026-10-14: "It was making money then... go back to this date."

What the real trades say (Alpaca round trips from 8 Sep; 256 of them, -$44.17):

  * 85% of the loss is two incidents, both already fixed: six META buys at once
    on 28 Sep (-$26.12) and the 14 Oct breaker sell-off (-$11.41). The other 246
    trades lost -$6.64, about -$0.03 each: break-even, 41% winners, the average
    winner (+$0.37) about the size of the average loser (-$0.38).
  * "It trades too much" is NOT what the data shows. Replayed on those trades, a
    plain first-come ceiling of three entries a day does no better than no
    ceiling (-$8.82 against -$6.64): it spends its slots on the morning.
  * What held up in both halves of the history is the time of day. Entries at or
    after 2 PM Eastern on the symbols the bot trades now made +$6.01 over 20
    trades; entries before 2 PM lost -$7.63 over 94. Start times from 1:00 to
    2:30 PM all give +$6 to +$7, so the exact minute is not the lucky part. A
    random draw of 20 trades from the same pool nets as much about 11% of the
    time: a test worth running for several weeks, not a proof.

So two guards, both on ENTRIES only (exits are never counted or refused):

  1. THE AFTERNOON WINDOW: no new position before ALPACA_ENTRY_EARLIEST_ET
     (default 14:00 Eastern). This switches the opening-bar entries off in
     practice, since they only fire in the first minutes of the session.
  2. THE DAILY CEILING: at most ALPACA_MAX_ENTRIES_PER_DAY (default 3), a
     backstop against a burst, not the thing expected to earn. In the replay,
     a ceiling of 3 inside the window gave +$5.77 against +$6.01 for no
     ceiling; a ceiling of 1 or 2 lost money, so it is not set lower.

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


# ---------------------------------------------------------------- the window
EARLIEST_ENV = "ALPACA_ENTRY_EARLIEST_ET"
DEFAULT_EARLIEST = "14:00"
WINDOW_PREFIX = "entry window closed"


def earliest_entry_minutes():
    """Minutes after midnight Eastern before which no position may be opened,
    or None when the window is switched off.

    "HH:MM" sets it. "off"/"none"/"any" switch it off. Unset, empty or a typo
    fall back to the default, never to "no window" - a typo must not remove a
    guard.
    """
    raw = os.getenv(EARLIEST_ENV)
    if raw is None:
        raw = DEFAULT_EARLIEST
    v = raw.strip().strip("\"'").strip().lower()
    if v in ("off", "none", "any", "no", "false"):
        return None
    try:
        hh, mm = v.split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    hh, mm = DEFAULT_EARLIEST.split(":")
    return int(hh) * 60 + int(mm)


def entry_window_reason(now=None):
    """None when a new position may be opened right now; otherwise why not.

    Eastern clock, so daylight saving moves with the market. Only the start of
    the window lives here: the market-closed check already knows when the
    session ends, including early closes.
    """
    start = earliest_entry_minutes()
    if start is None:
        return None
    now = (now or datetime.now(ET)).astimezone(ET)
    if now.hour * 60 + now.minute >= start:
        return None
    return (f"{WINDOW_PREFIX}: new positions only from {start // 60:02d}:{start % 60:02d} "
            f"Eastern ({now:%H:%M} Eastern now) - the afternoon test; exits are unaffected")


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
