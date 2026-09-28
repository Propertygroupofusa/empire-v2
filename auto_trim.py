"""Bring a holding back under the concentration limit without being asked.

WHY THIS EXISTS

The account owner set a 20% rule and then, reasonably, did not want to be
the one clicking sell every time a coin drifts over it. A rule that only
fires when someone is watching the dashboard is not a rule, it is a
reminder. On 2026-09-26 ZEC was 25.47% and XRP 21.06% of an $11,243
account, and both had been over the line for days with nothing happening.

WHAT IT WILL AND WILL NOT DO

It sells. That is the point and it should be stated plainly: arming this
moves real money without a human in the loop. Everything below exists to
bound how much.

  * It only ever SELLS, and only an asset that is OVER the limit. There is
    no code path here that buys anything.
  * It sells the smallest amount that clears the limit, and no more.
  * It cannot act at all unless the mode is explicitly "arm". Any other
    value - unset, misspelled, empty, "true", "yes" - observes and places
    nothing. The failure mode of a typo must be inaction.

THE ARITHMETIC, AND WHICH WAY IT ERRS

Selling coin into cash leaves the account total unchanged, so to bring an
asset holding `u` down to share `t` of a total `T`:

    (u - s) / T = t/100     ->     s = u - T * t / 100

Fees make the real post-sale total slightly SMALLER than T, which means
the true amount needed is slightly less than `s`. Ignoring fees therefore
oversells by a hair and lands a hair under the target. That is the correct
direction to be wrong in: undershooting leaves the coin still over the
limit and trims again tomorrow, which is the thing this was built to stop.

It also trims to `limit - buffer`, not to the limit itself. Landing exactly
on 20.00% means the next 1% price move puts it back over and the trimmer
fires again, paying a fee each time to chase a rounding edge.

WHAT IT DELIBERATELY DOES NOT DO

It does not know whether now is a good time to sell. It has no view on
price, trend, or whether the coin is mid-crash. A trimmer that tried to
time its exits would be a trading strategy wearing a risk-control label,
and this repository already has one study saying the timing edge it could
draw on is not statistically established. It reduces position size on a
schedule; that is the whole claim.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# --- the limit itself -------------------------------------------------
LIMIT_PCT = 20.0          # the owner's rule
BUFFER_PCT = 0.5          # trim to 19.5%, so a small tick does not re-fire

# --- bounds on a single pass ------------------------------------------
MIN_TRIM_USD = 25.0       # below this the fee is a meaningful share of it
MAX_TRIM_USD = 750.0      # no single order larger than this
MAX_DAILY_TRIM_USD = 1500.0
MAX_POSITION_SHARE_PCT = 35.0   # never sell more than this much of one holding at once
COOLDOWN_HOURS = 24.0     # one trim per asset per day

# --- the tail ---------------------------------------------------------
# Consolidation sells a WHOLE position, so it is bounded by count rather
# than by size: three a pass, which clears eighteen tail positions over
# six passes instead of emptying the tail in one burst that nobody sees
# until it is done. It shares the daily dollar budget with trimming, so
# the two together can never exceed MAX_DAILY_TRIM_USD.
MAX_CONSOLIDATE_PER_PASS = 3

# Consolidation uses the VENUE's floor, not MIN_TRIM_USD.
#
# The $25 trim floor exists so a trivial adjustment does not pay a fee to
# chase a rounding edge. Applied to the tail it does the opposite: a $23
# position can never clear a $25 bar, so it sits there forever - which is
# precisely the state consolidation was built to end. Selling $23 whole
# costs about eight cents at the maker rate. The only floor that belongs
# here is the smallest amount the venue will actually sell.
MIN_CONSOLIDATE_USD = 5.0

# --- the switch -------------------------------------------------------
MODE_OBSERVE = "observe"
MODE_ARM = "arm"
MODES = (MODE_OBSERVE, MODE_ARM)


def normalise_mode(value) -> str:
    """Anything that is not exactly "arm" observes.

    Deliberately strict: "true", "on", "1", "yes", "armed" and None all
    mean observe, because the cost of reading a malformed setting as
    permission to sell is unbounded and the cost of reading it as caution
    is a dashboard that says it is not armed.

    Surrounding whitespace and case ARE forgiven - " Arm " arms it. That
    is a deliberate exception and the docstring said the opposite until an
    audit caught it. A trailing space on a Railway variable is a near
    universal accident rather than a different intention, and a setting
    that silently fails to arm because of an invisible character is its
    own kind of dangerous: the operator believes protection is running.
    Every NON-whitespace difference still observes.
    """
    if not isinstance(value, str):
        return MODE_OBSERVE
    v = value.strip().lower()
    return MODE_ARM if v == MODE_ARM else MODE_OBSERVE


def _num(v):
    """A finite positive float, or None. None always means 'do nothing'."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def excess_usd(holding_usd, total_usd, limit_pct=LIMIT_PCT, buffer_pct=BUFFER_PCT):
    """Dollars to sell so the holding lands at (limit - buffer) of the account.

    Returns 0.0 when the holding is already at or under the limit, and
    None when the inputs cannot be trusted. The caller must treat None as
    "do nothing", never as zero: a total that failed to read is not the
    same as a position that is within its limit.
    """
    u = _num(holding_usd)
    t = _num(total_usd)
    if u is None or t is None or t <= 0 or u < 0:
        return None
    lim = _num(limit_pct)
    buf = _num(buffer_pct) or 0.0
    if lim is None or lim <= 0 or lim > 100:
        return None
    if (u / t) * 100.0 <= lim:
        return 0.0
    target_share = max(lim - buf, 0.0)
    need = u - t * target_share / 100.0
    return max(need, 0.0)


def last_trim_at(history, asset):
    """Most recent trim time for `asset`, or None. Bad rows are ignored."""
    latest = None
    for row in history or ():
        try:
            if (row.get("asset") or "").upper() != (asset or "").upper():
                continue
            ts = row.get("placed_at")
        except AttributeError:
            continue
        if not isinstance(ts, datetime):
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def spent_today(history, now):
    """Dollars already trimmed in the UTC day containing `now`.

    A row whose amount cannot be read counts as MAX_DAILY_TRIM_USD, not as
    zero. An unreadable ledger must exhaust the budget rather than reset
    it - the alternative is that a corrupt row unlocks unlimited selling.
    """
    if not isinstance(now, datetime):
        return MAX_DAILY_TRIM_USD
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    for row in history or ():
        try:
            ts = row.get("placed_at")
        except AttributeError:
            return MAX_DAILY_TRIM_USD
        if not isinstance(ts, datetime) or ts < start or ts > now:
            continue
        amt = _num(row.get("usd"))
        if amt is None:
            return MAX_DAILY_TRIM_USD
        total += amt
    return total


def plan_trims(holdings, total_usd, *, now, history=(), limit_pct=LIMIT_PCT,
               buffer_pct=BUFFER_PCT, max_trim_usd=MAX_TRIM_USD,
               max_daily_usd=MAX_DAILY_TRIM_USD, min_trim_usd=MIN_TRIM_USD,
               cooldown_hours=COOLDOWN_HOURS, actively_traded=()):
    """What to trim right now, and for every holding, why not.

    `holdings` is the census list: dicts with `asset`, `usd`, `units`,
    `price`. Returns a list covering EVERY holding, each with `act` and a
    `reason`. The ones that do nothing are in the output on purpose - a
    risk control that only reports its actions cannot be audited for the
    times it should have acted and didn't.

    The list is ordered by how far over the line each asset is, so when
    the daily budget runs out it is spent on the worst offender first.
    """
    t = _num(total_usd)
    out = []
    # Assets a grid branch currently holds open slices on. THIS IS THE
    # PATH THAT ACTUALLY SELLS - auto_trim_worker calls plan_trims, and
    # tail consolidation in plan_actions is preview-only - so the
    # protection has to be here or it protects nothing.
    #
    # Measured 2026-09-28: this rule trimmed $885.43 of ZEC and $244.78 of
    # XRP across two days while the grid went on claiming those units.
    # Both branches were left holding coin the wallet no longer had, and
    # a sale of their slices would have been an order for units that do
    # not exist. Two subsystems enforcing the same 20% ceiling against
    # different books, and neither telling the other.
    #
    # ONE-DIRECTIONAL: it can only ever cancel a trim, never cause one.
    protected = {str(a).upper() for a in (actively_traded or ())}

    # The account's own money is in the census `holdings` list, correctly -
    # it is part of the account - and this loop applied the ceiling to it.
    # Live on 2026-09-28 it planned to sell $61.62 of USD at 20.08%.
    #
    # There is no order to place: auto_trim_worker builds the product id as
    # f"{asset}-USD", so that one is "USD-USD". And the rule does not apply:
    # the ceiling exists because one bad week in a position this size moves
    # the whole account, and dollars do not have bad weeks - the proceeds of
    # selling dollars are dollars, so the share would not even move.
    #
    # Imported, never restated. A second copy of this list is how two
    # subsystems come to disagree about what counts as money.
    from account_census import STABLE as CASH_EQUIVALENTS

    if t is None or t <= 0:
        for h in holdings or ():
            out.append({"asset": (h.get("asset") or "?").upper(), "act": False,
                        "reason": "NO_TOTAL",
                        "detail": "account total could not be read, so no share can be computed"})
        return out

    budget_left = max(_num(max_daily_usd) or 0.0, 0.0) - spent_today(history, now)

    rows = []
    for h in holdings or ():
        asset = (h.get("asset") or "").upper()
        usd = _num(h.get("usd"))
        share = (usd / t * 100.0) if usd is not None else None
        rows.append((share if share is not None else -1.0, asset, h, usd, share))
    rows.sort(key=lambda r: -r[0])

    for _, asset, h, usd, share in rows:
        rec = {"asset": asset, "usd": usd, "share_pct": round(share, 2) if share is not None else None,
               "act": False, "trim_usd": 0.0}

        if not asset:
            rec.update(reason="NO_ASSET", detail="row carries no ticker")
            out.append(rec); continue
        # Checked before the share is even computed. Excluded from the RULE,
        # never from the TOTAL it is measured against - a coin's share is a
        # share of the whole account, cash included, or every share inflates.
        if asset in CASH_EQUIVALENTS:
            rec.update(reason="NOT_A_POSITION",
                       detail=(f"{asset} is the account's cash, not a position - there is "
                               f"no {asset}-USD to sell, and selling cash for cash would "
                               f"not move its share. It still counts toward the account "
                               f"total every other share is measured against."))
            out.append(rec); continue
        if usd is None:
            rec.update(reason="UNPRICED",
                       detail="no dollar value for this holding, so its share is unknown - "
                              "unknown is not the same as small, and nothing is sold on a guess")
            out.append(rec); continue

        need = excess_usd(usd, t, limit_pct, buffer_pct)
        if need is None:
            rec.update(reason="UNCOMPUTABLE", detail="share could not be computed from these inputs")
            out.append(rec); continue
        if need <= 0:
            rec.update(reason="WITHIN_LIMIT",
                       detail=f"{share:.2f}% of the account, at or under the {limit_pct:.0f}% rule")
            out.append(rec); continue

        rec["excess_usd"] = round(need, 2)

        # Checked before any other reason could let it through. A coin the
        # grid is actively trading is working inventory, not a position to
        # be sized down behind the grid's back.
        if asset in protected:
            rec.update(reason="ACTIVELY_TRADED",
                       detail=(f"{asset} is {share:.2f}% of the account and ${need:,.2f} "
                               f"over the {limit_pct:.0f}% rule, but it has open grid "
                               f"slices on it - trimming here would sell coin the grid "
                               f"still has on its books. Raise it with the grid instead."))
            out.append(rec); continue

        last = last_trim_at(history, asset)
        if last is not None and isinstance(now, datetime):
            hrs = (now - last).total_seconds() / 3600.0
            if hrs < (_num(cooldown_hours) or 0.0):
                rec.update(reason="COOLDOWN",
                           detail=f"trimmed {hrs:.1f}h ago; one trim per asset per "
                                  f"{cooldown_hours:.0f}h so a drifting price cannot "
                                  f"be charged a fee twice in a day")
                out.append(rec); continue

        amount = need
        caps = []
        cap1 = _num(max_trim_usd)
        if cap1 is not None and amount > cap1:
            amount = cap1; caps.append(f"single-order cap ${cap1:,.0f}")
        pos_cap = usd * (MAX_POSITION_SHARE_PCT / 100.0)
        if amount > pos_cap:
            amount = pos_cap
            caps.append(f"{MAX_POSITION_SHARE_PCT:.0f}% of the position")
        if amount > budget_left:
            amount = max(budget_left, 0.0); caps.append(f"${budget_left:,.2f} left in today's budget")

        floor = _num(min_trim_usd) or 0.0
        if amount < floor:
            rec.update(reason="TOO_SMALL",
                       detail=(f"${amount:,.2f} after limits is under the ${floor:,.2f} floor - "
                               f"the fee would be a meaningful share of the trade"),
                       capped_by=caps or None)
            out.append(rec); continue

        rec.update(act=True, trim_usd=round(amount, 2), reason="OVER_LIMIT",
                   capped_by=caps or None,
                   detail=(f"{share:.2f}% of the account against a {limit_pct:.0f}% rule; "
                           f"selling ${amount:,.2f} brings it to about "
                           f"{max(limit_pct - buffer_pct, 0):.1f}%"
                           + (f" (held back by {', '.join(caps)})" if caps else "")))
        budget_left -= amount
        out.append(rec)

    return out


def summarise(plans, mode):
    """One line for the dashboard, and the numbers behind it.

    The headline counts ONLY rows that place an order. Tail consolidation
    is sized in plan_actions and placed by nothing, so it is reported on
    its own line rather than added to the sale.

    Live on 2026-09-28 this read "4 holding(s) over the limit. $143.38
    will be sold on the next pass." while the same response carried
    consolidation_is_preview_only: true. Of that $143.38, $81.76 was
    three preview-only consolidations and $61.62 was the USD row, which
    is not a product. Nothing at all was going to be sold. Two numbers
    that must agree, computed in two places, and the one a reader sees
    first was the wrong one.

    plan_trims emits no CONSOLIDATE rows, so the worker's summary - the
    path that really sells - reads exactly as it did before.
    """
    acting = [p for p in plans if p.get("act")]
    ordering = [p for p in acting if p.get("kind") != "CONSOLIDATE"]
    preview = [p for p in acting if p.get("kind") == "CONSOLIDATE"]
    total = round(sum(p.get("trim_usd") or 0.0 for p in ordering), 2)
    preview_total = round(sum(p.get("trim_usd") or 0.0 for p in preview), 2)
    m = normalise_mode(mode)

    # Named, never dropped: a reader who saw the consolidations listed
    # needs to know why they are not in the figure above them.
    tail_note = ""
    if preview:
        tail_note = (f" Separately, {len(preview)} tail position(s) worth "
                     f"${preview_total:,.2f} are sized here as a preview - nothing "
                     f"places them.")

    if not ordering:
        headline = "Nothing is over the limit."
        over = [p for p in plans if p.get("reason") in ("COOLDOWN", "TOO_SMALL")]
        if over:
            headline = (f"{len(over)} holding(s) over the limit, none actionable this pass "
                        f"({', '.join(sorted({p['reason'] for p in over}))}).")
        headline += tail_note
    elif m == MODE_ARM:
        headline = (f"{len(ordering)} holding(s) over the limit. "
                    f"${total:,.2f} will be sold on the next pass." + tail_note)
    else:
        headline = (f"{len(ordering)} holding(s) over the limit. ${total:,.2f} would be sold, "
                    f"but the trimmer is OBSERVING - nothing will be placed." + tail_note)
    return {
        "mode": m,
        "armed": m == MODE_ARM,
        "would_trim_count": len(ordering),
        "would_trim_usd": total,
        "would_consolidate_count": len(preview),
        "would_consolidate_usd": preview_total,
        "headline": headline,
        "plans": plans,
    }


def plan_actions(holdings, total_usd, *, now, history=(), unpriced=(),
                 limit_pct=LIMIT_PCT, buffer_pct=BUFFER_PCT,
                 max_trim_usd=MAX_TRIM_USD, max_daily_usd=MAX_DAILY_TRIM_USD,
                 min_trim_usd=MIN_TRIM_USD, cooldown_hours=COOLDOWN_HOURS,
                 max_consolidate=MAX_CONSOLIDATE_PER_PASS,
                 min_consolidate_usd=MIN_CONSOLIDATE_USD,
                 actively_traded=()):
    """Every action available right now, across every tier - not just the ceiling.

    plan_trims answers "what is over 20%", which on this book is two
    positions out of forty-seven. This answers the question the owner
    actually asked: what rule applies to each of the others, and which of
    those rules can act today.

    Trims are planned FIRST and consolidations spend what is left of the
    daily budget. That ordering is deliberate: a $671 anchor trim reduces
    real risk, and an $8 tail sale tidies the book. If only one of them
    fits in a day's budget it should be the first.

    Returns the same shape plan_trims returns, with an added `kind` of
    "TRIM" or "CONSOLIDATE" on the rows that act.

    `actively_traded` is the set of assets a grid branch currently holds
    open slices on, and NOTHING in that set is ever sold here.

    WHY THAT EXCEPTION EXISTS. The TAIL rule reads "under 1% of the
    account - too small to change it whatever it does; sell into cash".
    That is true of a forgotten holding and false of working inventory.
    On 2026-09-28 this loop was one pass away from selling BCH-USD
    ($95.01), LTC-USD ($74.95) and ONDO-USD ($55.00) in full - all three
    live grid branches, and ONDO had completed a profitable round trip
    four hours earlier and rebought. That is not consolidating a tail, it
    is closing somebody else's open trade without telling them.

    It had already happened twice on the ceiling rule: $885.43 of ZEC and
    $244.78 of XRP were trimmed on 2026-09-27 and 09-28 while the grid
    went on claiming those units, leaving branches holding coin the wallet
    no longer had. See invariants.coin_tracked_is_held, which found it.

    The exception is ONE-DIRECTIONAL: it can only ever cancel a sale,
    never cause one, and every skipped row is still returned with its
    reason so the refusal is auditable like all the others.
    """
    import position_rules

    protected = {str(a).upper() for a in (actively_traded or ())}

    trims = plan_trims(holdings, total_usd, now=now, history=history,
                       limit_pct=limit_pct, buffer_pct=buffer_pct,
                       max_trim_usd=max_trim_usd, max_daily_usd=max_daily_usd,
                       min_trim_usd=min_trim_usd, cooldown_hours=cooldown_hours)
    for r in trims:
        if r.get("act"):
            r["kind"] = "TRIM"
        # Working inventory is never sold from here, at any tier.
        if r["asset"] in protected and r.get("act"):
            r.update(act=False, trim_usd=0.0, reason="ACTIVELY_TRADED",
                     detail=(f"{r['asset']} has open grid slices on it - selling it here "
                             f"would close a live trade the grid still has on its books"))

    book = position_rules.book(holdings, total_usd, unpriced=unpriced)
    by_asset = {r["asset"]: r for r in book["rows"]}
    for r in trims:
        tier = (by_asset.get(r["asset"]) or {}).get("tier")
        if tier:
            r["tier"] = tier

    spent = sum(r.get("trim_usd") or 0.0 for r in trims if r.get("act"))
    budget_left = max((_num(max_daily_usd) or 0.0) - spent_today(history, now) - spent, 0.0)

    # Largest tail position first: it is the one whose stop and dashboard
    # line cost the most to keep, and the one most likely to clear the
    # minimum cleanly.
    tail = sorted([r for r in book["rows"] if r["action"] == position_rules.CONSOLIDATE],
                  key=lambda r: -(r["usd"] or 0))
    placed = 0
    for row in tail:
        rec = {"asset": row["asset"], "usd": row["usd"], "share_pct": row["share_pct"],
               "tier": row["tier"], "kind": "CONSOLIDATE", "act": False, "trim_usd": 0.0}
        # Checked FIRST, before any other reason could let it through: a
        # tail position the grid is actively trading is working inventory,
        # not a forgotten holding.
        if row["asset"] in protected:
            rec.update(reason="ACTIVELY_TRADED",
                       detail=(f"{row['asset']} has open grid slices on it - selling the "
                               f"whole position here would close a live trade the grid "
                               f"still has on its books"))
            trims.append(rec); continue
        amount = _num(row["usd"]) or 0.0
        floor = _num(min_consolidate_usd) or 0.0

        last = last_trim_at(history, row["asset"])
        if last is not None and isinstance(now, datetime):
            hrs = (now - last).total_seconds() / 3600.0
            if hrs < (_num(cooldown_hours) or 0.0):
                rec.update(reason="COOLDOWN",
                           detail=f"sold {hrs:.1f}h ago; one action per asset per "
                                  f"{cooldown_hours:.0f}h")
                trims.append(rec); continue
        if placed >= (max_consolidate or 0):
            rec.update(reason="PASS_LIMIT",
                       detail=f"only {max_consolidate} tail positions are cleared per pass, "
                              f"so the tail empties over days and stays visible while it does")
            trims.append(rec); continue
        if amount < floor:
            rec.update(reason="BELOW_VENUE_MINIMUM",
                       detail=f"${amount:,.2f} is under the ${floor:,.2f} the venue will "
                              f"sell - there is no action available, at any price")
            trims.append(rec); continue
        if amount > budget_left:
            rec.update(reason="BUDGET",
                       detail=f"${amount:,.2f} does not fit in the ${budget_left:,.2f} "
                              f"left in today's budget after trimming")
            trims.append(rec); continue

        rec.update(act=True, trim_usd=round(amount, 2), reason="TAIL",
                   detail=(f"{row['share_pct']}% of the account - too small to change it "
                           f"whatever it does; selling the whole ${amount:,.2f} into cash"))
        budget_left -= amount
        placed += 1
        trims.append(rec)

    # One row per asset. The ceiling pass emits a WITHIN_LIMIT row for
    # every holding, including the tail ones, and leaving both in means a
    # reader sees FLOKI twice saying two different things. The tail rule
    # is the one that applies, so it supersedes.
    tail_assets = {r["asset"] for r in trims if r.get("kind") == "CONSOLIDATE"}
    return [r for r in trims
            if not (r.get("kind") != "CONSOLIDATE"
                    and r["asset"] in tail_assets
                    and r.get("reason") == "WITHIN_LIMIT")]
