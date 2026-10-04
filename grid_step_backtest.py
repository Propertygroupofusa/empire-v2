"""What step size actually completes round trips, measured - not argued.

READ-ONLY AND OFFLINE OF THE ACCOUNT. It reads public Coinbase candles and
the fleet's own /grid-status for the branch list. It places no order, calls
no write endpoint, and changes no configuration. Running it cannot move a
dollar.

THE QUESTION. The fleet banked $135.58 over 196 closes in 34 days and has
been flat for 17 hours. The Capital-blockers panel says why: exactly ONE
branch, holding $65.16, can complete a round trip right now. Buy-only
accumulates and sell-only drains; only a branch that can do both compounds.
So the question is not "is the edge positive" - it is "at what step size
does a branch actually CLOSE a cycle", given a ~2.71% median daily range
and a 0.70% round-trip maker fee.

TWO MEASUREMENTS, AND THE SECOND IS THE HONEST ONE.

  BACKTEST   the whole window. Picks the best step per coin. This number
             is optimistic by construction: the step was chosen knowing
             the answer.

  FRONT TEST walk-forward. The step is chosen on the FIRST part of the
             window and then applied, unchanged, to data it has never
             seen. This is the only figure that says anything about
             tomorrow, and it is the one to read.

A backtest that only reports the first number is a fit, not a finding.

ASSUMPTIONS, STATED SO THEY CAN BE ARGUED WITH:

  * A bar fills a level only if it trades THROUGH it (low < trigger, not
    low <= trigger). Touching a price is not evidence of a fill.
  * Maker fee 0.35% per leg, 0.70% the round trip, matching the live tier.
  * A sell needs the slice up net of BOTH legs - the same rule the live
    profit_target and parked_sell paths use, which is why neither has ever
    booked a negative in 111 closes.
  * No slippage beyond the fee, and no queue position. Real maker-only
    orders rest and are CANCELLED after the timeout when they do not fill,
    so the live fleet closes FEWER cycles than this says. Every count here
    is therefore an upper bound.
  * Candles are 15-minute. A faster round trip inside one bar is not seen.

WHAT IT DOES NOT DO. It does not recommend a change. The 3-rung cap is a
measured two-sided trade-off - it doubled the per-trade notional (median
rung $16.70 before 25 Sep, $41.54 after) while parking XRP and LINK - and
nothing here overrides that. Output is evidence for a decision the account
owner makes.
"""
import asyncio, json, math, statistics as st, sys, time, urllib.request
from datetime import datetime, timezone

GRAN = 900                      # 15-minute bars
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
FEE_LEG = 0.0035                # live maker tier, per leg
FEE_RT = FEE_LEG * 2
STEPS = [0.009, 0.012, 0.015, 0.020, 0.025, 0.030, 0.040]
LEVELS = 3                      # the live cap
OOS_FRACTION = 0.30             # the last 30% is never used to choose
BASE = "https://empire-v2-production.up.railway.app/api/trading-dashboard"


# A User-Agent is load-bearing, not decoration. urllib defaults to
# "Python-urllib/3.x" and the candles endpoint answers that with 403
# Forbidden while serving the identical URL to curl with a 200. The first
# run lost all 22 coins to it.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def _get(url, timeout=60):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def candles(product_id, days=DAYS, gran=GRAN, pause=0.34):
    """Oldest-first (ts, low, high, close), deduplicated.

    The timestamp format is load-bearing. isoformat() on a tz-aware
    datetime ends in "+00:00", and a literal "+" in a query string decodes
    as a SPACE, so every request came back empty and all 22 coins reported
    "candles unavailable". The endpoint wants a bare UTC stamp with a
    trailing Z, which is what horizon_study.fetch_history already sends.

    `err` carries the last failure out rather than swallowing it - the
    first version returned None for a network error and a bad URL alike,
    which is how one typo silently scored nothing.
    """
    rows, now = {}, int(time.time())
    end, floor, err = now, now - days * 86400, None
    while end > floor:
        start = max(floor, end - 300 * gran)
        url = (f"https://api.exchange.coinbase.com/products/{product_id}/candles"
               f"?granularity={gran}"
               f"&start={datetime.utcfromtimestamp(start).isoformat()}Z"
               f"&end={datetime.utcfromtimestamp(end).isoformat()}Z")
        try:
            data = _get(url, timeout=30)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            break
        if not data:
            err = err or "the endpoint returned an empty page"
            break
        for x in data:
            rows[int(x[0])] = (float(x[1]), float(x[2]), float(x[4]))
        end = start
        time.sleep(pause)
    if len(rows) < 200:
        return None, (err or f"only {len(rows)} bars came back")
    ts = sorted(rows)
    return [(t, rows[t][0], rows[t][1], rows[t][2]) for t in ts], None


def simulate(bars, step, levels=LEVELS, alloc=1000.0):
    """One grid branch over one window. Returns the closed-trade record.

    Faithful to the live rules in the ways that decide the answer: a fixed
    number of rungs, a reference price that moves to the last fill, and a
    sell that refuses unless the slice clears BOTH fee legs.
    """
    if not bars:
        return None
    slice_usd = alloc / levels
    ref = bars[0][3]
    open_slices, closed = [], []
    for _t, low, high, close in bars:
        # A FLAT BRANCH TRACKS THE MARKET. Without this the reference stays
        # pinned wherever the last fill happened, so after any sustained
        # rise the buy trigger sits permanently out of reach and the branch
        # never fires again. The first run of this script had no re-anchor
        # and reported ONE round trip across 22 coins in 18 days, while the
        # live fleet closed 24 trades on 2 Oct alone - the simulator was
        # wrong, not the fleet.
        #
        # This is the same behaviour money_check() raises as the
        # stale_reference finding and /grid-status/reanchor-flat-branches
        # corrects: a branch holding nothing measures its next buy from the
        # live price, not from a reference the market has left behind.
        if not open_slices:
            ref = close
        # SELL first. A bar that both fills a rung and closes one is counted
        # as the close only - assuming both would be inventing a round trip
        # out of a single 15-minute bar.
        if open_slices:
            best = min(open_slices, key=lambda s: s["entry"])
            target = best["entry"] * (1 + step)
            if high > target:
                gross = (target / best["entry"] - 1.0)
                net = gross - FEE_RT
                if net > 0:
                    closed.append(net * best["usd"])
                    open_slices.remove(best)
                    ref = target
                    continue
        trigger = ref * (1 - step)
        if len(open_slices) < levels and low < trigger:
            open_slices.append({"entry": trigger, "usd": slice_usd})
            ref = trigger
    return {
        "round_trips": len(closed),
        "net_usd": round(sum(closed), 2),
        "open_at_end": len(open_slices),
        "net_pct_of_alloc": round(100.0 * sum(closed) / alloc, 3),
    }


def main():
    try:
        gs = _get(f"{BASE}/grid-status", timeout=90)
    except Exception as e:
        print(f"could not read the live branch list: {e}")
        return 1
    branches = [b for b in (gs.get("branches") or []) if b.get("product_id")]
    print(f"{len(branches)} live branches, {DAYS}d of {GRAN//60}m candles, "
          f"fee {FEE_RT*100:.2f}% round trip, {LEVELS} rungs\n")
    print(f"{'coin':11} {'bars':>6} {'live':>6} | {'BACKTEST best':>26} | "
          f"{'FRONT TEST (out of sample)':>34}")
    print(f"{'':11} {'':>6} {'step':>6} | {'step':>6} {'trips':>6} {'net%':>7}  | "
          f"{'chosen':>7} {'trips':>6} {'net%':>7} {'vs live':>9}")
    print("-" * 104)

    rows = []
    for b in branches:
        pid = b["product_id"]
        live_step = b.get("grid_pct")
        bars, why = candles(pid)
        if not bars:
            print(f"{pid:11} {'--':>6} {'':>6} | SKIPPED, not scored - {why}")
            continue
        cut = int(len(bars) * (1 - OOS_FRACTION))
        ins, oos = bars[:cut], bars[cut:]

        full = {s: simulate(bars, s) for s in STEPS}
        best_full = max(STEPS, key=lambda s: full[s]["net_pct_of_alloc"])

        # FRONT TEST: choose on in-sample only, then apply to unseen data.
        insamp = {s: simulate(ins, s) for s in STEPS}
        chosen = max(STEPS, key=lambda s: insamp[s]["net_pct_of_alloc"])
        o_chosen = simulate(oos, chosen)
        o_live = simulate(oos, live_step) if live_step else None

        rows.append({"pid": pid, "live_step": live_step, "chosen": chosen,
                     "oos_trips": o_chosen["round_trips"],
                     "oos_net": o_chosen["net_pct_of_alloc"],
                     "oos_live_trips": (o_live or {}).get("round_trips"),
                     "oos_live_net": (o_live or {}).get("net_pct_of_alloc")})
        lv = f"{live_step*100:.2f}%" if live_step else "--"
        vs = (f"{o_live['net_pct_of_alloc']:+.2f}%" if o_live else "--")
        print(f"{pid:11} {len(bars):>6} {lv:>6} | {best_full*100:>5.1f}% "
              f"{full[best_full]['round_trips']:>6} {full[best_full]['net_pct_of_alloc']:>6.2f}%  | "
              f"{chosen*100:>6.1f}% {o_chosen['round_trips']:>6} "
              f"{o_chosen['net_pct_of_alloc']:>6.2f}% {vs:>9}")

    if not rows:
        print("\nnothing scored.")
        return 1

    print("\n" + "=" * 104)
    oos_days = DAYS * OOS_FRACTION
    tt = sum(r["oos_trips"] for r in rows)
    tl = sum(r["oos_live_trips"] or 0 for r in rows)
    mn = st.median(r["oos_net"] for r in rows)
    ml = st.median(r["oos_live_net"] for r in rows if r["oos_live_net"] is not None)
    print(f"OUT OF SAMPLE, {oos_days:.0f} days, {len(rows)} coins - the only figures worth reading")
    print(f"  tuned step   {tt:>5} round trips   median {mn:+.2f}% of allocation")
    print(f"  LIVE step    {tl:>5} round trips   median {ml:+.2f}% of allocation")
    print(f"  per coin per day, tuned: {tt/len(rows)/oos_days:.2f} trips   "
          f"live: {tl/len(rows)/oos_days:.2f} trips")
    best = sorted(rows, key=lambda r: -r["oos_net"])[:6]
    print("\n  best out-of-sample coins (tuned step):")
    for r in best:
        print(f"    {r['pid']:11} step {r['chosen']*100:.1f}%  "
              f"{r['oos_trips']:>3} trips  {r['oos_net']:+.2f}%   "
              f"(live {(r['live_step'] or 0)*100:.2f}% -> {r['oos_live_trips']} trips, "
              f"{r['oos_live_net']:+.2f}%)")
    agree = sum(1 for r in rows if r["live_step"]
                and abs(r["chosen"] - r["live_step"]) < 0.0025)
    print(f"\n  the tuned step lands within 0.25pp of the LIVE step on "
          f"{agree} of {len(rows)} coins")
    print("\nEvery count above is an UPPER BOUND: it assumes a resting maker order "
          "\nalways fills when price trades through it. The live fleet cancels "
          "\nunfilled makers after the timeout, so it closes fewer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
