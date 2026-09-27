"""Where the next dollar should go, and what is actually stopping it.

THE NUMBER ON THE SCREEN

$10,772.48 of an $11,397.11 account belongs to no branch. The bots count
$624.63 - 5.48%. That single line explains the small profit better than
any strategy question does: the edge measured +$0.2363 a trade with a
3.70 profit factor, and it is being applied to a twentieth of the money.

So the question is not "how do we trade better". It is "what is stopping
each dollar from reaching a strategy that already works", asked once per
lever, with the dollars attached.

THE FOUR LEVERS, LARGEST FIRST

  ADOPT_HELD_COIN     $10,504 of coin the account already owns. A grid
                      branch is funded with CASH and buys its own rungs -
                      create_grid_branch refuses an amount above free
                      spendable cash - so there is no path today for a
                      branch to take charge of coin already held. This is
                      the largest lever and the only one that needs new
                      engineering rather than a setting.

  FILL_RUNGS          Branches claim $553.89 and have converted $143.11
                      into coin. Twelve of eighteen rungs are unfilled,
                      waiting on a 2.5% fall that has not come. The money
                      is earmarked and idle at the same time.

  FUND_NEW_BRANCH     $481.52 of genuinely free cash. Auto-deploy exists
                      and is enabled. It opens nothing, for the reason
                      below.

  COMPOUND_REALIZED   $19.61 banked, and ALREADY put back to work.
                      run_grid_branch_cycle does allocated_usd += pnl on
                      every FIFO sell, so a win raises its own branch's
                      allocation as it closes. This module first sized
                      this lever at the full $19.61 and called it open,
                      which would have had the same profit deployed
                      twice - claims up, backing flat, which is the
                      precise shape of an unbacked branch.

WHY THE LADDER IS FROZEN, WHICH IS THE ACTUAL BUG

ADAPTIVE_FLEET_STAGES is walked in tuple order and one unmet gate sets
sequence_blocked, so every LATER stage reports waiting_for_prior_stage.
SOL sits third with a $688 gate against $19.61 realized. Behind it sit
DOGE, XRP, LINK, AVAX and DOT - whose own gates are $0.00.

Five coins that require no profit at all are waiting on profit, because
of where they appear in a tuple. The auto-deployer's own sizing comment
describes the intent plainly: "the seven coins whose gate is $0.00 - BTC,
ETH, DOGE, XRP, LINK, AVAX, DOT ... 7 x $70 = $490 deployed". Five of
those seven can never be reached. The comment and the loop disagree, and
the comment is the design.

AND THE HONEST PART, WHICH MATTERS MORE

Unblocking them opens nothing today. Each still faces its own edge gate
at MIN_REQUIRED_ROI_PCT, and the backtests read DOGE -23.8%, XRP -12.8%,
LINK -12.6%, DOT -10.9%, AVAX +11.1% - every one under 20%. So the fix
changes a FALSE reason into a TRUE one: these coins are not waiting their
turn, they are refused on measured edge. That is worth knowing exactly
because it kills "just add more branches" as the answer, and points the
capital at the coins already held instead.

This module plans. It places nothing, and it never lowers a gate to
manufacture activity.
"""
from __future__ import annotations

# A branch below levels x the venue minimum logs "waiting" forever.
MIN_TRADE_USD = 5.0
DEFAULT_LEVELS = 3

# The account owner's rule, in code: no coin over a fifth of the account.
MAX_POSITION_SHARE_PCT = 20.0

# Never spend the account down to its last dollar.
DEFAULT_RESERVE_USD = 88.0

# Coin worth less than this cannot carry a branch that trades.
MIN_ADOPTABLE_USD = 60.0

# Stablecoins are cash, not an adoption candidate.
STABLE = {"USD", "USDC", "USDT", "DAI", "PYUSD", "GUSD", "BUSD"}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (f != f or f in (float("inf"), float("-inf"))) else f


def _pos(v):
    f = _num(v)
    return f if (f is not None and f > 0) else None


def levels_for(usd, min_trade=MIN_TRADE_USD, cap=DEFAULT_LEVELS):
    """How many rungs an allocation can actually carry."""
    u = _pos(usd)
    if u is None:
        return 0
    return max(1, min(cap, int(u // min_trade))) if u >= min_trade else 0


def branch_floor_usd(levels=DEFAULT_LEVELS, min_trade=MIN_TRADE_USD):
    return levels * min_trade


def lever_adopt(holdings, claimed_products, account_total_usd, *,
                min_usd=MIN_ADOPTABLE_USD, max_share_pct=MAX_POSITION_SHARE_PCT,
                adoption_available=None):
    """Coin already owned that no branch manages.

    Returned as a lever with its blocker named, NOT as an action - there
    is no adoption path in the engine today. Reporting it as actionable
    would be the dashboard lying in the more expensive direction.
    """
    # Measured, not assumed: the module either imports or it does not.
    if adoption_available is None:
        try:
            import coin_adoption  # noqa: F401
            adoption_available = True
        except Exception:
            adoption_available = False

    total = _pos(account_total_usd)
    claimed = {str(c).split("-")[0].upper() for c in (claimed_products or ())}
    rows, usd = [], 0.0
    for h in (holdings or ()):
        if not hasattr(h, "get"):
            continue
        asset = str(h.get("asset") or "").upper()
        v = _pos(h.get("usd"))
        if not asset or v is None or asset in STABLE or asset in claimed:
            continue
        if v < min_usd:
            continue
        share = round(v / total * 100, 2) if total else None
        # A position already over the line is a TRIM candidate, not an
        # adoption one. Putting a grid on it would manage an over-weight
        # position rather than reduce it.
        rows.append({"asset": asset, "usd": round(v, 2), "share_pct": share,
                     "over_position_limit": bool(share and share > max_share_pct)})
        usd += v
    rows.sort(key=lambda r: -r["usd"])
    return {
        "lever": "ADOPT_HELD_COIN",
        "usd_addressable": round(usd, 2),
        "candidates": rows,
        # THE PATH EXISTS NOW. This said NO_ADOPTION_PATH for hours after
        # coin_adoption shipped and ran - XRP $400, ETH $400, SHIB $200 -
        # so the panel was reporting a blocker that had already been
        # fixed, on the largest lever on the page. A stale blocker is
        # worse than a vague one: it sends someone to build a thing that
        # is already built.
        "blocked_by": None if adoption_available else "NO_ADOPTION_PATH",
        "what_it_would_take": (
            ("A branch opens with existing units registered as open slices at the price on "
             "the day it adopts them, so it sells into strength and rebuys lower without a "
             "dollar being spent or a coin being sold. A position already over the 20% rule "
             "is adopted SELL-ONLY - it walks down through strength at a profit target "
             "instead of being bought straight back on the next dip, which is what the rule "
             "wanted. Capped at a test slice; arming is COIN_ADOPTION_MODE."
             ) if adoption_available else
            ("create_grid_branch() funds a branch from free spendable CASH and lets it buy "
             "its own rungs; it refuses an amount above that cash. Nothing in the engine can "
             "hand a branch coin the account already holds."))
    }


def lever_fill_rungs(branches):
    """Money a branch has earmarked and not converted into coin."""
    rows, claimed, deployed, empty = [], 0.0, 0.0, 0
    for b in (branches or ()):
        if not hasattr(b, "get"):
            continue
        alloc = _num(b.get("allocated_usd")) or 0.0
        slices = b.get("slices") or b.get("open_slices") or []
        coin = 0.0
        for s in slices:
            if not hasattr(s, "get"):
                continue
            q, p = _num(s.get("qty")), _num(s.get("entry_price"))
            if q is not None and p is not None:
                coin += abs(q * p)
        lv = _num(b.get("num_levels")) or DEFAULT_LEVELS
        open_n = len(slices)
        claimed += alloc
        deployed += coin
        if open_n < lv:
            empty += int(lv) - open_n
        rows.append({
            "bot_name": b.get("bot_name"), "product_id": b.get("product_id"),
            "allocated_usd": round(alloc, 2), "deployed_coin_usd": round(coin, 2),
            "unspent_usd": round(max(alloc - coin, 0.0), 2),
            "open_slices": open_n, "levels": int(lv),
            "grid_pct": _num(b.get("grid_pct")),
        })
    rows.sort(key=lambda r: -r["unspent_usd"])
    return {
        "lever": "FILL_RUNGS",
        "usd_addressable": round(max(claimed - deployed, 0.0), 2),
        "claimed_usd": round(claimed, 2),
        "deployed_coin_usd": round(deployed, 2),
        "empty_rungs": empty,
        "branches": rows,
        "blocked_by": "WAITING_ON_A_PRICE_MOVE",
        "what_it_would_take": (
            "Each unfilled rung is a limit buy waiting for the price to fall one grid step "
            "below the branch's reference. Nothing is broken - the money is earmarked and "
            "the move has not come. The ONLY safe way to fill them faster is a smaller step, "
            "and the step is already near the floor: tightening below the measured fee-safe "
            "minimum turns every completed round trip into a loss, which is exactly how a "
            "0.9% step took this fleet from +65.4% to -71.1%."),
    }


def lever_fund(free_cash_usd, *, reserve_usd=DEFAULT_RESERVE_USD,
               levels=DEFAULT_LEVELS, min_trade=MIN_TRADE_USD,
               eligible_products=(), ladder_note=None):
    """Free cash that could open a branch today."""
    cash = _num(free_cash_usd) or 0.0
    reserve = _num(reserve_usd) or 0.0
    spendable = round(max(cash - reserve, 0.0), 2)
    floor = branch_floor_usd(levels, min_trade)
    eligible = [p for p in (eligible_products or ())]

    if spendable < floor:
        blocked, take = "BELOW_A_TRADEABLE_BRANCH", (
            f"${spendable:,.2f} spendable after a ${reserve:,.2f} reserve, and a branch needs "
            f"${floor:,.2f} to carry {levels} rungs at the ${min_trade:,.2f} venue minimum. "
            f"A branch funded below that logs 'waiting' forever.")
    elif not eligible:
        blocked, take = "NO_COIN_CLEARS_THE_EDGE_GATE", (
            ladder_note or
            "There is cash and no coin to put it on: every candidate is refused on measured "
            "edge. Lowering that gate to create activity is how a fleet buys itself a "
            "negative expectancy, so the cash waits instead.")
    else:
        blocked, take = None, (
            f"${spendable:,.2f} is spendable now and {len(eligible)} coin(s) clear the edge "
            f"gate. This lever is open.")

    return {
        "lever": "FUND_NEW_BRANCH",
        "usd_addressable": spendable,
        "free_cash_usd": round(cash, 2),
        "reserve_usd": round(reserve, 2),
        "branch_floor_usd": floor,
        "eligible_products": eligible,
        "blocked_by": blocked,
        "what_it_would_take": take,
    }


def lever_compound(realized_usd, *, levels=DEFAULT_LEVELS, min_trade=MIN_TRADE_USD,
                   already_compounded_usd=None):
    """Profit already banked - and already put back to work by the engine.

    THIS LEVER IS NOT A TO-DO, AND THAT MATTERS.

    The first version of this module sized it at the full realised $19.61
    and reported it OPEN, as though that money were waiting to be
    deployed. It is not. run_grid_branch_cycle does `allocated_usd += pnl`
    on every FIFO sell, so a winning slice raises its own branch's
    allocation the moment it closes. The $19.61 is already inside the
    $553.89 those branches claim.

    Adding it again would raise claims without raising backing, which is
    exactly the hole allocation_backing exists to detect - a number in a
    database with nothing behind it. So this reports the mechanism and
    sizes the outstanding amount at zero unless a caller can show profit
    the branches have NOT already absorbed.

    The owner's rule still holds and is still what the engine does: the
    compound comes after the profit is made. It just does not need a
    second loop to do it.
    """
    banked = _num(realized_usd) or 0.0
    # Absent evidence to the contrary, every banked dollar is assumed
    # already compounded - because the trading loop compounds it. An
    # explicit figure overrides this; a missing one must never be read
    # as "none of it has been".
    done = _num(already_compounded_usd)
    if done is None:
        done = banked
    outstanding = round(max(banked - done, 0.0), 2)

    if outstanding < min_trade:
        return {
            "lever": "COMPOUND_REALIZED",
            "usd_addressable": outstanding,
            "realized_usd": round(banked, 2),
            "already_compounded_usd": round(done, 2),
            "blocked_by": "ALREADY_AUTOMATIC",
            "what_it_would_take": (
                f"Nothing - this already runs. run_grid_branch_cycle adds a slice's profit "
                f"to its own branch's allocation the moment the sell fills, so the "
                f"${banked:,.2f} realised is already inside what the branches claim. A second "
                f"loop adding it again would raise claims without raising backing, which is "
                f"the exact shape of an unbacked branch. The compound comes after the profit "
                f"is made, and the trading loop is where it happens."),
        }
    return {
        "lever": "COMPOUND_REALIZED",
        "usd_addressable": outstanding,
        "realized_usd": round(banked, 2),
        "already_compounded_usd": round(done, 2),
        "blocked_by": None,
        "what_it_would_take": (
            f"${outstanding:,.2f} of realised profit is NOT reflected in any branch's "
            f"allocation, which should not happen - the trading loop compounds on every "
            f"close. Worth reconciling before adding it by hand."),
    }


def ladder_diagnosis(stages):
    """What the staged fleet reports, and what is actually true.

    A stage blocked on `waiting_for_prior_stage` is not evidence about
    that coin at all - it is a position in a tuple. This separates the
    coins genuinely gated on their own profit requirement from the ones
    inheriting somebody else's.
    """
    rows = list(stages or ())
    frozen, own_gate, refused, active, eligible = [], [], [], [], []
    for s in rows:
        if not hasattr(s, "get"):
            continue
        pid, state = s.get("product_id"), s.get("state")
        req = _num(s.get("required_realized_pnl"))
        roi = _num(s.get("backtested_roi_pct"))
        rec = {"product_id": pid, "required_realized_pnl": req,
               "backtested_roi_pct": roi, "state": state}
        if state == "active":
            active.append(rec)
        elif state == "eligible":
            eligible.append(rec)
        elif state == "waiting_for_prior_stage":
            # Its OWN gate is what matters. A $0 gate inheriting a block
            # is the defect; a real gate of its own is not.
            (frozen if (req is not None and req <= 0) else own_gate).append(rec)
        elif state == "waiting_for_realized_profit":
            own_gate.append(rec)
        else:
            refused.append(rec)

    # What unfreezing would ACTUALLY produce, which is the honest part.
    would_open = [r for r in frozen
                  if r["backtested_roi_pct"] is not None and r["backtested_roi_pct"] >= 20.0]
    return {
        "active": active,
        "eligible_now": eligible,
        "frozen_behind_another_coins_gate": frozen,
        "gated_on_their_own_profit_requirement": own_gate,
        "refused_on_their_own_merits": refused,
        "is_ordering_defect": bool(frozen),
        "would_open_if_unfrozen": would_open,
        "detail": (
            (f"{len(frozen)} coin(s) with a $0.00 profit gate report "
             f"'waiting_for_prior_stage' - they are waiting on a gate that belongs to a "
             f"different coin earlier in the tuple. Their own gate asks for nothing. "
             + (f"Unfreezing them opens {len(would_open)} branch(es): "
                f"{', '.join(r['product_id'] for r in would_open)}."
                if would_open else
                "Unfreezing them opens NOTHING - every one is then refused on its own "
                "measured edge, which is the true reason and worth showing in place of "
                "the false one."))
            if frozen else
            "No stage is inheriting another coin's gate."),
    }


def plan(*, kpis, holdings, branches, free_cash_usd, account_total_usd,
         claimed_products=(), eligible_products=(), stages=(),
         realized_usd=None, reserve_usd=DEFAULT_RESERVE_USD,
         levels=DEFAULT_LEVELS, min_trade=MIN_TRADE_USD,
         already_compounded_usd=None):
    """Every lever, sized, ranked, each with the thing stopping it.

    Refuses wholesale on a negative edge. More capital placed against a
    losing strategy is not growth, it is a larger loss arriving sooner,
    and this is the one place in the system where that mistake would be
    made at scale.
    """
    k = kpis or {}
    edge = _num(k.get("net_edge_per_trade_usd"))
    ladder = ladder_diagnosis(stages)

    levers = [
        lever_adopt(holdings, claimed_products, account_total_usd),
        lever_fill_rungs(branches),
        lever_fund(free_cash_usd, reserve_usd=reserve_usd, levels=levels,
                   min_trade=min_trade, eligible_products=eligible_products,
                   ladder_note=ladder["detail"]),
        lever_compound(realized_usd if realized_usd is not None else k.get("net_usd"),
                       levels=levels, min_trade=min_trade,
                       already_compounded_usd=already_compounded_usd),
    ]
    levers.sort(key=lambda l: -l["usd_addressable"])

    if edge is not None and edge <= 0:
        return {
            "ok": False,
            "refused": "EDGE_IS_NOT_POSITIVE",
            "detail": (
                f"Net edge is {edge:+.4f} a trade after fees. Every lever below moves MORE "
                f"capital onto that edge, so all of them are refused until it clears its "
                f"costs. Placing capital against a negative edge is not growth - it is the "
                f"same loss, larger and sooner."),
            "levers": levers,
            "ladder": ladder,
            "total_addressable_usd": 0.0,
            "is_a_plan_not_a_change": True,
        }
    if edge is None:
        return {
            "ok": False,
            "refused": "EDGE_UNKNOWN",
            "detail": ("The edge could not be read, and an unreadable edge is not a positive "
                       "one. Nothing is placed against a number that could not be measured."),
            "levers": levers,
            "ladder": ladder,
            "total_addressable_usd": 0.0,
            "is_a_plan_not_a_change": True,
        }

    open_now = [l for l in levers if l["blocked_by"] is None]
    return {
        "ok": True,
        "refused": None,
        "edge_per_trade_usd": edge,
        "levers": levers,
        "ladder": ladder,
        "open_levers": [l["lever"] for l in open_now],
        "open_usd": round(sum(l["usd_addressable"] for l in open_now), 2),
        "total_addressable_usd": round(sum(l["usd_addressable"] for l in levers), 2),
        "biggest_lever": levers[0]["lever"] if levers else None,
        "detail": (
            f"Edge is {edge:+.4f} a trade, so placing more capital is the right direction. "
            f"${round(sum(l['usd_addressable'] for l in levers), 2):,.2f} is addressable in "
            f"total and ${round(sum(l['usd_addressable'] for l in open_now), 2):,.2f} of it "
            f"is open today; the rest is named with what blocks it."),
        "is_a_plan_not_a_change": True,
    }
