"""Which side is jammed, said out loud.

The buy/sell split of expired orders was already in the payload and the page
printed it as a parenthetical beside the total, where it reads as
bookkeeping. It is the diagnosis. Measured on the live fleet 2026-10-04:

  expired orders   3,732  ->     51 buy /  3,681 sell   98.6% sells
  skipped cycles  19,600  ->     65 buy / 19,535 sell   99.7% sells

The fleet is not failing to buy. It is failing to SELL, and the two are not
independent: the buy gate is `len(tradeable_slices(slices)) < num_levels`,
so a branch that cannot sell stays full on its rungs and is refused every
entry. A jammed exit presents as a dead entry, and a reader looking at
GATE_PASS 0 goes hunting for a buy-side problem that is not there.
"""
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

PASS, FAIL = [], []


def ok(label, cond):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


SRC = (REPO / "crypto_grid_bot.py").read_text()
FT = (REPO / "family_tree_dashboard.html").read_text()

# The block is pure arithmetic over the payload it already builds, so it is
# replayed here rather than mocked - the real statements, real values.
i = SRC.index("# WHICH SIDE IS JAMMED, SAID OUT LOUD.")
j = SRC.index("return out", i)
BLOCK = "\n".join(l[4:] if l.startswith("    ") else l
                  for l in SRC[i:j].splitlines())


def run(buy, sell, minimum=20):
    out = {"buy": buy, "sell": sell}
    ns = {"out": out, "_EXPIRY_MIN_RESOLVED": minimum}
    exec(BLOCK, ns)
    return out


# --- the live numbers -----------------------------------------------------
live = run(51, 3681)
ok("the live split is called a SELL jam", live.get("jammed_side") == "SELL")
ok(f"at 98.6% ({live.get('jammed_side_share_pct')}%)",
   live.get("jammed_side_share_pct") == 98.6)
ok("and it says plainly this is an exit problem, not an entry one",
   "EXIT problem, not an entry one" in live.get("jammed_side_note", ""))
ok("and explains why the dead entry follows from it",
   "stays full and is refused every entry" in live.get("jammed_side_note", ""))
ok("naming the gate rather than asserting the link",
   "fewer open slices than it has levels" in live.get("jammed_side_note", ""))

# --- it must not speak when it has nothing to say -------------------------
quiet = run(500, 500)
ok("a balanced split says nothing at all - 50/50 is not a finding",
   "jammed_side" not in quiet)

near = run(470, 530)
ok("and neither does a mild lean (53%)", "jammed_side" not in near)

edge = run(20, 80)
ok("80% is the point it starts speaking", edge.get("jammed_side") == "SELL")

just_under = run(21, 79)
ok("79% still says nothing - the threshold is not fudged",
   "jammed_side" not in just_under)

tiny = run(1, 9)
ok("below the minimum sample it stays silent however lopsided",
   "jammed_side" not in tiny)

# --- the other direction is a real case, not a mirror of the words --------
buyjam = run(3681, 51)
ok("a BUY jam is reported as one", buyjam.get("jammed_side") == "BUY")
ok("and does NOT claim an exit problem",
   "EXIT problem" not in buyjam.get("jammed_side_note", ""))
ok("nor borrow the sell-side explanation",
   "refused every entry" not in buyjam.get("jammed_side_note", ""))

zero = run(0, 0)
ok("no expiries at all is silent, not a division by zero",
   "jammed_side" not in zero)

# --- the page leads with it ----------------------------------------------
ok("the panel renders the diagnosis", "jammed_side_note" in FT)
ok("it leads, above the horizon table it explains",
   FT.index("jammed_side_note") < FT.index("After we cancelled"))
ok("the heading follows the measured side rather than being hardcoded",
   "drift.jammed_side === 'SELL' ?" in FT)
ok("and the whole block is absent when the payload omits it",
   "drift.available && drift.jammed_side_note" in FT)

if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
