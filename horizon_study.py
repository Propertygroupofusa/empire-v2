"""Does the move exist at a horizon longer than thirty minutes?

WHY THIS EXISTS

opportunity_signals.py scores a setup and then resolves it by looking at what
the market did over the next 5, 15 and 30 minutes. Its verdict, live and
measured over 234 resolved signals, is that no coin's setups pay after costs.
That verdict is TRUE, and it is also answering a thirty-minute question.

The fleet is a grid. A rung has no thirty-minute deadline. It rests until it
fills. So "the market does not move enough" needs to be asked of the horizon
the strategy actually trades on, and the signal ledger structurally cannot
ask it: window_excursion() is bounded at t0 + HORIZONS_MIN[-1], the live
payload reports median_minutes_to_target of 29-30 against a 30-minute cap,
and a maximum that keeps landing on the edge of the window is the signature
of a measurement that stopped before the move did.

WHAT IT MEASURES

  excursion_profile  MFE and MAE over 30m / 2h / 6h / 24h / 72h. MFE is the
                     perfect exit nobody gets, so it is an UPPER BOUND on
                     purpose: if the move does not clear a round trip even
                     there, no execution improvement rescues it.

  rung_profile       What a grid actually does - a resting limit exit at +X%,
                     filled only when a candle HIGH reaches through it. This
                     is the honest number, and it is much worse than the MFE,
                     because the capped winner has to pay for the uncapped
                     loser that never filled.

  adverse_selection  The 0.67% in GRID_ADVERSE_SELECTION_PCT has never been
                     measured. It is roughly half of the 1.37% all-in cost
                     that every refusal in the live funnel is measured
                     against, which makes it the single largest unverified
                     number in the fleet. Measured here as the gap between an
                     unconditional entry and a fill-conditioned one: a
                     resting bid fills exactly when the market comes down to
                     it, and the penalty is whatever that conditioning costs.

WHAT IT DOES NOT DO

It does not trade, and it does not change a threshold. It is evidence, dated
and reproducible, and the numbers it produces are meant to be argued with.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics as st
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# The six the fleet actually holds. Overridable so the same study can be run
# against a candidate list without editing the file.
DEFAULT_PRODUCTS = [p.strip() for p in os.getenv(
    "HORIZON_STUDY_PRODUCTS",
    "BONK-USD,BTC-USD,FLOKI-USD,NEAR-USD,ONDO-USD,TIA-USD").split(",") if p.strip()]

GRANULARITY = 300                       # 5-minute bars, same as the scorer
DEFAULT_DAYS = int(os.getenv("HORIZON_STUDY_DAYS", "21"))

# Named so the comparison with opportunity_signals.HORIZONS_MIN is explicit:
# the first entry here IS that module's last entry.
HORIZONS = (("30m", 1800), ("2h", 7200), ("6h", 21600),
            ("24h", 86400), ("72h", 259200))

RUNG_TARGETS_PCT = (1.0, 1.5, 2.0, 3.0)
RUNG_HORIZONS = ("6h", "24h", "72h")

# One entry every six bars. Overlapping windows stay correlated no matter how
# many of them there are, so n here is a count of samples, not of independent
# observations, and nothing downstream should treat it as the latter.
STRIDE_BARS = 6

# Fees are invoiced; adverse selection is assumed. Keeping them apart is the
# whole point - every result is reported against both, so the cost of the
# assumption is always visible as the gap between two columns.
FEES_ONLY_PCT = float(os.getenv("GRID_MAKER_FEE_PCT", "0.35")) * 2
ASSUMED_ADVERSE_PCT = float(os.getenv("GRID_ADVERSE_SELECTION_PCT", "0.67"))
ALL_IN_PCT = round(FEES_ONLY_PCT + ASSUMED_ADVERSE_PCT, 4)

# A post-only order that has not filled within the hour is the expiry case,
# which maker_expiry_drift already tracks. Counting it as a fill here would
# quietly merge two different populations.
ADVERSE_FILL_WINDOW_SEC = 3600
ADVERSE_DEPTHS_PCT = (0.25, 0.50, 1.00)
ADVERSE_HORIZONS = ("30m", "2h", "6h")


def nearest_rank(values, q: float):
    """The q-th percentile by nearest rank, never interpolated.

    Same convention opportunity_signals._percentiles uses, and for the same
    reason: an interpolated percentile invents a value that no observation
    took, which on a small sample is most of what it reports.
    """
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[k]


def _pct(a, b):
    return (a / b - 1.0) * 100.0 if b else None


def _entries(times, stride: int = STRIDE_BARS):
    return range(0, max(0, len(times) - 1), stride)


def excursion_profile(times, highs, lows, closes, horizons=HORIZONS,
                      stride: int = STRIDE_BARS,
                      fees_pct: float = FEES_ONLY_PCT,
                      all_in_pct: float = ALL_IN_PCT) -> dict:
    """MFE and MAE from every stride-th bar close, out to each horizon.

    Bounded by TIMESTAMP rather than bar count: BONK and FLOKI both have gaps
    where no trade printed inside a 5-minute bucket, and counting bars would
    silently give the illiquid coins a longer window than the liquid ones.
    """
    if not (times and highs and lows and closes):
        return {}
    n = len(times)
    out = {}
    for name, hsec in horizons:
        mfes, maes = [], []
        for i in _entries(times, stride):
            if times[-1] - times[i] < hsec:
                break
            entry = closes[i]
            if not entry:
                continue
            best, worst, j = None, None, i + 1
            while j < n and times[j] <= times[i] + hsec:
                if best is None or highs[j] > best:
                    best = highs[j]
                if worst is None or lows[j] < worst:
                    worst = lows[j]
                j += 1
            if best is None:
                continue
            mfes.append(_pct(best, entry))
            maes.append(_pct(worst, entry))
        if not mfes:
            continue
        out[name] = {
            "n": len(mfes),
            "mfe_mean_pct": round(st.mean(mfes), 4),
            "mfe_p50_pct": round(nearest_rank(mfes, 0.50), 4),
            "mfe_p75_pct": round(nearest_rank(mfes, 0.75), 4),
            "mae_p50_pct": round(nearest_rank(maes, 0.50), 4),
            "mae_p10_pct": round(nearest_rank(maes, 0.10), 4),
            "share_over_fees_pct": round(100.0 * sum(1 for m in mfes if m >= fees_pct) / len(mfes), 2),
            "share_over_all_in_pct": round(100.0 * sum(1 for m in mfes if m >= all_in_pct) / len(mfes), 2),
        }
    return out


def rung_profile(times, highs, lows, closes, targets=RUNG_TARGETS_PCT,
                 horizons=RUNG_HORIZONS, stride: int = STRIDE_BARS,
                 fees_pct: float = FEES_ONLY_PCT,
                 all_in_pct: float = ALL_IN_PCT) -> dict:
    """A resting limit exit at +X%, which is what a grid rung IS.

    The expectancy reported here marks every UNFILLED entry at the horizon's
    close and charges it a round trip, which is deliberately pessimistic - a
    real grid holds instead of liquidating. It is reported that way because
    the opposite convention is exactly the survivorship bias the live ledger
    already has: winners close and get counted, losers stay open and never
    enter the average. Somebody has to carry the unfilled leg, and marking it
    is the only version of that which cannot flatter the result.
    """
    by_name = dict(HORIZONS)
    n = len(times)
    out = {}
    for target in targets:
        for hname in horizons:
            hsec = by_name.get(hname)
            if hsec is None:
                continue
            attempts = fills = 0
            hours_to_fill, mae_before_fill, unfilled_mark = [], [], []
            for i in _entries(times, stride):
                if times[-1] - times[i] < hsec:
                    break
                entry = closes[i]
                if not entry:
                    continue
                attempts += 1
                rung = entry * (1.0 + target / 100.0)
                j, filled_at, worst, last = i + 1, None, entry, entry
                while j < n and times[j] <= times[i] + hsec:
                    if lows[j] < worst:
                        worst = lows[j]
                    if filled_at is None and highs[j] >= rung:
                        filled_at = times[j] - times[i]
                        mae_before_fill.append(_pct(worst, entry))
                    last = closes[j]
                    j += 1
                if filled_at is None:
                    unfilled_mark.append(_pct(last, entry))
                else:
                    fills += 1
                    hours_to_fill.append(filled_at / 3600.0)
            if not attempts:
                continue
            fr = fills / attempts
            mark = st.mean(unfilled_mark) if unfilled_mark else 0.0
            out[f"{target}%@{hname}"] = {
                "target_pct": target,
                "horizon": hname,
                "attempts": attempts,
                "fill_pct": round(100.0 * fr, 2),
                "median_hours_to_fill": round(st.median(hours_to_fill), 2) if hours_to_fill else None,
                "mae_before_fill_p50_pct": round(nearest_rank(mae_before_fill, 0.50), 3) if mae_before_fill else None,
                "mae_before_fill_p10_pct": round(nearest_rank(mae_before_fill, 0.10), 3) if mae_before_fill else None,
                "mean_mark_unfilled_pct": round(mark, 3) if unfilled_mark else None,
                "expectancy_all_in_pct": round(fr * (target - all_in_pct) + (1 - fr) * (mark - all_in_pct), 4),
                "expectancy_fees_only_pct": round(fr * (target - fees_pct) + (1 - fr) * (mark - fees_pct), 4),
            }
    return out


def adverse_selection(times, lows, closes, depths=ADVERSE_DEPTHS_PCT,
                      horizons=ADVERSE_HORIZONS, stride: int = 3,
                      fill_window_sec: int = ADVERSE_FILL_WINDOW_SEC) -> dict:
    """What a resting maker bid is really worth against entering at market.

    Two different numbers get called "adverse selection" and the fleet's cost
    model needs both, because they point in opposite directions.

      continuation  The classic one. A bid fills exactly when the market
                    comes DOWN to it, which is preferentially when it is
                    still going down, and that conditioning is a cost.
                    Measured from the MARKET price at the moment of the fill,
                    so the limit price plays no part in it.

      concession    The other half. The fill happened at the BID, which is
                    below the market. That discount is a benefit, and it is
                    the entire reason to rest an order instead of crossing.

      net_penalty   continuation minus concession: what the maker fill cost
                    in total against an unconditional entry. THIS is the
                    number that belongs beside a fee in a cost model, and it
                    is the one to compare with GRID_ADVERSE_SELECTION_PCT.

    Reporting only `continuation` overstates the cost of resting an order by
    the whole concession; reporting only `net_penalty` hides whether a benign
    total is a market that is genuinely easy to rest in, or a large adverse
    continuation that a large concession happens to be paying for.

    SIGN, once, for all three: POSITIVE is a cost.
    """
    by_name = dict(HORIZONS)
    n = len(times)
    out = {}
    baselines = {}
    for hname in horizons:
        hsec = by_name.get(hname)
        if hsec is None:
            continue
        vals = []
        for i in _entries(times, stride):
            if times[-1] - times[i] < hsec:
                break
            if not closes[i]:
                continue
            j, last = i + 1, closes[i]
            while j < n and times[j] <= times[i] + hsec:
                last = closes[j]
                j += 1
            vals.append(_pct(last, closes[i]))
        if vals:
            baselines[hname] = st.mean(vals)

    for depth in depths:
        for hname in horizons:
            hsec, base = by_name.get(hname), baselines.get(hname)
            if hsec is None or base is None:
                continue
            from_bid, from_mkt, fills, attempts = [], [], 0, 0
            for i in _entries(times, stride):
                if times[-1] - times[i] < fill_window_sec + hsec:
                    break
                if not closes[i]:
                    continue
                attempts += 1
                bid = closes[i] * (1.0 - depth / 100.0)
                j, fill_t = i + 1, None
                while j < n and times[j] <= times[i] + fill_window_sec:
                    if lows[j] <= bid:
                        fill_t = times[j]
                        break
                    j += 1
                if fill_t is None:
                    continue
                fills += 1
                mkt = closes[j]
                k, last = j + 1, bid
                while k < n and times[k] <= fill_t + hsec:
                    last = closes[k]
                    k += 1
                from_bid.append(_pct(last, bid))
                if mkt:
                    from_mkt.append(_pct(last, mkt))
            if not from_bid or not attempts:
                continue
            fb, fm = st.mean(from_bid), (st.mean(from_mkt) if from_mkt else None)
            net = base - fb
            cont = (base - fm) if fm is not None else None
            out[f"{depth}%@{hname}"] = {
                "depth_pct": depth,
                "horizon": hname,
                "n_fills": fills,
                "fill_pct": round(100.0 * fills / attempts, 2),
                "baseline_pct": round(base, 4),
                "fill_from_bid_pct": round(fb, 4),
                "fill_from_market_pct": round(fm, 4) if fm is not None else None,
                # All three carry the sign convention fixed in the
                # docstring above; it is stated there once and not restated
                # here, so the two can never drift apart.
                "continuation_pct": round(cont, 4) if cont is not None else None,
                "concession_pct": round(cont - net, 4) if cont is not None else None,
                "net_penalty_pct": round(net, 4),
            }
    return out


async def fetch_history(session, product_id: str, days: int = DEFAULT_DAYS,
                        granularity: int = GRANULARITY, pause: float = 0.35):
    """Paginated 5-minute candles, oldest-first, deduplicated by timestamp.

    opportunity_signals.fetch_candles_full reads the same endpoint but takes
    the default page - 300 bars, about 25 hours. A horizon study needs weeks,
    and the endpoint caps every response at 300 rows, so the window has to be
    walked backwards explicitly.

    Returns (times, lows, highs, closes) or None. Never raises.
    """
    rows, now = {}, int(time.time())
    end, floor = now, now - days * 86400
    try:
        while end > floor:
            start = max(floor, end - 300 * granularity)
            url = (f"https://api.exchange.coinbase.com/products/{product_id}/candles"
                   f"?granularity={granularity}"
                   f"&start={datetime.utcfromtimestamp(start).isoformat()}Z"
                   f"&end={datetime.utcfromtimestamp(end).isoformat()}Z")
            async with session.get(url, headers={"Accept": "application/json"},
                                   timeout=30) as r:
                if r.status != 200:
                    break
                data = await r.json()
            if not data:
                break
            for x in data:
                rows[int(x[0])] = (float(x[1]), float(x[2]), float(x[4]))
            end = start
            await asyncio.sleep(pause)
    except Exception as e:
        log.debug(f"[HORIZON] history fetch failed for {product_id}: {e}")
    if len(rows) < 100:
        return None
    ts = sorted(rows)
    return (ts, [rows[t][0] for t in ts], [rows[t][1] for t in ts],
            [rows[t][2] for t in ts])


def summarise(per_coin: dict) -> dict:
    """The three sentences the study exists to be able to say.

    Each one is a comparison the live payload cannot make, because the live
    payload only ever sees a thirty-minute window.
    """
    thirty, six = [], []
    adverse_at_half_2h, continuation_at_half_2h = [], []
    best_fees, best_all_in = None, None
    for coin, v in per_coin.items():
        h = v.get("horizons") or {}
        if "30m" in h:
            thirty.append(h["30m"]["share_over_all_in_pct"])
        if "6h" in h:
            six.append(h["6h"]["share_over_all_in_pct"])
        a = (v.get("adverse") or {}).get("0.5%@2h")
        if a:
            adverse_at_half_2h.append(a["net_penalty_pct"])
            if a.get("continuation_pct") is not None:
                continuation_at_half_2h.append(a["continuation_pct"])
        for k, r in (v.get("rungs") or {}).items():
            for field, cur in (("expectancy_fees_only_pct", best_fees),
                               ("expectancy_all_in_pct", best_all_in)):
                cand = (r[field], coin, k)
                if cur is None or cand[0] > cur[0]:
                    if field == "expectancy_fees_only_pct":
                        best_fees = cand
                    else:
                        best_all_in = cand
    out = {
        "coins": len(per_coin),
        "share_clearing_cost_at_30m_pct": round(st.mean(thirty), 2) if thirty else None,
        "share_clearing_cost_at_6h_pct": round(st.mean(six), 2) if six else None,
        "measured_net_penalty_pct": round(st.mean(adverse_at_half_2h), 4) if adverse_at_half_2h else None,
        "measured_continuation_pct": (round(st.mean(continuation_at_half_2h), 4)
                                      if continuation_at_half_2h else None),
        "assumed_adverse_pct": ASSUMED_ADVERSE_PCT,
        "all_in_pct": ALL_IN_PCT,
        "fees_only_pct": FEES_ONLY_PCT,
        "best_rung_all_in": ({"expectancy_pct": best_all_in[0], "product_id": best_all_in[1],
                              "rung": best_all_in[2]} if best_all_in else None),
        "best_rung_fees_only": ({"expectancy_pct": best_fees[0], "product_id": best_fees[1],
                                 "rung": best_fees[2]} if best_fees else None),
    }
    a, b = out["share_clearing_cost_at_30m_pct"], out["share_clearing_cost_at_6h_pct"]
    if a is not None and b is not None:
        out["horizon_verdict"] = (
            f"{a}% of entries ever reach the {ALL_IN_PCT}% round trip inside 30 minutes; "
            f"{b}% do inside 6 hours. The signal ledger stops looking at 30 minutes, so "
            f"its 'no coin pays' verdict is a statement about the window, not the market.")
    m, c = out["measured_net_penalty_pct"], out["measured_continuation_pct"]
    if m is not None:
        out["adverse_verdict"] = (
            f"A resting maker bid MEASURED at {m:+.3f}% per fill against the "
            f"{ASSUMED_ADVERSE_PCT}% assumed"
            + (f" (adverse continuation {c:+.3f}%, limit concession {c - m:+.3f}%)"
               if c is not None else "")
            + f". The assumption is {round(ASSUMED_ADVERSE_PCT - m, 3)} points of cost that this "
              f"sample does not support - but the sample has no bear leg in it, and a resting bid "
              f"is flattered by a rising market, so this replaces a guess with a dated "
              f"measurement, not with a new constant.")
    return out


async def run_study(session, products=None, days: int = DEFAULT_DAYS) -> dict:
    """Fetch, measure, summarise. Returns the whole study as a dict."""
    products = products or DEFAULT_PRODUCTS
    per_coin, drift = {}, {}
    for p in products:
        hist = await fetch_history(session, p, days=days)
        if not hist:
            log.warning(f"[HORIZON] no usable history for {p}")
            continue
        times, lows, highs, closes = hist
        per_coin[p] = {
            "bars": len(times),
            "span_days": round((times[-1] - times[0]) / 86400.0, 2),
            "window_return_pct": round(_pct(closes[-1], closes[0]), 3),
            "horizons": excursion_profile(times, highs, lows, closes),
            "rungs": rung_profile(times, highs, lows, closes),
            "adverse": adverse_selection(times, lows, closes),
        }
        drift[p] = per_coin[p]["window_return_pct"]
    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "granularity_seconds": GRANULARITY,
        "products": list(per_coin),
        "window_returns_pct": drift,
        "per_coin": per_coin,
        "summary": summarise(per_coin),
    }
    up = [v for v in drift.values() if v is not None and v > 0]
    if drift and len(up) == len(drift):
        # Stated on the object rather than in a comment, because every
        # favourable-looking number below was drawn from a sample in which
        # every instrument rose, and a reader who misses that will take a
        # bull-market artefact for an edge.
        out["sample_caveat"] = (
            f"ALL {len(drift)} instruments ROSE over this window "
            f"({min(drift.values()):+.1f}% to {max(drift.values()):+.1f}%). A long-only "
            f"resting-rung result that is NEGATIVE here is robust, because the sample "
            f"was favourable. A result that is POSITIVE here is not yet evidence of an "
            f"edge - it has not been shown a falling market.")
    return out


# ---------------------------------------------------------------- storage
#
# The study costs about ninety paginated requests and several minutes. The
# telemetry budget is 25 seconds and the loop lease is 180. Those numbers are
# not close, so the study never runs on the request path: it runs rarely, out
# of band, and the dashboard reads the last stored row.

STUDY_INTERVAL_HOURS = float(os.getenv("HORIZON_STUDY_INTERVAL_HOURS", "12"))

# In-process, deliberately. A DB-backed lock would be one more thing that can
# wedge, and the cost of two web dynos each running the study once a day is a
# few hundred public candle requests, not a trading decision.
_last_started_at = 0.0
_running = False


async def persist(study: dict) -> bool:
    """Append one run. Never raises - a failed write must not kill the task."""
    try:
        from database import get_session_factory
        from models import HorizonStudyRun
        async with get_session_factory()() as db:
            db.add(HorizonStudyRun(
                days=study.get("days"),
                products_csv=",".join(study.get("products") or []),
                payload_json=json.dumps(study)))
            await db.commit()
        return True
    except Exception as e:
        log.warning(f"[HORIZON] could not persist study: {type(e).__name__}: {e}")
        return False


async def latest() -> dict:
    """The most recent stored run, or a shape that says why there is none.

    Returns the study with `as_of` and `age_hours` attached, because a
    horizon measurement is only worth the date on it: a window that ended
    before the market turned is a historical fact, not a current one, and a
    panel that prints the number without the date invites it to be read as
    the latter.
    """
    try:
        from sqlalchemy import select
        from database import get_session_factory
        from models import HorizonStudyRun
        async with get_session_factory()() as db:
            row = (await db.execute(
                select(HorizonStudyRun)
                .order_by(HorizonStudyRun.run_at.desc()).limit(1))).scalars().first()
        if row is None or not row.payload_json:
            return {"available": False,
                    "reason": "no run stored yet - the study runs out of band, "
                              f"at most once every {STUDY_INTERVAL_HOURS:g}h"}
        study = json.loads(row.payload_json)
        study["available"] = True
        study["stored_at"] = row.run_at.isoformat() if row.run_at else None
        if row.run_at:
            study["age_hours"] = round(
                (datetime.utcnow() - row.run_at).total_seconds() / 3600.0, 1)
        return study
    except Exception as e:
        log.warning(f"[HORIZON] could not read stored study: {type(e).__name__}: {e}")
        return {"available": False, "reason": f"read failed: {type(e).__name__}"}


def due() -> bool:
    """Throttle, checked BEFORE anything is fetched.

    opportunity_signals learned this one the expensive way: a throttle that
    gates the WRITE instead of the work still makes every request, and the
    fleet spent about 1,500 wasted candle calls an hour proving it.
    """
    if _running:
        return False
    return (time.time() - _last_started_at) >= STUDY_INTERVAL_HOURS * 3600


def spawn_if_due() -> bool:
    """Fire the study into the background. Returns whether it was started.

    Fire-and-forget on purpose. The caller is inside the loop lease and must
    never await this: several minutes of candle pagination inside a 180-second
    lease is how a fleet stops trading in order to measure itself.
    """
    if not due():
        return False
    global _last_started_at, _running
    prev_started = _last_started_at
    _last_started_at, _running = time.time(), True

    async def _go():
        global _running
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                study = await run_study(s)
            if study.get("per_coin"):
                await persist(study)
                log.info(f"[HORIZON] study stored: {study['summary'].get('horizon_verdict')}")
            else:
                log.warning("[HORIZON] study produced no usable coins; nothing stored")
        except Exception as e:
            log.warning(f"[HORIZON] background study failed: {type(e).__name__}: {e}")
        finally:
            _running = False

    try:
        asyncio.get_running_loop().create_task(_go())
        return True
    except RuntimeError:
        # No running loop: nothing was started, so the throttle must be given
        # back. Consuming it here would silently buy twelve hours of silence
        # for a study that never ran - the failure mode where a panel keeps
        # showing a stale measurement and nothing anywhere says why.
        _last_started_at, _running = prev_started, False
        return False


async def main():
    import aiohttp
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    async with aiohttp.ClientSession() as session:
        study = await run_study(session)
    path = os.getenv("HORIZON_STUDY_OUT", "horizon_study.json")
    with open(path, "w") as f:
        json.dump(study, f, indent=1)
    if study.get("per_coin") and os.getenv("HORIZON_STUDY_PERSIST", "true").lower() == "true":
        print("stored to the database" if await persist(study) else "NOT stored")
    s = study["summary"]
    print(json.dumps(s, indent=1))
    if "sample_caveat" in study:
        print("\n" + study["sample_caveat"])
    print(f"\nwritten to {path}")


if __name__ == "__main__":
    asyncio.run(main())
