"""Short-horizon opportunity scoring, written down before it is wired up.

WHAT THIS IS FOR

The ask: find setups that support several profitable round trips per coin
within a short window, instead of waiting for a full 2.50% grid step. The
signals asked for are momentum, volume confirmation, pullback quality,
volatility, spread, liquidity, and an expected capturable move, blended into
a 0-100 score.

All of that is here. None of it can trade.

THE ARITHMETIC THAT SHAPES EVERYTHING BELOW

A round trip costs the same whether it captures 0.2% or 5%: 0.70% in maker
fees plus roughly 0.67% of adverse selection, so about 1.37% before a single
cent is earned. Crossing the spread to get filled faster makes it 2.17%.

Five profitable trips per coin in fifteen minutes therefore needs ~6.9% of
capturable movement per coin per quarter hour. This fleet's measured hourly
swing on NEAR is 3.32%. The target is roughly twice the HOURLY swing, every
fifteen minutes, repeatedly, on six coins at once.

No amount of signal quality creates movement that is not there. So the honest
thing this module can do is not to find those setups on demand - it is to
measure how often anything close to them actually appears, using real live
data, and to make that measurement cheap and falsifiable.

SO IT PREDICTS, AND THEN IT IS MARKED

Every evaluation writes a ShortTermSignal row: the score, its components, and
an explicit expected_move_pct. Later, the resolver fills in what the market
actually did - the realised move at 5/15/30 minutes, the best and worst
excursions, whether the predicted move ever arrived, how long it took, and
what a real round trip would have NETTED after costs.

net_after_costs_pct is the column that settles this. A signal can be
directionally right and still lose money, because the cost is per trip and
not per percent. That is the failure mode the whole session has been about,
and it is the one an indicator dashboard hides.

UNITS: PERCENT, EVERYWHERE IN THIS MODULE

Every percentage here is a real percent - 1.37 means 1.37%. That has to be
said out loud because the code this reads from does the opposite:
engine._atr_pct_from_candles returns a FRACTION despite the _pct in its
name, and crypto_nine_coin_scanner speaks fractions too (it prints
net_edge_pct * 100).

The caller converts at the boundary. Mixing them is silent: the first live
read showed "0.001% ATR", clamped every volatility sub-score to zero, and -
far worse - had materialized comparing a percent against a fraction and
net_after_costs subtracting one from the other. Numbers that are confident
and mean nothing.

WHAT IT DELIBERATELY DOES NOT DO

It does not gate, trigger, size or veto a trade. OPPORTUNITY_SIGNALS_LIVE
defaults to off and nothing reads the score on the execution path. The score
must never override the hard net-edge gate even when it is switched on: the
gate is arithmetic about cost, the score is a guess about opportunity, and a
guess does not get to overrule arithmetic.

It also does not lower a standard because nothing is happening. There is no
"trade something" floor and no path that spends capital because capital is
idle - the same rule opportunity_scanner.py already holds.
"""

UNITS = "percent"   # see the module docstring

import logging
import os
import time
from datetime import datetime, timedelta, timezone

from collections import Counter

from sqlalchemy import select

from database import get_session_factory
from models import RegimeCrossing, ShortTermSignal

log = logging.getLogger(__name__)

# The switch. Off means the score is recorded and ignored. It is read at call
# time, never cached, so it can be turned on without a restart - and it fails
# OFF, because an unreadable switch must not start steering real money.
SIGNALS_LIVE = os.getenv("OPPORTUNITY_SIGNALS_LIVE", "false").strip().lower() == "true"

# Minimum gap between two scores of the SAME coin. Candles are 5 minutes,
# so the cycle's ~37s cadence was writing ~8 rows per candle from identical
# inputs - 45 rows a minute across the fleet, 65,000 a day, none of the
# duplicates carrying information the first one did not.
#
# One observation per candle is the most the data can actually support, and
# it also makes the ledger honest: 8 copies of one setup would have counted
# as 8 independent predictions in every hit rate computed from it.
SCORE_MIN_GAP_SECONDS = int(os.getenv("GRID_SCORE_MIN_GAP_SECONDS", "300"))

# How much history a report reads. summary() used to load the WHOLE table on
# every grid-status call, which at 65k rows a day is an endpoint that gets
# slower until it stops. Bounded here; the ledger keeps everything, the
# report reads a window.
SUMMARY_MAX_ROWS = int(os.getenv("GRID_SIGNAL_SUMMARY_ROWS", "5000"))

# How far past break-even a coin must get before it counts as having become
# viable. Without a margin, a coin clearing its costs by a thousandth of a
# percent would raise an alert and stop being viable on the next scan, and
# the log would fill with crossings that were arithmetic noise around zero
# rather than a change in the market. This is the "safety buffer" that
# belongs between expected move and real cost.
VIABILITY_MARGIN_PCT = float(os.getenv("GRID_VIABILITY_MARGIN_PCT", "0.10"))

# How long an alert can take to reach a human. The grid detects a crossing
# every candle, but notification rides the hourly watch and the scheduler
# refuses anything shorter - 15 minutes was tried and rejected at the
# platform. A real constraint, not a tuning choice, and the thing the window
# distribution has to be measured against.
ALERT_DELIVERY_DELAY_SECONDS = int(os.getenv("GRID_ALERT_DELAY_SECONDS", "3600"))

# The live stop as a percent, read from the same env var the grid uses, so
# the two can never disagree about what would have been survivable.
GRID_STOP_PCT_LABEL = float(os.getenv("GRID_STOP_LOSS_PCT", "0.08")) * 100

# Candles are 5 minutes, so these are the horizons the data can actually
# support. A 1-minute return was asked for and is not offered: the series
# does not carry it, and interpolating one would be inventing a reading.
HORIZONS_MIN = (5, 15, 30)

# The adverse-selection figure the net-edge gate prices with. NOT used for
# arithmetic here - the gate owns that, and a second copy would drift. It is
# kept as a LABEL, so every report can say plainly that the cost side still
# contains an estimate which no completed trade on this configuration has
# checked. The day expiries and closes are numerous enough to measure it, the
# measured figure replaces this one and the reports must say which is which.
ADVERSE_PCT_ASSUMED = float(os.getenv("GRID_ADVERSE_SELECTION_PCT", "0.67"))


async def fetch_candles_full(session, product_id: str, granularity: int = 300):
    """Candles including their TIMESTAMPS, oldest-first.

    fetch_candles_with_volume drops column 0. Excursions need it: bounding a
    30-minute window and saying WHEN each extreme occurred are both questions
    about time, and neither can be answered from an unlabelled series.

    Returns (times, closes, highs, lows, volumes) or None. Never raises.
    """
    url = (f"https://api.exchange.coinbase.com/products/{product_id}"
           f"/candles?granularity={granularity}")
    try:
        async with session.get(url, headers={"Accept": "application/json"}, timeout=15) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data:
                return None
            c = list(reversed(data))
            return ([int(x[0]) for x in c], [float(x[4]) for x in c],
                    [float(x[2]) for x in c], [float(x[1]) for x in c],
                    [float(x[5]) for x in c])
    except Exception as e:
        log.debug(f"[SIGNAL] full candle fetch failed for {product_id}: {e}")
        return None


def window_excursion(times, highs, lows, since_epoch, ref_price, until_epoch=None):
    """MFE and MAE over a window, from candle HIGHS AND LOWS, with ordering.

    Two things a close-to-close read cannot do.

    WICKS COUNT. A candle that spikes 3% and closes flat still hit 3%, and it
    still would have hit a stop on the way. Sampling closes - or sampling the
    mid every 37 seconds, which is what this replaced - misses whatever
    happened between the samples. Highs and lows are the actual bounds.

    ORDER COUNTS. Returning both extremes without saying which came first
    makes a move that dumps 4% at minute 5 and rips 6% by minute 25 look like
    a clean 6% win. It was not; it was a position that had already been
    stopped out and could not collect the 6%.

    Returns (mfe_pct, mfe_at_min, mae_pct, mae_at_min) or None.
    """
    if not (times and highs and lows and ref_price):
        return None
    idx = [i for i, t in enumerate(times)
           if t >= since_epoch and (until_epoch is None or t <= until_epoch)]
    if not idx:
        return None
    hi_i = max(idx, key=lambda i: highs[i])
    lo_i = min(idx, key=lambda i: lows[i])
    return (round((highs[hi_i] / ref_price - 1.0) * 100.0, 4),
            round((times[hi_i] - since_epoch) / 60.0, 1),
            round((lows[lo_i] / ref_price - 1.0) * 100.0, 4),
            round((times[lo_i] - since_epoch) / 60.0, 1))


async def fetch_candles_with_volume(session, product_id: str, granularity: int = 300):
    """5-minute candles INCLUDING volume, oldest-first.

    The engine's _fetch_candles drops column 5. Volume confirmation was the
    second signal asked for and cannot be computed without it, so this reads
    the same public endpoint and keeps it. Separate function rather than a
    change to the engine's: that one has other callers and none of them
    should acquire a new return shape for this.

    Returns (closes, highs, lows, volumes) or None. Never raises.
    """
    # Two fetchers meant two identical HTTP calls per coin on any cycle where
    # both scoring and crossing resolution ran. fetch_candles_full is a strict
    # superset, so this drops the one column its callers do not use.
    full = await fetch_candles_full(session, product_id, granularity)
    if not full:
        return None
    _times, closes, highs, lows, volumes = full
    return (closes, highs, lows, volumes) if len(closes) >= 20 else None


def _pct(a, b):
    """(a/b - 1) * 100, or None when b is unusable."""
    return ((a / b) - 1.0) * 100.0 if b else None


def momentum(closes):
    """Returns over the horizons the 5-minute series supports.

    One bar is 5 minutes, so 1/3/6 bars back are the 5/15/30-minute returns.
    """
    out = {}
    for mins in HORIZONS_MIN:
        bars = mins // 5
        out[f"ret_{mins}m_pct"] = (_pct(closes[-1], closes[-1 - bars])
                                   if len(closes) > bars else None)
    return out


def volume_ratio(volumes, recent: int = 3, baseline: int = 24):
    """Recent volume against its own baseline.

    Above 1.0 means the current move is carrying more participation than
    usual. This is the whole point of the volume signal: an identical price
    move on falling volume is the one to distrust.

    Returns None rather than 1.0 when there is not enough history - a
    fabricated "normal" would read as confirmation that was never measured.
    """
    if len(volumes) < baseline + recent:
        return None
    base = sum(volumes[-baseline - recent:-recent]) / baseline
    if base <= 0:
        return None
    return (sum(volumes[-recent:]) / recent) / base


def pullback_quality(closes, highs, lows, look: int = 12):
    """Impulse, then a controlled retrace that holds. 0.0 to 1.0.

    The shape the ask described: a move up, a measured pullback, support, and
    room to recover. Scored on two things a grid actually cares about -

      depth    how far price has retraced from the impulse high. A retrace
               of roughly a third to two thirds is the useful band. Almost
               none means the move has not paused and entering is chasing;
               almost all of it means the impulse failed and there is no
               support to buy against.
      hold     whether the low of the retrace is holding above where the
               impulse began. A retrace that gives back the entire impulse
               is not a pullback, it is a reversal.

    Returns None when the window has no impulse to speak of, which is not a
    zero - zero would claim a bad setup where there is simply no setup.
    """
    if len(closes) < look + 2:
        return None
    window_h, window_l = highs[-look:], lows[-look:]
    hi, lo = max(window_h), min(window_l)
    if hi <= lo:
        return None
    impulse = (hi - lo) / lo * 100.0
    # An "impulse" smaller than a round trip costs is noise, not a setup.
    if impulse < 1.37:
        return None
    price = closes[-1]
    retrace = (hi - price) / (hi - lo)          # 0 = at the high, 1 = back at the low
    if retrace <= 0 or retrace >= 1:
        return 0.0
    # Peak quality at a ~50% retrace, falling off toward either extreme.
    depth = 1.0 - abs(retrace - 0.5) / 0.5
    # Did the pullback's own low hold above the impulse base?
    recent_low = min(window_l[-3:]) if len(window_l) >= 3 else lo
    hold = 1.0 if recent_low > lo else 0.5
    return round(max(0.0, min(1.0, depth * hold)), 4)


def _band(value, lo, hi):
    """Map a reading onto 0-100 across [lo, hi], clamped. None stays None."""
    if value is None:
        return None
    if hi == lo:
        return 0.0
    return round(max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0)), 1)


def score(*, closes, highs, lows, volumes, spread_pct, bid_depth_usd,
          ask_depth_usd, atr_pct, rsi, economics=None, gate_reason=None):
    """The 0-100 composite, its components, and the prediction it implies.

    THE ECONOMICS ARE NOT COMPUTED HERE.

    An earlier draft of this function re-derived expected cost and net edge
    from fees, spread and an adverse-selection constant. That was a second
    copy of a formula the fleet already owns:
    crypto_nine_coin_scanner.evaluate_grid_step() prices spread, book depth,
    slice size, fees and adverse selection, and the live net-edge gate has
    been calling it all along. Two copies of one formula drift, and the one
    that drifts is always the one nobody is watching.

    So the caller runs that same gate - asking it whether a step the size of
    the expected capturable move would clear - and passes its `detail` dict
    in as `economics`. This function contributes only what the gate does not
    measure: short-horizon shape.

    Two rules hold the rest together.

    A missing reading is not a zero. An unreadable book or a short candle
    series yields None for that component, dropped from the average, so the
    score reflects what was measured. Scoring an unknown as zero makes a
    venue hiccup look like a bad setup - the REJECT-versus-BLOCKED
    distinction opportunity_scanner.py already insists on.

    The score never decides anything. would_trade comes from the gate's net
    edge alone, so a 90-point score with a negative edge still returns False.
    The score ranks what is worth watching; the arithmetic decides what is
    worth doing.
    """
    m = momentum(closes)
    vr = volume_ratio(volumes) if volumes else None
    pb = pullback_quality(closes, highs, lows)

    parts = {
        # Momentum over 15 minutes, banded so a move smaller than one round
        # trip's cost scores near nothing and 3% tops it out.
        "score_momentum": _band(abs(m.get("ret_15m_pct") or 0.0), 1.37, 3.0)
                          if m.get("ret_15m_pct") is not None else None,
        # 1.0x volume is unremarkable, 2.5x is real participation.
        "score_volume": _band(vr, 1.0, 2.5),
        "score_pullback": round(pb * 100.0, 1) if pb is not None else None,
        # Volatility has to clear the cost to be worth anything. The top of
        # the band is left wide rather than penalised - the stop handles the
        # tail.
        "score_volatility": _band(atr_pct, 0.7, 3.0) if atr_pct is not None else None,
        # Spread inverted: tighter is better. 0.05% excellent, 0.5% ruinous
        # at this slice size.
        "score_spread": (_band(-spread_pct, -0.5, -0.05)
                         if spread_pct is not None else None),
        # Liquidity against the slice actually traded, not in the abstract.
        # The thinner side decides, because the exit is the one that strands.
        "score_liquidity": (_band(min(bid_depth_usd, ask_depth_usd) / slice_usd, 2.0, 25.0)
                            if (bid_depth_usd and ask_depth_usd and (slice_usd := _slice_of(economics)))
                            else None),
    }

    measured = [v for v in parts.values() if v is not None]
    total = round(sum(measured) / len(measured), 1) if measured else None

    # THE PREDICTION, and there are two of them on purpose.
    #
    # ATR is undirected: it says how far this thing typically travels, not
    # which way. A grid buys a dip and sells a bounce, so what it actually
    # needs to know is whether the move CONTINUES - and that is a question
    # about direction, which recent momentum answers and range does not.
    # So momentum is the primary estimator.
    #
    # Both are guesses, and swapping one unvalidated guess for another proves
    # nothing. So the ATR estimate is recorded beside it and scored against
    # the SAME realised move, and summary() reports which one was closer. The
    # ledger decides, not the argument.
    #
    # The same 0.5 discount applies to both, so the comparison isolates the
    # one thing that differs - range versus direction - rather than the
    # aggressiveness of the estimate. A real order captures a fraction of a
    # move, not its extremes; whether even half is optimistic is exactly what
    # actual_mfe_pct is for.
    expected_move_atr = round(atr_pct * 0.5, 4) if atr_pct is not None else None
    _mom = m.get("ret_15m_pct")
    expected_move = round(abs(_mom) * 0.5, 4) if _mom is not None else expected_move_atr
    # Straight from the gate. None when the gate could not price it, which is
    # BLOCKED, not REJECT - would_trade stays False either way, but the
    # ledger records the difference.
    net_edge = (economics or {}).get("net_edge_pct")
    cost = (round(expected_move - net_edge, 4)
            if expected_move is not None and net_edge is not None else None)

    return {
        **parts,
        "score_total": total,
        "components_measured": len(measured),
        **m,
        "rsi": rsi,
        "atr_pct": atr_pct,
        "volume_ratio": round(vr, 4) if vr is not None else None,
        "spread_pct": spread_pct,
        "bid_depth_usd": bid_depth_usd,
        "ask_depth_usd": ask_depth_usd,
        "expected_move_pct": expected_move,
        "expected_move_atr_pct": expected_move_atr,
        "expected_move_basis": "momentum_15m" if _mom is not None else "atr_fallback",
        "expected_net_edge_pct": net_edge,
        # Named "assumed" because it still contains an ESTIMATED adverse
        # selection term that no completed trade on this configuration has
        # yet checked. It is not a measurement and must never be reported as
        # one - see summary()["cost_basis"].
        "cost_assumed_pct": cost,
        # The arithmetic, alone. Never the score.
        "would_trade": bool(net_edge is not None and net_edge > 0),
        # WHY not, in the gate's own words and in one word.
        "reject_reason": gate_reason,
        "reject_category": categorise_reject(
            gate_reason, bool(net_edge is not None and net_edge > 0),
            expected_move),
    }


def _slice_of(economics):
    """Slice size the gate was priced against, so liquidity is scored against
    the order that would really be sent rather than a number chosen here."""
    return (economics or {}).get("slice_usd")


# The gate's refusals, grouped into the things you would actually DO about
# them. Matched on distinctive fragments of crypto_nine_coin_scanner's own
# strings rather than re-deriving the decision, so this can never disagree
# with the gate about why it refused.
#
# The categories are chosen so that each one points at a different fix:
#
#   expected_move  the move is too small for the cost. A strategy problem,
#                  or a market problem - not an execution one.
#   spread         paying to cross costs more than the move is worth.
#   liquidity      the book cannot absorb the slice at this size.
#   swing_multiple the step is large relative to what this coin does hourly.
#   unpriceable    the book or a required input could not be read at all.
#                  BLOCKED, not REJECTED - the absence of an answer.
#
# Anything unmatched lands in "other" WITH the raw text, so a new refusal
# added upstream shows up as itself instead of being silently bucketed.
# ORDER MATTERS, and so does the conjunction.
#
# unpriceable is checked FIRST because "order book unavailable - cannot price
# the spread" contains the word "spread" and would otherwise be filed as a
# spread rejection - turning the absence of an answer into a specific one,
# which is the exact REJECT-versus-BLOCKED confusion the scanner already
# refuses to make elsewhere. "hourly swing is None - not a usable number" had
# the same problem against swing_multiple.
#
# And each pattern is a conjunction, not a disjunction: a single shared word
# is not evidence. Matching ANY fragment is what produced both of the above.
_REJECT_PATTERNS = (
    ("unpriceable", ("unavailable",)),
    ("unpriceable", ("not a usable number",)),
    ("spread", ("spread", "is over")),
    ("liquidity", ("thin book",)),
    ("swing_multiple", ("x the", "hourly")),
    ("expected_move", ("net edge",)),
)


def categorise_reject(reason: str, qualified: bool, expected_move=None) -> str:
    """One word for why this scan did not become a trade.

    A missing reason is not automatically unknown. When the expected move is
    zero the gate was never asked - there was no step to price - and that is
    the most specific answer available, not the least. BONK returned exactly
    this on the first categorised scan and was filed as "unknown", which
    reads as a failure to classify rather than as a flat market.
    """
    if qualified:
        return "qualified"
    if expected_move is not None and expected_move <= 0:
        return "no_movement"
    if not reason:
        return "unknown"
    low = reason.lower()
    for name, frags in _REJECT_PATTERNS:
        if all(f in low for f in frags):
            return name
    return "other"


async def record(product_id: str, bot_name: str, price: float, scored: dict):
    """Write one prediction, at most once per candle per coin. Never raises.

    Returns True if written, False if skipped as a duplicate of a score
    taken inside the same candle.
    """
    try:
        async with get_session_factory()() as db:
            recent = (await db.execute(
                select(ShortTermSignal.scored_at)
                .where(ShortTermSignal.product_id == product_id)
                .order_by(ShortTermSignal.scored_at.desc()).limit(1))).scalar_one_or_none()
            if recent is not None and (datetime.utcnow() - recent).total_seconds() < SCORE_MIN_GAP_SECONDS:
                return False
            db.add(ShortTermSignal(
                product_id=product_id, bot_name=bot_name, price_at_score=price,
                **{k: scored.get(k) for k in (
                    "score_momentum", "score_volume", "score_pullback",
                    "score_volatility", "score_spread", "score_liquidity",
                    "score_total", "ret_5m_pct", "ret_15m_pct", "ret_30m_pct",
                    "rsi", "atr_pct", "volume_ratio", "spread_pct",
                    "bid_depth_usd", "ask_depth_usd", "expected_move_pct",
                    "expected_move_atr_pct",
                    "expected_net_edge_pct", "cost_assumed_pct", "would_trade",
                    "reject_category", "reject_reason")}))
            await db.commit()
            return True
    except Exception as e:
        log.debug(f"[SIGNAL] record failed for {product_id} (ignored): "
                  f"{type(e).__name__}: {e}")
        return False


async def resolve(session, price_for, max_rows: int = 8, deadline=None):
    """Mark the predictions whose horizons have come due. Never raises.

    `price_for` is an async callable taking a product_id and returning the
    current price, or None. Injected rather than imported so this module
    stays testable without a network.

    Each horizon fills once. The 30-minute pass also computes the verdict
    columns and stamps resolved_at, so a row is complete or visibly not.
    """
    try:
        now = datetime.utcnow()
        async with get_session_factory()() as db:
            rows = (await db.execute(
                select(ShortTermSignal)
                .where(ShortTermSignal.resolved_at.is_(None))
                .order_by(ShortTermSignal.scored_at)
                .limit(max_rows))).scalars().all()
            if not rows:
                return 0
            prices, done = {}, 0
            for row in rows:
                # Each unresolved product costs one book read at up to 15s.
                # Stopping mid-pass is free: an unfilled horizon stays
                # pending and is picked up next cycle. Holding the loop past
                # its lease is not.
                if deadline is not None and time.time() >= deadline:
                    break
                age_min = ((now - row.scored_at).total_seconds() / 60.0) if row.scored_at else 0
                due = [h for h in HORIZONS_MIN
                       if age_min >= h and getattr(row, f"actual_move_{h}m_pct") is None]
                if not due:
                    continue
                if row.product_id not in prices:
                    try:
                        prices[row.product_id] = await price_for(row.product_id)
                    except Exception:
                        prices[row.product_id] = None
                price = prices.get(row.product_id)
                if price is None or not row.price_at_score:
                    continue
                move = (price / row.price_at_score - 1.0) * 100.0
                for h in due:
                    setattr(row, f"actual_move_{h}m_pct", round(move, 4))
                # Excursions accumulate across every pass, so they track the
                # best and worst the move ever reached rather than only the
                # value at one arbitrary sample.
                if row.actual_mfe_pct is None or move > row.actual_mfe_pct:
                    row.actual_mfe_pct = round(move, 4)
                    if (row.expected_move_pct is not None
                            and move >= row.expected_move_pct
                            and row.minutes_to_target is None):
                        row.minutes_to_target = round(age_min, 1)
                if row.actual_mae_pct is None or move < row.actual_mae_pct:
                    row.actual_mae_pct = round(move, 4)

                if row.actual_move_30m_pct is not None:
                    row.materialized = bool(
                        row.expected_move_pct is not None
                        and row.actual_mfe_pct is not None
                        and row.actual_mfe_pct >= row.expected_move_pct)
                    # THE COLUMN THAT SETTLES IT. Priced against the BEST the
                    # move ever reached, which flatters the signal on purpose:
                    # it assumes a perfect exit nobody gets. If the number is
                    # still negative under that assumption, no execution
                    # improvement rescues it.
                    if row.actual_mfe_pct is not None and row.cost_assumed_pct is not None:
                        row.net_after_costs_pct = round(
                            row.actual_mfe_pct - row.cost_assumed_pct, 4)
                    row.resolved_at = now
                    done += 1
            await db.commit()
            return done
    except Exception as e:
        log.debug(f"[SIGNAL] resolve pass failed (ignored): {type(e).__name__}: {e}")
        return 0


def _funnel(rows) -> dict:
    """One coin's setup funnel: detected -> reached -> paid.

    This is the metric that replaces "5 trades per 15 minutes". That target
    asked the market for movement it may simply not have. This asks what the
    market actually provides, per coin, and lets the answer be "not enough".

    profitable is the column that matters and it is deliberately the
    narrowest: a setup can be detected, can reach its target, and still not
    pay, because a round trip's cost is charged per trip and not per percent.
    """
    resolved = [r for r in rows if r.resolved_at is not None]
    nets = [r.net_after_costs_pct for r in resolved if r.net_after_costs_pct is not None]
    reached = [r for r in resolved if r.materialized]
    times = sorted(r.minutes_to_target for r in reached if r.minutes_to_target is not None)
    profitable = [v for v in nets if v > 0]
    return {
        "detected": len(rows),
        "resolved": len(resolved),
        "reached_target": len(reached),
        "reached_pct": round(len(reached) / len(resolved) * 100, 1) if resolved else None,
        "profitable_after_costs": len(profitable),
        "profitable_pct": round(len(profitable) / len(nets) * 100, 1) if nets else None,
        "median_minutes_to_target": times[len(times) // 2] if times else None,
        # Expectancy over EVERY resolved setup, not only the ones that
        # worked. Averaging the winners is how a losing strategy reads as a
        # winning one.
        "net_expectancy_pct": round(sum(nets) / len(nets), 4) if nets else None,
        "would_trade_count": sum(1 for r in rows if r.would_trade),
        # WHERE THE FUNNEL LEAKS. "0 qualified" is not a diagnosis; this is.
        # Sorted most-common first so the top line is the bottleneck.
        "rejected_by": dict(sorted(
            Counter(r.reject_category or "pre_instrumentation"
                    for r in rows if not r.would_trade).items(),
            key=lambda kv: -kv[1])),
        # The most recent reading, so a funnel of zeros still says something.
        # "0 of 6 would trade" is a verdict with no magnitude; the shortfall
        # is what tells you whether this coin is marginally short of paying
        # or nowhere near it, and that difference decides whether waiting is
        # worth anything.
        "latest": _latest(rows),
    }


def _latest(rows) -> dict:
    """Newest reading for one coin: how much movement there is, how much of
    it is expected to be capturable, and how far that lands from paying."""
    if not rows:
        return {}
    r = max(rows, key=lambda x: x.scored_at or datetime.min)
    return {
        "scored_at": r.scored_at.isoformat() + "Z" if r.scored_at else None,
        "score_total": r.score_total,
        "atr_pct": round(r.atr_pct, 3) if r.atr_pct is not None else None,
        "expected_move_pct": r.expected_move_pct,
        "expected_move_basis": "momentum_15m",
        "expected_move_atr_pct": r.expected_move_atr_pct,
        "expected_net_edge_pct": r.expected_net_edge_pct,
        "cost_assumed_pct": r.cost_assumed_pct,
        "spread_pct": round(r.spread_pct, 4) if r.spread_pct is not None else None,
        "ret_15m_pct": r.ret_15m_pct,
        "volume_ratio": r.volume_ratio,
        # How much MORE movement this coin would need before a round trip
        # pays. Positive means it is short by that much.
        "short_by_pct": (round(-r.expected_net_edge_pct, 4)
                         if r.expected_net_edge_pct is not None
                         and r.expected_net_edge_pct < 0 else None),
    }



def _percentiles(values) -> dict:
    """p25 / median / p75 / mean over an already-sorted list.

    Computed in Python rather than SQL on purpose. percentile_cont is
    Postgres-only and this runs against SQLite in every test; a query that
    works in production and not under test is a query nobody checks. The rows
    are already in memory for the rest of this summary, so the round trip
    buys nothing either.
    """
    n = len(values)
    if not n:
        return {"n": 0}

    def at(q):
        # Nearest-rank. With a handful of windows an interpolated percentile
        # invents durations that never occurred; these are real observations.
        return values[min(n - 1, max(0, int(round(q * (n - 1)))))]

    return {"n": n, "p25": at(0.25), "median": at(0.50), "p75": at(0.75),
            "mean": round(sum(values) / n, 1),
            "min": values[0], "max": values[-1]}


def _actionability(dist: dict) -> dict:
    """Can these alerts be acted on, or are they only evidence?

      p75 < delay   three quarters of windows close before the alert lands.
                    The pipeline is formally an OFFLINE OBSERVER: good for
                    measuring whether edges exist, false-alarm rates and
                    diurnal patterns; useless for execution at this cadence.
      p25 > delay   even the short windows outlive the delay. Actionable.
      between       mixed - some windows survive delivery, most may not.

    Stated as a verdict because "p75 is 2100 and the delay is 3600" is the
    same fact and nobody applies it at a glance.
    """
    d = ALERT_DELIVERY_DELAY_SECONDS
    if not dist.get("n"):
        return {"verdict": "no closed windows yet - nothing to judge",
                "delivery_delay_seconds": d}
    if dist["p75"] < d:
        v = (f"OFFLINE OBSERVER - 75% of windows close inside the {d // 60}min alert "
             f"delay (p75 {dist['p75'] / 60:.1f}min). These alerts can measure "
             f"whether opportunities exist; they cannot be traded at this cadence.")
    elif dist["p25"] > d:
        v = (f"ACTIONABLE - even short windows outlive the {d // 60}min delay "
             f"(p25 {dist['p25'] / 60:.1f}min).")
    else:
        v = (f"MIXED - p25 {dist['p25'] / 60:.1f}min, p75 {dist['p75'] / 60:.1f}min "
             f"against a {d // 60}min delay. Some windows survive delivery, most "
             f"may not.")
    return {"verdict": v, "delivery_delay_seconds": d,
            "p25_minutes": round(dist["p25"] / 60, 1),
            "p75_minutes": round(dist["p75"] / 60, 1)}


def is_viable(net_edge_pct, margin: float = None) -> bool:
    """Whether this reading clears its costs by enough to count.

    None is NOT viable and never "unknown-so-assume-yes": a coin the gate
    could not price is BLOCKED, and an alert must never fire on the absence
    of an answer.
    """
    if net_edge_pct is None:
        return False
    return net_edge_pct > (VIABILITY_MARGIN_PCT if margin is None else margin)


async def due_for_score(product_id: str) -> bool:
    """Whether this coin's candle has turned over since its last score.

    Exposed so a caller can skip the NETWORK work, not just the write.
    observe() throttles the row, but the three fetches feeding it were running
    every cycle and having their results discarded seven times out of eight -
    roughly 1,500 wasted requests an hour on a six-coin fleet, every one of
    them inside the budget that protects the trading lease.

    Fails CLOSED: an unreadable throttle means "not due", because scoring more
    often than the candle updates produces duplicate rows that each count as
    an independent prediction in every hit rate computed from them.
    """
    try:
        async with get_session_factory()() as db:
            recent = (await db.execute(
                select(ShortTermSignal.scored_at)
                .where(ShortTermSignal.product_id == product_id)
                .order_by(ShortTermSignal.scored_at.desc()).limit(1))).scalar_one_or_none()
        return (recent is None
                or (datetime.utcnow() - recent).total_seconds() >= SCORE_MIN_GAP_SECONDS)
    except Exception as e:
        log.debug(f"[SIGNAL] throttle unreadable for {product_id}: {e}")
        return False


async def observe(product_id: str, bot_name: str, price: float, scored: dict):
    """One observation: detect any crossing, then store the reading.

    These are deliberately ONE call. Detection compares against the newest
    STORED row, and record() is throttled to one row per candle - so running
    them independently meant eight detections per stored row, each comparing
    against the same stale predecessor, and a coin that became viable would
    re-alert on every cycle for five minutes.

    A crossing is a change between two consecutive OBSERVATIONS. Tying both
    to the same throttle is what makes that true rather than nearly true.

    Returns the crossing dict if one happened, else None. Never raises.
    """
    try:
        async with get_session_factory()() as db:
            recent = (await db.execute(
                select(ShortTermSignal.scored_at)
                .where(ShortTermSignal.product_id == product_id)
                .order_by(ShortTermSignal.scored_at.desc()).limit(1))).scalar_one_or_none()
        if recent is not None and (datetime.utcnow() - recent).total_seconds() < SCORE_MIN_GAP_SECONDS:
            return None
    except Exception as e:
        log.debug(f"[SIGNAL] observe gate failed for {product_id}: {e}")
        return None
    crossing = await detect_crossing(product_id, scored, price)
    await record(product_id, bot_name, price, scored)
    return crossing


async def detect_crossing(product_id: str, scored: dict, price: float):
    """Compare this reading with the last one and record any transition.

    Call through observe(), not directly: this compares against the newest
    stored row, so it must only run when a new row is about to be written.

    Hysteresis is deliberate and asymmetric. Becoming viable requires
    clearing the margin; ceasing to be viable requires falling back below
    ZERO, not merely below the margin. A single threshold would make a coin
    hovering at the buffer flap in and out on rounding, and each flap would
    be an alert.

    Never raises. Returns the crossing dict if one happened, else None.
    """
    try:
        now_viable = is_viable(scored.get("expected_net_edge_pct"))
        async with get_session_factory()() as db:
            prev = (await db.execute(
                select(ShortTermSignal)
                .where(ShortTermSignal.product_id == product_id)
                .order_by(ShortTermSignal.scored_at.desc()).limit(1))).scalars().first()
            if prev is None:
                return None           # nothing to cross FROM

            # "Already viable" is the STATE of an open window, not a
            # re-evaluation of the previous reading against the margin.
            #
            # Deriving it with is_viable(prev) broke the hysteresis it was
            # meant to implement: a coin sitting at +0.05 - above zero, below
            # the margin, and inside a window that was never closed - read as
            # already-not-viable, so the eventual fall below zero produced no
            # close at all and the window stayed open forever. The asymmetry
            # only works if one side is a threshold and the other is a state.
            open_window = (await db.execute(
                select(RegimeCrossing)
                .where(RegimeCrossing.product_id == product_id,
                       RegimeCrossing.direction == "into_viable",
                       RegimeCrossing.window_seconds.is_(None))
                .order_by(RegimeCrossing.crossed_at.desc()).limit(1))).scalars().first()
            was_viable = open_window is not None
            edge = scored.get("expected_net_edge_pct")
            if not was_viable and now_viable:
                direction = "into_viable"
            elif was_viable and (edge is None or edge <= 0):
                direction = "out_of_viable"
            else:
                return None

            # A second open with no close between them would leave the first
            # unclosed forever - NULL window_seconds, excluded from every
            # percentile, and still counted as the open window by the state
            # query above. Cannot happen while the state check holds, which
            # is exactly why it is worth closing defensively rather than
            # trusting an invariant nobody re-checks.
            if direction == "into_viable" and open_window is not None:
                log.warning("[REGIME] %s had an unclosed window from %s - closing it "
                            "before opening a new one", product_id, open_window.crossed_at)
                open_window.window_seconds = round(
                    (datetime.utcnow() - open_window.crossed_at).total_seconds(), 1)

            row = RegimeCrossing(
                product_id=product_id, direction=direction, price_at_cross=price,
                net_edge_pct=edge, expected_move_pct=scored.get("expected_move_pct"),
                cost_pct=scored.get("cost_assumed_pct"),
                score_total=scored.get("score_total"),
                spread_pct=scored.get("spread_pct"),
                margin_required_pct=VIABILITY_MARGIN_PCT, alerted=False)
            db.add(row)

            # Close the window on the matching open crossing, so how long an
            # opportunity LASTED is recorded rather than inferred from two
            # timestamps by whoever reads the table.
            if direction == "out_of_viable" and open_window is not None \
                    and open_window.crossed_at:
                open_window.window_seconds = round(
                    (datetime.utcnow() - open_window.crossed_at).total_seconds(), 1)
            await db.commit()
        log.warning("[REGIME] %s %s - net edge %.3f%% on a %.3f%% expected move "
                    "against %.3f%% cost",
                    product_id, direction.upper().replace("_", " "),
                    edge if edge is not None else float("nan"),
                    scored.get("expected_move_pct") or float("nan"),
                    scored.get("cost_assumed_pct") or float("nan"))
        return {"product_id": product_id, "direction": direction, "net_edge_pct": edge}
    except Exception as e:
        log.debug(f"[REGIME] crossing check failed for {product_id} (ignored): "
                  f"{type(e).__name__}: {e}")
        return None


async def resolve_crossings(candles_for, max_rows: int = 8, deadline=None):
    """Track each alert's price PATH from candle highs and lows, and mark it.

    `candles_for` is an async callable taking a product_id and returning
    (times, closes, highs, lows, volumes) or None. Injected rather than
    imported so this stays testable without a network.

    This replaced sampling the mid price once per cycle. Two reasons, and the
    second is a correctness fix rather than a precision one:

      WICKS. A 37-second sampling interval misses whatever happened between
      samples, and a candle that spikes 3% and closes flat still hit 3% -
      and still would have hit a stop on the way there. Highs and lows are
      the real bounds; mid samples are a subset of them.

      ORDER. MFE and MAE without a sequence are two unrelated numbers. A
      crossing that dumps 4% at minute 5 and rips 6% by minute 25 scores as a
      clean +6% win under MFE alone, when the position had already been
      stopped out and could not collect it. paid_off now accounts for that.

    Also cheaper: one candle fetch per product per pass, against one mid read
    per product per cycle.
    """
    try:
        import time as _t
        now = datetime.utcnow()
        async with get_session_factory()() as db:
            rows = (await db.execute(
                select(RegimeCrossing)
                .where(RegimeCrossing.resolved_at.is_(None),
                       RegimeCrossing.direction == "into_viable")
                .order_by(RegimeCrossing.crossed_at).limit(max_rows))).scalars().all()
            series = {}
            for row in rows:
                if deadline is not None and _t.time() >= deadline:
                    break
                if not row.crossed_at or not row.price_at_cross:
                    continue
                if row.product_id not in series:
                    try:
                        series[row.product_id] = await candles_for(row.product_id)
                    except Exception:
                        series[row.product_id] = None
                c = series.get(row.product_id)
                if not c:
                    continue
                times, _closes, highs, lows, _vols = c
                t0 = int(row.crossed_at.replace(tzinfo=timezone.utc).timestamp())
                exc = window_excursion(times, highs, lows, t0, row.price_at_cross,
                                       until_epoch=t0 + 1800)
                if exc is None:
                    continue
                mfe, mfe_at, mae, mae_at = exc
                row.actual_mfe_pct, row.mfe_at_minutes = mfe, mfe_at
                row.actual_mae_pct, row.mae_at_minutes = mae, mae_at

                if (now - row.crossed_at).total_seconds() < 1800:
                    continue

                # THE SEQUENCE CHECK. If the drawdown breached the live stop
                # before the peak arrived, the position was gone and the MFE
                # was never collectable. Scoring it as a win would credit the
                # strategy with money it could not have taken.
                row.stopped_out_first = bool(
                    mae <= -GRID_STOP_PCT_LABEL and mae_at < mfe_at)
                row.actual_move_30m_pct = mfe if mfe_at >= mae_at else mae
                if row.cost_pct is not None:
                    row.net_after_costs_pct = round(mfe - row.cost_pct, 4)
                    row.paid_off = bool(row.net_after_costs_pct > 0
                                        and not row.stopped_out_first)
                row.resolved_at = now
            await db.commit()
    except Exception as e:
        log.debug(f"[REGIME] crossing resolution failed (ignored): "
                  f"{type(e).__name__}: {e}")


async def regime_summary() -> dict:
    """How close the fleet is to viable, and whether past crossings paid.

    closest_to_viable is the number to watch on a quiet day: it turns "no
    opportunities" from a flat zero into a distance that can be seen moving.
    """
    try:
        async with get_session_factory()() as db:
            rows = (await db.execute(
                select(RegimeCrossing)
                .order_by(RegimeCrossing.crossed_at.desc()).limit(500))).scalars().all()
            latest = (await db.execute(
                select(ShortTermSignal)
                .order_by(ShortTermSignal.scored_at.desc()).limit(60))).scalars().all()
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}

    seen, live = set(), []
    for r in latest:                       # newest reading per coin
        if r.product_id not in seen:
            seen.add(r.product_id)
            live.append(r)
    ranked = sorted(
        (r for r in live if r.expected_net_edge_pct is not None),
        key=lambda r: -r.expected_net_edge_pct)
    opens = [r for r in rows if r.direction == "into_viable"]
    resolved = [r for r in opens if r.resolved_at is not None]
    paid = [r for r in resolved if r.paid_off]
    windows = sorted(r.window_seconds for r in opens if r.window_seconds is not None)

    out = {
        "available": True,
        "margin_required_pct": VIABILITY_MARGIN_PCT,
        "viable_now": [r.product_id for r in ranked if is_viable(r.expected_net_edge_pct)],
        "closest_to_viable": ({"product_id": ranked[0].product_id,
                               "net_edge_pct": ranked[0].expected_net_edge_pct,
                               "short_by_pct": round(
                                   VIABILITY_MARGIN_PCT - ranked[0].expected_net_edge_pct, 4)}
                              if ranked else None),
        "crossings_into_viable": len(opens),
        "crossings_resolved": len(resolved),
        # The DISTRIBUTION, not a lone median. This is the number that decides
        # whether an alert delivered up to an hour late can ever be acted on,
        # and a median alone cannot answer it: windows of 2 and 200 minutes
        # have the same median as windows of 50 and 52, and only one of those
        # worlds is tradeable at an hourly cadence. p75 is the one to read -
        # if three quarters of windows close inside the notification delay,
        # the alert is evidence that opportunities exist and nothing more.
        "window_seconds": _percentiles(windows),
        # THE GO / NO-GO. The distribution only means something relative to
        # how long an alert takes to arrive, so the comparison is made here
        # rather than left to whoever reads two numbers. This line is the one
        # a revert-and-reapply dropped, leaving _actionability defined,
        # six-times tested, and never called.
        "alert_actionability": _actionability(_percentiles(windows)),
    }
    if len(resolved) < 10:
        out["verdict"] = (f"not enough data ({len(resolved)}/10 crossings resolved) - "
                          f"an alert is worth answering only once its hit rate is known")
    else:
        nets = [r.net_after_costs_pct for r in resolved if r.net_after_costs_pct is not None]
        out["paid_off_pct"] = round(len(paid) / len(resolved) * 100, 1)
        out["mean_net_after_costs_pct"] = (round(sum(nets) / len(nets), 4) if nets else None)
        # FALSE ALARM is not the complement of the win rate. One that nets
        # -0.01% was nearly right; one whose best price never covered half its
        # cost was never an opportunity. Those have opposite fixes - tighten
        # the threshold, or abandon the coin - and one number hides which.
        badly_wrong = [r for r in resolved
                       if r.net_after_costs_pct is not None and r.cost_pct
                       and r.net_after_costs_pct < -0.5 * r.cost_pct]
        out["false_alarm_pct"] = round(len(badly_wrong) / len(resolved) * 100, 1)
        maes = [r.actual_mae_pct for r in resolved if r.actual_mae_pct is not None]
        out["worst_drawdown_pct"] = min(maes) if maes else None
        # MAE ON THE WINNERS, a different question from worst-overall.
        #   MFE says whether the edge existed.
        #   MAE on winners says whether it could have been HELD.
        # A 65% hit rate whose winners each dipped 2.5% first is not a 65%
        # strategy at an 8% stop - it is one that gets shaken out before the
        # favourable move develops. The hit rate alone claims alpha nobody
        # could have harvested.
        win_maes = sorted(r.actual_mae_pct for r in paid if r.actual_mae_pct is not None)
        out["mae_on_winners"] = _percentiles(win_maes) if win_maes else {"n": 0}
        # Crossings whose drawdown hit the stop before the peak arrived. Their
        # MFE is real and was never collectable, so they are excluded from
        # paid_off and counted here instead of quietly inflating the win rate.
        out["stopped_out_first"] = sum(1 for r in resolved if r.stopped_out_first)
        if win_maes:
            med = win_maes[len(win_maes) // 2]
            out["holdable"] = (
                f"winners dipped a median {med:.2f}% before paying, against a live "
                f"stop at -{GRID_STOP_PCT_LABEL:.0f}% - "
                + ("survivable." if med > -GRID_STOP_PCT_LABEL else
                   "the stop would have taken most of them out first, so this edge "
                   "cannot be held as configured."))
        # FALSE ALARM is not the complement of the win rate. A crossing that
        # nets -0.01% was nearly right; one whose best price never covered
        # half its cost was never an opportunity at all. Lumping them
        # together hides the difference between a threshold slightly too
        # tight and a signal that means nothing.
        badly_wrong = [r for r in resolved
                       if r.net_after_costs_pct is not None and r.cost_pct
                       and r.net_after_costs_pct < -0.5 * r.cost_pct]
        out["false_alarm_pct"] = round(len(badly_wrong) / len(resolved) * 100, 1)
        # How deep the drawdown got before any of it paid off.
        maes = [r.actual_mae_pct for r in resolved if r.actual_mae_pct is not None]
        out["worst_drawdown_pct"] = min(maes) if maes else None
        out["verdict"] = (
            f"crossings pay: {out['paid_off_pct']}% of alerts cleared costs"
            if out["paid_off_pct"] > 50 else
            f"crossings do NOT pay: only {out['paid_off_pct']}% cleared costs - "
            f"this alert is crying wolf and should not be traded on")
    return out


async def summary(min_rows: int = 30) -> dict:
    """Has a detected setup ever paid? Per coin, and refusing a thin sample.

    Reported per COIN first, because that is the decision the fleet can act
    on: capital can concentrate where short-term movement demonstrably
    covers its costs and leave the rest idle. Score buckets come second, and
    answer a different question - whether the score itself carries any
    information, or is decoration.
    """
    try:
        async with get_session_factory()() as db:
            rows = (await db.execute(
                select(ShortTermSignal)
                .order_by(ShortTermSignal.scored_at.desc())
                .limit(SUMMARY_MAX_ROWS))).scalars().all()
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}
    if not rows:
        return {"available": False, "scored": 0,
                "note": "no opportunity has been scored yet"}

    # From here down is arithmetic over rows that are already in memory, but
    # it is still wrapped by the caller (_never_fails) rather than trusted:
    # this dict is served in the live status payload, and a diagnostic must
    # never be able to take the dashboard down with it.
    resolved = [r for r in rows if r.resolved_at is not None]
    coins = sorted({r.product_id for r in rows if r.product_id})
    out = {
        "available": True,
        "live": SIGNALS_LIVE,
        "scored": len(rows),
        "would_trade_count": sum(1 for r in rows if r.would_trade),
        "window": (f"most recent {SUMMARY_MAX_ROWS} scores"
                   if len(rows) >= SUMMARY_MAX_ROWS else "all scores"),
        "resolved": len(resolved),
        # Stated on every report. The cost side still carries an ESTIMATE
        # that no completed trade on this configuration has checked, so
        # net_after_costs and net_expectancy are assumption-dependent and
        # must not be read as measured outcomes.
        "cost_basis": (f"ASSUMED - fees are real, but adverse selection is still the "
                       f"estimated {ADVERSE_PCT_ASSUMED}%. No completed cycle on this "
                       f"configuration has checked it. Replace when measurable."),
        "per_coin": {c: _funnel([r for r in rows if r.product_id == c]) for c in coins},
        "buckets": {},
    }
    for label, lo, hi in (("0-40", 0, 40), ("40-60", 40, 60),
                          ("60-80", 60, 80), ("80+", 80, 1e9)):
        b = [r for r in resolved
             if r.score_total is not None and lo <= r.score_total < hi]
        nets = [r.net_after_costs_pct for r in b if r.net_after_costs_pct is not None]
        out["buckets"][label] = {
            "n": len(b),
            "materialized_pct": (round(sum(1 for r in b if r.materialized) / len(b) * 100, 1)
                                 if b else None),
            "mean_net_after_costs_pct": round(sum(nets) / len(nets), 4) if nets else None,
            "profitable_share_pct": (round(sum(1 for v in nets if v > 0) / len(nets) * 100, 1)
                                     if nets else None),
        }

    # WHICH ESTIMATOR PREDICTS? Both targets are compared against one
    # identical actual_mfe_pct, so the difference is the estimator and
    # nothing else. reached is the hit rate; mean_abs_error is how far each
    # was from the move that actually happened, which is the sharper measure
    # - an estimator can hit often by predicting almost nothing.
    def _est(field):
        rows_ = [r for r in resolved
                 if getattr(r, field) is not None and r.actual_mfe_pct is not None]
        if not rows_:
            return {"n": 0}
        return {
            "n": len(rows_),
            "reached_pct": round(sum(1 for r in rows_
                                     if r.actual_mfe_pct >= getattr(r, field))
                                 / len(rows_) * 100, 1),
            "mean_predicted_pct": round(sum(getattr(r, field) for r in rows_) / len(rows_), 4),
            "mean_actual_mfe_pct": round(sum(r.actual_mfe_pct for r in rows_) / len(rows_), 4),
            "mean_abs_error": round(sum(abs(getattr(r, field) - r.actual_mfe_pct)
                                        for r in rows_) / len(rows_), 4),
        }

    all_rejects = Counter(r.reject_category or "pre_instrumentation"
                          for r in rows if not r.would_trade)
    out["rejected_by"] = dict(sorted(all_rejects.items(), key=lambda kv: -kv[1]))
    named = {k: v for k, v in all_rejects.items() if k != "pre_instrumentation"}
    if named:
        top, n = max(named.items(), key=lambda kv: kv[1])
        out["top_rejection"] = (f"{top} ({n} of {sum(named.values())} categorised "
                                f"refusals, {n / sum(named.values()) * 100:.0f}%)")
    elif all_rejects:
        out["top_rejection"] = (
            f"none categorised yet - all {sum(all_rejects.values())} refusals "
            f"predate the instrumentation")

    out["estimators"] = {"momentum_15m": _est("expected_move_pct"),
                         "atr_half": _est("expected_move_atr_pct")}
    mom, atr = out["estimators"]["momentum_15m"], out["estimators"]["atr_half"]
    if mom.get("n", 0) >= 20 and atr.get("n", 0) >= 20:
        out["estimator_verdict"] = (
            f"momentum is closer (abs err {mom['mean_abs_error']} vs {atr['mean_abs_error']})"
            if mom["mean_abs_error"] < atr["mean_abs_error"] else
            f"ATR is closer (abs err {atr['mean_abs_error']} vs {mom['mean_abs_error']}) - "
            f"momentum is the primary estimator and the data disagrees with it")
    else:
        out["estimator_verdict"] = "not enough resolved rows to compare estimators"

    if len(resolved) < min_rows:
        out["verdict"] = (f"not enough data ({len(resolved)}/{min_rows} resolved) - "
                          f"a score is decoration until its buckets separate")
        return out

    payers = [c for c, f in out["per_coin"].items()
              if (f["net_expectancy_pct"] or 0) > 0 and f["resolved"] >= 10]
    top = out["buckets"]["80+"]["mean_net_after_costs_pct"]
    bot = out["buckets"]["0-40"]["mean_net_after_costs_pct"]
    if payers:
        out["verdict"] = ("coins whose detected setups pay after costs: "
                          + ", ".join(payers))
    else:
        out["verdict"] = ("NO coin's detected setups pay after costs - the market is "
                          "not providing enough short-term movement to cover a round "
                          "trip, which is an answer about the market, not the signals")
    if top is not None and bot is not None:
        out["score_verdict"] = (
            "high scores separate and clear costs" if top > bot and top > 0 else
            "high scores separate but still do not clear costs" if top > bot else
            "high scores do NOT separate - the score is decoration, do not wire it "
            "to execution")
    return out
