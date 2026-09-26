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


def backing(branches, wallet_cash):
    """{claimed, deployed_coin, wallet_cash, backed, unbacked, ...}.

    `branches` carry allocated_usd and their slices. `wallet_cash` is the
    real USD balance at the venue - None when it could not be read, which
    produces a verdict of "unknown" rather than a number. An unreadable
    wallet is not evidence that the claim is sound.

    Slices whose cost cannot be computed are COUNTED and reported in
    `unpriced_slices` rather than treated as zero, because a slice read as
    zero inflates the apparent hole and would raise a false alarm.
    """
    if isinstance(branches, (str, bytes)) or not hasattr(branches, "__iter__"):
        raise ValueError(f"branches must be an iterable of branches, got {type(branches).__name__}")

    claimed = 0.0
    deployed = 0.0
    unpriced = 0
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

    claimed = round(claimed, 2)
    deployed = round(deployed, 2)

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
    backed = round(deployed + wallet, 2)
    unbacked = round(claimed - backed, 2)
    pct = round(unbacked / claimed * 100, 2) if claimed else 0.0

    if unbacked <= BACKING_TOLERANCE_USD:
        verdict = "backed"
        detail = (f"Branches claim ${claimed:,.2f}; ${backed:,.2f} is really there "
                  f"(${deployed:,.2f} of coin, ${wallet:,.2f} of cash).")
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

    if unpriced:
        detail += (f" {unpriced} open slice(s) could not be priced and are NOT counted as "
                   f"backing, so the real gap is smaller than the figure shown by whatever "
                   f"they are worth.")

    return {
        "verdict": verdict,
        "claimed_usd": claimed,
        "deployed_coin_usd": deployed,
        "wallet_cash_usd": wallet,
        "backed_usd": backed,
        "unbacked_usd": unbacked,
        "unbacked_pct": pct,
        "unpriced_slices": unpriced,
        "detail": detail,
    }
