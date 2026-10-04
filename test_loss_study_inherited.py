"""An inherited position leaving is not one of this strategy's losses.

On 2026-10-04 four ZEC closes tagged adopted_exit entered the book. Two of
them were the WORST TWO losses in it, which is the one place a handful of
rows moves every statistic at once - and every one of those statistics is
read as a verdict on how this grid trades:

    avg_loss        -$12.76 with them        profit factor  0.47x with them
    worst_loss     -$123.24 with them        big_losses          8 with them

It funds exactly one decision: tighten the stop. The stop did not fire on
any of the four. Tightening it would cut winners to fix a loss that never
happened - the same trap config_epoch already exists to prevent, arriving
through a different door.

Nothing is hidden: the rows stay in losses_by_exit_reason, carry their own
counted line, and the panel says what they were.
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import loss_study

PASS, FAIL = [], []


def ok(label, cond):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


def t(pnl, reason, qty=1.0, entry=100.0, when="2026-10-04T08:00:00Z"):
    return {"pnl": pnl, "exit_reason": reason, "qty": qty,
            "entry_price": entry, "product_id": "X-USD", "closed_at": when}


# The real shape: a book of small wins, three stops, and the four ZEC rows.
BOOK = (
    [t(0.91, "profit_target") for _ in range(20)]
    + [t(-0.80, "stop_loss") for _ in range(3)]
    + [t(-26.72, "adopted_exit"), t(-123.21, "adopted_exit"),
       t(-123.24, "adopted_exit"), t(-38.07, "adopted_exit")]
)

a = loss_study.analyse(BOOK)
raw = loss_study.analyse(BOOK, exclude_inherited=False)

ok("the inherited rows are set aside by default",
   a["inherited_excluded"] == 4)
ok(f"and their booked total is reported (${a['inherited_excluded_usd']})",
   abs(a["inherited_excluded_usd"] - -311.24) < 0.01)

ok(f"the worst loss becomes a real one (${a['worst_loss_usd']}) instead of "
   f"${raw['worst_loss_usd']}",
   abs(a["worst_loss_usd"] - -0.80) < 0.01
   and abs(raw["worst_loss_usd"] - -123.24) < 0.01)
ok(f"the average loss stops being dominated (${a['avg_loss_usd']} vs "
   f"${raw['avg_loss_usd']})",
   abs(a["avg_loss_usd"]) < 1.0 and abs(raw["avg_loss_usd"]) > 10.0)
ok("the loss count is the strategy's own three stops",
   a["losses"] == 3 and raw["losses"] == 7)
ok("big_losses stops counting inherited rows against a $0.91 average win",
   a["big_losses"] == 0 and raw["big_losses"] > 0)

# THE VERDICT FLIPS, and that is the point - it was pointing at the stop.
ok(f"win/loss size ratio inverts ({a['win_loss_size_ratio']} vs "
   f"{raw['win_loss_size_ratio']})",
   a["win_loss_size_ratio"] > 1.0 and raw["win_loss_size_ratio"] < 0.2)
ok("the breakeven win rate stops being unreachable",
   a["breakeven_win_rate_pct"] < 60 and raw["breakeven_win_rate_pct"] > 80)

# NOTHING IS HIDDEN.
ok("the rows still appear in the reason breakdown",
   a["losses_by_exit_reason"].get("adopted_exit") == 4)
ok("alongside the stops they are being separated from",
   a["losses_by_exit_reason"].get("stop_loss") == 3)
ok("and a note says what was set aside and why",
   "kept OUT of the loss shape" in (a["inherited_note"] or ""))
ok("naming the reason the grid cannot be judged on them",
   "chose neither end" in (a["inherited_note"] or ""))

# A book with none of them must be untouched, and must not claim an
# exclusion it did not make.
clean = loss_study.analyse([t(0.91, "profit_target"), t(-0.80, "stop_loss")])
ok("a book with no inherited rows is unchanged",
   clean["inherited_excluded"] == 0 and clean["inherited_note"] is None)
ok("and its arithmetic matches the raw pass exactly",
   clean["avg_loss_usd"] == loss_study.analyse(
       [t(0.91, "profit_target"), t(-0.80, "stop_loss")],
       exclude_inherited=False)["avg_loss_usd"])

# exclude_inherited=False must really mean false.
ok("the raw pass reports no exclusion",
   raw["inherited_excluded"] == 0 and raw["inherited_note"] is None)

# An inherited row that WON must be excluded too - the rule is about whose
# decision it was, not about whether it helped.
won = loss_study.analyse(
    [t(0.91, "profit_target") for _ in range(5)]
    + [t(500.00, "adopted_exit")])
ok("an inherited GAIN is excluded on the same rule, not kept because it "
   "flatters the book",
   won["inherited_excluded"] == 1 and won["trades"] == 5)

if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
