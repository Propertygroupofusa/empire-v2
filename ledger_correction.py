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

  FEE_LEG         Every row - all 167 - charged commission on the exit leg
                  only. entry_price is a fill price and Coinbase bills
                  commission separately, so the real cost basis is
                  entry_price * qty PLUS the entry commission. $176.41
                  across the ledger, always flattering.

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

# The rate the tree itself used when these rows were written
# (crypto_family_tree_bot.ROUND_TRIP_FEE_RATE = engine.ROUND_TRIP_FEE_RATE).
# Deliberately the bot's own constant and not the 1.1931% blended rate
# measured from Coinbase fills: a correction has to be reproducible from
# the row plus a stated rate, and no per-row commission was ever recorded.
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


def plan_row(row: dict, round_trip_fee_rate: float = DEFAULT_ROUND_TRIP_FEE_RATE):
    """One row's correction, or None if it needs none.

    IDEMPOTENT. A row that already carries pnl_original has been corrected,
    and is left alone however its numbers read - otherwise a second run
    would treat the corrected value as a fresh error and correct it again.
    """
    if row.get("pnl_original") is not None:
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
    kind = classify(row, round_trip_fee_rate)
    if kind == CLEAN:
        return None
    new = correct_pnl(qty, entry, exit_, round_trip_fee_rate)
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
        "reason": (
            "proceeds were computed from filled_qty against a position.qty "
            "cost basis; recomputed from this row's own columns"
            if kind == BASIS_MISMATCH else
            "commission was charged on the exit leg only; entry leg added"
        ),
    }


def plan(rows, scope: str = "inconsistent",
         round_trip_fee_rate: float = DEFAULT_ROUND_TRIP_FEE_RATE) -> dict:
    """The whole correction, as data, before anything is written.

    scope="inconsistent"  only rows that cannot reproduce themselves (the 11)
    scope="all"           also the systematic one-fee-leg understatement

    The two are reported separately whichever scope runs, because they are
    different mistakes with different sizes and lumping them together is
    how the first audit produced a number nobody could act on.
    """
    if scope not in ("inconsistent", "all"):
        raise ValueError(f"scope must be 'inconsistent' or 'all', got {scope!r}")
    planned, skipped_fee_leg = [], []
    for row in rows:
        p = plan_row(row, round_trip_fee_rate)
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
