"""The family tree's coin selector, scored against the branches we already have.

THE OWNER'S INSTRUCTION, 2026-10-05: "When them coins... end of August to the
beginning of September... It knew how to find the coins that we needed to
make money off of... I want that deployed right now." And, in the same
breath, the constraint the old one never had: "I just don't want it to put
so much money into one coin like it was doing... I wanted to be able to
split it up."

WHAT IS BROUGHT BACK. The four gates
crypto_family_tree_bot.find_most_volatile_unclaimed_coin applied, unchanged
in substance and using the same engine functions it used:

  1. RSI(14) below engine.ENTRY_MAX_RSI (65) - never buy what is already
     extended. Added after PEPE, DOGE and XRP all lost on exactly that.
  2. The coin beats BTC over the same ~25h window (alpha > 0). Promoted to
     live selection after a 30-day/21-coin comparison moved 15 of 21.
  3. Hourly SMA20 > SMA50. Promoted after a 30-day/18-coin comparison moved
     15 of 18, several substantially.
  4. Among what survives, rank by ATR% - the most volatile BULLISH coin
     first, because a coin that moves gives the step more chances to fire.

Gates 2 and 3 FAIL OPEN, exactly as they did in the tree: a benchmark or an
hourly history that cannot be read is not evidence against a coin.

WHAT IS DELIBERATELY NOT BROUGHT BACK.

NO COIN HUNTING, NO NEW BRANCHES. The tree used this to pick an unclaimed
coin and spawn onto it. The owner said an hour earlier: "Do not make any
more branches, whatever we have. Keep it the way it is." So this scores
the branches that ALREADY exist and says which of them should get the next
dollar. It returns a name. It buys nothing and it rotates no branch off
its coin, which would mean selling, which the owner has ruled out.

THE CAP THE OLD ONE DID NOT HAVE. This is the part the owner added and it
is the part that matters most. The tree's selector had no size limit at
all, and the ledger shows where that ended: 167 trades for -$508.44, with
POL alone taking 87 of them and -$392.43, and single coins reaching 23% and
27% of the account. MAX_COIN_SHARE is checked BEFORE a coin can be ranked
at all, so the winner of the quality ranking is never a coin that is
already too big. A coin over the cap is not penalised - it is removed.

NOTHING HERE PLACES AN ORDER, SELLS COIN, CREATES A BRANCH, OR MOVES A
DOLLAR. It reads prices and returns a ranking.
"""
import logging
import os

log = logging.getLogger(__name__)

# The owner's "split it up", as a number. Same ceiling the buy gate uses.
MAX_COIN_SHARE = float(os.getenv("GRID_MAX_COIN_SHARE", "0.20"))

ENV_FLAG = "GRID_COIN_QUALITY"


def enabled() -> bool:
    return (os.getenv(ENV_FLAG, "false") or "").strip().lower() == "true"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def judge(rows, *, btc_return=None, max_rsi=65.0, max_share=None,
          fleet_allocated_usd=None, amount_usd=0.0, exclude=None):
    """Score coins the fleet already holds. Returns (ranked, rejected).

    `rows` carry: product_id, bot_name, allocated_usd, atr_pct, rsi,
    coin_return, is_bullish, trend_ok, open_slices, num_levels.

    rsi/coin_return/trend_ok may be None. None is UNKNOWN, and an unknown
    never blocks a coin here - the tree failed those two gates open and
    this keeps that, because a missing benchmark is not a bad coin.
    An unreadable ALLOCATION is different and does block, because the cap
    cannot be checked without it and an unchecked cap is the whole fault
    being fixed.
    """
    cap = MAX_COIN_SHARE if max_share is None else max_share
    rows = list(rows or [])
    fleet = _num(fleet_allocated_usd)
    if fleet is None:
        fleet = sum((_num(r.get("allocated_usd")) or 0.0) for r in rows)
    amount = _num(amount_usd) or 0.0
    btc = _num(btc_return)

    ranked, rejected = [], []
    for r in rows:
        pid, name = r.get("product_id"), r.get("bot_name")
        alloc = _num(r.get("allocated_usd"))

        def no(why):
            rejected.append({"product_id": pid, "bot_name": name, "why": why})

        if exclude and name in set(exclude):
            no("excluded by the caller")
            continue
        if alloc is None or fleet is None or fleet <= 0:
            no("allocation or fleet total unreadable - the cap cannot be "
               "checked, and an unchecked cap is the fault being fixed")
            continue

        # THE CAP FIRST. A coin already too big is removed before quality is
        # considered at all, so no amount of being the best coin can carry
        # it past the owner's "split it up".
        share_after = (alloc + amount) / fleet
        if share_after > cap:
            no(f"already {alloc / fleet * 100:.1f}% of the fleet; this would "
               f"make it {share_after * 100:.1f}%, past the {cap * 100:.0f}% cap")
            continue

        levels = _num(r.get("num_levels")) or 0
        if (r.get("open_slices") or 0) >= levels > 0:
            no("every rung is full - a dollar here cannot become a rung")
            continue

        rsi = _num(r.get("rsi"))
        if rsi is not None and rsi >= max_rsi:
            no(f"RSI {rsi:.1f} is already overbought (>= {max_rsi:.0f})")
            continue

        cret = _num(r.get("coin_return"))
        if btc is not None and cret is not None and (cret - btc) <= 0:
            no(f"not beating BTC over the same window "
               f"(coin {cret * 100:+.2f}% vs BTC {btc * 100:+.2f}%)")
            continue

        if r.get("trend_ok") is False:
            no("hourly SMA20/SMA50 trend is DOWN")
            continue

        ranked.append({
            "product_id": pid, "bot_name": name,
            "allocated_usd": round(alloc, 2),
            "share_now_pct": round(alloc / fleet * 100.0, 2),
            "share_after_pct": round(share_after * 100.0, 2),
            "atr_pct": _num(r.get("atr_pct")),
            "rsi": rsi,
            "alpha_vs_btc_pct": (None if (btc is None or cret is None)
                                 else round((cret - btc) * 100.0, 3)),
            "is_bullish": bool(r.get("is_bullish")),
            "trend_ok": r.get("trend_ok"),
            "empty_rungs": int(levels) - int(r.get("open_slices") or 0),
        })

    # The tree's own order: a BULLISH coin first, then the most volatile.
    # A coin with no readable ATR sorts last rather than first - an unknown
    # must not win a ranking it was never measured for.
    ranked.sort(key=lambda r: (not r["is_bullish"],
                               -(r["atr_pct"] if r["atr_pct"] is not None else -1)))
    return ranked, rejected


def best(rows, **kw):
    """The one coin to put the next dollar into, or (None, why not)."""
    ranked, rejected = judge(rows, **kw)
    if not ranked:
        return None, {"reason": "no coin cleared the gates and the cap",
                      "rejected": rejected}
    top = ranked[0]
    bits = []
    if top["atr_pct"] is not None:
        bits.append(f"ATR {top['atr_pct'] * 100:.2f}%")
    if top["rsi"] is not None:
        bits.append(f"RSI {top['rsi']:.0f}")
    if top["alpha_vs_btc_pct"] is not None:
        bits.append(f"{top['alpha_vs_btc_pct']:+.2f}% vs BTC")
    bits.append(f"{top['share_after_pct']:.1f}% of the fleet after")
    return top["bot_name"], {
        "reason": ("most volatile coin that is bullish, not overbought, "
                   "beating BTC, trending up, and under the cap - "
                   + ", ".join(bits)),
        "target": top, "runners_up": ranked[1:4], "rejected": rejected,
    }
