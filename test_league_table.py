"""A league that always crowns someone is a trophy cabinet, not a standing.

Two ways this framing goes wrong, and a test for each:

  * it flatters. Everyone is Elite, someone always wins Most Improved, and
    the coin that cannot be measured quietly vanishes from the table.
  * it invents movement. "Climbed three places" needs a past standing, and
    when there isn't one the honest answer is "no comparable standing",
    never a fabricated zero.
"""
import league_table as L
import copy_desk
import newsroom_brief as N

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


def row(asset, to_stop=15.0, stop_pct=0.10, vol=1.0, share=5.0, usd=1000.0,
        status="OK", price=95.0, level=90.0):
    return {"asset": asset, "usd": usd, "status": status, "pct_to_stop": to_stop,
            "stop_pct": stop_pct, "daily_vol_pct": vol, "price": price,
            "stop_level": level, "share_of_account_pct": share, "units": 1.0,
            "pct_from_peak": -1.0}


print("\ndivisions come from the score, never from a hand-picked list")

ok("a strong score is Elite", L.division_for(85) == L.ELITE)
ok("a middling one is Challenger", L.division_for(70) == L.CHALLENGER)
ok("a weak one is Prospect", L.division_for(20) == L.PROSPECT)
ok("no score at all is UNRATED, not Prospect", L.division_for(None) == L.UNRATED,
   "a coin nobody could measure has not earned a ranking, good or bad")
ok("the band edges are inclusive at the bottom",
   L.division_for(L.ELITE_MIN) == L.ELITE
   and L.division_for(L.ELITE_MIN - 1) == L.CHALLENGER)

print("\nUNMEASURABLE HOLDINGS ARE LISTED, NOT HIDDEN")

rows = [row("SOL"), row("RNDR", status="UNPRICED", to_stop=None, stop_pct=None, vol=None),
        row("AERGO", status="BELOW_MIN_TRADE", to_stop=None, stop_pct=None, vol=None, usd=1.3)]
t = L.standings(rows)
ok("all three appear", len(t) == 3, len(t))
un = [x for x in t if x["division"] == L.UNRATED]
ok("two are UNRATED", len(un) == 2)
ok("each says WHY it is unrated", all(x["unrated_reason"] for x in un),
   [x["unrated_reason"] for x in un])
ok("an unpriced coin is called unknown, not small",
   any("not small" in x["unrated_reason"] for x in un))
ok("unrated coins get no rank number", all(x["rank"] is None for x in un))
ok("rated coins are numbered from 1",
   [x["rank"] for x in t if x["score"] is not None] == [1])

print("\nranking is by score, ties broken by size, and it is stable")

t = L.standings([row("A", to_stop=5.0), row("B", to_stop=20.0),
                 row("C", to_stop=20.0, usd=5000.0)])
ok("the best score leads", t[0]["score"] >= t[1]["score"] >= t[2]["score"])
ok("a tie goes to the larger position", t[0]["asset"] == "C", [x["asset"] for x in t])
ok("ranks are contiguous", [x["rank"] for x in t] == [1, 2, 3])

print("\nMOVEMENT IS NEVER INVENTED")

now = L.standings([row("HBAR", to_stop=20.0), row("NEW", to_stop=18.0)])
then = L.standings([row("HBAR", to_stop=5.0)])
m = {x["asset"]: x for x in L.movement(now, then)}
ok("a coin present in both gets a real change",
   m["HBAR"]["score_change"] is not None and m["HBAR"]["score_change"] > 0,
   m["HBAR"]["score_change"])
ok("a coin absent earlier gets None, not zero",
   m["NEW"]["score_change"] is None,
   "'no change' and 'we could not compute it' are different statements")
ok("and it says which", "no comparable standing" in m["NEW"]["movement_note"])
ok("rank change is computed from the ranks", m["HBAR"]["rank_change"] is not None)

print("\nno earlier window means NO movement claimed anywhere")

b = L.build([row("BTC"), row("ETH")], rows_then=None)
ok("has_history is false", b["has_history"] is False)
ok("every row's change is None",
   all(r["score_change"] is None for r in b["table"]))
ok("and nothing is marked promoted or relegated",
   not b["promoted"] and not b["relegated"])
ok("no medal is awarded without history", b["most_improved"] is None)

print("\nthe medal is EARNED, not handed out")

flat_now = L.standings([row("A", to_stop=15.0), row("B", to_stop=15.0)])
flat_then = L.standings([row("A", to_stop=15.0), row("B", to_stop=14.9)])
ok("a quiet window awards nobody",
   L.most_improved(L.movement(flat_now, flat_then)) is None,
   "a Most Improved that always finds a winner is a participation trophy")
ok("the bar is a stated constant", L.MIN_IMPROVEMENT_POINTS >= 3)

big_now = L.standings([row("HBAR", to_stop=25.0, vol=1.0)])
big_then = L.standings([row("HBAR", to_stop=1.0, vol=9.0)])
w = L.most_improved(L.movement(big_now, big_then))
ok("a real climb wins it", w is not None and w["asset"] == "HBAR")
ok("with the point gain named", w["improvement_points"] >= L.MIN_IMPROVEMENT_POINTS)
ok("from and to scores are both shown",
   w["from_score"] is not None and w["to_score"] is not None)
ok("and reasons drawn from the measured components", w["reasons"])

print("\npromotion and relegation are real transitions")

up_now = L.standings([row("X", to_stop=25.0, vol=1.0)])
up_then = L.standings([row("X", to_stop=1.0, vol=12.0)])
mv = L.movement(up_now, up_then)[0]
ok("a coin crossing a band upward is PROMOTED", mv["promoted"] is True,
   (mv["previous_division"], mv["division"]))
ok("and not simultaneously relegated", mv["relegated"] is False)
down = L.movement(up_then, up_now)[0]
ok("the reverse is RELEGATED", down["relegated"] is True and down["promoted"] is False)
same = L.movement(up_now, up_now)[0]
ok("no band change means neither flag",
   not same["promoted"] and not same["relegated"])

print("\nit reuses the on-air score, it does not invent a second one")

r = row("Z", to_stop=12.0, stop_pct=0.08, vol=2.5, share=7.0)
ok("the league score IS newsroom_brief.quant_score",
   L.standings([r])[0]["score"] == N.quant_score(r)["score"])
ok("and the components come with it", L.standings([r])[0]["components"] is not None)
ok("the basis says so in words", "No second scoring system" in L.build([r])["basis"])

print("\nthe load-bearing assumption is stated, not buried")

b = L.build([row("A")], rows_then=L.standings([row("A")]))
ok("units-unchanged is on the payload",
   "unchanged over the window" in b["assumption"], b["assumption"])

print("\nTHE COPY DESK CATCHES WHAT TESTS OF THE SAME ARITHMETIC CANNOT")

watch = {"rows": [row("BTC", usd=1000.0), row("ETH", usd=500.0)],
         "coin_usd": 1500.0, "cash_usd": 100.0, "covered_share_pct": 100.0,
         "disclaimer": "These are ALERT LEVELS, not stop-loss orders.",
         "breached": [], "near": [], "over_concentration": [], "unpriced": []}
brief = N.build(watch)
c = copy_desk.check(brief, watch)
ok("a consistent broadcast clears for air", c["clears_for_air"] is True, c["corrections"])
ok("with zero corrections", c["correction_count"] == 0)

bad = dict(brief)
bad["capital"] = dict(brief["capital"], coin_usd=9999.0)
c = copy_desk.check(bad, watch)
ok("a total that disagrees with its own rows is CRITICAL",
   any(x["severity"] == "CRITICAL" and "coin_usd" in x["field"] for x in c["corrections"]),
   c["corrections"])
ok("and it does not clear for air", c["clears_for_air"] is False)

bad2 = dict(brief)
bad2["capital"] = dict(brief["capital"], daily_pnl_usd=318.0)
c = copy_desk.check(bad2, watch)
ok("an unmeasured daily P&L is caught",
   any("daily_pnl" in x["field"] for x in c["corrections"]),
   "the exact number from the mock that nobody measured")

silent = {"rows": [row("XRP", usd=2400.0, status="OK", price=80.0, level=90.0)],
          "coin_usd": 2400.0, "cash_usd": 0.0, "covered_share_pct": 100.0,
          "disclaimer": "These are ALERT LEVELS, not stop-loss orders.",
          "breached": [], "near": [], "over_concentration": [], "unpriced": []}
c = copy_desk.check(N.build(silent), silent)
ok("a position below its level but NOT reported breached is caught",
   any("BREACHED" in str(x["recomputed"]) for x in c["corrections"]),
   "silence about an exposed position is the worst failure here")

nodisc = dict(brief, disclaimer="Live market data")
c = copy_desk.check(nodisc, watch)
ok("a missing not-an-order disclaimer is CRITICAL",
   any(x["field"] == "disclaimer" and x["severity"] == "CRITICAL"
       for x in c["corrections"]))

print("\nthe copy desk reports, it does not fix")

ok("it says so", "does not fix" in copy_desk.check(brief, watch)["note"])
ok("and explains why", "teaches nobody" in copy_desk.check(brief, watch)["note"])

print("\ngoing blind is reported as going blind")

d = copy_desk.coverage_drop({"covered_share_pct": 86.4}, {"covered_share_pct": 99.5})
ok("a 13-point coverage fall is flagged", d is not None)
ok("and named as a lost feed, not a market move",
   "lost a data feed" in d["note"], d["note"])
ok("it says the dropped positions are UNWATCHED, not calm",
   "UNWATCHED, not calm" in d["note"])
ok("a small wobble is not flagged",
   copy_desk.coverage_drop({"covered_share_pct": 98.0},
                           {"covered_share_pct": 99.5}) is None)
ok("no previous broadcast means no claim",
   copy_desk.coverage_drop({"covered_share_pct": 90.0}, None) is None)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
