"""The exclusion list belongs to the operator, not to a source file.

crypto_grid_bot's own coin-selection note already named this set as one of
three filters that stacked into a total shutout - "a hardcoded set that
blocks UNI, the #1 ranked coin, and STX, which earned real money in
September" - and closed with the rule this broke: "An automated filter may
rank a deliberate choice lower; it may not veto it outright."

Measured 2026-10-04 over every trade in this fleet's ledger, the three
excluded coins that have a record are all green:

    STX   +$1.78   13 trips   85% won
    PEPE  +$2.23    4 trips  100% won
    WIF   +$1.06   12 trips   67% won

and not one retired coin is negative: 8 of them, 64 round trips, +$19.13.

THE DEFAULT IS UNCHANGED. Opening a coin spends real money on a real
position, so nothing here opens anything - it only moves the switch to
where the account owner can reach it without a deploy.
"""
import importlib
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

PASS, FAIL = [], []


def ok(label, cond):
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label)


FROZEN = {"STX-USD", "BLUR-USD", "UNI-USD", "DOT-USD",
          "PEPE-USD", "WIF-USD", "POL-USD"}


def load(raw):
    os.environ.pop("GRID_MANUAL_EXCLUDED_COINS", None)
    if raw is not None:
        os.environ["GRID_MANUAL_EXCLUDED_COINS"] = raw
    import crypto_family_tree_bot as t
    importlib.reload(t)
    return t.MANUAL_EXCLUDED_COINS


# THE ONE THAT MATTERS MOST. An unset variable must be the old behaviour
# exactly - this change must not reopen a single coin on its own.
ok("unset is byte-for-byte the set that was hardcoded",
   load(None) == FROZEN)

ok("an empty string opens all seven - the explicit 'open everything'",
   load("") == set())

ok("a list keeps exactly what it names",
   load("POL-USD") == {"POL-USD"})

ok("whitespace and case in the list are tolerated, not silently dropped",
   load(" stx-usd , pol-usd ") == {"STX-USD", "POL-USD"})

ok("a trailing comma does not create a blank entry that matches nothing",
   load("POL-USD,") == {"POL-USD"})

ok("commas alone are the same as empty, not a set containing ''",
   load(",,,") == set())

# EMPTY AND UNSET ARE DIFFERENT, and conflating them is how a deliberate
# "open everything" silently becomes "keep the defaults".
ok('"" and unset are not the same thing',
   load("") != load(None))

# The three coins the fleet's own ledger shows green are the ones the
# variable has to be able to release.
opened = FROZEN - load("POL-USD,BLUR-USD,UNI-USD,DOT-USD")
ok("STX, PEPE and WIF can all be released together",
   opened == {"STX-USD", "PEPE-USD", "WIF-USD"})

# POL is the one with a real case for staying, and it must survive a
# release of the others rather than being swept out with them.
ok("POL-USD can be kept while the rest open",
   "POL-USD" in load("POL-USD"))

src = (REPO / "crypto_family_tree_bot.py").read_text()
ok("the reason POL stays is still written down beside it",
   "-$337.96" in src)
ok("the default is named as a constant, not re-typed inside the parser",
   "_MANUAL_EXCLUDED_DEFAULT" in src)
ok("and the note says plainly that reading it from the environment opens "
   "nothing by itself",
   "does not reopen anything by itself" in src)

load(None)

if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
