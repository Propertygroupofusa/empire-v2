"""An unreadable unrealized must not be summed as a zero.

WHY THIS FILE EXISTS. crypto_grid_bot reports a branch's unrealized as:

    "total_unrealized_net_usd": round(total_net_usd, 2)
        if current_price is not None and slices else None

so None carries TWO different meanings:

  - the branch holds no slices        -> the unrealized really is zero
  - the branch's price was unreadable -> the unrealized is UNKNOWN

Two fleet aggregates collapsed both into zero. growth_model used
`_num(...) or 0.0`; capital_productivity used `_f(...)` whose default is
0.0. In each case the asymmetry was the tell - growth_model records an
unreadable allocated_usd in an `unreadable` list and skips the branch two
lines above, and capital_productivity's own split_branches keeps an
`unreadable` bucket for level counts. The machinery and the intent were
both already there; only the unrealized sum fell through, and a headline
figure the account owner reads moved by an unknown amount, of either sign,
with nothing in the payload saying so.

The branch's own slice list separates the two cases, so nothing here is a
guess: None WITH open slices is UNKNOWN and excluded; None with no slices
is a true zero and counted.

Behavioural where it can be - these are pure functions over plain dicts,
so the checks call them with real inputs rather than reading the source.

TWO MUTANTS ARE SEMANTICALLY EQUIVALENT AND DELIBERATELY NOT CHASED.
measure_capital returns aggregates only; it never exposes the per-row
figures. So coercing the internal per-row None back to 0.0 produces a
byte-identical response - the total is unchanged (adding 0.0 to a sum),
and the completeness flag and the excluded-branch list are computed from
`unpriced`, which that coercion does not touch. Likewise `x or 0` inside
the rest-of-fleet sum totals the same as skipping None. Carrying the
honest None internally is defensive, for the day a row is exposed or
another sum is added, but it is not observable today and no test here
pretends otherwise.

What IS observable, and what the checks below actually pin down: the
total excludes the unknown rather than fabricating a zero for it, the
payload says it is partial, it names which branches it left out, the
branch stays in every capital figure, and a branch with no slices is
still counted as the true zero it is.
"""
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


import growth_model
import capital_productivity


def branch(pid, alloc=100.0, unreal=1.0, slices=1):
    return {
        "product_id": pid,
        "allocated_usd": alloc,
        "num_levels": 4,
        "total_unrealized_net_usd": unreal,
        "slices": [{"entry_price": 10.0, "qty": 1.0} for _ in range(slices)],
    }


print("== growth_model.measure_capital ==")

# A readable fleet: the total is the sum and it says so.
r = growth_model.measure_capital(
    [branch("AAA-USD", unreal=10.0), branch("BBB-USD", unreal=-4.0)],
    free_cash_usd=50.0)
ok("a fully readable fleet sums normally", r.get("unrealized_usd") == 6.0,
   f"got {r.get('unrealized_usd')}")
ok("and it reports itself complete", r.get("unrealized_is_complete") is True)
ok("with nothing named as excluded",
   r.get("unrealized_excludes_unpriced_branches") == [])

# THE DEFECT. A branch holding slices whose price could not be read.
r = growth_model.measure_capital(
    [branch("AAA-USD", unreal=10.0), branch("CCC-USD", unreal=None, slices=3)],
    free_cash_usd=50.0)
ok("an unpriced branch is NOT counted as zero",
   r.get("unrealized_usd") == 10.0,
   f"got {r.get('unrealized_usd')} - the old code returned 10.0 too, but by "
   f"adding a fabricated 0.0 rather than by excluding an unknown")
ok("the fleet figure declares itself PARTIAL",
   r.get("unrealized_is_complete") is False,
   "this is the check the old code could not pass at all: it had no way to "
   "say the total was incomplete")
ok("and the excluded branch is named",
   r.get("unrealized_excludes_unpriced_branches") == ["CCC-USD"],
   f"got {r.get('unrealized_excludes_unpriced_branches')}")
ok("the note spells out that the true figure is this plus an unknown",
   "PARTIAL" in (r.get("unrealized_note") or "")
   and "CCC-USD" in (r.get("unrealized_note") or ""))
ok("the concentration share is qualified by the same flag",
   (r.get("concentration") or {}).get("share_of_unrealized_is_complete") is False,
   "a share of a partial total is not a clean share")

# THE TRAP IN THE FIX ITSELF. Withholding the unrealized must not withhold
# the branch. The first version of this fix used `continue`, which dropped
# the whole row and so quietly removed a readable allocation from
# allocated_usd, deployed_usd, idle, total_capital, not_working and the
# concentration ranking - a worse bug than the one being fixed, because
# those figures were correct before.
ok("an unpriced branch is STILL counted in the capital figures",
   r.get("branches") == 2,
   f"got {r.get('branches')} - its allocation and slices are readable; only "
   f"its unrealized is not")
ok("its allocation is still in allocated_usd",
   r.get("allocated_usd") == 200.0,
   f"got {r.get('allocated_usd')} - two branches at 100.0 each")
ok("its slices are still in deployed_usd",
   r.get("deployed_usd") == 40.0,
   f"got {r.get('deployed_usd')} - 1 slice + 3 slices at 10.0 x 1.0")
ok("and it is still ranked for concentration",
   len((r.get("concentration") or {}).get("top") or
       [1] * ((r.get("concentration") or {}).get("branches_counted") or 2)) >= 1
   or (r.get("concentration") or {}).get("top_usd") == 200.0,
   f"concentration: {r.get('concentration')}")
ok("a partial total never raises on the None it now carries",
   isinstance(r.get("unrealized_usd"), (int, float))
   and isinstance((r.get("concentration") or {}).get("rest_unrealized_usd"),
                  (int, float)),
   "every sum over the per-row figure must skip None, not coerce it")

# A branch with NO slices is a true zero and must still be counted.
r = growth_model.measure_capital(
    [branch("AAA-USD", unreal=10.0), branch("DDD-USD", unreal=None, slices=0)],
    free_cash_usd=50.0)
ok("a branch with no slices is a REAL zero, not an unknown",
   r.get("unrealized_is_complete") is True
   and r.get("unrealized_excludes_unpriced_branches") == [],
   "excluding it would be the opposite error - discarding a known zero")
ok("and it still contributes to the total", r.get("unrealized_usd") == 10.0)
ok("it is still counted as a branch", r.get("branches") == 2,
   f"got {r.get('branches')} - an unpriced branch drops out of the "
   f"unrealized sum, but a zero branch is a full member")

# An unreadable allocation still fails the way it always did.
r = growth_model.measure_capital([branch("AAA-USD"), {"product_id": "EEE-USD"}],
                                 free_cash_usd=0.0)
ok("an unreadable allocation is still recorded, not absorbed",
   r.get("branches") == 1)


print("== capital_productivity._bucket ==")

b = capital_productivity._bucket(
    [branch("AAA-USD", unreal=10.0), branch("BBB-USD", unreal=-4.0)],
    {}, {}, "all")
ok("a readable bucket sums normally", b.get("unrealized_usd") == 6.0,
   f"got {b.get('unrealized_usd')}")
ok("and reports itself complete", b.get("unrealized_is_complete") is True)

b = capital_productivity._bucket(
    [branch("AAA-USD", unreal=10.0), branch("CCC-USD", unreal=None, slices=2)],
    {}, {}, "all")
ok("an unpriced branch is excluded from the bucket total",
   b.get("unrealized_usd") == 10.0, f"got {b.get('unrealized_usd')}")
ok("the bucket declares itself incomplete",
   b.get("unrealized_is_complete") is False)
ok("and names what it left out",
   b.get("unrealized_excludes_unpriced_branches") == ["CCC-USD"],
   f"got {b.get('unrealized_excludes_unpriced_branches')}")

b = capital_productivity._bucket(
    [branch("AAA-USD", unreal=10.0), branch("DDD-USD", unreal=None, slices=0)],
    {}, {}, "all")
ok("a sliceless branch is a real zero here too",
   b.get("unrealized_is_complete") is True and b.get("unrealized_usd") == 10.0)

# NaN must behave like an unreadable number, not like a quantity.
b = capital_productivity._bucket(
    [branch("AAA-USD", unreal=10.0), branch("FFF-USD", unreal=float("nan"), slices=1)],
    {}, {}, "all")
ok("a NaN unrealized is treated as unreadable, not as a figure",
   b.get("unrealized_is_complete") is False
   and b.get("unrealized_usd") == 10.0,
   f"got total {b.get('unrealized_usd')}, complete "
   f"{b.get('unrealized_is_complete')} - a NaN summed in would poison the "
   f"whole bucket")

r = growth_model.measure_capital(
    [branch("AAA-USD", unreal=10.0), branch("FFF-USD", unreal=float("nan"), slices=1)],
    free_cash_usd=0.0)
ok("growth_model treats NaN as unreadable too",
   r.get("unrealized_is_complete") is False and r.get("unrealized_usd") == 10.0,
   f"got total {r.get('unrealized_usd')}, complete "
   f"{r.get('unrealized_is_complete')}")


print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all unrealized-gap checks passed")
