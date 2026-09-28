"""Is the money the fleet says it has actually there?

The account owner found this by underlining a subtitle: the dashboard
read "$-381.47 cash - $553.84 working" and the negative number was the
only sign that most of the second figure was not real.

A grid branch's allocated_usd is a CLAIM. It is backed by two things and
nothing else: coin the branch actually bought (its open slices, at cost),
and USD still sitting in the wallet. Anything beyond that is a number in a
database. On the live account, 2026-09-26:

    claimed by branches                  $553.84
    real coin in open slices             $ 93.07
    real USD in the wallet               $ 79.36
                                         -------
    actually behind the claim            $172.43
    unbacked                             $381.41

The bot's own real_free_cash_usd read -$381.47 at the same moment - six
cents apart, which is price drift between two reads, so the negative
figure was correct and simply not legible.

This module does that arithmetic in one place so it can be shown, tested
and alarmed on, instead of being inferred from a minus sign.
"""

# Below this the gap is rounding and price drift between reads, not a
# real hole. Two independent reads of a moving market will not agree to
# the cent and an alarm that fires on that is an alarm nobody reads.
BACKING_TOLERANCE_USD = 5.0

# A claim this far beyond its backing is not drift under any reading.
BACKING_ALARM_PCT = 10.0


def slice_cost(s):
    """What a slice actually cost, from whatever shape the row is in.

    entry_price * qty, never allocated_usd - the allocation is the claim
    being tested, so using it here would make the check agree with itself.
    """
    get = s.get if isinstance(s, dict) else (lambda k, d=None: getattr(s, k, d))
    price = get("entry_price", None)
    qty = get("qty", None)
    if price is None or qty is None:
        return None
    try:
        return float(price) * float(qty)
    except (TypeError, ValueError):
        return None


def slice_entry_commission(s):
    """What this slice's BUY leg already cost in commission. 0.0 when none.

    THE TIMING HOLE THIS CLOSES, measured live 2026-09-28.

    A buy debits the wallet by the spend PLUS Coinbase's commission. The
    slice records basis = entry_price * qty, which is the spend alone, and
    no branch's allocated_usd moves. So between buying and selling, the
    commission has left the account and nothing in the book has recognised
    it: the branches go on claiming cash that is already gone.

    It does get booked - eventually. _grid_slice_net_pnl charges BOTH legs
    when the slice sells and the result lands in allocated_usd, so a
    completed round trip reconciles exactly. The gap is purely the interval
    between the two, and it scales with how much inventory sits open.

    Live figures the day this was found: $44.27 of the claim was
    unaccounted for, against $42.22 of entry commission carried by
    $6,934.48 of open slices at the measured 0.006088/leg - a 4.6% match,
    and the residual is price drift between two reads. That $44.27 is also
    why real_free_cash_usd read -$246.87 and blocked funding a new coin:
    the fleet was budgeting cash it had already spent on commission.

    ADOPTED SLICES RETURN 0.0 and must. They were written by bookkeeping
    for coin the account already held - no order was placed, so no entry
    commission was billed against that basis. Charging one here would
    invent a hole the same size as the fleet's adopted inventory.
    """
    get = s.get if isinstance(s, dict) else (lambda k, d=None: getattr(s, k, d))
    if get("adopted", False):
        return 0.0
    rate = get("entry_fee_rate", None)
    cost = slice_cost(s)
    if rate is None or cost is None:
        # UNKNOWN, not zero: the caller counts these separately so an
        # unrecorded rate reads as "not measured here" rather than "free".
        return None
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return None
    if r <= 0:
        return 0.0
    return cost * r


def backing(branches, wallet_cash, usd_on_hold=None):
    """{claimed, deployed_coin, wallet_cash, backed, unbacked, ...}.

    `branches` carry allocated_usd and their slices. `wallet_cash` is the
    real USD balance at the venue - None when it could not be read, which
    produces a verdict of "unknown" rather than a number. An unreadable
    wallet is not evidence that the claim is sound.

    `usd_on_hold` is USD the venue has LOCKED against the fleet's own
    resting orders. It IS backing and must be counted.

    Measured live 2026-09-28: the wallet held $515.31 of USD, of which
    $450.75 was available and $64.56 was locked behind a resting maker buy.
    get_usd_balance() returns available only - correct for SIZING a buy,
    since locked money cannot be spent twice - and this check was being fed
    that same number. So $64.56 of real, present money vanished from the
    backing while the branch that committed it still counted it as unspent
    budget, and the difference showed up as an unexplained hole.

    Two different questions, two different numbers: what may I spend is
    available; what is actually behind the claim is available plus hold.
    Conflating them is what produced a drift nobody could account for.

    Slices whose cost cannot be computed are COUNTED and reported in
    `unpriced_slices` rather than treated as zero, because a slice read as
    zero inflates the apparent hole and would raise a false alarm.
    """
    if isinstance(branches, (str, bytes)) or not hasattr(branches, "__iter__"):
        raise ValueError(f"branches must be an iterable of branches, got {type(branches).__name__}")

    claimed = 0.0
    deployed = 0.0
    unpriced = 0
    entry_fees = 0.0
    unmeasured_fee_slices = 0
    for b in branches:
        # SHAPE is fatal, VALUES are skippable. Iterating a string yields
        # characters; each one silently misses every field and the whole
        # fleet reports as claiming $0.00 - i.e. perfectly backed. A check
        # that answers "all clear" when handed the wrong type is worse
        # than no check, so malformed input raises and the caller reports
        # "unknown". An unreadable NUMBER inside a real branch is still
        # just skipped, below.
        if not isinstance(b, dict) and not hasattr(b, "allocated_usd"):
            raise ValueError(f"not a branch: {type(b).__name__}")
        get = b.get if isinstance(b, dict) else (lambda k, d=None: getattr(b, k, d))
        try:
            claimed += float(get("allocated_usd", 0) or 0)
        except (TypeError, ValueError):
            pass
        for s in (get("slices", None) or []):
            cost = slice_cost(s)
            if cost is None:
                unpriced += 1
            else:
                deployed += cost
            fee = slice_entry_commission(s)
            if fee is None:
                unmeasured_fee_slices += 1
            else:
                entry_fees += fee

    claimed = round(claimed, 2)
    deployed = round(deployed, 2)
    entry_fees = round(entry_fees, 2)

    if wallet_cash is None:
        return {
            "verdict": "unknown",
            "claimed_usd": claimed,
            "deployed_coin_usd": deployed,
            "wallet_cash_usd": None,
            "backed_usd": None,
            "unbacked_usd": None,
            "unbacked_pct": None,
            "unpriced_slices": unpriced,
            "detail": ("The venue's USD balance could not be read, so the claim cannot be "
                       "checked. Not knowing is not the same as being fine."),
        }

    wallet = round(float(wallet_cash), 2)
    try:
        held = round(float(usd_on_hold), 2) if usd_on_hold is not None else 0.0
    except (TypeError, ValueError):
        held = 0.0
    held = max(0.0, held)
    backed = round(deployed + wallet + held, 2)
    gross_unbacked = round(claimed - backed, 2)
    # Commission on the OPEN slices' buy legs has already left the wallet and
    # is not yet booked against any allocation. It is spent, not missing, and
    # it reconciles the moment the slice sells - so it is named and removed
    # here rather than left to swamp a real hole. See slice_entry_commission.
    unbacked = round(gross_unbacked - entry_fees, 2)
    pct = round(unbacked / claimed * 100, 2) if claimed else 0.0

    if unbacked <= BACKING_TOLERANCE_USD:
        verdict = "backed"
        detail = (f"Branches claim ${claimed:,.2f}; ${backed:,.2f} is really there "
                  f"(${deployed:,.2f} of coin, ${wallet:,.2f} of cash"
                  + (f", ${held:,.2f} held against resting orders" if held else "") + ").")
    elif pct >= BACKING_ALARM_PCT:
        verdict = "unbacked"
        detail = (f"Branches claim ${claimed:,.2f}. Only ${backed:,.2f} is really there - "
                  f"${deployed:,.2f} of coin the branches actually bought and ${wallet:,.2f} "
                  f"of USD in the wallet. ${unbacked:,.2f} ({pct:.1f}%) is a number in a "
                  f"database with nothing behind it.")
    else:
        verdict = "drifting"
        detail = (f"Branches claim ${claimed:,.2f} against ${backed:,.2f} really there. "
                  f"${unbacked:,.2f} ({pct:.1f}%) is unaccounted for - small, but it is "
                  f"not rounding.")

    if entry_fees:
        detail += (f" ${entry_fees:,.2f} of that is commission already paid on the open "
                   f"slices' buy legs - spent, not missing, and booked back the moment each "
                   f"slice sells; it is excluded from the ${unbacked:,.2f} above.")
    if unmeasured_fee_slices:
        detail += (f" {unmeasured_fee_slices} open slice(s) carry no recorded entry fee rate, "
                   f"so their commission is NOT deducted - the real gap is smaller than shown "
                   f"by whatever they paid.")
    if unpriced:
        detail += (f" {unpriced} open slice(s) could not be priced and are NOT counted as "
                   f"backing, so the real gap is smaller than the figure shown by whatever "
                   f"they are worth.")

    return {
        "verdict": verdict,
        "claimed_usd": claimed,
        "deployed_coin_usd": deployed,
        "wallet_cash_usd": wallet,
        "usd_on_hold": held,
        "backed_usd": backed,
        "unbacked_usd": unbacked,
        "unbacked_pct": pct,
        "unpriced_slices": unpriced,
        "open_entry_commission_usd": entry_fees,
        "gross_unbacked_usd": gross_unbacked,
        "slices_without_fee_rate": unmeasured_fee_slices,
        "detail": detail,
    }
