"""The re-ranking loop, closed: measure, re-validate, rank - or refuse.

THE OWNER'S INSTRUCTION, 2026-10-06: "Close the loop, make the re-ranking
run on a schedule."

Until now the cycle that matters - measure outcomes, re-rank capital, act,
measure again - was closed ONCE, by hand, on 2026-10-05. The ranking in
harvest_redirect and portfolio_manager was built from that single pass and
would have aged into a frozen opinion about branches that have since
changed. This module runs the same measurement on demand so a schedule can
drive it.

THE PART THAT MATTERS MOST IS THE REFUSAL.

A scheduled ranker that always produces a ranking is worse than no ranker,
because it launders a stale or meaningless ordering as a fresh finding. The
ranking rests on ONE empirical claim: that a branch's net % per close
persists from one window to the next. That claim was true when measured
(Spearman +0.643, t=+3.03 on 13 df) and it is not a law of nature - it can
stop being true, and a market where it stops is exactly the market where
acting on it loses money.

So every run RE-TESTS the premise on the current ledger before using it. If
the correlation is no longer significant, this returns no ranking at all and
says why. The system is built to invalidate itself, because the alternative
is the failure that killed five of five candidate changes tested on real
candles: fitting to noise and calling it an edge.

WHY NET % PER CLOSE AND NOTHING ELSE. Measured over 125 own closes:

    net % per close persists    Spearman +0.643, t=+3.03, 13 df
    number of closes does not   Spearman +0.007 - noise

An earlier reallocation test ranked branches by TOTAL DOLLARS EARNED, which
is net% x closes. Half that signal is the half that does not persist, and it
lost $34 to $111 out of sample. Only the persistent half is used here, and
the test above is re-run every pass to confirm it is still the persistent
half.

NOTHING HERE EXECUTES. It reads the ledger, recomputes statistics, and
returns a plan. No order, no sale, no branch change, no write of any kind.
"""
import collections
import logging
import math

import harvest_redirect as hr
import portfolio_manager as pm
import regime_classifier as rc

log = logging.getLogger(__name__)

ADOPTED_EXIT = "adopted_exit"

# A branch needs this many closes IN EACH HALF before it can carry the
# persistence test. Two closes produce any percentage at all, and a
# correlation computed over such branches measures rounding.
MIN_CLOSES_PER_HALF = 1

# Branches needed before a rank correlation is worth computing. Below this
# the critical value is so large that nothing could ever reach it, and
# reporting "not significant" from n=4 would read as evidence against the
# premise when it is absence of evidence either way.
MIN_BRANCHES_FOR_PERSISTENCE = 8

# Two-sided t critical values at alpha=0.05, indexed by degrees of freedom.
# Written out rather than approximated: at the sample sizes this runs on
# (df 6 to 25) the normal approximation is 15-25% too permissive, which
# would pass a correlation the data does not support.
_T_CRIT_95 = {6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201,
              12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120,
              17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080,
              22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060}


def _t_crit(df):
    """The 95% two-sided critical value, never extrapolated downward."""
    if df < 6:
        return None
    if df in _T_CRIT_95:
        return _T_CRIT_95[df]
    return 1.96 if df > 120 else 2.042      # df 26..120, conservative


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def own_closes(trades):
    """Closed trades the grid itself opened. Inherited exits are not its work."""
    return [t for t in (trades or [])
            if (t.get("exit_reason") or "") != ADOPTED_EXIT]


def aggregate(trades):
    """Per branch: closes, realized, notional, net % per close."""
    g = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for t in trades or []:
        k = t.get("bot_name")
        if not k:
            continue
        g[k][0] += 1
        g[k][1] += _num(t.get("pnl")) or 0.0
        px, qty = _num(t.get("entry_price")), _num(t.get("qty"))
        if px and qty:
            g[k][2] += px * qty
    out = {}
    for k, (n, p, no) in g.items():
        out[k] = {"closes": n, "realized_usd": round(p, 4),
                  "notional_usd": round(no, 4),
                  "net_pct_per_close": (p / no * 100.0) if no else None}
    return out


def _spearman(xs, ys):
    """Rank correlation. None when either side has no spread to correlate."""
    n = len(xs)
    if n < 3:
        return None

    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0          # ties share the average rank
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def persistence(trades, *, split_at):
    """Does net % per close still carry from one window to the next?

    split_at is an ISO date string. Closes on or after it are the LATER
    window. Returns a verdict dict; `holds` False means the ranking must
    not be used this pass, and that is a normal outcome, not an error.
    """
    own = own_closes(trades)
    early = [t for t in own if (t.get("closed_at") or "") < split_at]
    late = [t for t in own if (t.get("closed_at") or "") >= split_at]
    a, b = aggregate(early), aggregate(late)

    common = [k for k in a
              if k in b
              and a[k]["net_pct_per_close"] is not None
              and b[k]["net_pct_per_close"] is not None
              and a[k]["closes"] >= MIN_CLOSES_PER_HALF
              and b[k]["closes"] >= MIN_CLOSES_PER_HALF]
    n = len(common)
    base = {"branches_compared": n, "split_at": split_at,
            "early_closes": len(early), "late_closes": len(late)}

    if n < MIN_BRANCHES_FOR_PERSISTENCE:
        return dict(base, holds=False, readable=False, rho=None, t=None,
                    detail=(f"only {n} branch(es) traded in BOTH windows, under "
                            f"the {MIN_BRANCHES_FOR_PERSISTENCE} needed. UNKNOWN, "
                            f"not disproved - the ranking is withheld because "
                            f"nothing was measured, not because it failed."))

    rho = _spearman([a[k]["net_pct_per_close"] for k in common],
                    [b[k]["net_pct_per_close"] for k in common])
    if rho is None:
        return dict(base, holds=False, readable=False, rho=None, t=None,
                    detail="rank correlation is undefined - one window has "
                           "no spread to correlate against the other")

    df = n - 2
    # A PERFECT ORDERING IS THE STRONGEST RESULT, NOT A BROKEN ONE. The t
    # statistic divides by (1 - rho^2), so rho = +/-1 sends it to infinity.
    # Rejecting that case as "undefined" - which this did first - threw away
    # the one sample that most clearly supports the premise, and would have
    # made a flawless ranking read as a failed test. The sign is kept so a
    # perfect REVERSAL still fails below.
    if abs(rho) >= 1.0:
        t = math.inf if rho > 0 else -math.inf
    else:
        t = rho * math.sqrt(df / (1.0 - rho * rho))
    crit = _t_crit(df)
    if crit is None:
        return dict(base, holds=False, readable=False, rho=round(rho, 4), t=round(t, 4),
                    detail=f"{df} degrees of freedom is too few for a 95% test")

    # ONE-SIDED IN SPIRIT: a NEGATIVE correlation that clears the threshold
    # is not support for this ranking, it is evidence the ordering should be
    # reversed - which is a finding, not a licence to rank. Only a positive
    # correlation lets the ranking run.
    holds = bool(t >= crit)
    _t_out = None if math.isinf(t) else round(t, 4)
    return dict(base, holds=holds, readable=True, rho=round(rho, 4),
                t=_t_out, t_is_infinite=math.isinf(t),
                t_critical_95=crit, df=df,
                detail=(f"net % per close persists: Spearman {rho:+.3f}, "
                        f"t={'infinite (a perfect ordering)' if math.isinf(t) else format(t, '+.2f')}"
                        f" against {crit:.3f} on {df} df"
                        if holds else
                        f"net % per close NO LONGER persists: Spearman {rho:+.3f}, "
                        f"t={'-infinite (a perfect reversal)' if math.isinf(t) else format(t, '+.2f')}"
                        f" against {crit:.3f} on {df} df. The ranking "
                        f"rests on this and is withheld. Acting on a stale "
                        f"ordering is how an edge becomes a loss."))


def build(*, trades, branches, rightsize_rows=None, loose_cash_usd=0.0,
          fleet_allocated_usd=None, split_at, since=None):
    """One full pass: measure, re-test the premise, then rank or refuse."""
    own = own_closes(trades)
    window = [t for t in own if not since or (t.get("closed_at") or "") >= since]
    stats = aggregate(window)

    basis = {r.get("bot_name"): _num(r.get("coin_basis_usd"))
             for r in (rightsize_rows or [])}

    rows = []
    for b in branches or []:
        nm = b.get("bot_name")
        s = stats.get(nm) or {}
        rows.append(dict(b,
                         coin_basis_usd=basis.get(nm, b.get("coin_basis_usd")),
                         closes=s.get("closes", 0),
                         net_pct_per_close=s.get("net_pct_per_close")))

    pers = persistence(trades, split_at=split_at)
    regime = hr.regime_verdict(rows)

    if not pers["holds"]:
        return {
            "verdict": "WITHHELD",
            "persistence": pers,
            "regime": regime,
            "plan": [], "sources": [], "would_move_usd": 0.0,
            "branches_measured": len([r for r in rows if r["closes"]]),
            "detail": pers["detail"],
            "what_this_is": (
                "A deliberate refusal, not a failure. The ranking is only "
                "valid while net % per close predicts itself, and this pass "
                "re-tested that on the current ledger and did not find it. "
                "No ordering is published rather than a stale one."),
        }

    plan = pm.plan(rows, loose_cash_usd=loose_cash_usd,
                   fleet_allocated_usd=fleet_allocated_usd)
    ranked = sorted((r for r in rows if r["net_pct_per_close"] is not None),
                    key=lambda r: -r["net_pct_per_close"])
    return {
        "verdict": plan["verdict"],
        "persistence": pers,
        "regime": plan["regime"],
        "plan": plan["plan"],
        "sources": plan["sources"],
        "capital_available_usd": plan["capital_available_usd"],
        "would_move_usd": plan["would_move_usd"],
        "unplaced_usd": plan.get("unplaced_usd"),
        "ranking": [{"bot_name": r.get("bot_name"),
                     "product_id": r.get("product_id"),
                     "allocated_usd": r.get("allocated_usd"),
                     "closes": r["closes"],
                     "net_pct_per_close": round(r["net_pct_per_close"], 3)}
                    for r in ranked],
        "branches_measured": len(ranked),
        "sells_nothing": True,
        "detail": plan["detail"],
    }
