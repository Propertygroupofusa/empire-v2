#!/usr/bin/env python3
"""Everything the fleet can tell you, checked every pass, against LAST pass.

WHY THIS EXISTS, and it is not a nice-to-have. On 2026-09-29 a one-word bug
(a loop variable shadowing a module) made every filled buy raise, get
swallowed by an outer handler, and leave no slice row. It ran for FIVE HOURS.
The evidence was in the data the whole time: `fill_mix.buy.legs` was climbing
while the slice count stood still. Both numbers were pulled every fifteen
minutes and nobody ever subtracted one from the other.

That is the lesson this script encodes. A watcher with a fixed list of
questions is blind to everything not on the list, and the most valuable
signal is almost never a level - it is a DELTA that does not add up. So this
keeps a snapshot between runs and checks CONSERVATION: coin bought must equal
slices written, cash that left must equal coin that arrived, a counter that
moves must move for a reason somebody can name.

Three other things were found by the account owner sending screenshots,
which is the clearest possible proof that this was not being watched:
  - the alert queue had 67 alerts pending and 0 ever sent, because
    ALERT_WEBHOOK_URL was absent from the process. An alarm nobody can
    receive is not an alarm.
  - $6,238.70 of coin had no automatic exit from either layer.
  - the grid reported ZEC "down $357" while the real wallet was UP $519 on
    it, because an ADOPTED basis is a price nobody paid. Reporting that as a
    loss nearly drove a real decision.
Each of those is a check below.

Exit codes:  0 = quiet, 1 = UNREADABLE (a gap, never a zero), 2 = REPORTABLE.
A gap is never silently treated as healthy; if a feed cannot be read, that is
exit 1 and it says which feed.

Usage:  python3 scripts/fleet_watchdog.py [--json] [--state PATH]
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("EMPIRE_BASE", "https://empire-v2-production.up.railway.app")
API = BASE + "/api/trading-dashboard"
DEFAULT_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             ".fleet_watchdog_state.json")

CRITICAL, WARN, INFO = "CRITICAL", "WARN", "INFO"
findings = []
gaps = []


def flag(level, code, msg):
    findings.append({"level": level, "code": code, "message": msg})


def money(x):
    if x is None:
        return "UNKNOWN"
    sign = "+" if x > 0 else ("-" if x < 0 else "")
    return f"{sign}${abs(x):,.2f}"


def get(path, timeout=45, attempts=2, pause=3.0):
    """Fetch one feed. A failure is recorded as a GAP and returns None.

    Never returns {} on failure: an empty dict reads downstream as "nothing
    is wrong", which is the exact confusion this whole script exists to
    prevent.

    RETRIED ONCE, because a single miss is not evidence of anything. These
    endpoints answer in 0.2-5.8s against a 45s timeout, so a failure is not
    slowness - it is the container being restarted, and every push to main
    redeploys. Eight pushes in one night produced NO_EXIT, UNTRACKED and the
    free-cash check each going dark on separate passes, which is a monitor
    reporting blind spots it created itself.

    One retry converts a restart into a successful read. When it still fails
    the gap is stronger evidence than before - it means twice, seconds apart
    - and the message says so, so nobody reads a transient as an outage.
    """
    url = path if path.startswith("http") else API + path
    last = None
    for attempt in range(max(1, attempts)):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            last = e
            if attempt + 1 < attempts:
                time.sleep(pause)
    gaps.append(f"{path}: {type(last).__name__}: {last} "
                f"(failed {attempts}x, {pause:.0f}s apart)")
    return None


PARKED_FLOOR_PCT = 0.010  # GRID_PARKED_MIN_NET_PCT


def _exit_threshold(branch):
    """Can this branch sell RIGHT NOW, and by which rule?

    Returns (can_sell, rule_name, detail) - detail is the gap still to go when
    it cannot, in percent, or None when the rule does not express one.

    THE RULE IS NOT WHAT I ASSUMED, and the wrong version reached the owner as
    advice. The grid's sell trigger is crypto_grid_bot.py:7050:

        _rise_hit = price >= branch.reference_price * (1 + grid_pct)

    It compares the PRICE to the BRANCH REFERENCE. It does not look at the
    slice's own entry at all. I had modelled it as "slice net% >= grid_pct",
    which is a different quantity, and on ALGO-USD the two disagreed
    completely: a slice +7.7% above its own entry of 0.115930, while the
    branch reference was 0.125990 and the trigger 0.129770 against a price of
    0.1256 - still 3.3% away. The watchdog called that slice ready and named a
    resting order as the thing blocking it, and I told the owner to cancel that
    order. It would have freed the coin and produced no sale.

    The parked route is the one that DOES use the slice's own gain, and it
    applies only to a branch full on its rungs. ALGO holds 1 of 3, so it was
    never on that route either.
    """
    sl = branch.get("slices") or []
    lv = branch.get("num_levels") or 0
    if not sl:
        return False, "no slices", None
    # PARKED IS TWO CONDITIONS, NOT ONE. crypto_grid_bot.py:7004:
    #
    #     _parked = bool(slices) and (len(slices) >= num_levels
    #                                 or branch_is_adopted_only(slices))
    #
    # A branch whose slices are ALL ADOPTED is parked however many rungs it
    # has free, because it will never rebuy from that reference. QNT-USD
    # proves it in the live log: "parked (2 slices / 3 levels - cannot buy),
    # and a slice is +77.56% net of fees. Selling on its own merit rather than
    # waiting for a 3.00% rise off a reference it will never rebuy from."
    # Two of three rungs, and still on the parked route.
    adopted_only = bool(sl) and all(x.get("adopted") for x in sl)
    if len(sl) >= lv or adopted_only:
        best = max((x.get("unrealized_net_pct") or 0) for x in sl)
        why = ("parked-sell floor (adopted-only branch)" if adopted_only
               and len(sl) < lv else "parked-sell floor")
        return (best >= PARKED_FLOOR_PCT, why,
                None if best >= PARKED_FLOOR_PCT
                else (PARKED_FLOOR_PCT - best) * 100.0)
    gp = branch.get("grid_pct")
    ref = branch.get("reference_price")
    px = branch.get("current_price")
    if not gp or not ref or not px:
        return None, "rise trigger unreadable", None
    trigger = ref * (1 + gp)
    return (px >= trigger, "grid rise trigger",
            None if px >= trigger else (trigger / px - 1) * 100.0)


def _phantom_short_positions(inv):
    """Branches that hold under 1% of the coin they claim, as
    (product_id, tracked, held, short_usd).

    ONE DEFINITION, TWO READERS. The refusal check in section 2 and the
    invariant readout in section 6 both need this, and a second inline copy
    is a second thing to drift. Returns [] when the invariant is unreadable -
    which means UNKNOWN, not "no branch is phantom", so callers must not read
    an empty list as an all-clear.
    """
    out = []
    for chk in ((inv or {}).get("checks") or []):
        if chk.get("status") != "FAIL" or chk.get("name") != "coin_tracked_is_held":
            continue
        for c in (chk.get("short_positions") or []):
            tr = c.get("tracked") or 0.0
            hd = c.get("held")
            if hd is None or tr <= 0:
                continue
            if hd / tr < 0.01:
                out.append((c.get("product_id"), tr, hd, c.get("short_usd") or 0.0))
    return out


def _age_seconds(stamp, now):
    """Seconds between two "%Y-%m-%dT%H:%M:%SZ" stamps, or None if either is
    unreadable. None is a real answer meaning UNKNOWN - a gap is not a zero,
    and an unreadable stamp read as "0 seconds old" would mark every standing
    refusal brand new for ever."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    try:
        a = datetime.datetime.strptime(stamp, fmt)
        b = datetime.datetime.strptime(now, fmt)
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--state", default=DEFAULT_STATE)
    args = ap.parse_args()

    now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    prev = {}
    if os.path.exists(args.state):
        try:
            prev = json.load(open(args.state))
        except (ValueError, OSError):
            prev = {}  # a corrupt snapshot is a missing snapshot, not a zero

    grid = get("/grid-status")
    ops = get("/live-ops")
    inv = get("/grid-status/invariants")
    alerts = get("/alert-queue")
    stops = get("/resting-stops")
    census = get("/account-census")
    alpaca = get("/alpaca-overview")
    health = get(BASE + "/health")

    if grid is None:
        print("UNREADABLE: /grid-status could not be read - no check is possible.")
        for g in gaps:
            print("  gap:", g)
        return 1

    branches = grid.get("branches") or []
    slices = [(b["product_id"], s) for b in branches for s in (b.get("slices") or [])]
    fill_mix = (grid.get("fill_mix") or {}).get("buy") or {}
    cur = {
        "at": now,
        "buy_legs": fill_mix.get("legs"),
        "slice_count": len(slices),
        "populated": sum(1 for _, s in slices if s.get("slice_state")),
        "skip_buy": (grid.get("maker_only_skipped_cycles") or {}).get("buy"),
        "expiry_buy": (grid.get("maker_expiry_drift") or {}).get("buy"),
        "commit": (health or {}).get("commit"),
    }

    # ---- 1. CONSERVATION: coin bought must equal slices written ------------
    #
    # THE CHECK THAT WOULD HAVE CAUGHT THE FIVE-HOUR BUG. Both writers of the
    # buy fill-mix counter sit inside grid_buy immediately before it returns a
    # fill, so an increment PROVES a fill happened and the caller went on to
    # the insert. If the book did not grow to match, the row was lost between
    # the two - which is a silent, money-shaped defect.
    #
    # Sales shrink the book, so this compares against the NET change and only
    # complains when buys outrun the book's growth. It is deliberately quiet
    # about the other direction: the book growing without a buy leg is what
    # adoption does, and that is check 5.
    if (prev.get("at") and prev.get("buy_legs") is not None
            and cur["buy_legs"] is not None):
        legs_delta = cur["buy_legs"] - prev["buy_legs"]
        if legs_delta > 0:
            # Slices opened since the last snapshot, counted by timestamp so a
            # sale in the same window cannot mask a lost buy.
            since = prev.get("at") or ""
            new_slices = [s for _, s in slices if (s.get("opened_at") or "") > since]
            if len(new_slices) < legs_delta:
                flag(CRITICAL, "LOST_FILL",
                     f"{legs_delta} buy leg(s) filled since {since} but only "
                     f"{len(new_slices)} slice row(s) appeared. Coin was bought "
                     f"that the book did not record.")

    # ---- 1b. THE MIRROR: a sale that fills must retire a slice -------------
    #
    # The buy check above exists because a lost buy ran for five hours. The
    # sell side has the identical shape and nobody has looked at it: if
    # fill_mix.sell.legs rises and the book does not SHRINK to match, coin
    # left the wallet and the slice it belonged to is still on the books -
    # which would overstate holdings and could sell the same coin twice.
    sell_mix = (grid.get("fill_mix") or {}).get("sell") or {}
    cur["sell_legs"] = sell_mix.get("legs")
    if (prev.get("at") and prev.get("sell_legs") is not None
            and cur["sell_legs"] is not None and prev.get("slice_count") is not None):
        sold = cur["sell_legs"] - prev["sell_legs"]
        if sold > 0:
            # Buys grow the book in the same window, so compare the NET move
            # against what both sides did rather than the raw count.
            bought = (cur["buy_legs"] or 0) - (prev.get("buy_legs") or 0)
            expected = prev["slice_count"] + bought - sold
            if cur["slice_count"] > expected:
                flag(CRITICAL, "SELL_NOT_RETIRED",
                     f"{sold} sell leg(s) filled but the book only fell to "
                     f"{cur['slice_count']} where {expected} was due - coin left "
                     f"the wallet and its slice is still open.")

    # ---- 1c. Realized P&L must never go backwards --------------------------
    #
    # A GRID exit only ever sells above its own entry. The STOP route does
    # not, and this comment used to claim realized P&L was "monotonic by
    # design" on the strength of the grid route alone. It is not: on
    # 2026-09-29 two stops closed TON-USD at -$7.29 and ONDO-USD at -$2.89,
    # and the lifetime book holds 21 negative round trips totalling -$17.45.
    #
    # So this check is necessary and NOT sufficient. It only sees a loss big
    # enough to drag the RUNNING TOTAL down, and wins landing in the same
    # interval cover for it - which is exactly what happened: both stops fired
    # and the total never fell, so nothing here said a word. Section 1d below
    # watches individual closes for that reason. Keep both.
    if ops is not None:
        try:
            realized = ops["headline"]["data"].get("realized_usd")
        except (KeyError, TypeError):
            realized = None
            gaps.append("/live-ops: headline.data.realized_usd missing")
        cur["realized"] = realized
        if (prev.get("realized") is not None and realized is not None
                and realized < prev["realized"] - 0.005):
            flag(CRITICAL, "REALIZED_FELL",
                 f"realized P&L fell {money(prev['realized'])} -> {money(realized)}. "
                 f"A normal exit cannot do that - something forced a close and "
                 f"booked a real loss.")

    # ---- 1d. A SINGLE close that booked a loss ------------------------------
    #
    # THE BLIND SPOT SECTION 1c COULD NOT SEE, and the owner's standing rule
    # is "nothing negative realized".
    #
    # 1c watches the running total, and a total only falls when the losses in
    # an interval outweigh the wins. On 2026-09-29 the stop route closed
    # TON-USD at -$7.29 and ONDO-USD at -$2.89 - the grid's wins over the same
    # hours more than covered them, the total went UP, and nothing reported
    # either one. A protection whose firing cannot be observed is
    # indistinguishable from one that never fires, and that cuts both ways: a
    # stop that fires unseen is a loss nobody was told about.
    #
    # Tracked BY TRADE ID, not by a count or a sum. An id is a transition - a
    # close either is new to this pass or it is not - so a duplicate run
    # cannot re-fire it and a rounding drift cannot invent one.
    #
    # The severity threshold comes from the FLEET'S OWN measured win, not a
    # number anybody chose: if one stop gives back more than the mean winning
    # round trip earns, the stop layer is eating the edge faster than the grid
    # makes it, and that is a CRITICAL. Measured on 2026-09-30 the mean win
    # was $0.74, so TON's -$7.29 was ten wins handed back in one close.
    hist = get("/grid-status/trade-history?limit=1000")
    if hist is not None:
        rows = hist.get("recent_trades")
        if rows is None:
            gaps.append("/grid-status/trade-history: recent_trades missing - "
                        "individual losing closes cannot be seen")
        else:
            def _pnl(r):
                try:
                    return float(r.get("pnl"))
                except (TypeError, ValueError):
                    return None
            unreadable = [r for r in rows if _pnl(r) is None]
            if unreadable:
                gaps.append(f"{len(unreadable)} closed trade(s) carry an "
                            f"unreadable pnl - a loss among them would be "
                            f"invisible")
            ids = [r.get("id") for r in rows if isinstance(r.get("id"), int)]
            high = max(ids) if ids else None
            if high is not None:
                cur["last_trade_id"] = high

            losses = [r for r in rows if (_pnl(r) or 0) < 0]
            loss_usd = round(sum(_pnl(r) or 0 for r in losses), 2)
            wins = [_pnl(r) for r in rows if (_pnl(r) or 0) > 0]
            mean_win = round(sum(wins) / len(wins), 4) if wins else None

            # TRUNCATION IS NOT AN EMPTY BOOK. A capped list makes the loss
            # count a floor, and saying so is the difference between "none" and
            # "none in the part I was shown".
            floor = " (at least - the list is capped)" if hist.get(
                "recent_trades_truncated") else ""

            seen = prev.get("last_trade_id")
            fresh = [r for r in losses
                     if isinstance(r.get("id"), int)
                     and seen is not None and r["id"] > seen]
            if fresh:
                worst = min(fresh, key=lambda r: _pnl(r) or 0)
                w = _pnl(worst) or 0.0
                sev = CRITICAL if (mean_win and abs(w) > mean_win) else WARN
                mw = (f" The mean winning round trip earns {money(mean_win)}, so "
                      f"this one close handed back "
                      f"{abs(w) / mean_win:.1f} of them."
                      if mean_win else
                      " The mean winning round trip is unreadable, so how much "
                      "of the edge this gave back is UNKNOWN.")
                flag(sev, "LOSS_CLOSED",
                     f"{len(fresh)} close(s) since the last pass booked a REAL "
                     f"loss totalling "
                     f"{money(round(sum(_pnl(r) or 0 for r in fresh), 2))}. "
                     f"Worst: {worst.get('product_id')} {money(w)} via "
                     f"{worst.get('exit_reason') or 'an unlabelled exit'}."
                     f"{mw} Lifetime: {len(losses)} losing round trip(s), "
                     f"{money(loss_usd)}{floor}. The owner's rule is nothing "
                     f"negative realized - arming or disarming a stop is the "
                     f"owner's call, never this script's.")
            elif seen is None and losses:
                # First run against a book that already holds losses. Not an
                # alarm - there is no transition to report - but reporting
                # nothing would let a reader carry on believing the book is
                # clean, which is the belief this section exists to correct.
                flag(INFO, "LOSS_BOOK",
                     f"no baseline yet, so no new-loss claim. The book already "
                     f"holds {len(losses)} losing round trip(s) totalling "
                     f"{money(loss_usd)}{floor} - realized P&L is a NET figure, "
                     f"not a run of wins.")

    # ---- 1e. A loss that has not happened yet -------------------------------
    #
    # LOSS_CLOSED reports a stop AFTER it fires. Both of yesterday's stops
    # were visible hours ahead in data the fleet already publishes, and
    # nothing read it. The owner's rule is that nothing negative is realized,
    # so the pass that matters is the one BEFORE the close, not after.
    #
    # Every branch publishes stop_pct (its own adaptive stop) and
    # stop_daily_vol_pct (the volatility that stop was sized from). The
    # threshold here is therefore the branch's OWN measured daily move, not a
    # number anybody picked: a slice within one average day of its stop can
    # reach it on an ordinary day, not an unusual one.
    #
    # UNITS DIFFER IN THE PAYLOAD and getting this backwards would silence the
    # check entirely: stop_pct and drawdown_pct are FRACTIONS (0.0823), while
    # stop_daily_vol_pct is a PERCENT (3.292). The /100 below is that, not a
    # fudge.
    #
    # WHAT IS BEING COMPARED, said plainly because it is not exact: the slice
    # figure is unrealized_net_pct, which is AFTER fees, and the stop triggers
    # on the gross adverse move. Yesterday's TON close proves the direction -
    # gross -8.34% against a 8.16% stop, net -9.02%. So net is always the more
    # negative of the two and this warns slightly EARLY. That is the right way
    # for a heads-up to be wrong.
    stop_near = []
    breached = []
    no_vol = []
    for b in branches:
        pid = b.get("product_id")
        dd = b.get("drawdown_pct")
        if b.get("drawdown_breached"):
            breached.append((pid, dd))
        sp = b.get("stop_pct") or 0.0
        vol_pct = b.get("stop_daily_vol_pct")
        sl3 = b.get("slices") or []
        if sp <= 0 or not sl3:
            continue                      # no stop armed, or nothing to stop
        if vol_pct is None:
            no_vol.append(pid)
            continue
        worst = min((x.get("unrealized_net_pct") or 0) for x in sl3)
        margin = sp - (vol_pct / 100.0)
        if worst <= -margin:
            stop_near.append((pid, worst, sp, vol_pct / 100.0))
    if no_vol:
        gaps.append(f"daily volatility unreadable for {', '.join(sorted(set(no_vol)))}"
                    f" - how close their slices are to stopping out is UNKNOWN")

    if stop_near:
        cur["stop_near"] = sorted(x[0] for x in stop_near)
        prev_sn = prev.get("stop_near")
        joined = ([x for x in cur["stop_near"] if x not in prev_sn]
                  if prev_sn is not None else [])
        # A branch that holds none of its coin cannot execute the stop either,
        # so calling it "about to stop out" would be the wrong warning.
        ph_ids2 = {x[0] for x in _phantom_short_positions(inv)}
        flag(CRITICAL if joined else WARN, "STOP_NEAR",
             f"{len(stop_near)} branch(es) hold a slice within ONE average day's "
             f"move of their own stop"
             + (f" - {', '.join(joined)} newly so" if joined else "")
             + ": "
             + ", ".join(
                 f"{p2} worst slice {w*100:+.2f}% vs a {sp2*100:.2f}% stop "
                 f"({v*100:.2f}% daily vol)"
                 + (" [holds none of its coin - the stop cannot execute either]"
                    if p2 in ph_ids2 else "")
                 for p2, w, sp2, v in sorted(stop_near, key=lambda x: x[1]))
             + ". Net is after fees and the stop triggers on the gross move, so "
               "the real distance is a little larger. Arming or disarming a stop "
               "is the owner's call, never this script's.")

    if breached:
        cur["breached"] = sorted(x[0] for x in breached)
        prev_br = prev.get("breached")
        joined_b = ([x for x in cur["breached"] if x not in prev_br]
                    if prev_br is not None else [])
        flag(CRITICAL if joined_b else WARN, "DRAWDOWN_BREACHED",
             f"{len(breached)} branch(es) have tripped their own drawdown "
             f"breaker and STOPPED BUYING"
             + (f" - {', '.join(joined_b)} newly so" if joined_b else "")
             + ": "
             + ", ".join(f"{p2} down {(dd or 0)*100:.1f}% from its own peak"
                         for p2, dd in sorted(breached, key=lambda x: -(x[1] or 0)))
             + ". Existing slices still sell normally; only new buys are paused, "
               "so this capital is out of the dip-buying strategy until the "
               "branch recovers. The breaker level is the owner's and is not "
               "touched here.")

    # ---- 1f. How close is the fleet to its NEXT sell? ----------------------
    #
    # THE QUESTION THAT TOOK HOURS TO ANSWER BY HAND. "Why haven't there been
    # sells today?" was answered tonight by pulling every branch, working out
    # which rule governed it, and computing the distance one at a time - and I
    # got the rule wrong twice on the way. _exit_threshold() now returns that
    # distance directly, so the answer is one line instead of an investigation.
    #
    # DISTANCE, NOT A FORECAST. Each number is "how far this branch's own rule
    # still has to travel", read off published prices and references. There is
    # no probability here and no expected-value estimate: a figure like
    # "expected realization $31.40, confidence 78%" would be a model's opinion
    # wearing a dollar sign, and this codebase has already retracted one set of
    # invented numbers.
    ladder = []
    for b3 in branches:
        can3, why3, gap3 = _exit_threshold(b3)
        if can3 is None or gap3 is None:
            continue
        ladder.append((b3.get("product_id"), gap3, why3))
    if ladder:
        ladder.sort(key=lambda r: r[1])
        w1 = sum(1 for r in ladder if r[1] <= 1.0)
        w2 = sum(1 for r in ladder if r[1] <= 2.0)
        cur["nearest_gap"] = round(ladder[0][1], 4)
        flag(INFO, "READINESS",
             f"nearest sell is {ladder[0][0]} at {ladder[0][1]:.2f}% away "
             f"({ladder[0][2]}); {w1} branch(es) within 1%, {w2} within 2%, of "
             f"{len(ladder)} measurable. Next five: "
             + ", ".join(f"{p4} {g4:.2f}%" for p4, g4, _ in ladder[:5])
             + ". These are distances the price still has to travel, not a "
               "forecast that it will.")

    # ---- 2. Any cycle error at all -----------------------------------------
    if ops is not None:
        try:
            events = ops["gate"]["data"]["events"]
        except (KeyError, TypeError):
            events = []
            gaps.append("/live-ops: gate.data.events missing")
        errs = [e for e in events if e.get("event_type") == "CYCLE_ERROR"]
        seen = set(prev.get("cycle_errors") or [])
        fresh = [e for e in errs if e.get("created_at") not in seen]
        cur["cycle_errors"] = [e.get("created_at") for e in errs][:50]
        if fresh:
            for e in fresh[:5]:
                flag(CRITICAL, "CYCLE_ERROR",
                     f"{e.get('created_at')} {e.get('product_id')}: {e.get('message')}")
        elif errs:
            flag(WARN, "CYCLE_ERROR_STANDING",
                 f"{len(errs)} cycle error(s) still on the feed, none new since "
                 f"last pass.")

        # A parked branch whose escape sell will not fill is in a retry loop:
        # the gate passes every cycle, the order is refused, the cycle returns
        # having done nothing. From outside that looks exactly like a healthy
        # quiet branch, which is why it is read off the feed rather than
        # inferred from the branch looking unchanged.
        # CANNOT and HAS NOT YET are different failures, and calling both
        # critical is how an alarm stops being read. Under maker-only there is
        # no market fallback, so a sell that nobody crossed within the wait
        # window simply does not fill - that is the mode working as configured,
        # not a fault. SHIB-USD proved it on this very pass: three no-fills,
        # then it filled twice and left the full set with +$1.65 realized.
        # A product the venue REFUSES to accept an order from is the real
        # trap - it can never fill, at any price, however long it waits.
        _refused_now = set((( grid.get("order_refusals") or {}
                             ).get("by_product") or {}))
        nofill = [e for e in events if e.get("event_type") == "PARKED_SELL_NOFILL"]
        if nofill:
            coins = sorted({e.get("product_id") for e in nofill if e.get("product_id")})
            blocked = [c for c in coins if c in _refused_now]
            waiting = [c for c in coins if c not in _refused_now]
            if blocked:
                # WHY it cannot resolve decides what to do about it, and the
                # two reasons want opposite actions.
                #
                # QNT-USD looked like a rounding problem: it holds 0.00097323
                # against a 0.001 venue increment, a shortfall of 0.00002677 -
                # three-quarters of one cent at $281.99 - and its branch still
                # had a free rung, so the obvious reading was "its next buy
                # lifts it over the minimum and this clears itself".
                #
                # That reading is wrong, and shipping it would have been a
                # reassurance. The branch claims 0.676 QNT and holds 0.00097 -
                # 0.14% of it. Clearing the increment only makes the order
                # PLACEABLE; it would then ask to sell units that still do not
                # exist. The increment is a symptom, the shortfall is the
                # trap, and only reconciliation - the owner's call, never this
                # script's - closes it.
                ph_ids = {x[0] for x in _phantom_short_positions(inv)}
                trapped = [c for c in blocked if c in ph_ids]
                short_only = [c for c in blocked if c not in ph_ids]
                if trapped:
                    flag(CRITICAL, "PARKED_RETRY_LOOP",
                         f"{', '.join(trapped)} cannot place the escape sell at "
                         f"all (see ORDER_REFUSED) - and raising inventory past "
                         f"the venue's minimum would NOT fix it: these branches "
                         f"hold under 1% of the coin they claim (see "
                         f"PHANTOM_BRANCH), so the order would then be for units "
                         f"that still do not exist. Reconciliation is the only "
                         f"way out and it is the owner's call.")
                if short_only:
                    flag(CRITICAL, "PARKED_RETRY_LOOP",
                         f"{', '.join(short_only)} cannot place the escape sell "
                         f"at all (see ORDER_REFUSED) - the gate passes every "
                         f"cycle and the order can never fill. The branch does "
                         f"hold roughly what it claims, so this is the venue's "
                         f"own rule and not a shortfall. It does not resolve by "
                         f"waiting.")
            if waiting:
                flag(INFO, "PARKED_SELL_WAITING",
                     f"{', '.join(waiting)}: escape sell posted and not taken yet. "
                     f"Maker-only has no market fallback, so this is the mode "
                     f"working - it clears when a taker crosses it.")

    # ---- 3. The alarm can actually reach a human ---------------------------
    #
    # A queue that holds alerts and reports success is worse than no queue.
    if alerts is not None:
        counts = alerts.get("counts") or {}
        pending, sent = counts.get("pending"), counts.get("sent")
        if not alerts.get("channel_configured"):
            flag(CRITICAL, "ALARM_DEAD",
                 f"No alert channel configured ({(alerts.get('channel_diagnosis') or {}).get('expected_variable')} "
                 f"absent) - {pending} alert(s) held, {sent} ever delivered. "
                 f"Nothing reaches anyone.")
            # A COUNT IS NOT THE CONTENTS. "74 held" reads like a backlog to
            # clear at leisure; among those 74 sat "XRP broke its level -
            # $2,098.72 exposed", written 2026-09-28 and seen by nobody. The
            # number is the one thing about a dead queue that does NOT matter.
            #
            # Ranked by the queue's OWN severity field and recency. The dollar
            # figures live only inside the English message, so they are quoted
            # whole and never parsed - a reworded sentence must not become a
            # wrong number here.
            rows = alerts.get("alerts")
            if rows is None:
                gaps.append("alert-queue returned no alert bodies - what is "
                            "being held is UNKNOWN, not nothing")
            else:
                # The feed caps what it returns, so this is the worst of what
                # was SHOWN. Saying "the worst held" of a truncated list would
                # be a claim the data cannot support.
                short = len(rows) < (pending or 0)
                by_sev = {}
                for r in rows:
                    by_sev[r.get("severity") or "?"] = by_sev.get(
                        r.get("severity") or "?", 0) + 1
                crit = sorted((r for r in rows if r.get("severity") == "CRITICAL"),
                              key=lambda r: r.get("created_at") or "", reverse=True)
                mix = ", ".join(f"{v} {k}" for k, v in
                                sorted(by_sev.items(), key=lambda kv: -kv[1]))
                oldest = min((r.get("created_at") or "" for r in rows), default="")
                flag(CRITICAL, "ALARM_DEAD",
                     f"  of the {len(rows)} held alert(s) shown"
                     + (f" (of {pending} - the feed caps the list, so this is a "
                        f"floor)" if short else "")
                     + f": {mix}. Oldest {oldest[:19] or 'UNKNOWN'}.")
                for r in crit[:5]:
                    flag(CRITICAL, "ALARM_DEAD",
                         f"    [{(r.get('created_at') or '')[:19]}] "
                         f"{r.get('message')} - {r.get('detail') or ''}")
        elif pending and not sent:
            flag(CRITICAL, "ALARM_STUCK",
                 f"{pending} alert(s) pending and {sent} sent - the channel is "
                 f"configured but nothing is going out.")

    # ---- 4. Coin with no automatic exit ------------------------------------
    if stops is not None:
        unc = stops.get("uncovered_usd")
        names = sorted(stops.get("uncovered") or [])
        cur["uncovered_usd"] = unc
        cur["uncovered_names"] = names
        if unc:
            prev_unc = prev.get("uncovered_usd")
            prev_names = prev.get("uncovered_names")
            moved = "" if prev_unc is None else f" (was {money(prev_unc)})"
            # Escalate on a NEW ASSET, never on the dollar figure alone. This
            # exposure is unsold inventory marked to market, so it moves every
            # single pass on price: a $6.54 drift across a $6,187 book was
            # enough to raise a CRITICAL under the old rule, which is the
            # "a figure that swings on price alone is not a trend" mistake
            # built into the alarm meant to catch it. An alarm that fires on
            # noise teaches its reader to stop reading it.
            joined = ([n for n in names if n not in prev_names]
                      if prev_names is not None else [])
            if joined:
                flag(CRITICAL, "NO_EXIT",
                     f"{money(unc)} across {stops.get('uncovered_count')} asset(s) "
                     f"has no automatic exit from either layer{moved} - and "
                     f"{', '.join(joined)} JOINED that set since the last pass: "
                     f"{', '.join(names[:14])}")
            else:
                same = (" - same assets as last pass, the change is price only"
                        if prev_names is not None else "")
                flag(WARN, "NO_EXIT",
                     f"{money(unc)} across {stops.get('uncovered_count')} asset(s) "
                     f"has no automatic exit from either layer{moved}{same}: "
                     f"{', '.join(names[:14])}")

    # ---- 4b. A parked branch whose only way out is dust --------------------
    #
    # The escape hatch asks whether ANY slice clears the floor. It does not
    # ask whether selling that slice is worth doing. Measured live: LINK-USD
    # sat full on 3 rungs with its only qualifying slice holding 0.01 LINK -
    # fourteen cents of basis against ~$90 locked in the two real slices.
    # Selling it releases nothing and may not even clear the exchange's
    # minimum, so the branch reports itself escapable while staying locked.
    # Reported, not acted on: the floor is the owner's and is not touched.
    for b in branches:
        sl = b.get("slices") or []
        lv = b.get("num_levels") or 0
        if not sl or len(sl) < lv:
            continue
        qual = [s2 for s2 in sl if (s2.get("unrealized_net_pct") or 0) >= 0.010]
        if not qual:
            continue
        book = sum((s2.get("qty") or 0) * (s2.get("entry_price") or 0) for s2 in sl)
        best = max(qual, key=lambda s2: (s2.get("qty") or 0) * (s2.get("entry_price") or 0))
        notional = (best.get("qty") or 0) * (best.get("entry_price") or 0)
        # A dollar is below any plausible exchange minimum, so this is a
        # reporting threshold for a monitor - not a trading threshold, and
        # nothing in the fleet's own gates is changed by it.
        if notional < 1.00:
            flag(WARN, "PARKED_DUST",
                 f"{b['product_id']} is full on its rungs and its ONLY slice "
                 f"clearing the exit floor holds {money(notional)} of basis "
                 f"against {money(book)} locked in the branch. Selling it "
                 f"releases nothing - this branch has no real way out.")

    # ---- 4c. The venue's own refusal reasons -------------------------------
    #
    # /grid-status has published `order_refusals` all along, naming each
    # blocked product and WHY, and nothing was reading it. Two passes were
    # spent inferring from slice shapes what this field states outright -
    # that is the watchdog's failure, not the fleet's, and it is exactly the
    # kind of gap this file exists to close. A first-class diagnostic the
    # system already emits is the first thing an overseer should read.
    #
    # Reported verbatim. These reasons come from the exchange's own rules and
    # the fleet's reading of them; paraphrasing them into a category would
    # throw away the numbers that say what to do about each one.
    refus = grid.get("order_refusals") or {}
    if not refus.get("available"):
        gaps.append("order_refusals unavailable - cannot see venue refusals")
    else:
        by_product = refus.get("by_product") or {}
        cur["refused_products"] = sorted(by_product)

        # A TRANSITION MUST NOT BE CONSUMABLE BY A DUPLICATE RUN.
        #
        # The old rule escalated only when a product was absent from the
        # PREVIOUS snapshot. Run the watchdog twice in a row and the second
        # run overwrites that snapshot, so the first run owns the only
        # announcement a new refusal will ever get - and if nobody read that
        # one line, the finding is gone for good. TIA-USD went refused this
        # afternoon and its escalation cannot now be proven to have fired,
        # which is exactly the failure mode. With ALARM_DEAD standing, no
        # alert is delivered anywhere either, so "reported once" really does
        # mean "reported to nobody".
        #
        # Fixed by remembering WHEN each product was first seen refused
        # rather than only whether it was in the last snapshot. A refusal
        # keeps announcing itself as new until it has been standing an hour,
        # however many times the watchdog runs in between.
        refused_since = dict(prev.get("refused_since") or {})
        for p2 in list(refused_since):
            if p2 not in by_product:
                del refused_since[p2]               # it can trade again
        for p2 in by_product:
            refused_since.setdefault(p2, now)
        cur["refused_since"] = refused_since

        if by_product:
            fresh = []
            for p2 in sorted(by_product):
                age = _age_seconds(refused_since.get(p2), now)
                if age is None or age < 3600:
                    fresh.append(p2)
            level = CRITICAL if fresh else WARN
            head = (f"{len(by_product)} product(s) cannot place an order"
                    + (f" - {', '.join(fresh)} refused within the last hour"
                       if fresh else " - all standing over an hour"))
            flag(level, "ORDER_REFUSED", head)
            for p2 in sorted(by_product):
                age = _age_seconds(refused_since.get(p2), now)
                stamp = ("" if age is None
                         else f" [refused {age / 3600:.1f}h]" if age >= 3600
                         else f" [refused {age / 60:.0f}m ago]")
                flag(level, "ORDER_REFUSED", f"  {p2}{stamp}: {by_product[p2]}")
        unreadable = refus.get("product_rules_unreadable") or []
        if unreadable:
            gaps.append(f"product rules unreadable for {', '.join(unreadable)} - "
                        f"a refusal there would be invisible")

    # ---- 4d. Coin the fleet does not manage at all -------------------------
    #
    # Every other check here asks whether a branch is working. None of them
    # asked how much of the wallet has no branch at all - so a quarter of the
    # account sat outside the fleet, earning nothing and reported by nothing,
    # for as long as this watchdog has been running.
    #
    # Measured when this was written: $2,417.62 of $10,094.55 untracked, and
    # $1,497.18 of that was BITCOIN - 97% of the BTC holding, sitting beside
    # a BTC branch that is "full" on its rungs with $35.49 of position. The
    # branch cannot buy because it is full; full means full of thirty-five
    # dollars.
    if census is None or not census.get("available"):
        gaps.append("account-census unavailable - cannot see coin outside the fleet")
    else:
        untr = census.get("untracked_usd")
        tot = census.get("total_usd")
        share = census.get("tracked_share_pct")
        cur["untracked_usd"] = untr
        prev_untr = prev.get("untracked_usd")
        if untr is None or not tot:
            gaps.append("census untracked figure unreadable")
        elif untr > 0:
            grew = ("" if prev_untr is None
                    else f" (was {money(prev_untr)})")
            # Reported by SHARE, not by dollars: the figure moves on price
            # every pass, and escalating on that is the mistake NO_EXIT made.
            level = WARN if (share is not None and share < 90) else INFO
            flag(level, "UNTRACKED",
                 f"{money(untr)} of {money(tot)}{grew} is held in the wallet but "
                 f"managed by no grid branch - {100 - (share or 0):.1f}% of the "
                 f"account. Nothing buys or sells it; it only moves on price.")

    # ---- 4d2. The total moved because the PRICING changed, not the price ---
    #
    # The owner, watching his own dashboard: "I'm not seeing no updates on
    # these numbers and accounts."
    #
    # The crypto total went $9,086.67 -> $9,993.56 in about an hour while the
    # fleet's unrealized P&L got WORSE, -$444.70 -> -$465.00. Those two should
    # move together. They did not, because coin_usd is a sum over the assets
    # the census could PRICE, and that set changes: the payload's own warning
    # says "4 of 60 assets could not be priced and are NOT in total_usd... The
    # real total is higher than the figure shown by whatever they are worth."
    #
    # So the headline total is not comparable across passes unless the same
    # assets were priced in both. A jump with no trade behind it is alarming
    # to read and impossible to explain from the number alone. This records
    # the priced/unpriced counts so the next move is attributable to one cause
    # or the other instead of guessed at afterwards - which is all that could
    # be done this time, and was not enough.
    if census is not None:
        unp = census.get("assets_unpriced")
        held = census.get("assets_held")
        cur["assets_unpriced"], cur["assets_held"] = unp, held
        cur["coin_usd"] = census.get("coin_usd")
        prev_unp = prev.get("assets_unpriced")
        # A census that publishes NEITHER count simply does not report
        # coverage - there is nothing to say and nothing missing. Gapping on
        # that fired on every healthy fleet, which is the cry-wolf failure this
        # file keeps having to unlearn. Only a payload that gives one count
        # without the other is genuinely incomplete.
        if (unp is None) != (held is None):
            gaps.append("census reports one asset-pricing count without the "
                        "other - a move in the coin total cannot be attributed")
        elif unp is not None and prev_unp is not None and unp != prev_unp:
            names = ", ".join(str((x or {}).get("asset")) for x in
                              (census.get("unpriced") or [])) or "unnamed"
            flag(WARN, "PRICING_COVERAGE",
                 f"the census priced a DIFFERENT set of assets this pass - "
                 f"{prev_unp} unpriced before, {unp} now ({names}). The coin "
                 f"total moves when coverage changes, with no trade behind it, "
                 f"so this pass's total is not comparable with the last one.")
        elif unp:
            flag(INFO, "PRICING_COVERAGE",
                 f"{unp} of {held} assets cannot be priced and are NOT in the "
                 f"total ({', '.join(str((x or {}).get('asset')) for x in (census.get('unpriced') or []))}). "
                 f"The real total is higher by whatever they are worth.")

    # ---- 4e. A step that is safe only while maker-only holds ---------------
    #
    # fee_floor.py exists because the same defect appeared in four places in
    # one evening: a target below the round-trip fee, where a WINNING trade
    # still loses. The fleet's live floor is computed off the MAKER round
    # trip, which is correct while maker-only is armed and wrong the instant
    # it is not - the market fallback returns, the round trip roughly doubles,
    # and a step that cleared 0.90% no longer clears 1.70%.
    #
    # So a branch can sit above the active floor and below the one that would
    # apply if an environment variable changed. That is not a bug today; it is
    # a dependency worth naming, because nothing else states it. Measured when
    # written: BTC-USD at 1.38%, and maker_only_holds already BROKEN with
    # taker legs observed after maker-only was armed.
    _rt_taker = grid.get("real_round_trip_fee_rate")
    _maker_on = grid.get("maker_only_active")
    if _rt_taker is None:
        gaps.append("taker round trip unreadable - cannot judge step safety")
    elif _maker_on:
        _taker_floor = (_rt_taker + 0.002) * 100
        _exposed = []
        for b in branches:
            gp = (b.get("grid_pct") or 0) * 100
            if 0 < gp < _taker_floor:
                _exposed.append((b["product_id"], gp))
        if _exposed:
            cur["maker_dependent"] = sorted(p2 for p2, _ in _exposed)
            flag(WARN, "MAKER_DEPENDENT_STEP",
                 f"{len(_exposed)} branch(es) clear the fee floor ONLY because "
                 f"maker-only is on: "
                 + ", ".join(f"{p2} {gp:.2f}%" for p2, gp in sorted(_exposed, key=lambda x: x[1]))
                 + f" — all below the {_taker_floor:.2f}% taker floor. If "
                 f"maker-only comes off, a winning trade there nets a loss.")

    # ---- 4f. Running out of money to buy dips with --------------------------
    #
    # Nothing watched this. A grid earns by buying a rung and selling it
    # higher; when free cash runs out it simply stops buying. Closes keep
    # happening until there is nothing left to close, and then the fleet goes
    # quiet - a healthy heartbeat, no errors, no trades. The same silent
    # shape as the sixteen-day deadlock, arriving by a different road.
    #
    # Measured against the fleet's OWN mean slice rather than a hardcoded
    # dollar figure, so the alarm cannot drift out of date the way a copied
    # constant does. Deliberately NOT hardcoding GRID_CASH_RESERVE_USD: the
    # endpoint does not publish it, and inventing a number the system never
    # told me is how the parked figure ended up $155 wrong for hours. The
    # true usable amount is LOWER than what is reported here by that unread
    # reserve, and the message says so rather than pretending otherwise.
    _free = grid.get("real_free_cash_usd")
    _slice = ((grid.get("realized_edge") or {}).get("current") or {}).get("mean_slice_usd")
    if _free is None:
        gaps.append("real_free_cash_usd unreadable - cannot tell if the fleet "
                    "can still buy")
    elif not _slice or _slice <= 0:
        gaps.append("mean slice size unreadable - no baseline for buying room")
    else:
        _buys = _free / _slice
        cur["free_cash"] = round(_free, 2)
        if _buys < 1:
            flag(CRITICAL, "CASH_STARVED",
                 f"{money(_free)} of free cash against a {money(_slice)} mean "
                 f"slice - under one more buy. The fleet stops deploying and "
                 f"goes quiet with a healthy heartbeat. Less than this is "
                 f"usable: the cash reserve is not published, so the real "
                 f"room is smaller.")
        elif _buys < 3:
            flag(WARN, "CASH_LOW",
                 f"{money(_free)} of free cash is about {_buys:.1f} more buys "
                 f"at the {money(_slice)} mean slice. Below one, the fleet "
                 f"stops buying dips entirely. The unpublished cash reserve "
                 f"makes the real room smaller than this.")

    # ---- 5. An ADOPTED basis is not a cost basis ---------------------------
    #
    # The grid marks an adopted slice against a reference price nobody paid,
    # so a branch made entirely of adopted slices can show a large "loss"
    # while the real wallet is up on that coin. Reported as a caveat, never
    # as a loss, because a real decision was nearly made on one of these.
    for b in branches:
        sl = b.get("slices") or []
        if not sl:
            continue
        u = b.get("total_unrealized_net_usd") or 0.0
        if u < -25 and all(s.get("adopted") for s in sl):
            flag(INFO, "ADOPTED_BASIS",
                 f"{b['product_id']} shows {money(u)} unrealized, but every slice "
                 f"is ADOPTED - that is measured against a price nobody paid. "
                 f"Check the real cost basis before calling it a loss.")

    # ---- 6. Invariants ------------------------------------------------------
    if inv is not None:
        failed = inv.get("failed")
        prev_failed = prev.get("inv_failed")
        cur["inv_failed"] = failed
        if failed:
            level = CRITICAL if (prev_failed is not None and failed > prev_failed) else WARN
            flag(level, "INVARIANT",
                 f"{failed} invariant(s) BROKEN"
                 + ("" if prev_failed is None else f" (was {prev_failed})")
                 + f": {inv.get('headline')}")

            # The headline is a list of NAMES. The money is in the fields
            # underneath it, and printing only the names summarised a
            # first-class diagnostic into uselessness - the same mistake as
            # not reading order_refusals at all. These two carry dollar
            # figures that say why sells fail, so they are surfaced with
            # their numbers rather than left behind a name.
            for chk in (inv.get("checks") or []):
                if chk.get("status") != "FAIL":
                    continue
                nm = chk.get("name")
                if nm == "coin_tracked_is_held":
                    pos = chk.get("short_positions") or []

                    # HOLDS NONE is a different thing from HOLDS LESS, and
                    # sorting by dollars hides it. PRIME is $8.95 short and
                    # holds literally zero; XRP is $104 short and holds 95%.
                    # The first can never trade again at any price - every
                    # order it places is for units that do not exist, which is
                    # what ORDER_REFUSED already shows for TIA and QNT. The
                    # second is a rounding drift that still trades fine.
                    #
                    # This is also what "lock state unreadable" actually
                    # meant: a locked PERCENTAGE cannot be computed against a
                    # zero balance, so that gap was never an observability
                    # quirk - it was this, arriving one branch at a time.
                    phantom = _phantom_short_positions(inv)  # holds <1% of claim
                    if phantom:
                        cur["phantom"] = sorted(x[0] for x in phantom)
                        prev_ph = prev.get("phantom")
                        joined = ([x for x in sorted(x[0] for x in phantom)
                                   if x not in prev_ph] if prev_ph is not None else [])
                        flag(CRITICAL if joined else WARN, "PHANTOM_BRANCH",
                             f"{len(phantom)} branch(es) track coin the wallet has "
                             f"essentially NONE of"
                             + (f" - {', '.join(joined)} newly so" if joined else "")
                             + ": "
                             + ", ".join(f"{p2} claims {tr:,.4f} holds {hd:,.6f}"
                                         for p2, tr, hd, _ in
                                         sorted(phantom, key=lambda x: -x[3]))
                             + ". These cannot trade at any price.")

                    worst = sorted(pos, key=lambda c: -(c.get("short_usd") or 0))[:4]
                    flag(level, "INVENTORY_SHORT",
                         f"{money(chk.get('short_usd'))} of coin is claimed by "
                         f"{len(pos)} position(s) but not held. A sale of those "
                         f"slices orders units that do not exist. Worst: "
                         + ", ".join(f"{c['product_id']} {money(c.get('short_usd'))}"
                                     for c in worst))
                elif nm == "grid_inventory_is_free":
                    pos = chk.get("locked_positions") or []
                    worst = sorted(pos, key=lambda c: -(c.get("locked_usd") or 0))[:4]
                    flag(level, "INVENTORY_LOCKED",
                         f"{money(chk.get('locked_usd'))} of coin is reserved by "
                         f"resting orders and cannot be sold by the grid. Worst: "
                         + ", ".join(f"{c['product_id']} {c.get('locked_pct')}% "
                                     f"({money(c.get('locked_usd'))})" for c in worst))
                    unread = chk.get("unreadable") or []
                    if unread:
                        gaps.append(f"lock state unreadable for {', '.join(unread)} "
                                    f"- reserved coin there would be invisible")

                    # Money that is BOTH in profit and locked is the one worth
                    # naming on its own: it is reachable, unlike an underwater
                    # branch, and the only thing between it and a sale is a
                    # resting order the owner armed on purpose.
                    lk = {c.get("product_id"): c for c in pos}
                    for b2 in branches:
                        c = lk.get(b2.get("product_id"))
                        if not c or (c.get("locked_pct") or 0) < 50:
                            continue
                        sl2 = b2.get("slices") or []
                        good = [x for x in sl2
                                if (x.get("unrealized_net_pct") or 0) >= 0.010]
                        if not good:
                            continue
                        best = max((x.get("unrealized_net_pct") or 0) for x in good)
                        flag(WARN, "LOCKED_PROFIT",
                             f"{b2['product_id']} has a slice at +{best * 100:.2f}% "
                             f"- past the exit floor - but {c.get('locked_pct')}% of "
                             f"its coin ({money(c.get('locked_usd'))}) is reserved by "
                             f"a resting order. The profit is reachable; the coin is "
                             f"not. Cancelling that order frees it and gives up the "
                             f"protection it was armed for - the owner's call.")

    # ---- 7. Is the loop actually running -----------------------------------
    hb = grid.get("heartbeat") or {}
    age = hb.get("age_seconds")
    if age is None:
        gaps.append("heartbeat age unreadable")
    elif age > 300:
        flag(CRITICAL, "STALLED",
             f"last cycle was {age:.0f}s ago - the fleet loop is not running.")

    # ---- 7b. A live loop that has stopped closing anything -----------------
    #
    # The sixteen-day dead period (Sep 10-25) was NOT a crash. The loop ran,
    # the heartbeat was fresh, and check 7 above would have reported the fleet
    # healthy the entire time - because a branch full on every rung cannot buy
    # and will not sell below entry. A deadlock looks exactly like a working
    # system that happens to have nothing to do.
    #
    # So measure the thing the heartbeat cannot see: time since the last
    # close, against the fleet's OWN measured close rate rather than a magic
    # number, because a slow fleet and a stuck fleet are different failures.
    # The three ways a ready slice can still be unable to sell, read from
    # STRUCTURED fields rather than out of any English sentence: a wording
    # change must never silence this quietly.
    # AN ABSENT INVARIANT IS NOT AN UNREADABLE ONE. grid_inventory_is_free
    # appears in the checks list when it FAILS; a fleet with nothing reserved
    # has no row at all, and that is a real answer meaning nothing is locked.
    # The first version of this gapped on the absence and fired on every
    # healthy fleet - a gap that cries wolf is worse than the one it replaced.
    # The only genuine unknown is the check failing while withholding its
    # structured positions.
    _locked_ids = set()
    _lock_unreadable = False
    for chk2 in ((inv or {}).get("checks") or []):
        if chk2.get("name") != "grid_inventory_is_free":
            continue
        lp2 = chk2.get("locked_positions")
        if lp2 is None:
            _lock_unreadable = chk2.get("status") == "FAIL"
        else:
            _locked_ids = {x.get("product_id") for x in lp2}
    _phantom_ids = {x[0] for x in _phantom_short_positions(inv)}
    _refused_ids = set(((grid.get("order_refusals") or {}).get("by_product") or {}))
    if _lock_unreadable:
        gaps.append("locked_positions unreadable - a ready slice held behind a "
                    "resting order would look like a working sell path")

    edge = ((grid.get("realized_edge") or {}).get("current") or {})
    if not (grid.get("realized_edge") or {}).get("available"):
        gaps.append("realized_edge unavailable - cannot tell a dry spell from a quiet one")
    else:
        dslc = edge.get("days_since_last_close")
        cpd = edge.get("closes_per_day")
        if dslc is None:
            gaps.append("days_since_last_close unreadable")
        elif not cpd or cpd <= 0:
            # A gap is not a zero. Without a rate there is no baseline to
            # judge the silence against, so say so instead of guessing one.
            gaps.append("closes_per_day unreadable - no baseline for a dry spell")
        else:
            dry_h = dslc * 24.0
            mean_h = 24.0 / cpd
            cur["dry_hours"] = round(dry_h, 2)
            prev_dry = prev.get("dry_hours")
            # Two conditions, both required: well past this fleet's own typical
            # gap, AND long enough in absolute terms that a run of quiet luck
            # does not trip it. A threshold that cries wolf trains its reader
            # to ignore the alarm - the same reason DEPLOY_LAG is not 15 min.
            if dry_h > max(3.0, 6.0 * mean_h):
                level = CRITICAL if dry_h > 12.0 else WARN
                grew = ("" if prev_dry is None
                        else f" (was {prev_dry:.1f}h last pass)")
                # WHY, not just HOW LONG. The hours alone send a reader
                # looking for a broken loop, and on 2026-09-30 the loop was
                # fine: of 79 slices exactly 3 stood at or past the threshold
                # that governs them, and every one of the 3 was unsellable -
                # two on a phantom branch marked against an adopted price, one
                # behind a resting order. A symptom with no diagnosis is a
                # number the reader cannot act on, and this is the alarm most
                # worth acting on.
                ready = []
                for b2 in branches:
                    pid2 = b2.get("product_id")
                    can2, why2, gap2 = _exit_threshold(b2)
                    if not can2:
                        continue
                    best2 = max(((x.get("unrealized_net_pct") or 0)
                                 for x in (b2.get("slices") or [])), default=0)
                    thr2 = 0.0
                    blockers = []
                    if pid2 in _locked_ids:
                        blockers.append("coin locked by a resting order")
                    if pid2 in _phantom_ids:
                        blockers.append("branch holds almost none of its claim")
                    if pid2 in _refused_ids:
                        blockers.append("venue refuses the order")
                    if all((x.get("adopted") for x in (b2.get("slices") or []))):
                        blockers.append("ADOPTED basis - the gain is measured "
                                        "against a price nobody paid")
                    ready.append((pid2, best2, thr2, why2, blockers))
                # AN ACCUSATION NEEDS TWO READINGS.
                #
                # "nothing is blocking it, so the sell path itself is the
                # suspect" points the owner at a code bug. On 2026-09-30 it
                # said exactly that about ALGO-USD while ALGO was locked -
                # 1134.300 of 1134.346 units reserved, confirmed by a direct
                # read seconds later. The tell was in the same output:
                # "4 invariant(s) BROKEN (was 2)". The invariants feed briefly
                # reported two failures instead of four, grid_inventory_is_free
                # among the missing, so the blocker lookup came back empty and
                # absence read as "nothing locked".
                #
                # Absence really does mean "nothing locked" when the invariant
                # ran and passed - that is why the gap for a missing check was
                # removed earlier. What it cannot do is carry an accusation on
                # its own. So the sell-path verdict requires the branch to have
                # looked unblocked on the PREVIOUS pass too.
                #
                # THREE STATES, NOT TWO, and the first draft of this fix
                # collapsed them into two: it filtered the unblocked list down
                # to the confirmed ones, which sent an unconfirmed branch into
                # the "every one is blocked" arm and printed it with an empty
                # blocker list - asserting a blocker it could not name. Worse
                # than the bug being fixed. The unfiltered set decides whether
                # anything is unblocked; the filtered one decides whether that
                # is sayable yet.
                free_now = [r for r in ready if not r[4]]
                prev_free = prev.get("free_ready")
                cur["free_ready"] = sorted(r[0] for r in free_now)
                free_confirmed = ([r for r in free_now
                                   if r[0] in set(prev_free)]
                                  if prev_free is not None else [])
                if not ready:
                    diag = (f" NOTHING to sell: no slice of {len(slices)} stands at "
                            f"the threshold that governs it, so the fleet is "
                            f"waiting on price, not stuck.")
                elif not free_now:
                    diag = (f" {len(ready)} slice-holding branch(es) of "
                            f"{len(branches)} ARE past their own threshold and "
                            f"every one is blocked: "
                            + "; ".join(f"{p3} best slice {n3*100:+.2f}%, past its "
                                        f"{w3} - {', '.join(bl)}"
                                        for p3, n3, t3, w3, bl in
                                        sorted(ready, key=lambda r: -r[1]))
                            + f". The other {len(branches) - len(ready)} branch(es) "
                              f"are not past their own sell rule yet, so THEY are "
                              f"waiting on price - only these ones are blocked.")
                elif free_confirmed:
                    diag = (f" {len(free_confirmed)} branch(es) are past their "
                            f"own threshold with NOTHING blocking them on this "
                            f"pass AND the last - "
                            + ", ".join(f"{p3} at {n3*100:+.2f}%"
                                        for p3, n3, _, _, _ in free_confirmed)
                            + " - so the sell path itself is the suspect.")
                else:
                    diag = (f" {len(free_now)} branch(es) are past their own "
                            f"threshold and look unblocked on THIS reading only"
                            + (" (no baseline to compare against)"
                               if prev_free is None else "")
                            + " - "
                            + ", ".join(f"{p3} at {n3*100:+.2f}%"
                                        for p3, n3, _, _, _ in free_now)
                            + ". That is not enough to accuse the sell path: "
                              "the blocker feed can drop a check for one poll. "
                              "Confirm on the next pass.")
                flag(level, "DRY_SPELL",
                     f"{dry_h:.1f}h since the last close{grew}, on a fleet that "
                     f"averages one every {mean_h:.1f}h ({cpd:.2f}/day). The loop "
                     f"is alive - this is the shape the 16-day dead period had."
                     + diag)

    # ---- 8. A pushed fix that never went live ------------------------------
    #
    # A deploy can fail and leave the previous commit serving, which looks
    # exactly like a healthy system running the wrong code.
    head = os.popen("git -C %s rev-parse --short HEAD 2>/dev/null"
                    % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).read().strip()
    served = (health or {}).get("commit")
    if head and served and not served.startswith(head) and not head.startswith(served):
        # NOT "~15 minutes". Measured build times on this project ranged from
        # ~1.5 to ~26 minutes in a single afternoon, and calling a deploy
        # failed at 15 was wrong twice in a row - the second time after the
        # lesson had already been written down. A threshold that produces
        # false alarms trains its reader to ignore the alarm.
        flag(WARN, "DEPLOY_LAG",
             f"serving {served} but local HEAD is {head} - a push has not gone "
             f"live yet. Builds here have taken up to ~26 minutes; do not call "
             f"it failed before 30.")

    # ---- 9. Stuck slices ----------------------------------------------------
    # A slice sitting ONE ULP below a single tradeable unit can never be
    # sold, at any price, ever. grid_sell_residual's float subtraction made
    # two of them - LINK at 0.009999999999998899 and PRIME at
    # 0.00999999999999801, both against a 0.01 increment - and that cause is
    # fixed now (exact Decimal arithmetic), so these two are legacy.
    #
    # The DETECTOR was not fixed. It hardcoded 0.01, which is LINK's and
    # PRIME's increment and nobody else's: the same defect at QNT's 0.001, or
    # at 1.0, would have been invisible. A check that only finds the instance
    # that prompted it is not a check, it is a memory of one bug.
    #
    # Generalised: any quantity that is a hair BELOW a round multiple of a
    # plausible increment, where that multiple is exactly one - so rounding
    # it to what the venue accepts gives zero units, and the slice is dead.
    # Larger residue is harmless (ALGO at 279.4 is 27,940 units and misses by
    # 3.6e-14); it is only fatal when the whole quantity is one unit.
    def _is_stuck(q):
        try:
            q = float(q)
        except (TypeError, ValueError):
            return False
        if not (q > 0):
            return False
        for unit in (1.0, 0.1, 0.01, 0.001, 0.0001, 0.00001, 0.000001):
            units = q / unit
            nearest = round(units)
            if nearest != 1:
                continue
            # below the unit, but only by floating-point error
            if 0 < (nearest - units) < 1e-6:
                return True
        return False

    stuck = [(p, s["qty"]) for p, s in slices if _is_stuck(s.get("qty"))]
    cur["stuck"] = len(stuck)
    if prev.get("stuck") is not None and len(stuck) > prev["stuck"]:
        flag(CRITICAL, "STUCK_ROSE",
             f"slices stuck below one tradeable unit rose {prev['stuck']} -> {len(stuck)}")

    # ---- 10. Section 1 stamping, once a buy really lands -------------------
    #
    # Only meaningful on a slice the BUY path wrote. An adopted row has these
    # NULL correctly, so judging §1 by the whole book reports a false failure.
    # FIRST RUN HAS NO BASELINE, and an absent baseline is a GAP, not a zero.
    # Without this guard `prev.get("at")` is "" and every slice ever bought
    # reads as new - the first run of this script reported 29 silent failures
    # that did not exist. A delta check with no previous snapshot must say so
    # and check nothing, exactly like every other gap here.
    bought = [s for _, s in slices if s.get("entry_fee_rate") and not s.get("adopted")]
    baseline = prev.get("at")
    if not baseline:
        gaps.append("no previous snapshot - delta checks skipped this run")
        recent_bought = []
    else:
        recent_bought = [s for s in bought if (s.get("opened_at") or "") > baseline]
    if recent_bought:
        unstamped = [s for s in recent_bought if not s.get("slice_state")]
        if unstamped:
            flag(CRITICAL, "SECTION1_SILENT",
                 f"{len(unstamped)} newly BOUGHT slice(s) carry no slice_state - "
                 f"the state write did not run on a row its own insert created.")
        else:
            flag(INFO, "SECTION1_OK",
                 f"{len(recent_bought)} newly bought slice(s) fully stamped.")

    # ---- 11. Profit that is reachable and still is not selling -------------
    #
    # THE OWNER'S ACTUAL QUESTION - is it doing the thing at the time it
    # should. A slice that clears the parked-sell floor and is NOT blocked by
    # reserved or short inventory should retire within a cycle or two. One
    # that sits across passes means the sell path is not acting on money it
    # could take, and that is different from "inventory is locked".
    if inv is not None:
        import re as _re
        det = {c.get("name"): (c.get("detail") or "") for c in (inv.get("checks") or [])}
        blocked = set(_re.findall(r"([A-Z0-9]+-USD)", det.get("grid_inventory_is_free", "")))
        blocked |= set(_re.findall(r"([A-Z0-9]+-USD)", det.get("coin_tracked_is_held", "")))
        # THE FLOOR A SLICE IS MEASURED AGAINST DEPENDS ON ITS BRANCH.
        #
        # This used to apply GRID_PARKED_MIN_NET_PCT (1.0%) to EVERY slice.
        # That floor governs PARKED branches - ones full on their rungs, whose
        # only way out is the parked-sell path. A branch with a rung still
        # free does not use it at all: its exit is the grid rise trigger, one
        # grid step above entry.
        #
        # TON-USD showed the difference live. Its branch held 2 of 3 rungs, so
        # it was not parked, and a slice sat at +1.33% net on a 3.0% step -
        # entry 1.4609, trigger 1.5047, price 1.4907. Real profit, genuinely
        # unreserved, and nothing was wrong: it was 0.94% short of the only
        # trigger that applies to it. Reported as "cleared the floor and did
        # not sell across two passes", it reads as a stuck sell path, and an
        # alarm that fires where no action exists is one the reader learns to
        # skip.
        #
        # For a non-parked branch this requires net >= grid_pct. Net is always
        # below gross, so clearing the step on NET means the gross move
        # certainly cleared it - deliberately conservative, because a false
        # IDLE_PROFIT is the defect being fixed.
        # A slice is only "reachable" if its BRANCH can actually sell right
        # now. Measuring the slice against grid_pct was the same mistake the
        # helper's docstring records: the rise trigger is a branch-level
        # price-vs-reference test, so a slice deep in profit on a branch far
        # from its trigger is not reachable at all.
        _thr = {b.get("product_id"): _exit_threshold(b) for b in branches}
        reachable = []
        unreadable_step = []
        for p, s2 in slices:
            if p in blocked:
                continue
            can, why, _gap = _thr.get(p, (None, "branch not found", None))
            if can is None:
                # UNKNOWN, not "not reachable". Saying nothing here would hide
                # a slice whose exit rule could not be read at all.
                unreadable_step.append(p)
            elif can:
                reachable.append((p, s2, 0.0, why))
        if unreadable_step:
            gaps.append(f"exit rule unreadable for {', '.join(sorted(set(unreadable_step)))}"
                        f" - profit sitting there would be invisible")
        cur["reachable"] = sorted({f"{p}|{s2.get('opened_at')}" for p, s2, _, _ in reachable})
        held = set(prev.get("reachable") or []) & set(cur["reachable"])
        if held:
            _why = {p: (thr, name) for p, s2, thr, name in reachable
                    if f"{p}|{s2.get('opened_at')}" in held}
            flag(WARN, "IDLE_PROFIT",
                 f"{len(held)} slice(s) sit on a branch whose own sell rule is "
                 f"SATISFIED, with no reserved or short inventory to explain "
                 f"it, and did not sell across two passes: "
                 + ", ".join(f"{p} (its {name} has fired)"
                             for p, (thr, name) in sorted(_why.items())[:6]))
        elif reachable:
            flag(INFO, "REACHABLE",
                 f"{len(reachable)} profitable slice(s) past their own exit "
                 f"threshold this pass - expect them to retire shortly.")

    # ---- 11b. Bought coin quietly becoming "adopted" ------------------------
    #
    # THE SECOND DETECTOR FOR TODAY'S FAILURE, independent of LOST_FILL. When
    # a buy filled and its insert raised, the coin sat unclaimed in the wallet
    # until coin_adoption_worker.topup_once picked it up and wrote it as an
    # ADOPTED slice - a price nobody paid, with no entry_fee_rate. That is not
    # cosmetic: _slice_rate resolves an adopted slice to the EXIT LEG ONLY,
    # which is correct for coin that was never bought and wrong for coin that
    # was. Each such slice understates its round trip by one maker leg, so its
    # net reads better than reality and the parked-sell floor clears early.
    #
    # LOST_FILL catches the moment the row goes missing. This catches the
    # consequence arriving later, on a pass where no buy leg moved at all -
    # so a missed LOST_FILL does not mean the damage goes unseen.
    no_fee = [(p, s) for p, s in slices
              if s.get("adopted") and s.get("entry_fee_rate") is None]
    book = sum((s.get("qty") or 0) * (s.get("entry_price") or 0) for _, s in no_fee)
    cur["adopted_nofee_usd"] = round(book, 2)
    prev_book = prev.get("adopted_nofee_usd")
    if prev_book is not None and book > prev_book + 0.01:
        flag(CRITICAL, "ADOPTED_GREW",
             f"coin priced without a fee rate grew {money(prev_book)} -> "
             f"{money(book)}. Real bought coin is being written as ADOPTED, "
             f"which prices its round trip at the exit leg only.")
    elif no_fee:
        # A level, not a change - stated so the standing cost is never a
        # surprise, at the maker leg the fleet actually pays.
        flag(INFO, "ADOPTED_NOFEE",
             f"{len(no_fee)} adopted slice(s), {money(book)} of book, carry no "
             f"fee rate - their round trips are understated by about "
             f"{money(book * 0.0035)} in total.")

    # ---- 11c. THE DEADLOCK THAT KILLED SIXTEEN DAYS ------------------------
    #
    # Between 2026-09-10 and 2026-09-25 the fleet closed ZERO trades. Not a
    # crash - a deadlock. A branch that fills every rung on the way down can
    # no longer buy (it is full) and will not sell (the grid never sells below
    # entry), so it simply sits. The old configuration fell in on Sep 9 and
    # stayed there until the fleet was rebuilt on Sep 26.
    #
    # It is measured off the STABLE figure the invariant names itself -
    # branches full on their rungs - NOT the underwater dollar amount beside
    # it, which that same text warns moved $3,156 -> $588 -> $717 inside 36
    # minutes with nothing traded. A number that swings that far on price is
    # not a trend and must never be reported as one.
    #
    # Watched because the escape is narrow: the parked-sell floor retires a
    # slice on its OWN basis at +1.0% net, which only clears at all because
    # maker-only cut the round trip to 0.70%. If full branches keep growing,
    # the fleet is walking back into the state that produced sixteen days of
    # nothing.
    # COMPUTED FROM THE BRANCHES, NOT SCRAPED OUT OF A SENTENCE.
    #
    # This used to regex a dollar figure out of the no_dead_capital
    # invariant's English prose. Two things were wrong with that. A wording
    # change would silently stop the match, and with no match the whole block
    # - PARKED_GREW included - was skipped without reporting anything: the
    # single most valuable alarm here, the one that catches the state behind
    # sixteen days of zero closes, could go quiet and say nothing about it.
    #
    # And it disagreed with the data. The prose read $3,492.09 while the
    # branches themselves summed to $3,647.40 over the same ten branches -
    # a $155 gap that was reported as fact all night.
    #
    # Basis (qty x entry), deliberately, not market value: a parked branch's
    # cost does not move with price, so a figure that changes is a branch
    # joining or leaving, which is the thing worth an alarm.
    full_usd = 0.0
    full_n = 0
    for b in branches:
        sl = b.get("slices") or []
        if not sl or len(sl) < (b.get("num_levels") or 0):
            continue
        full_n += 1
        full_usd += sum((x.get("qty") or 0) * (x.get("entry_price") or 0) for x in sl)
    full_usd = round(full_usd, 2)

    # Only when something is ACTUALLY parked. Reporting "$0.00 across 0
    # branch(es)" on a healthy fleet is noise, and noise on a quiet pass is
    # what trains a reader to skim.
    if full_n:
        cur["full_usd"], cur["full_n"] = full_usd, full_n
        # The endpoint's own prose is kept ONLY as a second opinion. Two
        # sources that disagree is an UNKNOWN worth saying out loud, not a
        # number to quietly prefer one of.
        import re as _re2
        _dead = ""
        for c in (inv.get("checks") or []) if inv else []:
            if c.get("name") == "no_dead_capital":
                _dead = c.get("detail") or ""
        # THE TWO DOLLAR FIGURES MEASURE DIFFERENT THINGS. NOT A DISAGREEMENT.
        #
        # This block used to flag the dollar difference as a GAP, and it fired
        # on every single pass - $3,647.40 here against $3,492.09 there, for
        # days on end. Neither source was wrong. no_dead_capital sums each
        # branch's allocated_usd, the capital the fleet COMMITTED to it; this
        # block sums qty x entry_price over that branch's slices, what those
        # slices actually COST. Measured live on 2026-09-30 the per-branch
        # spread ran both directions over the same ten branches - LTC +$139.74
        # on $214.22 allocated, HBAR -$55.69 on $332.71 - which is what
        # off-rung fills and adopted slices look like, not an error anybody can
        # fix. A gap line that fires every pass on an explained structural
        # difference is the noise that teaches a reader to skim past the gaps
        # that mean something.
        #
        # What IS a real cross-check is the branch SET. If the two sources
        # count a different number of branches full on their rungs, one of them
        # cannot see a branch the other can, and THAT is an UNKNOWN. Both
        # dollar figures are now carried in the PARKED line instead, each named
        # for what it measures, so neither is hidden and neither can be
        # mistaken for the other.
        _said = None
        m = _re2.search(r"\$([\d,]+\.\d{2}) across (\d+) branch\(es\) full on their rungs", _dead)
        if m:
            _said = float(m.group(1).replace(",", ""))
            _said_n = int(m.group(2))
            if _said_n != full_n:
                gaps.append(f"parked branch COUNT disagrees: this pass sees "
                            f"{full_n} branch(es) full on their rungs, the "
                            f"no_dead_capital invariant sees {_said_n} - one of "
                            f"the two cannot see a branch the other can")
        # Carried on BOTH lines. A reader who only ever sees the growth alarm
        # would otherwise never meet the invariant's figure, and would be left
        # comparing two numbers with nothing to tell them apart.
        _alloc = ("" if _said is None
                  else f" ({money(_said)} allocated to them - a different "
                       f"measurement, not a contradiction)")
        if True:
            prev_usd = prev.get("full_usd")
            if prev_usd is not None and full_usd > prev_usd + 0.01:
                flag(CRITICAL, "PARKED_GREW",
                     f"slice cost (qty x entry) in branches FULL on their rungs "
                     f"grew {money(prev_usd)} -> {money(full_usd)} "
                     f"({full_n} branches){_alloc}. A full branch cannot buy and "
                     f"will not sell below entry - this is the state that produced "
                     f"16 days of zero closes from 2026-09-10.")
            else:
                flag(INFO, "PARKED",
                     f"{money(full_usd)} of slice cost (qty x entry){_alloc} across "
                     f"{full_n} branch(es) is full on its rungs and can only move "
                     f"when a slice clears the parked-sell floor.")

    # ---- 12. ALPACA - the half of the account nobody was watching ----------
    #
    # Every check above is the crypto grid. The stock side has its own
    # capital, its own bots and its own halt conditions, and nothing in this
    # session had ever looked at it. A floor that halts trading is worth
    # watching BEFORE it is crossed, not after: by the time equity is under
    # it the bots have already stopped and the first anyone knows is silence,
    # which reads exactly like a quiet market.
    if alpaca is not None:
        eq = alpaca.get("equity")
        floor = alpaca.get("equity_floor")
        cur["alpaca_equity"] = eq
        if eq is not None and floor:
            cushion = eq - floor
            pct = (cushion / floor * 100) if floor else None
            if cushion <= 0:
                flag(CRITICAL, "ALPACA_FLOOR",
                     f"equity {money(eq)} is AT OR BELOW its {money(floor)} floor - "
                     f"the stock bots are halted.")
            elif pct is not None and pct < 10:
                flag(WARN, "ALPACA_FLOOR_NEAR",
                     f"equity {money(eq)} is {money(cushion)} ({pct:.1f}%) above its "
                     f"{money(floor)} floor - a {pct:.1f}% fall halts the stock bots.")
        # A halt or a block is never inferred from quietness - these say it.
        for key, label in (("buying_power_halted", "buying power halted"),
                           ("account_blocked", "account BLOCKED"),
                           ("trading_blocked", "trading BLOCKED"),
                           ("trade_suspended_by_user", "trading suspended by user")):
            if alpaca.get(key):
                reason = alpaca.get("buying_power_reason") or alpaca.get("orders_blocked_reason")
                flag(CRITICAL, "ALPACA_HALT",
                     f"{label}" + (f" - {reason}" if reason else ""))
        bp, bpf = alpaca.get("buying_power"), alpaca.get("buying_power_floor")
        if bp is not None and bpf and bp < bpf:
            flag(WARN, "ALPACA_BP",
                 f"buying power {money(bp)} is under its {money(bpf)} floor - "
                 f"no new stock entries will be taken.")
        # Equity falling between passes, reported with BOTH sides so a normal
        # intraday swing is not dressed up as an alarm.
        if prev.get("alpaca_equity") is not None and eq is not None:
            move = eq - prev["alpaca_equity"]
            if move < -20:
                flag(WARN, "ALPACA_DROP",
                     f"equity fell {money(move)} since last pass "
                     f"({money(prev['alpaca_equity'])} -> {money(eq)}).")

    # ---- report -------------------------------------------------------------
    try:
        json.dump(cur, open(args.state, "w"))
    except OSError as e:
        gaps.append(f"could not save snapshot: {e}")

    if args.json:
        print(json.dumps({"at": now, "findings": findings, "gaps": gaps,
                          "snapshot": cur}, indent=1))
    else:
        print(f"fleet watchdog {now}   serving={served or 'UNKNOWN'}")
        print(f"  slices {cur['slice_count']} | buy legs {cur['buy_legs']} | "
              f"§1 populated {cur['populated']} | stuck {cur['stuck']}")
        # QUIET MEANS NOTHING NEEDS ATTENTION, NOT THAT NOTHING WAS PRINTED.
        #
        # This used to require an empty findings list, so adding the first INFO
        # line silenced the all-clear on a healthy fleet - caught by the
        # control case. An INFO is context (what is parked, how far the next
        # sell is); only CRITICAL and WARN are things to act on.
        #
        # The exit code is deliberately NOT changed here. It has treated any
        # finding as reportable since before tonight, and quietly redefining a
        # guard's machine-readable signal to suit a new line is how a
        # monitoring change becomes an outage nobody sees.
        if not any(f["level"] in (CRITICAL, WARN) for f in findings) and not gaps:
            print("  quiet - every conservation check balanced.")
        for f in sorted(findings, key=lambda f: [CRITICAL, WARN, INFO].index(f["level"])):
            print(f"  {f['level']:8s} {f['code']:18s} {f['message']}")
        for g in gaps:
            print(f"  GAP      {'':18s} {g}")

    if gaps and not findings:
        return 1
    if any(f["level"] == CRITICAL for f in findings):
        return 2
    return 2 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
