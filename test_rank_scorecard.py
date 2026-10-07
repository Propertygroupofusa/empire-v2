"""The gate between a ranking and a router.

Run as written: python3 test_rank_scorecard.py

Every assertion is on pure functions - no database, no network, no venue.
The bars are synthetic BECAUSE the point is to control what the forward tape
did, so a ranker that is right and a ranker that is wrong can both be built
on purpose and the module must tell them apart.
"""

import sys

sys.path.insert(0, ".")
import rank_scorecard as rs

FAILS = []


def ok(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def section(t):
    print("\n" + t)


GRAN = 3600


def bars(seq, t0=0, gran=GRAN):
    """(t, low, high, close) from (low, high, close) triples."""
    return [(t0 + i * gran, lo, hi, c) for i, (lo, hi, c) in enumerate(seq)]


def oscillate(n, lo, hi, t0=0, gran=GRAN):
    """A tape that swings between lo and hi - a grid's best case."""
    out = []
    for i in range(n):
        a, b = (lo, hi) if i % 2 else (hi, lo)
        out.append((min(a, b), max(a, b), b))
    return bars(out, t0, gran)


def flat(n, p, t0=0, gran=GRAN):
    return bars([(p, p, p)] * n, t0, gran)


def row(pid, state="TRADE", epr=1.0, step=0.05):
    return {"product_id": pid, "state": state, "edge_per_risk": epr,
            "net_edge_pct": 0.01, "target_pct": step}


section("[1] a snapshot is a claim, and only about what it ranked")
snap = rs.snapshot([row("A-USD"), row("B-USD"),
                    {"product_id": "C-USD", "state": "REJECT"},
                    {"product_id": "D-USD", "state": "BLOCKED"}], at_epoch=1000)
ok("REJECT and BLOCKED are not scored - a refusal is not a prediction",
   [r["product_id"] for r in snap["ranked"]] == ["A-USD", "B-USD"])
ok("the ranks are dense and in the order given",
   [r["predicted_rank"] for r in snap["ranked"]] == [0, 1])
ok("it says plainly what it claims nothing about",
   "REJECT" in snap["claims_nothing_about"])
one = rs.snapshot([row("A-USD")], at_epoch=1000)
ok("one coin is not an ordering and is marked unusable",
   one["usable"] is False and "two" in one["not_usable_reason"])

section("[2] the forward window excludes the bar the claim was made on")
b = oscillate(40, 100.0, 110.0, t0=0)
out = rs.forward_outcome(b, at_epoch=b[20][0], step=0.05, gran_seconds=GRAN)
ok("only bars after the claim are scored", out["bars_forward"] < len(b))
ok("and it is labelled a counterfactual, every time",
   out["is_counterfactual"] is True and "No order was placed" in out["counterfactual_means"])
ok("a tape with nothing after the claim returns None, not zero",
   rs.forward_outcome(b, at_epoch=b[-1][0] + 10 * GRAN, step=0.05) is None)

section("[3] a RIGHT ranker is recognised")
# A ranked first and oscillates hard; B ranked second and is flat.
snap_r = rs.snapshot([row("A-USD"), row("B-USD")], at_epoch=0)
res_r = rs.resolve(snap_r, {"A-USD": oscillate(60, 100.0, 112.0),
                            "B-USD": flat(60, 100.0)}, gran_seconds=GRAN)
ok("both coins resolve", res_r["resolved_count"] == 2 and not res_r["partial"])
c_r = rs.concordance(res_r["resolved"])
ok("the ordered pair is scored right", c_r["pairs_right"] == 1 and c_r["pairs_wrong"] == 0)

section("[4] a WRONG ranker is NOT waved through")
snap_w = rs.snapshot([row("FLAT-USD"), row("SWING-USD")], at_epoch=0)
res_w = rs.resolve(snap_w, {"FLAT-USD": flat(60, 100.0),
                            "SWING-USD": oscillate(60, 100.0, 112.0)},
                   gran_seconds=GRAN)
c_w = rs.concordance(res_w["resolved"])
ok("ranking the flat coin first is scored wrong",
   c_w["pairs_wrong"] == 1 and c_w["pairs_right"] == 0)

section("[5] a tie is never credited to the ranker")
snap_t = rs.snapshot([row("X-USD"), row("Y-USD")], at_epoch=0)
res_t = rs.resolve(snap_t, {"X-USD": oscillate(60, 100.0, 112.0),
                            "Y-USD": oscillate(60, 100.0, 112.0)}, gran_seconds=GRAN)
c_t = rs.concordance(res_t["resolved"])
ok("two identical tapes tie, and the tie is counted apart from right",
   c_t["pairs_tied"] == 1 and c_t["pairs_right"] == 0 and c_t["pairs_decided"] == 0)
ok("concordance over zero decided pairs is None, not 0.0 and not 1.0",
   c_t["concordance"] is None)

section("[6] rho is signed so POSITIVE means the ranker was right")
rows = [{"product_id": "A", "predicted_rank": 0, "realised_usd_per_day": 9.0},
        {"product_id": "B", "predicted_rank": 1, "realised_usd_per_day": 6.0},
        {"product_id": "C", "predicted_rank": 2, "realised_usd_per_day": 3.0},
        {"product_id": "D", "predicted_rank": 3, "realised_usd_per_day": 1.0}]
r_good = rs.rank_rho(rows)
ok("a perfectly correct ordering gives rho +1.0", abs(r_good["rho"] - 1.0) < 1e-9)
rev = [dict(r, predicted_rank=3 - r["predicted_rank"]) for r in rows]
r_bad = rs.rank_rho(rev)
ok("a perfectly inverted ordering gives rho -1.0", abs(r_bad["rho"] + 1.0) < 1e-9)
ok("the sign convention is stated in the payload, not left to the reader",
   "POSITIVE means the ranker was right" in r_good["sign_note"])
ok("n below three is UNKNOWN, never a number",
   rs.rank_rho(rows[:2])["verdict"] == "UNKNOWN")
ok("no spread in the outcome is UNKNOWN, not rho 0",
   rs.rank_rho([dict(r, realised_usd_per_day=5.0) for r in rows])["verdict"] == "UNKNOWN")

section("[7] THE GATE. A perfect ranker on a thin sample still routes nothing.")
perfect = {"resolved": rows, "at_epoch": 0, "partial": False}
card = rs.scorecard([perfect])
ok("concordance is a flawless 1.0", card["concordance"] == 1.0)
# A FLAWLESS ORDERING OVER FOUR COINS STILL CANNOT CLEAR, and that is the
# point of the gate rather than a gap in it. The critical-value table starts
# at n=5; below that _spearman_critical returns None and nothing can be
# called significant. A first draft of this test asserted the opposite and
# was wrong about the arithmetic, not about the code.
ok("four coins cannot be significant however perfectly ordered - rho has no "
   "critical value to clear below n=5",
   card["rho"]["critical"] is None and card["rho"]["clears"] is False)
ok("AND ROUTING IS REFUSED - 6 pairs is not 30, and n=4 is not significant",
   card["routing_allowed"] is False)
ok("the refusal names the shortage as the finding",
   "IS the finding" in card["reason"])

section("[8] enough pairs AND right AND significant -> the gate can clear")
many = [{"resolved": rows, "at_epoch": i, "partial": False} for i in range(6)]
card_many = rs.scorecard(many)
ok("36 pooled pairs clears the sample bar", card_many["pairs_decided"] >= 30)
ok("and only then does routing_allowed go True", card_many["routing_allowed"] is True)
ok("even then it says it is not permission",
   "never that capital should move" in card_many["what_this_is_not"])

section("[9] a RANDOM ranker with plenty of pairs is still refused")
# Alternating right and wrong orderings - exactly a coin flip, large sample.
mixed = []
for i in range(10):
    good = [{"product_id": "A", "predicted_rank": 0, "realised_usd_per_day": 9.0},
            {"product_id": "B", "predicted_rank": 1, "realised_usd_per_day": 1.0}]
    bad = [{"product_id": "A", "predicted_rank": 0, "realised_usd_per_day": 1.0},
           {"product_id": "B", "predicted_rank": 1, "realised_usd_per_day": 9.0}]
    mixed.append({"resolved": good if i % 2 else bad, "at_epoch": i, "partial": False})
card_rand = rs.scorecard(mixed, min_pairs=5)
ok("a coin-flip ranker scores 0.5 concordance", card_rand["concordance"] == 0.5)
ok("and is REFUSED even with the sample bar cleared",
   card_rand["routing_allowed"] is False)
ok("and is told plainly that it is ordering at random",
   "at random" in card_rand["reason"])

section("[10] a partial resolve is never silently treated as a clean one")
snap_p = rs.snapshot([row("A-USD"), row("B-USD"), row("GONE-USD")], at_epoch=0)
res_p = rs.resolve(snap_p, {"A-USD": oscillate(60, 100.0, 112.0),
                            "B-USD": flat(60, 100.0)}, gran_seconds=GRAN)
ok("the missing coin is carried with a reason, not dropped",
   res_p["partial"] is True and res_p["unresolved"][0]["product_id"] == "GONE-USD")
ok("and the scorecard counts how many snapshots were partial",
   rs.scorecard([res_p])["snapshots_partial"] == 1)
no_step = rs.resolve(rs.snapshot([{"product_id": "Z-USD", "state": "TRADE"},
                                  row("A-USD")], at_epoch=0),
                     {"Z-USD": oscillate(60, 100.0, 112.0),
                      "A-USD": oscillate(60, 100.0, 112.0)}, gran_seconds=GRAN)
ok("a coin with no step is unresolved, never run at a guessed one",
   any(u["product_id"] == "Z-USD" and "step" in u["why"]
       for u in (no_step["unresolved"] or [])))

section("[10b] a snapshot that claimed nothing never resolves as clean")
# Found by running this module against live data: every candle fetch 403'd,
# the snapshot ended up empty, and the resolve came back partial False -
# which reads as "nothing to check" rather than "no claim was made".
empty_snap = rs.snapshot([], at_epoch=0)
empty_res = rs.resolve(empty_snap, {}, gran_seconds=GRAN)
ok("an empty snapshot is marked as having claimed nothing",
   empty_res["claimed_nothing"] is True and empty_res["snapshot_usable"] is False)
ok("and is NOT reported as a clean resolve", empty_res["partial"] is True)
ok("a one-coin snapshot is the same - an ordering of one is not an ordering",
   rs.resolve(rs.snapshot([row("A-USD")], at_epoch=0),
              {"A-USD": oscillate(60, 100.0, 112.0)},
              gran_seconds=GRAN)["claimed_nothing"] is True)

section("[11] it reuses the fleet's own engine and table, not copies of them")
import incubator, capital_recycle
ok("the forward outcome is incubator.simulate, not a local re-implementation",
   rs._inc is incubator)
ok("the critical values are capital_recycle's table, not restated here",
   rs._cr is capital_recycle and rs._cr._spearman_critical(6) == 0.886)

section("[12] empty in, honest out - never a confident zero")
empty = rs.scorecard([])
ok("no snapshots means routing refused and concordance None",
   empty["routing_allowed"] is False and empty["concordance"] is None)
ok("and it still declares itself observation-only",
   empty["observation_only"] is True)

print("\nALL PASS" if not FAILS else "\n%d FAILED: " % len(FAILS) + "; ".join(FAILS))
sys.exit(0 if not FAILS else 1)
