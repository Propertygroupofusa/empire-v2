"""The rotation engine must not create, destroy, or move committed money.

Four properties, each of which would silently fake a good result:

  1. CAPITAL IS CONSERVED. Rotation moves money; it must never mint or
     delete it. A leak either way would show up as profit or as a loss
     that has nothing to do with trading.
  2. DEPLOYED CAPITAL IS UNTOUCHABLE. The entire claim that rotation is
     free rests on only idle cash moving - idle money sells nothing. If a
     transfer could take money that is sitting in coin, every figure would
     be understating a round-trip fee and, on an underwater rung, a
     realised loss.
  3. IT NEVER DEPLOYS MORE THAN IT HOLDS. A coin's open rungs must always
     cost no more than its allocation.
  4. WITH ROTATION OFF, A SINGLE COIN MATCHES THE VERIFIED CARRY REPLAY,
     which test_worst_month_split.py already pins against
     crypto_selection_backtest's own engine. Without this the new engine
     could be measuring different rules and nobody would know.
"""
import sys

sys.path.insert(0, "/home/user/empire-v2")
import continuous_rotation as cr  # noqa: E402
import worst_month_study as wm  # noqa: E402

fail = 0


def ok(label, cond, detail=""):
    global fail
    print(f"{'PASS' if cond else 'FAIL'}  {label}"
          + ("" if cond or not detail else f"\n        -> {detail}"))
    if not cond:
        fail += 1


def make(seed, n=600, drift=0.0):
    """A deterministic walk with real 3%+ moves in both directions."""
    out, p = [], 100.0
    x = seed
    for i in range(n):
        x = (1103515245 * x + 12345) % (1 << 31)
        step = ((x / (1 << 31)) - 0.5) * 0.09 + drift
        p = max(1e-6, p * (1 + step))
        out.append(p)
    return out


series = {}
for k, pid in enumerate(["AAA-USD", "BBB-USD", "CCC-USD", "DDD-USD"]):
    c = make(7 + k * 31, drift=(-0.0005 if k == 0 else 0.0003))
    series[pid] = (c, [v * 1.01 for v in c], [v * 0.99 for v in c], None)
bounds_by = {p: cr.windows_for(len(series[p][0]), 6) for p in series}
POT = 3253.84

print("\n[1] capital is conserved through every rotation")
for sig in ("none", "dip_depth", "swing", "fills_prev"):
    r = cr.run(series, bounds_by, POT, sig, 2, None, None)
    ok(f"{sig}: end capital equals the pot",
       abs(r["capital_end_usd"] - POT) < 0.05,
       f"{r['capital_end_usd']} vs {POT}")

print("\n[2] rotation never takes money that is sitting in coin")
# Drive the engine by hand so the state can be inspected at the boundary.
coins = {p: cr.Coin(p) for p in series}
for c in coins.values():
    c.alloc = POT / len(coins)
bnds = bounds_by["AAA-USD"]
a, b = bnds[0]
for i in range(a, b):
    for p, c in coins.items():
        c.step(series[p][0][i])
before = {p: (c.deployed, c.alloc) for p, c in coins.items()}
open_coins = [p for p, c in coins.items() if c.rungs]
ok("the first month really left rungs open, so this is a live test",
   bool(open_coins), before)
log = []
cr.rotate(coins, list(series), 2, None, None, log)
viol = [(p, before[p][0], coins[p].alloc) for p in coins
        if coins[p].alloc + 1e-6 < before[p][0]]
ok("no coin's allocation fell below the cost of its own open rungs",
   not viol, viol)
ok("the rungs themselves are untouched by the transfer",
   all(len(coins[p].rungs) > 0 for p in open_coins),
   {p: len(coins[p].rungs) for p in open_coins})

print("\n[3] a coin never deploys more than it holds")
bad = []
for sig in ("none", "dip_depth"):
    cs = {p: cr.Coin(p) for p in series}
    for c in cs.values():
        c.alloc = POT / len(cs)
    for m, (a, b) in enumerate(bnds):
        for i in range(a, b):
            for p, c in cs.items():
                c.step(series[p][0][i])
                if c.deployed > c.alloc + 1e-6:
                    bad.append((sig, p, m, c.deployed, c.alloc))
        if sig != "none" and m < len(bnds) - 1:
            feats = sorted(((p, cr.dip_depth(series[p][0][a:b])) for p in cs),
                           key=lambda r: -(r[1] or 0))
            cr.rotate(cs, [p for p, _ in feats], 2, None, None, [])
ok("deployed never exceeds allocation", not bad, bad[:3])

print("\n[4] rotation off, one coin, matches the verified carry replay")
one = {"AAA-USD": series["AAA-USD"]}
r1 = cr.run(one, {"AAA-USD": bnds}, 200.0, "none", 1, None, None)
cl, hi, lo, _ = series["AAA-USD"]
car = wm.replay_carry(cl, hi, lo, 200.0, bnds)
mine = sum(p["realised"] for p in r1["per_month"])
theirs = sum(d["realised"] for d in car["per_segment"].values())
ok("realised agrees with the carry replay",
   abs(mine - theirs) < 0.01, f"{mine:.6f} vs {theirs:.6f}")
ok("the end mark agrees",
   abs(r1["final_mark_usd"] - car["final_mark"]) < 0.01,
   f"{r1['final_mark_usd']} vs {car['final_mark']:.4f}")
ok("the cycle count agrees",
   r1["cycles_total"] == sum(d["cycles"] for d in car["per_segment"].values()),
   f"{r1['cycles_total']} vs {sum(d['cycles'] for d in car['per_segment'].values())}")

print("\n[5] the 20% ceiling actually refuses a transfer when it binds")
cs = {p: cr.Coin(p) for p in series}
for c in cs.values():
    c.alloc = POT / len(cs)
# A book where AAA is already at the ceiling.
book = {"AAA-USD": 2000.0, "BBB-USD": 100.0, "CCC-USD": 100.0, "DDD-USD": 100.0}
log = []
cr.rotate(cs, ["AAA-USD", "BBB-USD"], 2, book, sum(book.values()), log)
ok("the over-concentrated coin was refused and said why",
   any(p == "AAA-USD" for p, _ in log), log)
ok("and capital was still conserved after the refusal",
   abs(sum(c.alloc for c in cs.values()) - POT) < 0.05,
   sum(c.alloc for c in cs.values()))

print("\n[6] a coin missing from the live book is refused, not guessed at")
cs = {p: cr.Coin(p) for p in series}
for c in cs.values():
    c.alloc = POT / len(cs)
log = []
cr.rotate(cs, ["ZZZ-USD", "BBB-USD"], 1, {"BBB-USD": 10.0}, 10.0, log)
ok("the unknown coin is named in the refusal log",
   any(p == "ZZZ-USD" for p, _ in log), log)

print("\n" + ("ALL PASS" if not fail else f"{fail} FAILURE(S)"))
sys.exit(1 if fail else 0)
