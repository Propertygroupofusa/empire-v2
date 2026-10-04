"""The place where a mispriced row stops being a display fault.

net_edge_per_trade_usd is net / n over every closed row, and bottleneck()
returns NO_EDGE the moment it is <= 0. "Where the next dollar should go"
then refuses EVERY lever on that verdict - correctly, by its own design:
more capital onto a losing strategy is the same loss, larger and sooner.

On 2026-10-04 four ZEC closes tagged adopted_exit did exactly that:

    net   +$135.58  ->  -$175.66
    edge  +$0.6917  ->  -$0.88 per trade
    binding cause   LOW_VELOCITY -> NO_EDGE

The allocator locked itself shut on a reading taken from coin the grid never
bought, priced against a mark nobody paid, while its own 196 round trips had
not changed at all.
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import capital_kpis

PASS, FAIL = [], []


def ok(label, cond):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


def t(pnl, reason=None, i=0):
    d = 1 + (i // 20)
    return {"pnl": pnl, "exit_reason": reason, "qty": 1.0,
            "entry_price": 100.0, "exit_price": 100.0 + pnl,
            "opened_at": f"2026-09-{d:02d}T00:00:00Z",
            "closed_at": f"2026-09-{d:02d}T06:00:00Z",
            "product_id": "X-USD"}


OWN = [t(0.6917, "profit_target", i) for i in range(196)]
ZEC = [t(-26.72, "adopted_exit"), t(-123.21, "adopted_exit"),
       t(-123.24, "adopted_exit"), t(-38.07, "adopted_exit")]

kw = dict(allocated_usd=8205.72, free_cash_usd=501.66,
          account_total_usd=11397.11)
clean = capital_kpis.compute(OWN + ZEC, **kw)
raw = capital_kpis.compute(OWN + ZEC, exclude_inherited=False, **kw)

ok(f"the raw book reads a NEGATIVE edge ({raw['net_edge_per_trade_usd']})",
   raw["net_edge_per_trade_usd"] < 0)
ok(f"excluding the inherited rows it is POSITIVE "
   f"({clean['net_edge_per_trade_usd']})",
   clean["net_edge_per_trade_usd"] > 0)
ok("the trade count is the grid's own round trips",
   clean["trades"] == 196 and raw["trades"] == 200)
ok("and the four rows are reported, not silently dropped",
   clean["inherited_excluded"] == 4
   and abs(clean["inherited_excluded_usd"] - -311.24) < 0.01)

# THE VERDICT THAT GATES THE MONEY.
raw_code, _ = capital_kpis.bottleneck(raw)
clean_code, _ = capital_kpis.bottleneck(clean)
ok(f"the raw book reaches NO_EDGE ({raw_code})", raw_code == "NO_EDGE")
ok(f"the grid's own book does not ({clean_code})", clean_code != "NO_EDGE")

ok("the note names what refusing every lever would have rested on",
   "refuses every capital lever" in (clean["inherited_note"] or ""))

# AN INHERITED GAIN MUST BE EXCLUDED TOO. Otherwise the same mechanism
# UNLOCKS the levers just as falsely, which is the more expensive direction.
losing_grid = [t(-0.50, "stop_loss", i) for i in range(196)]
flattered = capital_kpis.compute(
    losing_grid + [t(500.0, "adopted_exit")], exclude_inherited=False, **kw)
honest = capital_kpis.compute(losing_grid + [t(500.0, "adopted_exit")], **kw)
ok("an inherited GAIN would otherwise hide a real negative edge",
   flattered["net_edge_per_trade_usd"] > 0)
ok("excluded, the real negative edge is still reported",
   honest["net_edge_per_trade_usd"] < 0)
ok("and the lever refusal it should trigger still triggers",
   capital_kpis.bottleneck(honest)[0] == "NO_EDGE")

# A book with none of them must be untouched.
plain = capital_kpis.compute(OWN, **kw)
ok("a book with no inherited rows is unchanged",
   plain["inherited_excluded"] == 0 and plain["inherited_note"] is None
   and plain["net_edge_per_trade_usd"] == clean["net_edge_per_trade_usd"])
ok("and the raw pass claims no exclusion",
   raw["inherited_excluded"] == 0 and raw["inherited_note"] is None)

# The exclusion must not quietly change the capital-placement figures,
# which are read off balances and not off the ledger at all.
ok("capital placement figures are untouched by the exclusion",
   clean["idle_capital_pct"] == raw["idle_capital_pct"]
   and clean["outside_any_branch_pct"] == raw["outside_any_branch_pct"])

if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
