"""A truncated body is a 200, and it must never render as a zero.

WHAT HAPPENED, 2026-09-28 00:46Z

/grid-status answered HTTP 200 with the JSON cut off mid-object at 18,615
bytes of ~108,000. res.ok was true. The status was not in
TRANSIENT_STATUSES. res.json() threw a SyntaxError carrying no .transient,
so apiGet's `if (!e.transient) throw e` rethrew it on the spot - the single
most retryable failure there is was the one case that never retried. The
very next attempt returned the full 108KB.

TWO RULES THIS FILE PROTECTS

1. A cut-off read RETRIES. It is transient by definition - the server
   answered, the bytes were lost in transit.
2. A cut-off read still REACHES THE CALLER AS AN ERROR if it keeps
   failing. It must never be swallowed into an empty object that renders
   as $0.00. A gap is not a zero.

And the rule that was already there and must survive: a 4xx never retries.
"""
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = open(os.path.join(HERE, "family_tree_dashboard.html"), encoding="utf-8").read()


def block(start, end):
    i = PAGE.index(start)
    return PAGE[i:PAGE.index(end, i)]


ONCE = block("async function _apiGetOnce", "function fmtUsd")


def test_the_response_body_is_parsed_where_failure_can_be_classified():
    # res.json() returns a promise that rejects with an unclassifiable
    # SyntaxError. Parsing from text is what makes the catch possible.
    assert "await res.text()" in ONCE
    assert "JSON.parse(text)" in ONCE
    assert re.search(r"return\s+res\.json\(\)", ONCE) is None


def test_a_parse_failure_is_marked_transient_so_apiget_retries_it():
    m = re.search(r"catch\s*\(parseErr\)\s*\{(.+?)\n    \}", ONCE, re.S)
    assert m, "the parse catch block moved"
    body = m.group(1)
    assert "transient = true" in body.replace(" ", " ")
    assert "throw err" in body


def test_a_truncated_read_is_thrown_not_returned():
    """The failure must propagate. A `return {}` or `return null` here is
    exactly the bug this guards - it would render as $0.00."""
    m = re.search(r"catch\s*\(parseErr\)\s*\{(.+?)\n    \}", ONCE, re.S)
    body = m.group(1)
    assert not re.search(r"\breturn\b", body), "a truncated read must never return a value"


def test_the_error_says_it_is_a_gap_not_a_zero():
    m = re.search(r"catch\s*\(parseErr\)\s*\{(.+?)\n    \}", ONCE, re.S)
    assert "not a zero" in m.group(1)


def test_the_byte_count_is_reported_so_the_reader_can_see_it_was_cut_off():
    m = re.search(r"catch\s*\(parseErr\)\s*\{(.+?)\n    \}", ONCE, re.S)
    assert "text.length" in m.group(1)


# ----------------------------------------- the pre-existing rules survive
def test_a_4xx_still_never_retries():
    assert "const TRANSIENT_STATUSES = [502, 503, 504];" in PAGE
    for bad in ("400", "401", "403", "404", "422"):
        assert bad not in "502, 503, 504"


def test_apiget_still_retries_exactly_once():
    wrapper = block("async function apiGet(path, timeoutMs)", "async function _apiGetOnce")
    assert wrapper.count("_apiGetOnce(path, timeoutMs)") == 2


def test_the_retry_path_is_reachable_from_a_parse_failure():
    """The wrapper gates its retry on e.transient. If the parse catch set
    any other flag, the retry would still never happen - which is the
    original bug wearing a different hat."""
    wrapper = block("async function apiGet(path, timeoutMs)", "async function _apiGetOnce")
    assert "!e.transient" in wrapper.replace(" ", "")
    m = re.search(r"catch\s*\(parseErr\)\s*\{(.+?)\n    \}", ONCE, re.S)
    assert re.search(r"err\.transient\s*=\s*true", m.group(1))


# ------------------------------------------------------------ mutation
@pytest.mark.parametrize("old,new", [
    ("err.transient = true;\n        err.truncated = true;",
     "err.truncated = true;"),                      # drop the transient flag
    ("const text = await res.text();\n    try {\n        return JSON.parse(text);",
     "return res.json(); // \n    try {\n        return JSON.parse(text);"),
])
def test_breaking_the_guard_fails_these_tests(old, new):
    assert PAGE.count(old) == 1, "mutation anchor moved - this test is blind"
    mutated = PAGE.replace(old, new, 1)
    i = mutated.index("async function _apiGetOnce")
    once = mutated[i:mutated.index("function fmtUsd", i)]
    broken = (re.search(r"return\s+res\.json\(\)", once) is not None
              or not re.search(r"err\.transient\s*=\s*true",
                               re.search(r"catch\s*\(parseErr\)\s*\{(.+?)\n    \}",
                                         once, re.S).group(1)))
    assert broken, "the mutation left the guard intact - the test cannot fail"
