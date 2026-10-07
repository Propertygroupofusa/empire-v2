"""Was the RANKING right? Measured forward, on bars that did not exist yet.

WHAT IS ALREADY BUILT, so this does not build it again.
opportunity_scanner.py scores every coin each cycle and rank()s them on net
edge per unit of risk. opportunity_signals.py scores momentum, volume,
pullback, volatility, spread and liquidity into 0-100 and resolves each
prediction against what the market then did. coin_scan, universe_scan,
slice_edge and concentration_gate hold the refusal gates. All of it runs
without touching capital.

WHAT NOBODY MEASURES. The scanner asserts an ORDER - "this coin before that
one" - and that assertion is never marked. The signal layer resolves whether
a coin moved as far as predicted; it does not resolve whether the coin ranked
FIRST did better than the coin ranked SECOND. Those are different claims and
only the second one justifies routing a dollar. A ranker can be badly
calibrated on absolute move and still order correctly, or be well calibrated
and order at random. This module marks the order.

WHY IT MATTERS MORE THAN IT SOUNDS. Three ranking ideas have been tested on
this fleet's real tape and lost money out of sample; the yield-weighted one -
"give the capital to the highest-scoring branch" - was the worst of them.
Seven recorded entry features fail to predict the realised outcome, best
|rho| 0.167 against a 0.211 threshold at n=87. The signal layer's own 234
resolved rows say no setup pays after costs. Four independent measurements,
all pointing the same way. A fifth ranker that merely LOOKS reasonable is not
evidence, and this module exists so the next one has to earn its way in.

THE COUNTERFACTUAL PROBLEM, AND HOW IT IS HANDLED HONESTLY.
In observation mode the scanner ranks coins it does not buy, so the realised
P&L of the trade not taken cannot be observed. What CAN be observed is the
forward price path. So a ranking is resolved by running THE FLEET'S OWN grid
rule forward on each ranked coin's real candles over a fixed horizon, and
comparing what each would have returned. That is a counterfactual, and it is
labelled one everywhere below - but it is not a model this module invented:
it is incubator.simulate, which test_incubator.py already asserts agrees
bar-for-bar with grid_step_backtest.simulate over 200 random tapes. No new
trading assumption is introduced here.

NOTHING HERE PLACES AN ORDER, SIZES A SLICE, TOUCHES A BRANCH OR SPENDS A
DOLLAR. Every function is pure: bars and rows in, a verdict out. It cannot
reach the venue and has no write path. `routing_allowed` is False until the
evidence says otherwise, and this module never sets it True on its own
authority - it reports what the arithmetic found.
"""

from collections import defaultdict

import capital_recycle as _cr   # reuse the critical-value table, do not restate it
import incubator as _inc        # reuse the grid engine, do not re-implement it

# A REJECT is a refusal, not a prediction. BLOCKED is the absence of an
# answer. Scoring either as though the scanner had claimed something would
# measure a claim it never made - and would quietly reward a scanner for
# refusing coins that later fell, which is not what routing needs to know.
PREDICTIVE_STATES = ("TRADE", "WATCH")

MIN_RANKED_PER_SNAPSHOT = 2      # one coin is an ordering of nothing
MIN_RESOLVED_PAIRS = 30          # below this, concordance is arithmetic not evidence


def snapshot(ranked_rows, at_epoch, states=PREDICTIVE_STATES):
    """Freeze the scanner's ordering as a falsifiable claim, with its clock.

    at_epoch is the moment of the claim and is NOT settable from the row
    data: a start time chosen after seeing the chart turns a forward test
    into a backtest wearing a forward label. The caller passes the clock.
    """
    keep = []
    for i, r in enumerate(ranked_rows or []):
        if (r or {}).get("state") not in states:
            continue
        keep.append({
            "product_id": r.get("product_id"),
            "predicted_rank": len(keep),      # dense, in the order given
            "scanner_position": i,            # where it sat in the full list
            "state": r.get("state"),
            "edge_per_risk": r.get("edge_per_risk"),
            "net_edge_pct": r.get("net_edge_pct"),
            "step_pct": r.get("target_pct") or r.get("grid_pct"),
        })
    return {
        "at_epoch": int(at_epoch),
        "ranked": keep,
        "ranked_count": len(keep),
        "usable": len(keep) >= MIN_RANKED_PER_SNAPSHOT,
        "not_usable_reason": (None if len(keep) >= MIN_RANKED_PER_SNAPSHOT else
                              "fewer than two coins were ranked, so no ordering "
                              "was asserted and there is nothing to be right or "
                              "wrong about"),
        "claims_nothing_about": ("coins in REJECT or BLOCKED. A refusal is not a "
                                 "prediction and is not scored here."),
    }


def forward_outcome(bars, at_epoch, step, gran_seconds=3600, levels=3, alloc=1000.0):
    """What the FLEET'S OWN rule would have returned on bars after at_epoch.

    Deliberately not a new model. split_bars drops the bar containing the
    arming instant from both sides, so the horizon cannot include the bar the
    ranking was made on, and simulate() is the same engine the incubator and
    the step backtest share.

    Returns None when the forward window is too thin to say anything, which
    is a different answer from zero and is kept distinct by every caller.
    """
    if not bars or not step or step <= 0:
        return None
    _before, after = _inc.split_bars(bars, at_epoch)
    if not after:
        return None
    res = _inc.simulate(after, step, levels=levels, alloc=alloc)
    if not res:
        return None
    days = (len(after) * gran_seconds) / 86400.0
    return {
        "bars_forward": len(after),
        "span_days": round(days, 3),
        "round_trips": res.get("round_trips"),
        "net_usd": res.get("net_usd"),
        "net_pct_of_alloc": res.get("net_pct_of_alloc"),
        "open_at_end": res.get("open_at_end"),
        # The ordering is judged on this. Per-day, because a longer window is
        # not a better coin, and on the same alloc for every coin, because
        # ranking must not reward whichever happened to be funded larger.
        "usd_per_day": round(res["net_usd"] / days, 6) if days > 0 else None,
        "is_counterfactual": True,
        "counterfactual_means": ("this is what the fleet's own grid rule would "
                                 "have returned on real forward candles. No "
                                 "order was placed and no money was at risk."),
    }


def resolve(snap, bars_by_product, gran_seconds=3600, levels=3, alloc=1000.0,
            default_step=None):
    """Attach each ranked coin's forward outcome. Unreadable stays unreadable."""
    rows, unresolved = [], []
    for r in (snap or {}).get("ranked", []):
        pid = r["product_id"]
        step = r.get("step_pct") or default_step
        bars = (bars_by_product or {}).get(pid)
        if not step:
            unresolved.append({"product_id": pid, "why": "no step to run the grid at"})
            continue
        if not bars:
            unresolved.append({"product_id": pid, "why": "no candles for this product"})
            continue
        out = forward_outcome(bars, snap["at_epoch"], step,
                              gran_seconds=gran_seconds, levels=levels, alloc=alloc)
        if out is None or out.get("usd_per_day") is None:
            unresolved.append({"product_id": pid, "why": "forward window too thin"})
            continue
        rows.append({**r, "outcome": out, "realised_usd_per_day": out["usd_per_day"]})
    # A SNAPSHOT THAT ASSERTED NOTHING MUST NOT RESOLVE AS CLEAN. With no
    # ordering claimed there is nothing to be right about, and a caller
    # reading resolved_count 0 beside partial False would take it for "there
    # was nothing to check" rather than "no claim was ever made". Found by
    # running this module against live data, where every candle fetch failed
    # and the empty snapshot came back looking tidy.
    usable = bool(snap and snap.get("usable"))
    return {
        "at_epoch": snap.get("at_epoch") if snap else None,
        "resolved": rows,
        "resolved_count": len(rows),
        "snapshot_usable": usable,
        "claimed_nothing": not usable,
        "unresolved": unresolved or None,
        # A snapshot that loses coins to unreadable data is NOT a smaller
        # valid snapshot - the ranker's claim was about the full set. Said
        # plainly so a thin resolve is not quietly treated as a clean one.
        "partial": bool(unresolved) or not usable,
    }


def concordance(resolved_rows):
    """Of every pair the ranker ordered, how often did the forward tape agree?

    Pairwise rather than top-pick-only, because "was the best one best" throws
    away most of the information in an ordering and needs far more snapshots
    to say anything. Ties in the outcome are counted separately and never
    credited to the ranker - a coin that did exactly as well as another is not
    evidence the ordering was right.
    """
    rows = [r for r in (resolved_rows or []) if r.get("realised_usd_per_day") is not None]
    right = wrong = tied = 0
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if a["predicted_rank"] == b["predicted_rank"]:
                continue
            better, worse = ((a, b) if a["predicted_rank"] < b["predicted_rank"]
                             else (b, a))
            da = better["realised_usd_per_day"]
            db = worse["realised_usd_per_day"]
            if da == db:
                tied += 1
            elif da > db:
                right += 1
            else:
                wrong += 1
    decided = right + wrong
    return {
        "pairs_right": right,
        "pairs_wrong": wrong,
        "pairs_tied": tied,
        "pairs_decided": decided,
        "concordance": round(right / decided, 4) if decided else None,
        "coin_flip": 0.5,
        "means": ("the share of ordered pairs the forward tape agreed with. "
                  "0.50 is a coin flip; a ranker at 0.50 is ordering at random "
                  "however sensible its inputs read."),
    }


def rank_rho(resolved_rows):
    """Spearman between the rank asserted and the outcome delivered.

    Negated deliberately: predicted_rank 0 is the ranker's BEST, so a ranker
    that works produces a NEGATIVE raw correlation between rank and return.
    Reporting that as a negative number has misled a reader before, so the
    sign is flipped here and the flip is stated in the payload.
    """
    rows = [r for r in (resolved_rows or []) if r.get("realised_usd_per_day") is not None]
    n = len(rows)
    if n < 3:
        return {"verdict": "UNKNOWN", "n": n,
                "reason": "fewer than three resolved coins; rho is undefined"}

    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        out = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    pr = ranks([r["predicted_rank"] for r in rows])
    ar = ranks([r["realised_usd_per_day"] for r in rows])
    mp, ma = sum(pr) / n, sum(ar) / n
    num = sum((pr[i] - mp) * (ar[i] - ma) for i in range(n))
    dp = sum((pr[i] - mp) ** 2 for i in range(n)) ** 0.5
    da = sum((ar[i] - ma) ** 2 for i in range(n)) ** 0.5
    if dp == 0 or da == 0:
        return {"verdict": "UNKNOWN", "n": n,
                "reason": "no spread in rank or in outcome; rho is undefined"}
    raw = num / (dp * da)
    rho = -raw                      # see the docstring: rank 0 is best
    crit = _cr._spearman_critical(n)
    return {
        "n": n,
        "rho": round(rho, 4),
        "critical": crit,
        "clears": (crit is not None and abs(rho) >= crit and rho > 0),
        "sign_note": ("rho is reported so POSITIVE means the ranker was right: "
                      "predicted_rank 0 is its best pick, so the raw rank-vs-"
                      "return correlation is negative when the ranker works."),
        "verdict": ("SUPPORTED" if (crit is not None and rho >= crit)
                    else "NOT SUPPORTED"),
    }


def scorecard(resolutions, min_pairs=MIN_RESOLVED_PAIRS):
    """Every snapshot pooled. The only thing that may ever flip routing on.

    Pools PAIRS across snapshots rather than averaging each snapshot's own
    concordance: a snapshot that ranked two coins and a snapshot that ranked
    eight carry very different amounts of information, and averaging them
    equally would let a run of thin snapshots outvote the informative ones.
    """
    all_rows, pooled = [], {"pairs_right": 0, "pairs_wrong": 0, "pairs_tied": 0}
    per_snapshot, partials = [], 0
    for res in (resolutions or []):
        rows = (res or {}).get("resolved") or []
        if res and res.get("partial"):
            partials += 1
        c = concordance(rows)
        per_snapshot.append({"at_epoch": (res or {}).get("at_epoch"),
                             "coins": len(rows), **c})
        for k in pooled:
            pooled[k] += c[k]
        all_rows.extend(rows)

    decided = pooled["pairs_right"] + pooled["pairs_wrong"]
    conc = round(pooled["pairs_right"] / decided, 4) if decided else None
    rho = rank_rho(all_rows)
    enough = decided >= min_pairs

    # BOTH have to hold, and neither is allowed to carry the decision alone.
    # Concordance above a coin flip on a small sample is the easiest number in
    # this codebase to get by accident; rho clearing its critical value on a
    # pooled set that mixes snapshots is the other. Requiring both is the
    # cheapest protection against the failure that has already happened four
    # times here - a ranking that looked right in the window it was read in.
    allowed = bool(enough and conc is not None and conc > 0.5 and rho.get("clears"))

    if not enough:
        reason = (f"only {decided} ordered pair(s) have been resolved; the gate "
                  f"needs {min_pairs} before concordance means anything. That "
                  f"shortage IS the finding - it is not a reason to route.")
    elif conc is not None and conc <= 0.5:
        reason = (f"concordance {conc:.4f} is at or below a coin flip across "
                  f"{decided} resolved pairs. The ranker is ordering at random.")
    elif not rho.get("clears"):
        reason = (f"rho {rho.get('rho')} does not clear {rho.get('critical')} "
                  f"at n={rho.get('n')}. The ordering is not distinguishable "
                  f"from chance at this sample size.")
    else:
        reason = (f"concordance {conc:.4f} over {decided} resolved pairs with "
                  f"rho {rho.get('rho')} clearing {rho.get('critical')}. This is "
                  f"the arithmetic clearing its own bar - it is NOT permission; "
                  f"a human decides whether a dollar moves.")

    return {
        "snapshots": len(resolutions or []),
        "snapshots_partial": partials,
        "pairs": pooled,
        "pairs_decided": decided,
        "concordance": conc,
        "rho": rho,
        "per_snapshot": per_snapshot,
        "routing_allowed": allowed,
        "reason": reason,
        "observation_only": True,
        "what_this_is_not": ("This scores an ORDERING against a counterfactual "
                            "forward tape. It is not a P&L, no order was placed, "
                            "and routing_allowed True would mean only that the "
                            "ranker beat chance - never that capital should move."),
    }
