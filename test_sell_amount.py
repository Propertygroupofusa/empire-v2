"""Selling someone's position is not undoable. Every rule gets a test.

The operation these back: "sell $400 of ZEC". The two endpoints that
existed on 2026-09-26 would have either 404'd (ZEC belongs to no branch)
or sold all 1.82953266 ZEC - $2,822 against a $400 instruction.
"""

from decimal import Decimal

import sell_amount as S

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


ZEC_PRICE = 1542.49
ZEC_HELD = 1.82953266

print("\nTHE REAL ORDER: $400 of ZEC")

p = S.plan_sale(400, ZEC_PRICE, ZEC_HELD)
ok("it plans a sale", p["ok"], p.get("reason"))
ok("it raises AT MOST the $400 asked for", p["est_usd"] <= 400.0, p["est_usd"])
ok("and lands close to it, not miles under", p["est_usd"] >= 395.0, p["est_usd"])
ok("it sells a fraction of the position, not the position",
   p["units"] < ZEC_HELD * 0.2, p)
ok("and says what fraction in plain terms", p["pct_of_holding"] == 14.1, p["pct_of_holding"])

print("\nit never sells more than was asked for - in either direction")

for target in (5, 50, 400, 1000, 2000, 2822.03):
    p = S.plan_sale(target, ZEC_PRICE, ZEC_HELD)
    ok(f"${target:>7,.2f} target raises ${p.get('est_usd', 0):>8,.2f} - never over",
       p["ok"] and p["est_usd"] <= target, p.get("reason") or p.get("est_usd"))

print("\nthe holding is a hard ceiling, whatever is asked")

p = S.plan_sale(99999, ZEC_PRICE, ZEC_HELD)
ok("asking for more than is held is capped, not refused", p["ok"], p.get("reason"))
ok("and never sells more units than exist", p["units"] <= ZEC_HELD, p)
ok("and says it was capped", p["capped_to_holding"] is True, p)

p = S.plan_sale(400, ZEC_PRICE, ZEC_HELD)
ok("a normal sale is NOT flagged as capped", p["capped_to_holding"] is False, p)

print("\nrounding is always DOWN, onto the product's own grid")

ok("truncates, never rounds up",
   S.round_down_to_increment("0.99999999", "0.01") == Decimal("0.99"),
   S.round_down_to_increment("0.99999999", "0.01"))
ok("an exact multiple is left alone",
   S.round_down_to_increment("0.25", "0.05") == Decimal("0.25"))
ok("a whole-unit product truncates to whole units",
   S.round_down_to_increment("7.99", "1") == Decimal("7"))
try:
    S.round_down_to_increment("1", "0")
    _zero_ok = False
except ValueError:
    _zero_ok = True
ok("a zero increment raises rather than dividing by it", _zero_ok)

print("\ncoarse increments and dust are refused, not silently mangled")

p = S.plan_sale(400, ZEC_PRICE, ZEC_HELD, base_increment="1")
ok("$400 of a $1,542 coin quoted in whole units rounds to zero -> refused",
   not p["ok"] and "rounds to zero" in p["reason"], p)

p = S.plan_sale(400, ZEC_PRICE, ZEC_HELD, base_min_size="1.0")
ok("a size below the product minimum is refused with the number",
   not p["ok"] and "minimum size" in p["reason"], p)

p = S.plan_sale(400, ZEC_PRICE, ZEC_HELD, base_min_size="0.0001")
ok("a size above the product minimum passes", p["ok"], p.get("reason"))

print("\nbad inputs stop the order, they do not become one")

for bad in (0, -1, None, "abc", float("nan"), float("inf")):
    p = S.plan_sale(bad, ZEC_PRICE, ZEC_HELD)
    ok(f"usd_target {bad!r:>8.8} is refused", not p["ok"], p)

for bad in (0, -5, None, float("nan")):
    ok(f"price {bad!r:>8.8} is refused", not S.plan_sale(400, bad, ZEC_HELD)["ok"])
    ok(f"units_held {bad!r:>8.8} is refused", not S.plan_sale(400, ZEC_PRICE, bad)["ok"])

print("\nthe haircut exists so a market fill cannot overshoot")

p_none = S.plan_sale(400, ZEC_PRICE, ZEC_HELD, haircut=0.0)
p_cut = S.plan_sale(400, ZEC_PRICE, ZEC_HELD)
ok("with no haircut the size is larger", p_none["units"] > p_cut["units"])
ok("the haircut costs well under 1% of the target",
   (p_none["est_usd"] - p_cut["est_usd"]) / 400 < 0.01,
   p_none["est_usd"] - p_cut["est_usd"])
ok("and it is reported, not hidden", p_cut["haircut_pct"] == 0.5)

print(f"\n{_passed}/{_passed + _failed} checks passed")
raise SystemExit(1 if _failed else 0)
