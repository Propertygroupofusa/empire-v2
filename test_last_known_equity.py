"""A failed poll shows the last real number with its age, never a blank.

The combined-progress panel refused - correctly - to caption one leg as
the combined total. But it also threw away the figure it had sixty
seconds earlier: the gauge fell to "Combined total unavailable this
poll" and the legend to a bare em dash. On a phone dropping in and out
of signal that is most reads, and the owner sees an empty panel over an
account that is fine.

Last known WITH ITS AGE is the correct middle: stale and labelled stale
beats blank, and it is still never a fabricated zero. Nothing is kept
for a field that has never once been read - a gap stays a gap.

This is display-only. It changes no endpoint and no trading behaviour.
"""
import re

HTML = open("family_tree_dashboard.html").read()
SCRIPTS = re.findall(r"<script[^>]*>(.*?)</script>", HTML, re.S)


def _fn(name):
    """The function's real body, by brace matching.

    A fixed-width slice was the first attempt and it lied in BOTH
    directions: too short for renderCombinedProgress (the split legend
    sits ~35 lines in, so the assertions on it failed against code that
    was there) and too long for _lastKnownStr (it ran into unrelated
    code and reported a `|| 0` that belonged to someone else). Three
    false results from one careless helper.
    """
    i = HTML.index("function %s(" % name)
    j = HTML.index("{", i)
    depth, k = 0, j
    while k < len(HTML):
        if HTML[k] == "{":
            depth += 1
        elif HTML[k] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:k + 1]
        k += 1
    raise AssertionError("unbalanced braces reading %s" % name)


# --- the parse check this file must not break -----------------------------

def test_exactly_one_script_block():
    # test_maker_expiry_panel.py checks the dashboard's JS parses ONLY when
    # there is a single block: `_parses(_scripts[0]) if len(_scripts)==1`.
    # A second <script> does not fail that test, it silently degrades it to
    # COULD-NOT-CHECK - so the new code had to go INSIDE the existing block.
    assert len(SCRIPTS) == 1, f"{len(SCRIPTS)} script blocks; the parse check goes blind"


# --- last known, with its age ---------------------------------------------

def test_the_rule_lives_in_one_helper():
    src = _fn("_lastKnownStr")
    assert "_remember(key, live)" in src
    assert "fmtUsd(live)" in src, "a live reading must still win"
    assert "as of ' + _fmtAge(seen.at)" in src, "a stale figure must carry its age"
    assert "return null;" in src, "never-read must stay a gap, not a number"


def test_only_a_real_number_is_remembered():
    src = _fn("_remember")
    assert "value != null && isFinite(value)" in src, \
        "null or NaN would be stored and later shown as a real reading"


def test_zero_is_never_invented():
    # The one failure this panel must never have: a poll that did not
    # happen rendering as $0.00 beside real money.
    for name in ("_lastKnownStr", "_remember", "_fmtAge"):
        src = _fn(name)
        assert "|| 0" not in src and "?? 0" not in src, f"{name} defaults to zero"


def test_age_is_reported_in_human_units():
    src = _fn("_fmtAge")
    for unit in ("'s ago'", "'m ago'", "'h ago'", "'d ago'"):
        assert unit in src, f"missing {unit}"
    assert "Math.max(0," in src, "a clock skew must not print a negative age"


# --- both places the panel used to go blank -------------------------------

def test_the_gauge_uses_it():
    src = _fn("renderCombinedProgress")
    assert "_lastKnownStr('combined', data.combined_equity)" in src
    assert "last good read, this poll failed" in src
    # the original refusal survives: one leg is never captioned as the total
    assert "surviving leg alone is not the combined number" in src


def test_the_split_legend_uses_it():
    src = _fn("renderCombinedProgress")
    assert "_lastKnownStr('alpaca', data.alpaca_equity)" in src
    assert "_lastKnownStr('crypto', data.crypto_equity)" in src
    assert "data.alpaca_error ? 'unavailable'" in src, \
        "the never-read case must still read 'unavailable', not a stale figure"


def test_each_field_is_remembered_separately():
    # Alpaca succeeding must not backfill Coinbase's missing figure.
    src = _fn("renderCombinedProgress")
    assert src.count("_lastKnownStr(") == 3
    decl = HTML[HTML.index("const _lastGood ="):HTML.index("const _lastGood =") + 120]
    for k in ("combined", "alpaca", "crypto"):
        assert k in decl, f"{k} has no slot of its own"


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
            except Exception as e:
                fails += 1; print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{fails} failure(s)")
    sys.exit(1 if fails else 0)
