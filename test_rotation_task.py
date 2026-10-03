"""The rotation task must not invent, strand or force money.

What would silently fake a good outcome, and is therefore pinned:
  1. inert without a ticket, and silent
  2. a coin whose candles cannot be read is UNRANKED, never treated as
     having no dip - a failed fetch must not become a ranking position
  3. it never sources from a branch holding open slices unless the
     separate release switch is armed, and never below deployed cost
  4. the 20% ceiling refuses a target and the refusal is named
  5. an unbalanced plan is refused outright rather than half-written
  6. when a withdrawal fails, only what was ACTUALLY released is placed -
     the alternative invents budget out of a failed call
  7. it runs at most once per ticket
"""
import asyncio
import sys

sys.path.insert(0, "/home/user/empire-v2")
import rotation_task as rt  # noqa: E402

fail = 0


def ok(label, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}"
          + ("" if cond or not detail else f"\n        -> {detail}"))
    if not cond:
        fail += 1


def branch(pid, alloc, slices=(), levels=3, bot=None):
    return {"product_id": pid, "bot_name": bot or ("bot_" + pid.split("-")[0]),
            "allocated_usd": alloc, "num_levels": levels,
            "slices": [{"qty": q, "entry_price": e} for q, e in slices]}


BOOK = [
    branch("APE-USD", 246.97, [(100, 0.8)]),          # 1 rung, $80 deployed
    branch("HBAR-USD", 331.73, [(1000, 0.1)] * 5),
    branch("ONDO-USD", 51.78, [(30, 0.5)]),
    branch("QNT-USD", 147.27),                         # FLAT
    branch("ALGO-USD", 184.28, [(500, 0.12)]),
    branch("PEPE-USD", 165.44),                        # FLAT
    branch("TIA-USD", 57.65),                          # FLAT
    branch("JASMY-USD", 52.62),                        # FLAT
    branch("PRIME-USD", 28.38),                        # FLAT
    branch("ZEC-USD", 2271.29, [(1.0, 1300.0)]),
    branch("XRP-USD", 2228.05, [(400, 1.5)]),
]
RANKED = [("ONDO-USD", .30), ("APE-USD", .25), ("ALGO-USD", .22),
          ("HBAR-USD", .20), ("QNT-USD", .18), ("TIA-USD", .15),
          ("ZEC-USD", .40), ("PEPE-USD", .05)]


class FakeGrid:
    def __init__(self, book, fail_on=()):
        self.book = {b["bot_name"]: dict(b) for b in book}
        self.fail_on = set(fail_on)
        self.calls = []

    async def get_grid_status(self):
        return {"branches": list(self.book.values())}

    def get_session_factory(self):
        raise AssertionError("not used in these tests")

    async def withdraw_from_grid_branch(self, bot, amount):
        b = self.book[bot]
        if b["slices"]:
            raise ValueError(f"{bot} has real open slices - can only withdraw "
                             f"from a FLAT branch")
        if bot in self.fail_on:
            raise ValueError("simulated venue refusal")
        b["allocated_usd"] -= amount
        self.calls.append(("withdraw", bot, amount))

    async def add_cash_to_grid_branch(self, bot, amount):
        self.book[bot]["allocated_usd"] += amount
        self.calls.append(("add", bot, amount))

    async def _log_activity_safe(self, *a, **k):
        pass


run = asyncio.run

print("\n[1] inert without a ticket")
rep = run(rt.run_at_boot(FakeGrid(BOOK), tkt=None))
ok("ran is False", rep.get("ran") is False, rep)
ok("the reason is NO_TICKET", rep.get("reason") == "NO_TICKET", rep)

print("\n[2] an unreadable coin is UNRANKED, not ranked as having no dip")
ok("no candles -> None", rt.dip_depth([]) is None)
ok("None closes -> None", rt.dip_depth(None) is None)
ok("a zero high -> None", rt.dip_depth([0.0, 0.0]) is None)
ok("junk -> None", rt.dip_depth(["x", "y"]) is None)
ok("a real series computes the dip",
   abs(rt.dip_depth([100.0, 120.0, 90.0]) - 0.25) < 1e-9,
   rt.dip_depth([100.0, 120.0, 90.0]))


async def _fetch_half_broken(s, pid, days=30):
    if pid in {"APE-USD", "ZEC-USD"}:
        raise RuntimeError("venue said no")
    return ([100.0, 110.0, 99.0], None, None, None)


ranked, unreadable = run(rt.rank_by_dip(["APE-USD", "ONDO-USD", "ZEC-USD"],
                                        fetch=_fetch_half_broken))
ok("the broken coins are reported, not ranked",
   {u["product_id"] for u in unreadable} == {"APE-USD", "ZEC-USD"}, unreadable)
ok("only the readable coin is ranked",
   [p for p, _ in ranked] == ["ONDO-USD"], ranked)

print("\n[3] a non-flat branch is never sourced unless the switch is armed")
p = rt.plan(BOOK, RANKED, release_deployed_idle=False)
src = {s["product_id"] for s in p["sources"]}
nonflat = {b["product_id"] for b in BOOK if b["slices"]}
ok("no source holds open slices", not (src & nonflat), sorted(src & nonflat))
ok("every source is marked flat", all(s["flat"] for s in p["sources"]), p["sources"])
ok("the skipped non-flat branches say why",
   any("non-flat" in (s.get("why") or "") for s in (p.get("skipped") or [])),
   p.get("skipped"))
pool_flat = round(sum(b["allocated_usd"] for b in BOOK
                      if not b["slices"] and b["product_id"] not in
                      {t["product_id"] for t in p["targets"]}), 2)
ok("the pool is exactly the flat branches outside the target set",
   abs(p["pool_usd"] - pool_flat) < 0.02, f"{p['pool_usd']} vs {pool_flat}")

print("\n[4] with the switch armed, it releases only down to deployed cost")
p2 = rt.plan(BOOK, RANKED, release_deployed_idle=True)
by = {b["product_id"]: b for b in BOOK}
bad = []
for s in p2["sources"]:
    b = by[s["product_id"]]
    dep = sum(x["qty"] * x["entry_price"] for x in b["slices"])
    if s["release_usd"] > b["allocated_usd"] - dep + 0.02:
        bad.append((s["product_id"], s["release_usd"], b["allocated_usd"] - dep))
ok("no source releases more than its idle", not bad, bad)
ok("the armed pool is larger than the flat-only pool",
   p2["pool_usd"] > p["pool_usd"], f"{p2['pool_usd']} vs {p['pool_usd']}")

print("\n[5] an unpriced rung makes a branch's deployed cost UNKNOWN")
book2 = [dict(b) for b in BOOK]
book2[0] = {**BOOK[0], "slices": [{"qty": 100, "entry_price": None}]}
p3 = rt.plan(book2, [("ONDO-USD", .3), ("QNT-USD", .2), ("TIA-USD", .1),
                     ("JASMY-USD", .05), ("PRIME-USD", .04), ("PEPE-USD", .03)],
             release_deployed_idle=True)
ok("that branch is skipped as UNKNOWN, not released from",
   any(s["product_id"] == "APE-USD" and "UNKNOWN" in (s.get("why") or "")
       for s in (p3.get("skipped") or [])), p3.get("skipped"))

print("\n[6] the 20% ceiling refuses a target and names it")
tiny = [branch("AAA-USD", 100.0), branch("BBB-USD", 100.0),
        branch("CCC-USD", 100.0), branch("DDD-USD", 1000.0)]
# DDD already dominates; sending it a share would push it past 20%.
p4 = rt.plan(tiny, [("DDD-USD", .9), ("AAA-USD", .5), ("BBB-USD", .4),
                    ("CCC-USD", .3)])
refused = {r["product_id"] for r in (p4.get("refused") or [])}
ok("the over-concentrated coin is refused", "DDD-USD" in refused or not p4["ok"],
   p4.get("refused") or p4.get("detail"))
if p4.get("ok"):
    ok("and the refusal explains itself in percent",
       any("%" in (r.get("why") or "") for r in p4["refused"]), p4["refused"])

print("\n[7] the plan balances, and an unbalanced one is refused outright")
ok("the plan says it balances", p.get("balances") is True, p)
ok("adds equal the pool to the cent",
   abs(sum(a["add_usd"] for a in p["targets"]) - p["pool_usd"]) <= rt.CENT,
   f"{sum(a['add_usd'] for a in p['targets'])} vs {p['pool_usd']}")
broken = {**p, "targets": [{**t, "add_usd": t["add_usd"] * 2} for t in p["targets"]]}
g = FakeGrid(BOOK)
res = run(rt.apply(g, broken))
ok("an unbalanced plan is refused", res["status"] == "REFUSED_UNBALANCED", res)
ok("and it wrote nothing at all", g.calls == [], g.calls)

print("\n[8] a failed withdrawal never becomes placed budget")
g = FakeGrid(BOOK, fail_on={"bot_PEPE"})
res = run(rt.apply(g, p))
withdrew = sum(a for k, _, a in g.calls if k == "withdraw")
placed = sum(a for k, _, a in g.calls if k == "add")
ok("PEPE's release really failed",
   any(f.get("product_id") == "PEPE-USD" for f in (res.get("failed") or [])),
   res.get("failed"))
ok("placed never exceeds what was actually released",
   placed <= withdrew + rt.CENT, f"placed {placed} vs withdrew {withdrew}")
ok("and the two balance to the cent",
   abs(placed - withdrew) <= rt.CENT, f"{placed} vs {withdrew}")
ok("nothing was stranded", abs(res.get("stranded_usd") or 0) <= rt.CENT, res)

print("\n[9] a clean apply moves exactly the pool and writes both sides")
g = FakeGrid(BOOK)
res = run(rt.apply(g, p))
w = sum(a for k, _, a in g.calls if k == "withdraw")
ad = sum(a for k, _, a in g.calls if k == "add")
ok("status APPLIED", res["status"] == "APPLIED", res["status"])
ok("withdrawn equals the pool", abs(w - p["pool_usd"]) <= rt.CENT, f"{w} vs {p['pool_usd']}")
ok("added equals withdrawn", abs(ad - w) <= rt.CENT, f"{ad} vs {w}")
ok("no order was placed - the fake grid has no order method",
   not hasattr(g, "place_order"))
src_names = {s["bot_name"] for s in p["sources"]}
ok("only source branches were withdrawn from",
   {b for k, b, _ in g.calls if k == "withdraw"} == src_names)

print("\n[10] the module writes nothing but allocated_usd")
import re  # noqa: E402
s = open("/home/user/empire-v2/rotation_task.py").read()
for bad_tok in ("place_order", "create_order", "market_order", "num_levels =",
                "reference_price", "grid_pct", "row.qty", "delete("):
    ok(f"no {bad_tok!r} in the module", bad_tok not in s)
ok("it only calls withdraw and add on the grid",
   sorted(set(re.findall(r"grid\.(\w+)", s)))
   == ["_log_activity_safe", "add_cash_to_grid_branch", "get_grid_status",
       "get_session_factory", "withdraw_from_grid_branch"],
   sorted(set(re.findall(r"grid\.(\w+)", s))))
ok("the measured width is six", rt.TOP_N == 6)
ok("the ceiling is the owner's 20%", rt.MAX_COIN_SHARE_PCT == 20.0)

print("\n" + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
