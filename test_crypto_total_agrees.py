"""A total must equal its own breakdown.

Caught on a screenshot, 2026-09-28. The grid fleet card read:

    CRYPTO TOTAL
    $8148.74
    $7407.02 in coin - $1131.93 cash in the wallet - -$189.55 open

$7,407.02 + $1,131.93 is $8,538.95. The headline was $390.21 short of the
line printed directly beneath it, because the two were built from
different pairs of fields:

    headline   total_allocated_usd + real_free_cash_usd
    subtitle   deployed_coin_usd   + wallet_cash_usd

The headline pair is not a total of anything - real_free_cash_usd is the
wallet with every unspent branch reserve already subtracted, so adding it
to what those same branches claim removes the reserves twice.
"""
import pathlib
import re

PAGE = pathlib.Path(__file__).with_name("family_tree_dashboard.html").read_text()


def _fn(name):
    """One function body, by brace matching. Comments are skipped first so
    an apostrophe in prose cannot be mistaken for a string delimiter - the
    trap that the sibling test still carries and that this file documents.
    """
    m = re.search(r"^(?:async )?function %s\s*\(" % re.escape(name), PAGE, re.M)
    assert m, f"{name} not found"
    i = PAGE.index("{", m.end() - 1)
    depth, j, in_s, esc, q = 0, i, False, False, ""
    while j < len(PAGE):
        c = PAGE[j]
        if not in_s and PAGE.startswith("//", j):
            j = PAGE.find("\n", j)
            if j == -1:
                break
            continue
        if in_s:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                in_s = False
        elif c in "\"'`":
            in_s, q = True, c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return PAGE[i:j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces in {name}")


def _code_only(src):
    """The function with its // comments stripped, so assertions below are
    about what RUNS. The comments quote the old broken expression verbatim
    to explain it, and would otherwise match every check here."""
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


STRIP = _code_only(_fn("renderStatusStrip"))


def test_the_headline_no_longer_adds_a_claim_to_a_net_of_reserves_cash():
    assert "alloc + cash" not in STRIP, (
        "total_allocated_usd + real_free_cash_usd double-removes the branch "
        "reserves and disagrees with the subtitle printed under it")


def test_the_headline_is_built_from_the_same_backing_the_subtitle_uses():
    assert "backed_usd" in STRIP
    assert "strip-total" in STRIP


def test_the_arithmetic_the_screenshot_caught():
    """The live figures, so the numbers in the docstring above stay honest."""
    claimed, free_cash = 8072.26, 76.48
    coin, wallet_cash = 7407.02, 1131.93
    assert round(claimed + free_cash, 2) == 8148.74          # what was shown
    assert round(coin + wallet_cash, 2) == 8538.95           # what it should be
    assert round(8538.95 - 8148.74, 2) == 390.21             # the gap


def test_an_unreadable_backing_shows_no_total_rather_than_the_old_pair():
    """A gap is not a zero, and it is not a different number either."""
    assert "'\\u2014'" in STRIP or "'—'" in STRIP, (
        "with backing unreadable the headline must show an em-dash, not fall "
        "back to the mismatched pair")


def test_the_subtitle_still_uses_the_real_coin_and_wallet_figures():
    """The subtitle was always right - the fix must not 'align' them by
    breaking the half that was correct."""
    assert "deployed_coin_usd" in STRIP
    assert "wallet_cash_usd" in STRIP
