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


def allocation_backed(claimed_usd, really_there_usd, tolerance_pct=BACKING_TOLERANCE_PCT,
                      unallocated_cash_usd=None, over_deployed_usd=None,
                      open_commission_usd=None):
    """Branch bookkeeping vs the real account. The dashboard already shows
    this; having it here makes it a scheduled check rather than a banner
    somebody has to be looking at."""
    if claimed_usd is None or really_there_usd is None:
        return _v("allocation_backed", UNKNOWN, "claimed or real total unreadable")
    if claimed_usd <= 0:
        return _v("allocation_backed", UNKNOWN, "nothing claimed - no ratio to take")
    gap = float(claimed_usd) - float(really_there_usd)
    pct = abs(gap) / float(claimed_usd)

    if pct <= tolerance_pct:
        return _v("allocation_backed", OK,
                  f"branches claim ${claimed_usd:,.2f}, ${really_there_usd:,.2f} really there - "
                  f"${abs(gap):,.2f} ({pct*100:.2f}%) apart, within tolerance")

    # OVER-CLAIMING is the hole this check exists for: a number in a
    # database with nothing behind it.
    if gap > 0:
        return _v("allocation_backed", FAIL,
                  f"branches claim ${claimed_usd:,.2f} against ${really_there_usd:,.2f} really "
                  f"there - ${gap:,.2f} ({pct*100:.2f}%) MORE than is really there, over the "
                  f"{tolerance_pct*100:.2f}% tolerance.", gap_usd=round(gap, 2))

    # UNDER-CLAIMING IS THE OPPOSITE CONDITION AND WAS BEING REPORTED AS
    # THE SAME FAILURE. There is MORE real value than the branches claim -
    # nobody is short a dollar. It ran FAIL for hours at about -$470 with
    # a residual that never moved, and the arithmetic turned out to be
    # exact and entirely benign:
    #
    #     unallocated cash   $76.49   real USD no branch has claimed yet
    #     over-deployed     $390.20   coin costing more than its allocation
    #     open commission     $3.18   buy-leg fees already paid
    #                       -------
    #                       $469.87   = the whole surplus, to the cent
    #
    # So the surplus is now EXPLAINED rather than alarmed about. Fully
    # accounted for is OK. Not fully accounted for is UNKNOWN, never FAIL:
    # a surplus is not a solvency problem, but an unexplained one is still
    # worth a look, and silence must not read as health.
    surplus = -gap
    # open_commission_usd is OPTIONAL and must be passed only when the
    # `really_there_usd` handed in ALREADY includes it. The two backed
    # figures in this codebase differ by exactly that term -
    # reconcile.snapshot adds it, allocation_backing.backed_usd does not -
    # so passing it against the wrong one explains $3.24 twice. Omitted,
    # it simply is not part of the sum.
    parts = {"unallocated cash": unallocated_cash_usd,
             "over-deployed coin": over_deployed_usd}
    if open_commission_usd is not None:
        parts["open commission"] = open_commission_usd
    known = {k: _num(v) for k, v in parts.items()}
    if any(v is None for v in known.values()):
        return _v("allocation_backed", UNKNOWN,
                  f"branches claim ${claimed_usd:,.2f} against ${really_there_usd:,.2f} really "
                  f"there - ${surplus:,.2f} ({pct*100:.2f}%) MORE is present than is claimed. "
                  f"That is not a hole, but the components that would explain it were not "
                  f"supplied, so it is not read as a pass either.",
                  surplus_usd=round(surplus, 2))

    explained = sum(known.values())
    breakdown = " + ".join(f"${v:,.2f} {k}" for k, v in known.items() if abs(v) >= 0.005)
    # A cent per contributing figure, floored at $1: rounding noise, not a
    # tolerance anyone should tune.
    slack = max(1.00, 0.01 * len(known))
    if abs(explained - surplus) <= slack:
        return _v("allocation_backed", OK,
                  f"branches claim ${claimed_usd:,.2f} against ${really_there_usd:,.2f} really "
                  f"there. The ${surplus:,.2f} difference is MORE money than is claimed, not "
                  f"less, and it is fully accounted for: {breakdown}. Nothing is unbacked.",
                  surplus_usd=round(surplus, 2), explained_usd=round(explained, 2))

    return _v("allocation_backed", UNKNOWN,
              f"branches claim ${claimed_usd:,.2f} against ${really_there_usd:,.2f} really "
              f"there - ${surplus:,.2f} MORE is present than is claimed. "
              f"{breakdown or 'No component'} accounts for ${explained:,.2f}, leaving "
              f"${surplus - explained:,.2f} unexplained. Not a hole - nobody is short - but "
              f"not understood either.",
              surplus_usd=round(surplus, 2), explained_usd=round(explained, 2),
              unexplained_usd=round(surplus - explained, 2))


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
# A branch whose best slice is within this of breaking even is one ordinary
# move from selling, not stranded. Measured on the live fleet 2026-09-28:
# XRP was 0.02% away holding $2,240.54, and the whole $6,557.08 was being
# reported as one lump of dead capital. A tenth of a percent is inside a
# single tick on most of these coins; a full 1% is inside a normal hour on
# every one of them. This changes NO trading behaviour - nothing sells
# sooner, no threshold moves - it only stops "about to trade" being
# reported as "stuck".
NEAR_EXIT_PCT = 1.0


def no_dead_capital(branches, near_exit_pct=NEAR_EXIT_PCT):
    """A branch full of slices AND underwater is a hold, not a grid.

    branches: dicts with product_id, allocated_usd, open_slices, num_levels,
    best_slice_net_pct. Deliberately takes the shape the status payload
    already has, so the check cannot drift from what the dashboard shows.

    best_slice_net_pct of None means UNREADABLE and is reported as such.
    It used to arrive as 0 because the caller wrote `(pct or 0)`, and 0 is
    not less than 0, so an unreadable slice made its branch look exactly
    like one sitting at break-even - fabricating dead capital out of a
    failed read. The caller no longer does that; this refuses to guess
    either way if it ever comes back None.
    """
    if not branches:
        return _v("no_dead_capital", UNKNOWN, "no branch data")
    stuck, near, unreadable = [], [], []
    stuck_usd = near_usd = 0.0
    # THE STRUCTURAL HALF, counted separately and regardless of price.
    #
    # This check requires BOTH "full on rungs" AND "underwater past the
    # near-exit band", and only the first is structural. The second moves
    # with every tick, so the reported figure inherited its volatility
    # entirely: on 2026-09-28 it read $3,156.23 across 8 branches at
    # 12:38Z, $588.59 across 4 at 12:55Z and $717.07 at 13:14Z, with
    # nothing bought or sold to explain any of it.
    #
    # Meanwhile nine branches were full on their rungs the whole time -
    # $3,611.79, 44.6% of allocated capital, unable to deploy another
    # dollar at any price until a slice sells. That is the number a
    # reader needs, and it was invisible because only the price-filtered
    # subset of it was ever reported.
    #
    # Same shape as the maker_only_holds fix: there a frozen number could
    # not show recency, here a volatile one hid a stable one.
    full, full_usd = [], 0.0
    for b in branches:
        n, lv = b.get("open_slices"), b.get("num_levels")
        best = b.get("best_slice_net_pct")
        if n is None or lv is None:
            continue
        if n >= lv:
            # Being in profit does not give a branch a spare rung.
            full.append(b.get("product_id"))
            full_usd += float(b.get("allocated_usd") or 0.0)
        if best is None:
            if n >= lv:
                unreadable.append(b.get("product_id"))
            continue
        if n >= lv and best <= 0:
            usd = float(b.get("allocated_usd") or 0.0)
            if best >= -abs(near_exit_pct):
                near.append((b.get("product_id"), best))
                near_usd += usd
            else:
                stuck.append((b.get("product_id"), best))
                stuck_usd += usd

    def _names(rows):
        return ", ".join(f"{p} ({v:+.2f}%)" for p, v in rows)

    # Always present, including on a clean verdict: a pass must not hide
    # that half the fleet cannot buy.
    extra = {"full_usd": round(full_usd, 2), "full_branches": full}
    if near:
        extra["near_exit_usd"] = round(near_usd, 2)
        extra["near_exit"] = [p for p, _ in near]
    if unreadable:
        extra["unreadable"] = unreadable

    if stuck:
        detail = (f"${stuck_usd:,.2f} across {len(stuck)} branch(es) can neither buy (full) "
                  f"nor sell (underwater): {_names(stuck)}. That capital is held, not "
                  f"gridded. THAT FIGURE MOVES ON PRICE - it read $3,156.23 across 8 "
                  f"branches, then $588.59 across 4, then $717.07, inside 36 minutes "
                  f"with nothing traded. The stable one is ${full_usd:,.2f} across "
                  f"{len(full)} branch(es) full on their rungs, which cannot buy at any "
                  f"price until a slice sells.")
        if near:
            detail += (f" Separately, ${near_usd:,.2f} across {len(near)} branch(es) is "
                       f"within {abs(near_exit_pct):.1f}% of a profitable exit and is "
                       f"counted as between fills, not stuck: {_names(near)}.")
        if unreadable:
            detail += (f" {len(unreadable)} full branch(es) could not be priced and are in "
                       f"neither figure: {', '.join(str(u) for u in unreadable)}.")
        return _v("no_dead_capital", FAIL, detail,
                  stuck_usd=round(stuck_usd, 2), branches=[p for p, _ in stuck], **extra)

    if unreadable:
        return _v("no_dead_capital", UNKNOWN,
                  f"{len(unreadable)} full branch(es) could not be priced, so whether their "
                  f"capital is stuck cannot be established: "
                  f"{', '.join(str(u) for u in unreadable)}. Not read as a pass.", **extra)

    if near:
        return _v("no_dead_capital", OK,
                  f"no branch is stranded. ${near_usd:,.2f} across {len(near)} branch(es) is "
                  f"full and marginally under, but within {abs(near_exit_pct):.1f}% of a "
                  f"profitable exit - a grid between fills: {_names(near)}.", **extra)

    ok = "every branch can either buy a dip or sell a rise"
    if full:
        # A clean verdict must not hide the structural picture.
        ok += (f". ${full_usd:,.2f} across {len(full)} branch(es) is full on its rungs and "
               f"cannot buy until a slice sells, but every one of them can sell at a "
               f"profit, which is a grid working rather than capital stranded")
    return _v("no_dead_capital", OK, ok, **extra)


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


#: A taker fill newer than this is treated as live; older, and whatever
#: was crossing the spread has stopped. One hour because the grid cycles
#: in minutes, so an hour of maker-only fills is evidence rather than a
#: quiet patch.
STALE_TAKER_HOURS = 1.0

#: Every module that can put a MARKET order on this wallet. Hand-written
#: prose listing four of them went stale the moment two more were added,
#: and an incomplete list of suspects is worse than none: a reader checks
#: the named four, finds nothing, and concludes the alarm is noise.
#: test_maker_only_recency greps the repo for place_market_* callers and
#: fails if any is missing from here.
MARKET_ORDER_PATHS = (
    "the grid's own close and stop paths (crypto_grid_bot)",
    "the concentration trimmer (auto_trim_worker)",
    "the family-tree bot (crypto_family_tree_bot)",
    "the mean-reversion bot (crypto_mean_reversion_bot)",
    "the BTC compound bot (crypto_btc_compound_bot)",
    "a manual buy or sell from the dashboard (routers/trading_dashboard)",
)


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
        age_h = _hours_between(newest, _utcnow())

        # BOTH NUMBERS, BECAUSE THEY ANSWER DIFFERENT QUESTIONS.
        #
        # after_h is newest_taker minus armed_at. Both are fixed points, so
        # it never moves - it read "3.2h AFTER" at 09:22Z, at 11:00Z and at
        # 11:31Z on 2026-09-28, while newest_taker_at stayed at 07:49:41
        # and the maker count in the same window climbed from 72 to 81.
        # Nothing had crossed the spread for hours and the only figure on
        # offer could not show it. An alarm that reads the same whether the
        # leak is live or long finished is one people stop reading.
        if age_h <= STALE_TAKER_HOURS:
            when = (f"and the newest was {age_h * 60:.0f} minutes old when this ran, "
                    f"so it is STILL HAPPENING")
        else:
            when = (f"but the newest is {age_h:.1f}h ago, so whatever was doing it has "
                    f"stopped - this window still reaches back to it")

        return {"name": "maker_only_holds", "status": FAIL,
                # SAY ONLY WHAT THIS CAN PROVE. The first live FAIL read
                # "something is still crossing the spread", which points at
                # the grid - and the grid cannot be the cause on its maker
                # path: place_maker_buy/sell both set post_only=True, so
                # Coinbase REJECTS such an order rather than filling it as a
                # taker. What is actually known is that a taker leg was
                # billed after arming. The fills feed carries no originating
                # subsystem, so naming one would be a guess dressed as a
                # finding.
                "detail": (f"{seen}, filled {after_h:.1f}h AFTER maker-only was armed - "
                           f"{when}. A taker leg costs ~0.75% against the 0.35% the "
                           f"spacing floor is priced on, so it is worth tracing. The "
                           f"grid's own maker orders are post_only and are rejected "
                           f"rather than crossed, so the source is a MARKET order from "
                           f"another path: a resting stop firing, the trimmer, a close, "
                           f"a manual sale, or one of the other bots on this wallet. "
                           f"WHICH cannot be said from here - orders reach Coinbase "
                           f"with a bare uuid as their client_order_id, so no fill is "
                           f"attributable to a subsystem. Every path that can place one: "
                           f"{'; '.join(MARKET_ORDER_PATHS)}."),
                "newest_taker_at": newest.isoformat(),
                "newest_taker_age_hours": round(age_h, 2),
                "hours_after_arming": round(after_h, 2),
                "still_happening": age_h <= STALE_TAKER_HOURS,
                "market_order_paths": list(MARKET_ORDER_PATHS),
                "armed_at": armed.isoformat()}

    return {"name": "maker_only_holds", "status": OK,
            "detail": (f"{seen}, but every one predates maker-only being armed "
                       f"{_hours_between(armed, _utcnow()):.1f}h ago - the window simply "
                       f"reaches back further than the mode does."),
            "armed_at": armed.isoformat()}


# Tolerance: 0.5% of the tracked quantity. Exchange rounding and dust
# differences live well inside that; a stop-loss eating a third of a
# position does not. A percentage rather than a fixed unit count because
# the fleet holds everything from 0.017 BTC to 5,862,000 JASMY.
COIN_SHORTFALL_TOLERANCE_PCT = 0.005


def coin_tracked_is_held(tracked_units_by_product, wallet_units_by_asset,
                         prices_by_product=None,
                         tolerance_pct=COIN_SHORTFALL_TOLERANCE_PCT):
    """Every unit a branch claims must actually be in the wallet.

    THE GAP THIS CLOSES. Every other check here compares DOLLARS, in
    aggregate: allocation_backed sums coin at cost against the wallet,
    reconcile names which bucket moved. None of them can see a branch
    holding units that are gone, because a shortfall in one coin hides
    inside a fleet-wide total that still adds up.

    Live on 2026-09-28: a resting stop-loss fired on ETH-USD and sold
    0.347873 units. Most of that was ETH no branch tracked - but 0.031600
    of it belonged to the grid's own open slices. The branch went on
    claiming 0.162240 ETH while the wallet held 0.130640, and the next
    sale of its oldest slice would have been an order for coin that does
    not exist. Nothing in the system said a word.

    Keyed per COIN, because that is where the shortfall is real: the
    wallet holds one pool per asset, several branches can draw on it, and
    the question "is there enough ETH" cannot be answered in dollars.

    A coin missing from the wallet map is UNKNOWN, not zero - an
    unreadable balance is not an empty one, and reporting a full position
    as a total shortfall would be the loudest false alarm this module
    could raise.
    """
    tracked = tracked_units_by_product or {}
    if not tracked:
        return _v("coin_tracked_is_held", UNKNOWN, "no tracked positions to check")
    if wallet_units_by_asset is None:
        return _v("coin_tracked_is_held", UNKNOWN,
                  "the wallet holdings could not be read, so no position can be "
                  "confirmed. Not knowing is not the same as being fine.")

    wallet = {str(k).upper(): _num(v) for k, v in dict(wallet_units_by_asset).items()}
    prices = prices_by_product or {}
    short, unknown = [], []
    for product, want in tracked.items():
        want = _num(want)
        if want is None or want <= 0:
            continue
        asset = str(product).split("-")[0].upper()
        have = wallet.get(asset)
        if have is None:
            unknown.append(str(product))
            continue
        gap = want - have
        if gap > want * tolerance_pct:
            usd = _num(prices.get(product))
            short.append({"product_id": product, "tracked": want, "held": have,
                          "short_units": round(gap, 8),
                          "short_usd": round(gap * usd, 2) if usd else None})

    if short:
        worst = ", ".join(
            f"{s['product_id']} claims {s['tracked']:.6f} against {s['held']:.6f} held"
            + (f" (${s['short_usd']:,.2f} short)" if s["short_usd"] is not None else "")
            for s in short)
        total = sum(s["short_usd"] for s in short if s["short_usd"] is not None)
        return _v("coin_tracked_is_held", FAIL,
                  f"{len(short)} branch position(s) claim coin the wallet does not hold: "
                  f"{worst}. A sale of those slices would be an order for units that do "
                  f"not exist - the usual cause is a resting stop or a manual sale taking "
                  f"coin the grid still has on its books.",
                  short_usd=round(total, 2), short_positions=short,
                  unreadable=unknown or None)

    if unknown:
        return _v("coin_tracked_is_held", UNKNOWN,
                  f"{len(unknown)} tracked coin(s) are absent from the wallet reading, so "
                  f"their positions cannot be confirmed: {', '.join(unknown)}. An "
                  f"unreadable balance is not an empty one.", unreadable=unknown)

    return _v("coin_tracked_is_held", OK,
              f"every one of {len(tracked)} tracked position(s) is fully held in the wallet")


# A lock smaller than this is a partially-filled order or a rounding
# remainder, not a reserved position. Same shape as the shortfall
# tolerance above and for the same reason: a check that fires on dust
# gets muted, and a muted check protects nothing.
INVENTORY_LOCK_TOLERANCE_PCT = 0.005


def grid_inventory_is_free(tracked_units_by_product, wallet_holdings,
                           tolerance_pct=INVENTORY_LOCK_TOLERANCE_PCT):
    """Coin a branch trades with must not be reserved by another order.

    THE GAP THIS CLOSES. coin_tracked_is_held sees a shortfall AFTER the
    coin has gone. This names the condition that produces it, while it
    can still be undone.

    A resting stop-limit at the venue HOLDS the units it covers. Live at
    2026-09-28T09:44Z, $923.23 was reserved this way across XLM, NEAR,
    LINK, SOL, ALGO, ACH and JASMY - six of them live grid branches.
    ALGO had 0.046 units free out of 1134.35; the whole position was
    spoken for.

    Two costs, neither of them previously reported anywhere:

      1. The grid cannot sell what the venue has reserved. A branch whose
         slice finally comes good places a sell for units the exchange is
         holding against another order.
      2. If one fires it sells 75% of the position in a single order
         while the branch goes on tracking it slice by slice. That is how
         the shortfall grew from $473.28 across 5 branches to $510.72
         across 7.

    Only coins a branch actually tracks are counted. JASMY was reserved
    too and is not a grid branch; folding it in would inflate the figure
    the owner would act on.

    Locked units are READ, never inferred. A holding whose available
    balance the venue did not return is UNKNOWN - a caller that cannot
    tell "nothing locked" from "could not tell" is exactly the caller
    that reports $0.00 reserved on a fully reserved position.

    This check states a condition. It does not cancel anything: a resting
    stop is downside protection somebody armed on purpose, and trading
    that away is the owner's decision, not this module's.
    """
    tracked = tracked_units_by_product or {}
    if not tracked:
        return _v("grid_inventory_is_free", UNKNOWN, "no tracked positions to check")
    if wallet_holdings is None:
        return _v("grid_inventory_is_free", UNKNOWN,
                  "the wallet holdings could not be read, so no position can be "
                  "confirmed free. Not knowing is not the same as being fine.")

    by_asset = {}
    for row in wallet_holdings:
        a = str((row or {}).get("asset") or "").upper()
        if a:
            by_asset[a] = row

    locked, unknown = [], []
    for product in tracked:
        asset = str(product).split("-")[0].upper()
        row = by_asset.get(asset)
        if row is None:
            unknown.append(str(product))
            continue
        units = _num(row.get("units"))
        avail = _num(row.get("available_units"))
        if units is None or avail is None:
            unknown.append(str(product))
            continue
        gap = units - avail
        if units > 0 and gap > units * tolerance_pct:
            price = _num(row.get("price"))
            locked.append({"product_id": str(product), "units": units,
                           "available_units": avail,
                           "locked_units": round(gap, 8),
                           "locked_pct": round(gap / units * 100.0, 1),
                           "locked_usd": round(gap * price, 2) if price else None})

    if locked:
        locked.sort(key=lambda r: -(r["locked_usd"] or 0))
        total = sum(r["locked_usd"] for r in locked if r["locked_usd"] is not None)
        worst = ", ".join(
            f"{r['product_id']} has {r['locked_pct']:.0f}% reserved"
            + (f" (${r['locked_usd']:,.2f})" if r["locked_usd"] is not None else "")
            for r in locked)
        return _v("grid_inventory_is_free", FAIL,
                  f"${total:,.2f} of coin across {len(locked)} grid branch(es) is "
                  f"reserved by resting orders at the venue and cannot be traded: "
                  f"{worst}. A branch cannot sell units the exchange is holding "
                  f"against another order, and if one of those orders fires it sells "
                  f"the position out from under the slices still tracking it. "
                  f"Cancelling a resting sell places no order and frees the units - "
                  f"but it also gives up the protection it was armed for, so it is a "
                  f"decision to take deliberately.",
                  locked_usd=round(total, 2), locked_positions=locked,
                  unreadable=unknown or None)

    if unknown:
        return _v("grid_inventory_is_free", UNKNOWN,
                  f"{len(unknown)} tracked coin(s) have no readable available balance, so "
                  f"it cannot be said whether their units are free: {', '.join(unknown)}. "
                  f"An unreadable reservation is not an absent one.", unreadable=unknown)

    return _v("grid_inventory_is_free", OK,
              f"every one of {len(tracked)} tracked position(s) is free to trade - "
              f"nothing is reserved by a resting order")
