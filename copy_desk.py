"""The copy desk: re-derive every number on air, from source, independently.

WHY THIS EXISTS

In a real newsroom the copy desk aggressively checks the math before
anything runs. This newsroom did not have one, and the session that built
it is the argument for it. Four numbers went on the record wrong in a
single day:

    the ledger gap       74%  ->  51%  ->  11.4%     (twice wrong)
    entry-leg fees       $176.41  ->  $94.97         (today's rate on
                                                      old rows)
    "hold longer"        recommended, then refuted twice
    the account total    $79.30 reported for an $11,292 account

Every one was believed because it had been computed. None was caught by a
test, because each test asserted the same wrong arithmetic the code used.
The only thing that catches that class of error is a SECOND, independent
derivation that disagrees out loud.

WHAT IT DOES

Takes the finished broadcast and the raw watch it came from, and recomputes
each headline figure from the underlying rows without reusing the code that
produced it. Where the two disagree it files a CORRECTION, which the
newsroom runs on air exactly the way a real one does - visibly, not by
quietly amending the original.

WHAT IT DELIBERATELY DOES NOT DO

It does not fix anything. A desk that silently corrects a number teaches
nobody that the number was wrong, and the next error of the same kind ships
just as quietly. It reports, and the discrepancy stays visible until
someone fixes the source.
"""
from __future__ import annotations

# A cent of float drift is not a correction. A dollar is.
USD_TOLERANCE = 0.02
PCT_TOLERANCE = 0.05

SEVERITY_ORDER = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2}


def _f(x, default=None):
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


def _correction(severity, field, on_air, recomputed, note):
    return {"severity": severity, "field": field, "on_air": on_air,
            "recomputed": recomputed, "note": note}


def check(brief: dict, watch: dict) -> dict:
    """Every headline number, re-derived from the rows. Returns corrections."""
    out = []
    rows = watch.get("rows") or []
    cap = brief.get("capital") or {}

    # --- the book total, from the holdings themselves -------------------
    summed = round(sum(_f(r.get("usd"), 0.0) or 0.0 for r in rows), 2)
    on_air_coin = _f(cap.get("coin_usd"))
    if on_air_coin is not None and abs(summed - on_air_coin) > USD_TOLERANCE:
        out.append(_correction(
            "CRITICAL", "capital.coin_usd", on_air_coin, summed,
            "The coin total on air does not equal the sum of the holdings "
            "behind it. One of them is wrong and it is not knowable which "
            "from this side."))

    coin, cash = _f(cap.get("coin_usd"), 0.0), _f(cap.get("cash_usd"), 0.0)
    total = _f(cap.get("total_usd"))
    if total is not None and abs((coin + cash) - total) > USD_TOLERANCE:
        out.append(_correction(
            "CRITICAL", "capital.total_usd", total, round(coin + cash, 2),
            "Book total is not coin plus cash."))

    # --- coverage -------------------------------------------------------
    covered = [r for r in rows if r.get("status") in ("OK", "NEAR_STOP", "BREACHED")]
    cov_usd = sum(_f(r.get("usd"), 0.0) or 0.0 for r in covered)
    if summed:
        recomputed_cov = round(100.0 * cov_usd / summed, 2)
        on_air_cov = _f(cap.get("covered_share_pct"))
        if on_air_cov is not None and abs(recomputed_cov - on_air_cov) > PCT_TOLERANCE:
            out.append(_correction(
                "MAJOR", "capital.covered_share_pct", on_air_cov, recomputed_cov,
                "Coverage on air disagrees with the rows that actually carry "
                "a level."))

    # --- every BREACHED row is genuinely below its level ----------------
    for r in rows:
        price, level = _f(r.get("price")), _f(r.get("stop_level"))
        if price is None or level is None:
            continue
        if r.get("status") == "BREACHED" and price > level:
            out.append(_correction(
                "CRITICAL", f"status[{r.get('asset')}]", "BREACHED", "not breached",
                f"Reported as through its level at {price:,.8g}, but the level "
                f"is {level:,.8g}. A false breach is worse than a missed one - "
                f"it invites a sale that was never called for."))
        if r.get("status") in ("OK", "NEAR_STOP") and price <= level:
            out.append(_correction(
                "CRITICAL", f"status[{r.get('asset')}]", r.get("status"), "BREACHED",
                f"{r.get('asset')} is at {price:,.8g}, at or below its "
                f"{level:,.8g} level, and is NOT being reported as breached. "
                f"${_f(r.get('usd'), 0):,.2f} is exposed and the desk is silent "
                f"on it."))

    # --- every story names an asset that exists -------------------------
    known = {r.get("asset") for r in rows}
    for note in brief.get("producer_notes") or []:
        for asset in str(note.get("asset", "")).split(", "):
            if asset and asset not in known:
                out.append(_correction(
                    "MAJOR", "producer_notes", asset, "not in the holdings",
                    f"A story ran about {asset}, which is not in the book."))

    # --- concentration claims match the arithmetic ----------------------
    for r in watch.get("over_concentration") or []:
        share, usd = _f(r.get("share_of_account_pct")), _f(r.get("usd"))
        book = _f(cap.get("total_usd"))
        if share is None or usd is None or not book:
            continue
        recomputed = round(100.0 * usd / book, 2)
        if abs(recomputed - share) > 1.0:
            out.append(_correction(
                "MINOR", f"share[{r.get('asset')}]", share, recomputed,
                "Concentration share on air is computed against a different "
                "denominator than the book total."))

    # --- nothing claims a P&L nobody measured ---------------------------
    if cap.get("daily_pnl_usd") is not None:
        out.append(_correction(
            "CRITICAL", "capital.daily_pnl_usd", cap.get("daily_pnl_usd"), None,
            "A daily P&L is on air. No measurement in this system separates "
            "today's trading from price drift, so whatever this number is, it "
            "was not measured."))

    # --- a score with no components is a horoscope ----------------------
    for d in brief.get("quant_desk") or []:
        if d.get("score") is not None and not d.get("components"):
            out.append(_correction(
                "MAJOR", f"score[{d.get('asset')}]", d.get("score"), None,
                "A score is on air with no components behind it. An "
                "undecomposable number reads as authority it has not earned."))

    # --- the disclaimer survived to air ---------------------------------
    disc = (brief.get("disclaimer") or "").lower()
    if "not stop-loss orders" not in disc:
        out.append(_correction(
            "CRITICAL", "disclaimer", brief.get("disclaimer"),
            "must state these are not orders",
            "The broadcast is showing stop LEVELS without saying they are not "
            "stop ORDERS. Nothing rests at the exchange; a viewer who believes "
            "otherwise is unprotected and thinks they are covered."))

    out.sort(key=lambda c: SEVERITY_ORDER.get(c["severity"], 9))
    worst = out[0]["severity"] if out else None
    return {
        "checked": True,
        "corrections": out,
        "correction_count": len(out),
        "worst_severity": worst,
        "clears_for_air": not any(c["severity"] == "CRITICAL" for c in out),
        "basis": ("Every figure re-derived from the underlying rows without "
                  "reusing the code that produced it. A test that asserts the "
                  "same arithmetic as the code cannot catch the code being "
                  "wrong - only an independent derivation can."),
        "note": ("The desk reports, it does not fix. Silently correcting a "
                 "number teaches nobody it was wrong, and the next error of "
                 "the same kind ships just as quietly."),
    }


def coverage_drop(current: dict, previous: dict, threshold_pct: float = 5.0):
    """Did the watch go blind since the last broadcast?

    Coverage fell 99.5% -> 86.4% between two consecutive live runs because a
    volatility fetch was rate-limited. Nothing was wrong with the account;
    the DESK went blind, which is a different story and needs telling as one.
    A missing measurement that looks like a calm reading is the failure this
    whole system keeps rediscovering.
    """
    if not previous:
        return None
    a = _f((current or {}).get("covered_share_pct"))
    b = _f((previous or {}).get("covered_share_pct"))
    if a is None or b is None:
        return None
    drop = b - a
    if drop < threshold_pct:
        return None
    return {
        "severity": "MAJOR",
        "field": "capital.covered_share_pct",
        "on_air": a, "recomputed": b,
        "note": (f"Coverage fell {drop:.1f} points since the last broadcast "
                 f"({b:.1f}% to {a:.1f}%). The holdings did not change that "
                 f"fast - the desk lost a data feed. Positions that dropped "
                 f"out are UNWATCHED, not calm."),
    }
