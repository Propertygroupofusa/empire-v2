"""Recompute a trade-history row's P&L from the row's own columns.

WHY THIS EXISTS

The coin-history ledger contains rows that contradict themselves. A live
audit on 2026-09-26 found 11 of 167 whose recorded P&L cannot be produced
from the entry price, exit price and quantity stored beside it - $339.59
more negative in aggregate, every one in the same direction. Trade 48 is a
WINNING price move on a $24 position booked as -$91.52.

There were two separate defects, and they need telling apart:

  BASIS_MISMATCH  Proceeds were computed from filled_qty while the cost
                  basis came from position.qty. On a full fill the two are
                  equal and nothing is wrong; on a PARTIAL fill the row
                  books the proceeds of what was sold against the cost of
                  everything that was held. These are the 11. The size of
                  the error has nothing to do with the trade - it is the
                  cost of the UNSOLD remainder, charged to the sold part.

  FEE_LEG         Every row charged commission on the exit leg only.
                  entry_price is a fill price and Coinbase bills commission
                  separately, so the real cost basis is entry_price * qty
                  PLUS the entry commission. $94.97 across the 156
                  otherwise-consistent rows, always flattering.

WHAT A CORRECTION CAN AND CANNOT DO

It can make a row reproduce itself: P&L consistent with the prices and
quantity on that same row, under one formula, for every row.

It cannot bring money back. The 11 rows were never a real $339.59 loss,
but the coin that the over-charge represents is a genuinely unaccounted-for
remainder - the position was cleared while part of it was still held. This
module fixes the BOOKKEEPING. The hole in the account is a separate
problem and correcting these rows does not close it.

NOTHING HERE TOUCHES A DATABASE. These are pure functions over plain
dicts so the arithmetic can be tested, and so a dry run and a real run
compute the identical thing rather than two implementations of it.
"""
from __future__ import annotations

# THE RATE IS NOT A CONSTANT, AND USING TODAY'S WOULD FABRICATE FEES.
#
# The obvious implementation charges both legs at the bot's current
# ROUND_TRIP_FEE_RATE (0.015). Run against the live ledger that proposed
# -$253.44 across 156 rows. But solving each row for the rate it was
# actually WRITTEN with gives a median of 0.00800, with 148 of 156 inside
# 0.05% of it: these rows were booked when the fee schedule was 0.8%.
#
#   entry leg omitted, at the rate in force        -$ 90.76
#   re-rating those same rows 0.8% -> 1.5%         -$162.68
#                                                  --------
#   what the naive version proposed                -$253.44
#
# Two thirds of that "correction" would have been commission nobody ever
# paid. A correction that invents charges is not more conservative than
# one that omits them - it is just wrong in the other direction. So the
# rate is derived per row from the row itself, and this constant is only
# the last resort for a row too corrupt to derive one from.
DEFAULT_ROUND_TRIP_FEE_RATE = 0.015

# A row is "unable to reproduce itself" when the gap between its recorded
# P&L and the gross price move implies a commission outside this band, as
# a fraction of round-trip notional. Below zero means the row paid a
# NEGATIVE fee; above the ceiling means it paid more than any plausible
# schedule. Expressed as a rate rather than as dollars so it holds for a
# $7 trade and a $470 one alike, and so it survives the fee rate having
# changed over the life of the ledger.
IMPLIED_FEE_FLOOR = -0.0005          # allow rounding to go slightly negative
IMPLIED_FEE_CEILING = 0.05           # 5% round trip - far above any real tier

BASIS_MISMATCH = "BASIS_MISMATCH"
FEE_LEG = "FEE_LEG"
CLEAN = "CLEAN"

# Below this, a correction is rounding noise and the row is left alone.
MIN_CORRECTION_USD = 0.005




# THE CUTOVER, AND WHY ARITHMETIC ALONE CANNOT REPLACE IT.
#
# From (qty, entry, exit, pnl) there is NO way to tell a row that charged
# ONE leg at rate 2r from one that charged TWO legs at rate r. They are the
# same number. So a correction that derives its rate from the row will
# happily "fix" a row that was already right, doubling its fee - and it
# will do that to every row the fixed bot writes from now on, quietly,
# forever.
#
# The tiebreak has to come from outside the row. crypto_family_tree_bot.py
# started charging both legs in commit c235ead, 2026-09-26T17:12:42Z. A row
# closed at or after that instant was written by the corrected code and is
# already right; a row closed before it was not. That is a fact about the
# deployment, not about the numbers, which is exactly why it can settle a
# question the numbers cannot.
#
# If the formula changes again, add the new cutover here rather than
# widening the tolerance - a tolerance wide enough to spare correct rows is
# also wide enough to spare broken ones.
BOTH_LEGS_CUTOVER_ISO = "2026-09-26T17:12:42+00:00"


def _closed_at_or_after_cutover(row: dict) -> bool:
    """True when this row was written by code that already charged both legs.

    An unparseable or missing closed_at returns False - it is treated as
    old, so it stays in scope and gets looked at. The other default would
    let a row with a broken timestamp silently escape correction.
    """
    raw = row.get("closed_at")
    if not raw:
        return False
    try:
        from datetime import datetime
        txt = str(raw).replace("Z", "+00:00")
        when = datetime.fromisoformat(txt)
        cut = datetime.fromisoformat(BOTH_LEGS_CUTOVER_ISO)
        if when.tzinfo is None:
            when = when.replace(tzinfo=cut.tzinfo)
        return when >= cut
    except Exception:
        return False


def rate_as_written(qty: float, entry_price: float, exit_price: float,
                    recorded_pnl: float):
    """The fee rate a row was BOOKED with, solved from the row.

    Every row was written as gross minus an EXIT-leg charge:

        recorded = qty * (exit - entry) - qty * exit * (rate / 2)

    so rate = 2 * (gross - recorded) / (qty * exit). Returns None when the
    row has no exit notional to divide by, or when the answer is not a
    plausible fee - a row whose implied rate is negative or enormous is one
    of the BASIS_MISMATCH rows, and its arithmetic cannot be trusted to
    tell us anything, least of all what it was charged.
    """
    denom = qty * exit_price
    if not denom:
        return None
    gross = qty * (exit_price - entry_price)
    rate = 2.0 * (gross - recorded_pnl) / denom
    if rate < 0 or rate > IMPLIED_FEE_CEILING:
        return None
    return rate


def batch_rate(rows) -> float:
    """The era's fee rate, from the rows that can still state theirs.

    The BASIS_MISMATCH rows cannot be solved for a rate - that is what
    makes them mismatches. They are contemporaries of the rows that can be,
    though, so the median of their neighbours is a far better estimate than
    a constant from a later fee schedule. Falls back to the constant only
    when no row in the batch yields a rate at all.
    """
    rates = []
    for row in rows:
        try:
            r = rate_as_written(float(row.get("qty") or 0),
                                float(row.get("entry_price") or 0),
                                float(row.get("exit_price") or 0),
                                float(row.get("pnl") or 0))
        except (TypeError, ValueError):
            continue
        if r is not None:
            rates.append(r)
    if not rates:
        return DEFAULT_ROUND_TRIP_FEE_RATE
    rates.sort()
    n = len(rates)
    return rates[n // 2] if n % 2 else (rates[n // 2 - 1] + rates[n // 2]) / 2.0


def correct_pnl(qty: float, entry_price: float, exit_price: float,
                round_trip_fee_rate: float = DEFAULT_ROUND_TRIP_FEE_RATE) -> float:
    """What the row SHOULD say, from its own columns, charging both legs.

    Identical in shape to crypto_family_tree_bot._tree_realized_pnl and
    crypto_grid_bot._grid_slice_net_pnl. Three copies of one formula is how
    this ledger got into trouble; this one exists because a correction must
    run without importing a trading module.
    """
    gross = qty * (exit_price - entry_price)
    fee = qty * (entry_price + exit_price) * (round_trip_fee_rate / 2.0)
    return round(gross - fee, 2)


def implied_fee_rate(qty: float, entry_price: float, exit_price: float,
                     recorded_pnl: float):
    """The round-trip commission rate the recorded P&L implies.

    Returns None when the row carries no notional to divide by - an
    unknown, which is not the same as zero and must not be classified as
    if it were.
    """
    notional = qty * (entry_price + exit_price)
    if not notional:
        return None
    # fee_dollars = qty * (entry + exit) * (rate / 2), so rate is the
    # implied fee over that notional, doubled.
    gross = qty * (exit_price - entry_price)
    return (gross - recorded_pnl) / notional * 2.0


def classify(row: dict, round_trip_fee_rate: float = DEFAULT_ROUND_TRIP_FEE_RATE) -> str:
    """BASIS_MISMATCH, FEE_LEG or CLEAN, from the row alone."""
    try:
        qty = float(row.get("qty") or 0)
        entry = float(row.get("entry_price") or 0)
        exit_ = float(row.get("exit_price") or 0)
        recorded = float(row.get("pnl") or 0)
    except (TypeError, ValueError):
        return BASIS_MISMATCH
    rate = implied_fee_rate(qty, entry, exit_, recorded)
    if rate is None:
        return BASIS_MISMATCH
    if rate < IMPLIED_FEE_FLOOR or rate > IMPLIED_FEE_CEILING:
        return BASIS_MISMATCH
    if abs(correct_pnl(qty, entry, exit_, round_trip_fee_rate) - recorded) < MIN_CORRECTION_USD:
        return CLEAN
    return FEE_LEG


def plan_row(row: dict, round_trip_fee_rate: float = None,
             fallback_rate: float = DEFAULT_ROUND_TRIP_FEE_RATE):
    """One row's correction, or None if it needs none.

    IDEMPOTENT. A row that already carries pnl_original has been corrected,
    and is left alone however its numbers read - otherwise a second run
    would treat the corrected value as a fresh error and correct it again.
    """
    if row.get("pnl_original") is not None:
        return None
    # Written by the corrected bot - already charges both legs, and cannot
    # be distinguished from a one-leg row by arithmetic. Leave it alone.
    if _closed_at_or_after_cutover(row):
        return None
    try:
        qty = float(row.get("qty") or 0)
        entry = float(row.get("entry_price") or 0)
        exit_ = float(row.get("exit_price") or 0)
        recorded = float(row.get("pnl") or 0)
    except (TypeError, ValueError):
        return None
    if not qty or not entry or not exit_:
        # No columns to recompute from. A row like this cannot be repaired
        # by arithmetic and must not be silently zeroed.
        return None
    # THE RATE THIS ROW WAS CHARGED, not the rate the bot uses today.
    # An explicit round_trip_fee_rate overrides, so a caller can ask
    # "what would these look like at the current schedule" - but that is
    # never the default, because it would book fees nobody paid.
    if round_trip_fee_rate is not None:
        rate = round_trip_fee_rate
        rate_source = "supplied"
    else:
        derived = rate_as_written(qty, entry, exit_, recorded)
        if derived is None:
            rate, rate_source = fallback_rate, "batch_median"
        else:
            rate, rate_source = derived, "as_written"
    kind = classify(row, rate)
    if kind == CLEAN:
        return None
    new = correct_pnl(qty, entry, exit_, rate)
    delta = round(new - recorded, 2)
    if abs(delta) < MIN_CORRECTION_USD:
        return None
    return {
        "id": row.get("id"),
        "product_id": row.get("product_id"),
        "bot_name": row.get("bot_name"),
        "closed_at": row.get("closed_at"),
        "qty": qty,
        "entry_price": entry,
        "exit_price": exit_,
        "kind": kind,
        "pnl_recorded": round(recorded, 2),
        "pnl_corrected": new,
        "delta_usd": delta,
        "implied_fee_rate": round(implied_fee_rate(qty, entry, exit_, recorded), 6),
        "fee_rate_used": round(rate, 6),
        "fee_rate_source": rate_source,
        "reason": (
            "proceeds were computed from filled_qty against a position.qty "
            "cost basis; recomputed from this row's own columns"
            if kind == BASIS_MISMATCH else
            "commission was charged on the exit leg only; entry leg added"
        ),
    }


def plan(rows, scope: str = "inconsistent",
         round_trip_fee_rate: float = None) -> dict:
    """The whole correction, as data, before anything is written.

    scope="inconsistent"  only rows that cannot reproduce themselves (the 11)
    scope="all"           also the systematic one-fee-leg understatement

    The two are reported separately whichever scope runs, because they are
    different mistakes with different sizes and lumping them together is
    how the first audit produced a number nobody could act on.
    """
    if scope not in ("inconsistent", "all"):
        raise ValueError(f"scope must be 'inconsistent' or 'all', got {scope!r}")
    # The era's rate, for the rows too corrupt to state their own. Computed
    # from THIS batch, so a correction run years apart still uses the fee
    # schedule its own rows were written under.
    fallback = batch_rate(rows)
    planned, skipped_fee_leg = [], []
    for row in rows:
        p = plan_row(row, round_trip_fee_rate, fallback_rate=fallback)
        if p is None:
            continue
        if scope == "inconsistent" and p["kind"] != BASIS_MISMATCH:
            skipped_fee_leg.append(p)
            continue
        planned.append(p)
    planned.sort(key=lambda p: p["delta_usd"])

    def _tot(ps):
        return round(sum(p["delta_usd"] for p in ps), 2)

    basis = [p for p in planned if p["kind"] == BASIS_MISMATCH]
    feeleg = [p for p in planned if p["kind"] == FEE_LEG]
    return {
        "scope": scope,
        "round_trip_fee_rate": round_trip_fee_rate,
        "fee_rate_mode": ("supplied" if round_trip_fee_rate is not None
                          else "as_written, per row"),
        "both_legs_cutover": BOTH_LEGS_CUTOVER_ISO,
        "batch_fallback_rate": round(fallback, 6),
        "rows_examined": len(rows),
        "rows_to_change": len(planned),
        "basis_mismatch": {"rows": len(basis), "delta_usd": _tot(basis)},
        "fee_leg": {"rows": len(feeleg), "delta_usd": _tot(feeleg)},
        "not_in_scope": {"rows": len(skipped_fee_leg),
                         "delta_usd": _tot(skipped_fee_leg)},
        "total_delta_usd": _tot(planned),
        "changes": planned,
        "note": ("A correction makes a row reproduce itself. It does not "
                 "recover money. The BASIS_MISMATCH over-charge represents "
                 "an unsold remainder that was cleared from the position "
                 "and is still unaccounted for in the account."),
    }
