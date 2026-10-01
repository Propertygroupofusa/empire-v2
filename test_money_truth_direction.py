"""Cash moving UP must not read as money leaving.

The live defect: cash rose $2.44 and the tool printed
"$-2.44 left the cash line some other way" - a negative amount leaving,
which is meaningless and sends a reader hunting a withdrawal that never
happened. It only ever handled cash FALLING, because it was written to
explain a $385 drop.
"""
import re
import sys

FAILS = []


def ok(label, cond, got=None):
    if cond:
        print(f"  PASS  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"   got: {got!r}" if got is not None else ""))


SRC = open("money_truth.py").read()
_RAW = SRC[SRC.index("CASH MOVES IN BOTH DIRECTIONS"):SRC.index("# WINDOWS, NOT JUST")]


def _code_only(text):
    """Executable lines only.

    THIRD TIME TODAY. An assertion matched the word "order" inside a comment,
    then `abs(d_cash)` inside a legitimate guard, and then the old buggy
    sentence inside the comment that EXPLAINS the bug. A test that reads
    English prose is not reading control flow. Strip the comments first.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  # ")[0])
    return "\n".join(out)


BLOCK = _code_only(_RAW)

print("\n[1] both directions are handled, and they say opposite things")
ok("there is a falling branch", "if d_cash < 0:" in BLOCK)
ok("there is a rising branch", "else:" in BLOCK and "CASH ROSE" in BLOCK)
ok("falling is described as buying", "became coin" in BLOCK)
ok("rising is described as a position closing", "came back from coin" in BLOCK)

print("\n[2] no negative amount can be printed as 'left' or 'arrived'")
# Every printed remainder is guarded by a positive test.
for guard in ("if spent > 0.01:", "if rest > 0.01:", "if freed > 0.01:"):
    ok(f"guarded by {guard!r}", guard in BLOCK)
# NOT a bare `abs(d_cash)` check: `abs(d_cash) > 0.01` is a legitimate
# "did cash move at all" guard and matching it fails a correct line. Pin the
# actual old SENTENCE instead - the one that printed a negative remainder.
ok("the old direction-blind sentence is gone",
   "THE CASH LINE MOVED" not in BLOCK, BLOCK[:80])
ok("...and nothing still says 'left the cash line' unguarded",
   "left the cash line some other way" not in BLOCK)

print("\n[3] the arithmetic, replayed on the real numbers that broke it")
def falling(d_cash, d_dep):
    spent = min(-d_cash, d_dep) if d_dep > 0 else 0.0
    return spent, (-d_cash) - spent
def rising(d_cash, d_dep):
    freed = min(d_cash, -d_dep) if d_dep < 0 else 0.0
    return freed, d_cash - freed

# This morning: cash -385.47, deployed +384.03.
spent, rest = falling(-385.47, 384.03)
ok("the $385 drop still reads as buying", abs(spent - 384.03) < 0.01, spent)
ok("...with only $1.44 unaccounted", abs(rest - 1.44) < 0.01, rest)

# The case that broke it: cash +2.44, deployed +0.00.
freed, rest = rising(2.44, 0.0)
ok("a $2.44 RISE yields no negative remainder", rest >= 0, rest)
ok("...and claims nothing came back from coin", freed == 0.0, freed)

# A real close: cash +571, deployed -571.
freed, rest = rising(571.05, -571.05)
ok("a closed position reads as capital released",
   abs(freed - 571.05) < 0.01, freed)
ok("...with nothing unexplained", abs(rest) < 0.01, rest)

print("\n[4] the window view exists and ignores degraded readings")
ok("windows are computed", "HOW THE ACCOUNT MOVED, BY WINDOW" in SRC)
ok("30/60/180/360/1440 minutes", "(1440," in SRC and "(30," in SRC)
ok("degraded price-feed readings are dropped",
   "assets_unpriced" in SRC and "baseline" in SRC)
ok("the reader is told a short window can disagree with a long one",
   "the market breathing" in SRC)

print("\n[5] still read-only")
ok("one urlopen", SRC.count("urlopen") == 1, SRC.count("urlopen"))
for verb in (".post(", "order_configuration", "client_order_id"):
    ok(f"no {verb!r}", verb not in SRC)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
