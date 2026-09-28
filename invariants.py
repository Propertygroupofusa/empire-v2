"""Numbers that must agree, checked on a schedule instead of by eye.

WHY THIS EXISTS

Every real defect found on 2026-09-27/28 was found the same way: the
account owner noticed a figure that looked wrong, and a person went
digging. That does not scale and it should not be the owner's job.

    phantom entry fee on adopted slices   found by digging
    maker rate never persisted            found by digging
    a truncated 200 that never retried    found by luck
    cash labelled "balance"               found by the owner panicking
    spacing evidence priced at 1.37%      found while doing something else

Each one was a number disagreeing with its own source. None of them
announced itself. So each is now an INVARIANT: a statement that two
independently-derived numbers must agree, checked every pass, which fails
loudly the moment it stops being true.

THE RULES EVERY CHECK FOLLOWS

  * Compare against an INDEPENDENT source. A number checked against itself
    proves nothing. The fee rate is checked against Coinbase's own billed
    commission, not against another copy of the same cached value.
  * UNKNOWN is a third verdict, never a pass and never a zero. A check that
    could not read its input says so. Silence must not look like health.
  * State the arithmetic. A failure that says "mismatch" sends the reader
    digging - the exact cost this module exists to remove. Every verdict
    carries both numbers and their difference.
  * A tolerance is a number with a reason, never a round guess.

Pure functions over already-fetched data: no network, no database, so the
checks are testable and cannot themselves be the thing that breaks.
"""

import datetime as _dt

OK, FAIL, UNKNOWN = "OK", "FAIL", "UNKNOWN"


def _v(name, status, detail, **extra):
    out = {"name": name, "status": status, "detail": detail}
    out.update(extra)
    return out


def _num(v):
    """A float, or None. None means UNREADABLE and never 0.0."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _utcnow():
    return _dt.datetime.now(_dt.timezone.utc)


def _as_dt(v):
    """A timezone-aware datetime, or None. Accepts a datetime, an ISO
    string (with or without a Z), or epoch seconds. A naive datetime is
    read as UTC, which is what every writer in this codebase stores.
    """
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)
    if isinstance(v, (int, float)):
        try:
            return _dt.datetime.fromtimestamp(float(v), _dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        out = _dt.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return out if out.tzinfo else out.replace(tzinfo=_dt.timezone.utc)


def _hours_between(a, b):
    return abs((b - a).total_seconds()) / 3600.0


# ── 1. the fee rate every other number is priced against ────────────────
# Tolerance: 0.0005 per leg = 0.05%. Coinbase quotes fee tiers to the basis
# point, so anything under half a basis point is quoting noise; anything
# above it means two parts of the system are pricing trades differently.
FEE_RATE_TOLERANCE = 0.0005


def fee_rate_agreement(floor_leg_rate, reported_maker_leg, measured_leg_rate,
                       tolerance=FEE_RATE_TOLERANCE):
    """The rate the FLOOR prices against vs the rate the dashboard REPORTS
    vs the rate Coinbase actually BILLED.

    Caught live 2026-09-28: one worker computed a 0.90% floor (maker leg
    0.0035) while another reported maker_round_trip_fee_rate 0.015 - the
    taker rate wearing the maker label - from the same deploy in the same
    second, because the maker rate was cached in memory and never persisted.
    Two copies of one number silently disagreeing is precisely what a
    single-source check cannot see and this one can.
    """
    if floor_leg_rate is None or reported_maker_leg is None:
        return _v("fee_rate_agreement", UNKNOWN,
                  "one of the rates could not be read - not a pass and not a zero",
                  floor_leg_rate=floor_leg_rate, reported_maker_leg=reported_maker_leg)
    gap = abs(float(floor_leg_rate) - float(reported_maker_leg))
    if gap > tolerance:
        return _v("fee_rate_agreement", FAIL,
                  f"the floor prices {floor_leg_rate:.6f}/leg while the panel reports "
                  f"{reported_maker_leg:.6f}/leg - a gap of {gap:.6f}, over the "
                  f"{tolerance:.6f} tolerance. Two parts of the system are pricing "
                  f"trades differently; a slice looks sellable or not depending on "
                  f"which one is asked.", gap=round(gap, 6))
    if measured_leg_rate is None:
        return _v("fee_rate_agreement", UNKNOWN,
                  f"internally consistent at {floor_leg_rate:.6f}/leg, but Coinbase's "
                  f"own billed rate could not be read to confirm it")
    drift = abs(float(floor_leg_rate) - float(measured_leg_rate))
    if drift > tolerance:
        return _v("fee_rate_agreement", FAIL,
                  f"the system prices {floor_leg_rate:.6f}/leg but Coinbase actually "
                  f"billed {measured_leg_rate:.6f}/leg across real fills - a gap of "
                  f"{drift:.6f}. Every P&L figure is off by that much.",
                  gap=round(drift, 6))
    return _v("fee_rate_agreement", OK,
              f"floor, panel and Coinbase's billed rate agree at ~{floor_leg_rate:.6f}/leg")


# ── 2. evidence that was priced at a fee which has since moved ──────────
# Tolerance: 0.002 on a round trip = 0.20%, the same DEFAULT_MIN_NET_MARGIN_PCT
# the fee floor adds. A cost error smaller than the margin cannot flip a
# spacing decision; one larger than it can.
EVIDENCE_FEE_TOLERANCE = 0.002


def spacing_evidence_current(evidence_priced_at_round_trip, measured_round_trip,
                             tolerance=EVIDENCE_FEE_TOLERANCE):
    """The fleet's minimum step is set by a measured table. That table was
    priced at a round-trip fee. If the real fee has moved away from it, the
    table's ranking may no longer hold - and nothing re-runs it.

    Found 2026-09-28: FLEET_MIN_STEP_PCT rests on 14 days of candles priced
    at a 1.37% round trip, while the measured cost is 0.70%. Re-running the
    same model at the real fee flattens the ordering that justified the
    2.5% minimum. The constant is correct; the evidence under it is stale.

    A comment is not a checkable claim, which is why this takes the figure
    as an argument and fails when it drifts.
    """
    if evidence_priced_at_round_trip is None or measured_round_trip is None:
        return _v("spacing_evidence_current", UNKNOWN,
                  "either the evidence's assumed fee or the measured fee is unreadable")
    gap = abs(float(evidence_priced_at_round_trip) - float(measured_round_trip))
    if gap > tolerance:
        return _v("spacing_evidence_current", FAIL,
                  f"the spacing evidence was priced at a {evidence_priced_at_round_trip*100:.2f}% "
                  f"round trip; the measured cost is {measured_round_trip*100:.2f}% - a "
                  f"{gap*100:.2f} point gap, over the {tolerance*100:.2f} point tolerance. "
                  f"Re-run the step comparison at the real fee before trusting the "
                  f"minimum it sets.", gap=round(gap, 6))
    return _v("spacing_evidence_current", OK,
              f"the spacing evidence was priced at {evidence_priced_at_round_trip*100:.2f}% "
              f"against a measured {measured_round_trip*100:.2f}% - still current")


# ── 3. what the branches claim vs what is really on the exchange ────────
# Tolerance: 0.5% of claimed. Below that is fee dust and mid-price drift on
# open slices; above it means a branch is trading against money that is not
# there, which is how an order gets rejected mid-cycle.
BACKING_TOLERANCE_PCT = 0.005


def allocation_backed(claimed_usd, really_there_usd, tolerance_pct=BACKING_TOLERANCE_PCT):
    """Branch bookkeeping vs the real account. The dashboard already shows
    this; having it here makes it a scheduled check rather than a banner
    somebody has to be looking at."""
    if claimed_usd is None or really_there_usd is None:
        return _v("allocation_backed", UNKNOWN, "claimed or real total unreadable")
    if claimed_usd <= 0:
        return _v("allocation_backed", UNKNOWN, "nothing claimed - no ratio to take")
    gap = float(claimed_usd) - float(really_there_usd)
    pct = abs(gap) / float(claimed_usd)
    direction = "MORE than is really there" if gap > 0 else "less than is really there"
    if pct > tolerance_pct:
        return _v("allocation_backed", FAIL,
                  f"branches claim ${claimed_usd:,.2f} against ${really_there_usd:,.2f} really "
                  f"there - ${abs(gap):,.2f} ({pct*100:.2f}%) {direction}, over the "
                  f"{tolerance_pct*100:.2f}% tolerance.", gap_usd=round(gap, 2))
    return _v("allocation_backed", OK,
              f"branches claim ${claimed_usd:,.2f}, ${really_there_usd:,.2f} really there - "
              f"${abs(gap):,.2f} ({pct*100:.2f}%) apart, within tolerance")


# ── 4. cash that fell without a trade to explain it ─────────────────────
# Tolerance: $5.00. Commission on a round trip at this fleet's sizes runs
# well under a dollar, and several can settle between two reads; $5 is
# comfortably above that and far below any single slice.
CASH_RECONCILE_TOLERANCE_USD = 5.00


def cash_reconciles(cash_before, cash_now, spent_on_buys, proceeds_from_sells,
                    tolerance_usd=CASH_RECONCILE_TOLERANCE_USD):
    """Wallet movement must equal the trades that caused it.

    This is the check that answers "where did my money go" without anyone
    digging. A residual in the account's FAVOUR rules out a leak; only a
    large negative residual is a finding.
    """
    for v in (cash_before, cash_now, spent_on_buys, proceeds_from_sells):
        if v is None:
            return _v("cash_reconciles", UNKNOWN,
                      "a leg of the reconciliation is unreadable - a gap is not a zero")
    predicted = float(cash_before) - float(spent_on_buys) + float(proceeds_from_sells)
    residual = float(cash_now) - predicted
    line = (f"${cash_before:,.2f} - ${spent_on_buys:,.2f} bought + "
            f"${proceeds_from_sells:,.2f} sold = ${predicted:,.2f} predicted, "
            f"${cash_now:,.2f} actual, residual ${residual:+,.2f}")
    if residual < -tolerance_usd:
        return _v("cash_reconciles", FAIL,
                  f"cash is ${abs(residual):,.2f} LOWER than the trades explain. {line}",
                  residual_usd=round(residual, 2))
    return _v("cash_reconciles", OK, line + (
        " - in the account's favour, which rules out a leak" if residual > 0 else ""),
        residual_usd=round(residual, 2))


# ── 5. capital that can neither buy nor sell ────────────────────────────
def no_dead_capital(branches):
    """A branch full of slices AND underwater is a hold, not a grid.

    branches: dicts with product_id, allocated_usd, open_slices, num_levels,
    best_slice_net_pct. Deliberately takes the shape the status payload
    already has, so the check cannot drift from what the dashboard shows.
    """
    if not branches:
        return _v("no_dead_capital", UNKNOWN, "no branch data")
    dead, usd = [], 0.0
    for b in branches:
        n, lv = b.get("open_slices"), b.get("num_levels")
        best = b.get("best_slice_net_pct")
        if n is None or lv is None or best is None:
            continue
        if n >= lv and best <= 0:
            dead.append(b.get("product_id"))
            usd += float(b.get("allocated_usd") or 0.0)
    if dead:
        return _v("no_dead_capital", FAIL,
                  f"${usd:,.2f} across {len(dead)} branch(es) can neither buy (full) nor "
                  f"sell (underwater): {', '.join(str(d) for d in dead)}. That capital is "
                  f"held, not gridded.", stuck_usd=round(usd, 2), branches=dead)
    return _v("no_dead_capital", OK, "every branch can either buy a dip or sell a rise")


# ── 6. the read itself ──────────────────────────────────────────────────
def read_complete(payload_bytes, parsed_ok, expected_min_bytes):
    """A TRUNCATED BODY IS A 200. Hit live 2026-09-28: grid-status answered
    HTTP 200 with the JSON cut off at 18,615 of ~108,000 bytes. Every number
    downstream of an unchecked read is worthless, so this runs first."""
    if payload_bytes is None:
        return _v("read_complete", UNKNOWN, "no byte count available")
    if not parsed_ok:
        return _v("read_complete", FAIL,
                  f"the response did not parse - {payload_bytes} bytes received. This is a "
                  f"cut-off read, NOT a zero. Every figure from it must be discarded.")
    if expected_min_bytes and payload_bytes < expected_min_bytes:
        return _v("read_complete", FAIL,
                  f"the response parsed but is {payload_bytes} bytes against at least "
                  f"{expected_min_bytes} expected - it is short, so something is missing "
                  f"from it.")
    return _v("read_complete", OK, f"{payload_bytes} bytes, parsed whole")


def summarize(results):
    """One line a human can act on, and a status that sorts worst-first."""
    fails = [r for r in results if r["status"] == FAIL]
    unknown = [r for r in results if r["status"] == UNKNOWN]
    if fails:
        status = FAIL
        headline = (f"{len(fails)} invariant(s) BROKEN: "
                    + "; ".join(r["name"] for r in fails))
    elif unknown:
        status = UNKNOWN
        headline = (f"all readable invariants hold, but {len(unknown)} could not be "
                    f"checked: " + "; ".join(r["name"] for r in unknown))
    else:
        status = OK
        headline = f"all {len(results)} invariants hold"
    order = {FAIL: 0, UNKNOWN: 1, OK: 2}
    return {"status": status, "headline": headline,
            "checks": sorted(results, key=lambda r: order[r["status"]]),
            "failed": len(fails), "unknown": len(unknown), "total": len(results)}


def maker_only_holds(taker_fills, classified_fills, newest_taker_at, armed_at,
                     maker_only_active=True):
    """Is the market fallback firing while maker-only is on?

    This check used to be permanently UNKNOWN, and its own detail text told
    the reader to go and work the answer out by hand: "check the newest
    taker fill's timestamp against when it was armed." An invariant that
    delegates its verdict to a human is not an invariant - it sat UNKNOWN
    for as long as anyone looked at it, reporting 34 of 85 fills as TAKER
    and drawing no conclusion from it either way.

    It was unanswerable because two facts were missing, not because the
    question was hard. Both are now recorded:

      * the newest TAKER fill's timestamp (summarise_fills), rather than a
        bare count over a window that reaches back 250 fills
      * when the current run of maker-only started
        (crypto_grid_bot.record_maker_only_state)

    With both, the verdict is arithmetic. A taker fill stamped AFTER the
    mode was armed means the fallback fired under a mode that removes it -
    a real bug. Every taker fill stamped before it is history the window
    happens to still reach.

    UNKNOWN is kept for exactly one case: a missing arming time. It is then
    reported WITH the newest taker fill's age, because that is the fact
    that makes it actionable - a taker fill minutes old is worth chasing
    whatever the arming time turns out to be.
    """
    if not maker_only_active:
        return {"name": "maker_only_holds", "status": OK,
                "detail": "maker-only is off - the market fallback is allowed"}

    taker = _num(taker_fills) or 0
    if not taker:
        return {"name": "maker_only_holds", "status": OK,
                "detail": "no taker fills in the recent window"}

    seen = f"{int(taker)} of {int(_num(classified_fills) or 0)} recent fills were TAKER"
    newest = _as_dt(newest_taker_at)
    armed = _as_dt(armed_at)

    if newest is None:
        return {"name": "maker_only_holds", "status": UNKNOWN,
                "detail": (f"{seen}, but none of them carried a timestamp - so whether "
                           f"they predate the mode cannot be established from this "
                           f"window. Not read as a pass.")}

    if armed is None:
        age_h = _hours_between(newest, _utcnow())
        return {"name": "maker_only_holds", "status": UNKNOWN,
                "detail": (f"{seen}. The newest is {age_h:.1f}h old, but there is no "
                           f"record of when maker-only was armed, so it cannot be "
                           f"called stale or live. The stamp is written on the next "
                           f"grid cycle and this answers itself from then on."),
                "newest_taker_age_hours": round(age_h, 1)}

    if newest > armed:
        after_h = _hours_between(armed, newest)
        return {"name": "maker_only_holds", "status": FAIL,
                "detail": (f"{seen}, and the newest was filled {after_h:.1f}h AFTER "
                           f"maker-only was armed. Under maker-only there is no market "
                           f"fallback, so something is still crossing the spread - each "
                           f"such leg costs 0.75% against the 0.35% the spacing floor "
                           f"is priced on."),
                "newest_taker_at": newest.isoformat(),
                "armed_at": armed.isoformat()}

    return {"name": "maker_only_holds", "status": OK,
            "detail": (f"{seen}, but every one predates maker-only being armed "
                       f"{_hours_between(armed, _utcnow()):.1f}h ago - the window simply "
                       f"reaches back further than the mode does."),
            "armed_at": armed.isoformat()}
