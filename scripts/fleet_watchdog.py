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


def get(path, timeout=45):
    """Fetch one feed. A failure is recorded as a GAP and returns None.

    Never returns {} on failure: an empty dict reads downstream as "nothing
    is wrong", which is the exact confusion this whole script exists to
    prevent.
    """
    url = path if path.startswith("http") else API + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        gaps.append(f"{path}: {type(e).__name__}: {e}")
        return None


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
    # A normal grid exit only ever sells ABOVE its own entry, so realized P&L
    # is monotonic by design. If it falls, something closed a position
    # OUTSIDE that rule - an emergency exit, a retirement, a liquidation -
    # and that books a real loss that the "realized cannot go negative"
    # reassurance would otherwise hide.
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
                flag(CRITICAL, "PARKED_RETRY_LOOP",
                     f"{', '.join(blocked)} cannot place the escape sell at all "
                     f"(see ORDER_REFUSED) - the gate passes every cycle and the "
                     f"order can never fill. This one does not resolve by waiting.")
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
        prev_refused = prev.get("refused_products")
        if by_product:
            joined = ([p2 for p2 in sorted(by_product) if p2 not in prev_refused]
                      if prev_refused is not None else [])
            level = CRITICAL if joined else WARN
            head = (f"{len(by_product)} product(s) cannot place an order"
                    + (f" - {', '.join(joined)} newly refused" if joined else
                       " - same products as last pass" if prev_refused is not None
                       else ""))
            flag(level, "ORDER_REFUSED", head)
            for p2 in sorted(by_product):
                flag(level, "ORDER_REFUSED", f"  {p2}: {by_product[p2]}")
        unreadable = refus.get("product_rules_unreadable") or []
        if unreadable:
            gaps.append(f"product rules unreadable for {', '.join(unreadable)} - "
                        f"a refusal there would be invisible")

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
                flag(level, "DRY_SPELL",
                     f"{dry_h:.1f}h since the last close{grew}, on a fleet that "
                     f"averages one every {mean_h:.1f}h ({cpd:.2f}/day). The loop "
                     f"is alive - this is the shape the 16-day dead period had.")

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
    stuck = [(p, s["qty"]) for p, s in slices
             if s.get("qty") and 0 < s["qty"] < 0.011 and abs(s["qty"] - 0.01) < 1e-12]
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
        FLOOR = 0.010  # GRID_PARKED_MIN_NET_PCT
        reachable = [(p, s) for p, s in slices
                     if (s.get("unrealized_net_pct") or 0) >= FLOOR and p not in blocked]
        cur["reachable"] = sorted({f"{p}|{s.get('opened_at')}" for p, s in reachable})
        held = set(prev.get("reachable") or []) & set(cur["reachable"])
        if held:
            flag(WARN, "IDLE_PROFIT",
                 f"{len(held)} slice(s) cleared the +{FLOOR*100:.1f}% floor, are NOT "
                 f"reserved or short, and did not sell across two passes: "
                 f"{', '.join(sorted(h.split('|')[0] for h in held)[:6])}")
        elif reachable:
            flag(INFO, "REACHABLE",
                 f"{len(reachable)} profitable slice(s) reachable this pass - "
                 f"expect them to retire shortly.")

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
    if inv is not None:
        import re as _re2
        _dead = ""
        for c in (inv.get("checks") or []):
            if c.get("name") == "no_dead_capital":
                _dead = c.get("detail") or ""
        m = _re2.search(r"\$([\d,]+\.\d{2}) across (\d+) branch\(es\) full on their rungs", _dead)
        if m:
            full_usd = float(m.group(1).replace(",", ""))
            full_n = int(m.group(2))
            cur["full_usd"], cur["full_n"] = full_usd, full_n
            prev_usd = prev.get("full_usd")
            if prev_usd is not None and full_usd > prev_usd + 0.01:
                flag(CRITICAL, "PARKED_GREW",
                     f"capital in branches FULL on their rungs grew "
                     f"{money(prev_usd)} -> {money(full_usd)} ({full_n} branches). "
                     f"A full branch cannot buy and will not sell below entry - "
                     f"this is the state that produced 16 days of zero closes "
                     f"from 2026-09-10.")
            else:
                flag(INFO, "PARKED",
                     f"{money(full_usd)} across {full_n} branch(es) is full on its "
                     f"rungs and can only move when a slice clears the parked-sell "
                     f"floor.")

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
        if not findings and not gaps:
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
