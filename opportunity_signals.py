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
from datetime import datetime, timedelta

from sqlalchemy import select

from database import get_session_factory
from models import ShortTermSignal

log = logging.getLogger(__name__)

# The switch. Off means the score is recorded and ignored. It is read at call
# time, never cached, so it can be turned on without a restart - and it fails
# OFF, because an unreadable switch must not start steering real money.
SIGNALS_LIVE = os.getenv("OPPORTUNITY_SIGNALS_LIVE", "false").strip().lower() == "true"

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


async def fetch_candles_with_volume(session, product_id: str, granularity: int = 300):
    """5-minute candles INCLUDING volume, oldest-first.

    The engine's _fetch_candles drops column 5. Volume confirmation was the
    second signal asked for and cannot be computed without it, so this reads
    the same public endpoint and keeps it. Separate function rather than a
    change to the engine's: that one has other callers and none of them
    should acquire a new return shape for this.

    Returns (closes, highs, lows, volumes) or None. Never raises.
    """
    url = (f"https://api.exchange.coinbase.com/products/{product_id}"
           f"/candles?granularity={granularity}")
    try:
        async with session.get(url, headers={"Accept": "application/json"}, timeout=15) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if not data or len(data) < 20:
                return None
            # Coinbase returns newest-first: [time, low, high, open, close, volume]
            c = list(reversed(data))
            return ([float(x[4]) for x in c], [float(x[2]) for x in c],
                    [float(x[1]) for x in c], [float(x[5]) for x in c])
    except Exception as e:
        log.debug(f"[SIGNAL] candle fetch failed for {product_id}: {e}")
        return None


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
          ask_depth_usd, atr_pct, rsi, economics=None):
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

    # THE PREDICTION. Expected capturable move is deliberately NOT the full
    # recent range: a real order captures a fraction of a swing, not its
    # extremes. Half of ATR is the honest starting estimate, and the resolver
    # exists to find out whether even that is optimistic.
    expected_move = round(atr_pct * 0.5, 4) if atr_pct is not None else None
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
        "expected_net_edge_pct": net_edge,
        # Named "assumed" because it still contains an ESTIMATED adverse
        # selection term that no completed trade on this configuration has
        # yet checked. It is not a measurement and must never be reported as
        # one - see summary()["cost_basis"].
        "cost_assumed_pct": cost,
        # The arithmetic, alone. Never the score.
        "would_trade": bool(net_edge is not None and net_edge > 0),
    }


def _slice_of(economics):
    """Slice size the gate was priced against, so liquidity is scored against
    the order that would really be sent rather than a number chosen here."""
    return (economics or {}).get("slice_usd")


async def record(product_id: str, bot_name: str, price: float, scored: dict):
    """Write one prediction. Never raises."""
    try:
        async with get_session_factory()() as db:
            db.add(ShortTermSignal(
                product_id=product_id, bot_name=bot_name, price_at_score=price,
                **{k: scored.get(k) for k in (
                    "score_momentum", "score_volume", "score_pullback",
                    "score_volatility", "score_spread", "score_liquidity",
                    "score_total", "ret_5m_pct", "ret_15m_pct", "ret_30m_pct",
                    "rsi", "atr_pct", "volume_ratio", "spread_pct",
                    "bid_depth_usd", "ask_depth_usd", "expected_move_pct",
                    "expected_net_edge_pct", "cost_assumed_pct", "would_trade")}))
            await db.commit()
    except Exception as e:
        log.debug(f"[SIGNAL] record failed for {product_id} (ignored): "
                  f"{type(e).__name__}: {e}")


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
            rows = (await db.execute(select(ShortTermSignal))).scalars().all()
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
