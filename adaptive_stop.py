"""A stop distance that means the same thing on every coin.

GRID_STOP_LOSS_PCT is one number, 8%, applied to six coins with wildly
different weather. Measured on 60 days of hourly candles, asking "from
any hour as an entry, did price fall 8% below it within a week?":

    coin    daily vol    8% stop fires
    BTC        1.90%          0.0%     <- never. it is decorative
    NEAR       6.02%         13.8%
    FLOKI      4.61%         25.8%
    TIA        4.81%         39.7%
    ONDO       4.61%         39.8%
    BONK       5.71%         54.0%     <- more than half of all entries

The same "8%" is no protection at all on BTC and a coin-flip that costs a
fee every time on BONK. It is not one policy applied six times; it is six
different policies that happen to share a number.

Scaling by the coin's OWN volatility fixes that. The stop distance that
produces a roughly uniform ~10% stop-out rate sits at 1.48x to 3.05x
daily volatility across the six, averaging about 2.5x:

    BTC  1.90% x 2.5 =  4.8%      ONDO   4.61% x 2.5 = 11.5%
    NEAR 6.02% x 2.5 = 15.1%      FLOKI  4.61% x 2.5 = 11.5%
    BONK 5.71% x 2.5 = 14.3%      TIA    4.81% x 2.5 = 12.0%

SAFETY. This module widens some stops and tightens others, so every path
that cannot measure is a path that keeps the EXISTING fixed stop rather
than removing protection:

  * volatility unreadable            -> the fixed stop, unchanged
  * a computed stop outside [floor, cap] -> clamped, never unbounded
  * mode not explicitly set to adaptive  -> the fixed stop, unchanged

Turning a stop OFF entirely is possible and is deliberately made loud: it
takes an explicit per-coin override of 0, and resolve() returns a reason
string saying so, for the caller to log at WARNING.
"""

import os

MODE_ENV = "GRID_STOP_MODE"                    # "fixed" (default) | "adaptive"
MULTIPLE_ENV = "GRID_STOP_VOL_MULTIPLE"        # x daily volatility
FLOOR_ENV = "GRID_STOP_FLOOR_PCT"
CAP_ENV = "GRID_STOP_CAP_PCT"
OVERRIDES_ENV = "GRID_STOP_OVERRIDES"          # "NEAR-USD:0,BTC-USD:0.05"

DEFAULT_MULTIPLE = 2.5

# A stop nearer than this is inside ordinary noise for every coin the
# fleet trades and would fire on the spread. A stop wider than the cap is
# not a stop, it is a hope.
DEFAULT_FLOOR = 0.03
DEFAULT_CAP = 0.25


def daily_vol_pct_from_closes(closes, bars_per_day=24):
    """Daily volatility, in percent, from a series of periodic closes.

    Standard deviation of period-over-period returns, scaled to a day.
    Returns None on a series too short or too degenerate to judge - which
    resolve() reads as "keep the existing stop".

    WHY NOT ATR. The bot already carries an hourly ATR% and using it was
    the obvious shortcut. Calibrated against the stop distance that
    actually produced a uniform ~10% stop-out rate over 60 days, the
    required multiple was 27.7x to 45.6x across five coins and 113.3x on
    BTC - a 4x spread, so hourly ATR does not predict weekly drawdown
    across coins of different character. The same calibration on daily
    volatility lands between 1.48x and 3.05x. That is the stable input.
    """
    if not closes:
        return None
    try:
        vals = [float(c) for c in closes]
    except (TypeError, ValueError):
        return None
    if len(vals) < 5:
        return None

    rets = []
    for prev, cur in zip(vals, vals[1:]):
        if prev:
            rets.append((cur - prev) / prev)
    if len(rets) < 4:
        return None

    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    if var <= 0:
        return None
    return (var ** 0.5) * (float(bars_per_day) ** 0.5) * 100.0


# HOW LONG A WINDOW THE VOLATILITY IS MEASURED OVER.
#
# The first wiring of this module fed it the ~25 hourly candles the bot
# already fetches for price. That window is far too short, and the error
# ran the wrong way. Measured 2026-09-26 against longer windows on the
# same coins:
#
#     coin      25h      7d      30d      60d
#     BTC     0.49%   1.91%    1.81%    1.90%
#     NEAR    2.44%   9.59%    7.40%    6.02%
#     BONK    1.84%   7.46%    5.84%    5.71%
#
# The 7, 30 and 60-day figures agree with each other. The 25-hour one
# reads three to four times lower, because one quiet day is not a sample.
# Scaled from it, EVERY stop tightened - NEAR would have gone from 8% to
# 6.14% instead of the 18.5% a 30-day window gives. A stop that tightens
# because yesterday happened to be calm is a stop that shrinks right
# before volatility returns, which is precisely backwards.
VOL_WINDOW_DAYS = int(os.getenv("GRID_STOP_VOL_WINDOW_DAYS", "30") or 30)

# 30 days of hourly candles per branch per cycle would hammer the candle
# endpoint for a number that barely moves between two five-minute cycles.
# Same cache shape coin_rotation already uses for its trip counts.
VOL_CACHE_SECONDS = float(os.getenv("GRID_STOP_VOL_CACHE_SECONDS", str(6 * 3600)))
_VOL_CACHE = {}


async def measure_daily_vol(session, product_id, days=None, fetcher=None, now=None,
                            cached_only=False):
    """Daily volatility over a window long enough to mean something.

    Cached per product. Returns None when the history will not load or is
    too thin, which resolve() reads as "keep the existing stop" - never as
    "no stop".

    cached_only=True never fetches. A dashboard poll must not pay for a
    month of candles per coin just to display a number; the trading cycle
    populates the cache and the read side uses whatever is there.
    """
    import time as _time
    days = int(days or VOL_WINDOW_DAYS)
    now = _time.monotonic() if now is None else now

    hit = _VOL_CACHE.get(product_id)
    if hit and (now - hit[0]) < VOL_CACHE_SECONDS:
        return hit[1]
    if cached_only:
        return None

    if fetcher is None:
        import crypto_selection_backtest as CSB
        fetcher = CSB.fetch_candles_window

    import datetime as _dt
    end = _dt.datetime.now(_dt.timezone.utc)
    start = end - _dt.timedelta(days=days)
    try:
        got = await fetcher(session, product_id, start, end, granularity=3600)
    except Exception:
        return None
    if not got or not got[0]:
        return None

    vol = daily_vol_pct_from_closes(got[0])
    # Only a real reading is cached. Caching a None would pin a branch to
    # the fixed stop for six hours over one failed fetch.
    if vol is not None:
        _VOL_CACHE[product_id] = (now, vol)
    return vol


def _env_float(name, default):
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v > 0 else default


def mode():
    """'adaptive' only when explicitly asked for. Anything else is 'fixed'."""
    return "adaptive" if (os.getenv(MODE_ENV) or "").strip().lower() == "adaptive" else "fixed"


def parse_overrides(raw=None):
    """{product_id: stop_fraction}. 0 means the stop is OFF for that coin.

    A malformed entry is skipped rather than defaulting to 0 - a typo must
    never be the thing that silently removes a stop.
    """
    if raw is None:
        raw = os.getenv(OVERRIDES_ENV) or ""
    out = {}
    for chunk in str(raw).replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        pid, _, val = chunk.partition(":")
        pid = pid.strip().upper()
        try:
            v = float(val.strip())
        except ValueError:
            continue
        if pid and 0 <= v < 1:
            out[pid] = v
    return out


def scaled_stop(daily_vol_pct, multiple=None, floor=None, cap=None):
    """Stop distance as a fraction, from this coin's own daily volatility.

    Returns None when volatility is unusable, which the caller must read
    as "keep the fixed stop", never as "no stop".
    """
    if daily_vol_pct is None:
        return None
    try:
        vol = float(daily_vol_pct)
    except (TypeError, ValueError):
        return None
    if vol != vol or vol <= 0 or vol in (float("inf"), float("-inf")):
        return None

    multiple = DEFAULT_MULTIPLE if multiple is None else float(multiple)
    floor = DEFAULT_FLOOR if floor is None else float(floor)
    cap = DEFAULT_CAP if cap is None else float(cap)
    if floor > cap:
        floor, cap = cap, floor

    return round(min(cap, max(floor, vol / 100.0 * multiple)), 6)


def resolve(product_id, fixed_pct, daily_vol_pct=None, overrides=None,
            mode_override=None):
    """The stop this branch should actually use, and why.

    Returns {"stop_pct", "source", "reason"}. stop_pct of 0.0 means no
    stop; every other value is a live trigger distance below entry.

    Precedence, most specific first:
      1. an explicit per-coin override (including 0 = off)
      2. volatility scaling, when mode is adaptive AND volatility is usable
      3. the fixed stop
    """
    pid = (product_id or "").upper()
    ov = parse_overrides() if overrides is None else overrides
    try:
        fixed = float(fixed_pct)
    except (TypeError, ValueError):
        fixed = 0.0

    if pid in ov:
        v = ov[pid]
        if v == 0:
            return {"stop_pct": 0.0, "source": "override",
                    "reason": (f"stop is OFF for {pid} by explicit override - this slice has no "
                               f"downside trigger and can fall without limit until it is sold "
                               f"another way")}
        return {"stop_pct": v, "source": "override",
                "reason": f"{v * 100:.2f}% stop for {pid} by explicit override"}

    use_mode = mode() if mode_override is None else mode_override
    if use_mode == "adaptive":
        scaled = scaled_stop(daily_vol_pct,
                             multiple=_env_float(MULTIPLE_ENV, DEFAULT_MULTIPLE),
                             floor=_env_float(FLOOR_ENV, DEFAULT_FLOOR),
                             cap=_env_float(CAP_ENV, DEFAULT_CAP))
        if scaled is not None:
            return {"stop_pct": scaled, "source": "adaptive",
                    "reason": (f"{scaled * 100:.2f}% stop for {pid}, "
                               f"{_env_float(MULTIPLE_ENV, DEFAULT_MULTIPLE):.2f}x its own "
                               f"{float(daily_vol_pct):.2f}% daily volatility")}
        return {"stop_pct": fixed, "source": "fixed_no_volatility",
                "reason": (f"{fixed * 100:.2f}% fixed stop for {pid} - adaptive mode is on but "
                           f"volatility could not be read, so the existing stop is kept rather "
                           f"than removed")}

    return {"stop_pct": fixed, "source": "fixed",
            "reason": f"{fixed * 100:.2f}% fixed stop for {pid}"}


def policy():
    """The stop configuration actually in force, for reporting.

    A safety setting nobody can read is one being trusted on faith - this
    exists so "did the environment variable take" is answerable without
    guessing from an uptime counter.
    """
    return {
        "mode": mode(),
        "fixed_default_pct": None,          # filled by the caller that owns the constant
        "vol_multiple": _env_float(MULTIPLE_ENV, DEFAULT_MULTIPLE),
        "vol_window_days": VOL_WINDOW_DAYS,
        "floor_pct": _env_float(FLOOR_ENV, DEFAULT_FLOOR),
        "cap_pct": _env_float(CAP_ENV, DEFAULT_CAP),
        "overrides": parse_overrides(),
        "vol_cache_seconds": VOL_CACHE_SECONDS,
        "measured_coins": sorted(_VOL_CACHE),
    }
