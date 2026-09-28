"""Which coins are worth gridding - measured, not picked.

Built 2026-09-28 when the account owner said "scan for more ACH-class coins
and move the money there". ACH turns over 16 round trips in 60 days on $100
while ZEC turns over 1 on $2,273, so the instinct was right. The scan found
the coins. It also found two things that change what "ACH-class" means.

ONE: ACH ITSELF CANNOT BE FUNDED. Its 24h notional is under the $750,000
depth floor the fleet already applies. The 16 trips are real and the coin is
too thin to put size on, so "more coins like ACH" cannot mean "more coins as
thin as ACH" - it means coins that oscillate like ACH and have a book.

TWO: TRIPS ALONE PICK TRAPS. The two highest-trip coins in the whole liquid
universe were COTI (18 trips) and HFT (18 trips). COTI fell 21% over the
window with a 50% drawdown; HFT fell 26% with an 85% drawdown. A grid on
either would have oscillated beautifully into a pile of open red slices.
Ranking on trips alone would have funded exactly those two.

So a grid coin needs three things at once, and this module scores all three:

    TRIPS      it has to actually oscillate, or nothing ever fills
    FLAT NET   a strong trend either way is disqualifying - a runner gets
               sold out early and the grid sits in cash while it climbs; a
               bleeder leaves every slice open and underwater
    SHALLOW DD the path matters, not just the endpoints. A coin that ends
               flat after a 60% round trip down spent that time red.

This is the same asymmetry the fleet already learned pricing replays: over a
rising window a POSITIVE result proves nothing, because the climb produced
it. USELESS rose 434% with 9 trips and is rejected here for that reason, not
in spite of it.

Pure functions over already-measured inputs - the measurement itself is
coin_rotation.measure_universe / measure_liquidity and real candles - so the
ranking is testable and re-runnable rather than remembered from a chat.
"""

# The fleet's existing depth floor, reused rather than re-invented: a book
# thinner than this cannot absorb a branch without the order moving it.
MIN_24H_NOTIONAL_USD = 750_000.0

# 60 days of hourly candles is ~1440. A coin with much less has not been
# listed long enough to have an opinion about, and a short history is
# UNKNOWN - never scored zero, which would rank it as "does not move".
MIN_CANDLES = 1200

# A coin that fell this far over the window, or drew down this deep inside
# it, is rejected outright rather than penalised. Below these the grid is
# not oscillating, it is catching a falling knife in instalments.
MAX_DECLINE_PCT = -20.0
MAX_DRAWDOWN_PCT = -45.0

# A coin that ran this far is rejected the other way: the grid would have
# sold its inventory into the first leg and watched the rest from cash.
MAX_RUNUP_PCT = 100.0

# How hard travel and drawdown are discounted. Both are denominators, so the
# score is trips-per-unit-of-drama rather than trips minus a fudge.
TRAVEL_SCALE_PCT = 25.0
DRAWDOWN_SCALE_PCT = 40.0


def reject_reason(trips, candles, notional_usd, change_pct, drawdown_pct):
    """Why this coin is not fundable, or None when it is. Checked before any
    score, so a rejected coin can never out-rank a fundable one on a big
    trip count - which is precisely how COTI and HFT would have won."""
    if candles is None or candles < MIN_CANDLES:
        return f"history too short ({candles} candles) - unknown, not zero"
    if notional_usd is None:
        return "depth unreadable - cannot be funded on an unknown book"
    if notional_usd < MIN_24H_NOTIONAL_USD:
        return (f"${notional_usd:,.0f} 24h notional is under the "
                f"${MIN_24H_NOTIONAL_USD:,.0f} depth floor - too thin to fund")
    if change_pct is None or drawdown_pct is None:
        return "price path unreadable - trips alone are not enough to judge"
    if drawdown_pct <= MAX_DRAWDOWN_PCT:
        return (f"{drawdown_pct:.0f}% drawdown inside the window - the grid "
                f"would have held it red, however many times it oscillated")
    if change_pct <= MAX_DECLINE_PCT:
        return f"fell {change_pct:.1f}% over the window - oscillation is decay here"
    if change_pct >= MAX_RUNUP_PCT:
        return (f"rose {change_pct:.1f}% - a runner, the grid sells its inventory "
                f"into the first leg and watches the rest from cash")
    if not trips:
        return "no completed round trips at this step - nothing would ever fill"
    return None


def suitability(trips, change_pct, drawdown_pct):
    """Trips per unit of drama. Higher is better; only meaningful for a coin
    reject_reason() has already cleared."""
    return (float(trips)
            / (1.0 + abs(float(change_pct)) / TRAVEL_SCALE_PCT)
            / (1.0 + abs(float(drawdown_pct)) / DRAWDOWN_SCALE_PCT))


def rank(rows):
    """rows: {product_id: {trips, candles, notional_usd, change_pct,
    drawdown_pct}} -> (fundable, rejected), each a list of dicts, best first.

    Every input coin appears in exactly one of the two lists, with a reason.
    A coin is never silently dropped: a scan that quietly loses candidates is
    indistinguishable from one that found nothing.
    """
    fundable, rejected = [], []
    for pid, r in rows.items():
        why = reject_reason(r.get("trips"), r.get("candles"), r.get("notional_usd"),
                            r.get("change_pct"), r.get("drawdown_pct"))
        if why:
            rejected.append({"product_id": pid, "reason": why, **r})
        else:
            fundable.append({"product_id": pid,
                             "score": round(suitability(r["trips"], r["change_pct"],
                                                        r["drawdown_pct"]), 3), **r})
    fundable.sort(key=lambda x: -x["score"])
    rejected.sort(key=lambda x: x["product_id"])
    return fundable, rejected


def fundable_usd(free_cash_usd, reserve_usd, count):
    """What may actually be put behind `count` new branches. (usd_each, why).

    Returns 0.0 whenever there is nothing free, which on this fleet is the
    normal case and not an error: measured 2026-09-28, free cash was
    -$246.87 because fifteen adopted branches each hold an open rung
    reserving roughly $73.76 against a $518.39 wallet. Finding a better coin
    and having money to put on it are different questions, and this one gets
    answered before anything is created.
    """
    if free_cash_usd is None:
        return 0.0, "free cash unreadable - a gap is not a zero, so nothing is funded"
    free = float(free_cash_usd)
    if free <= 0:
        return 0.0, (f"free cash is ${free:,.2f} - the fleet's branches already claim "
                     f"every dollar in the wallet. A new coin can only be funded by "
                     f"taking capital off an existing one.")
    if count <= 0:
        return 0.0, "no coins to fund"
    each = int((free / count) * 100) / 100.0
    return each, f"${each:,.2f} each - ${free:,.2f} free split {count} ways"
