"""The decisions summary counted the page and called it the window.

Live at 2026-09-28T10:19Z, the same 24-hour window, three limits:

    /mandates/decisions?hours=24&limit=3    -> "0 of 3 decision(s)..."
    /mandates/decisions?hours=24&limit=25   -> "0 of 25 decision(s)..."
    /mandates/decisions?hours=24&limit=200  -> "0 of 89 decision(s)..."

89 is the true count. The other two are page sizes wearing a window
label, and the endpoint's default limit is 100 - so any window with more
than 100 decisions silently reports 100.

top_blockers was worse than the total. It was tallied over the page too,
so the rule that refused most often across a day could be under-counted,
or a blocker that happens to fill the newest page could be promoted to
the top of a list a reader uses to decide what to fix.

I reported "60 / 82 / 83 decisions in 24 hours" from this endpoint
earlier today. Those happened to be near-complete pages, but the number
I quoted did not mean what I said it meant.

THE FIX. The summary is computed over the whole window, from a narrow
query of just the two columns it needs; the page is what it always was.
When the window is larger than the safety cap, the summary says so
rather than quietly counting a slice.
"""
import decision_log as dl


def row(admitted=False, rules="kill_condition"):
    return {"admitted": admitted, "failed_rules": rules}


WINDOW = [row() for _ in range(89)]


def test_the_summary_counts_what_it_was_given_not_a_page_of_it():
    s = dl.summarise(WINDOW)
    assert s["decisions"] == 89
    assert s["refused"] == 89
    assert "89 decision(s)" in s["detail"]


def test_the_page_size_is_reported_separately_from_the_window_count():
    s = dl.summarise(WINDOW, returned=25)
    assert s["decisions"] == 89, "the window count must not become the page size"
    assert s["returned"] == 25


def test_a_page_smaller_than_the_window_says_so_in_words():
    s = dl.summarise(WINDOW, returned=25)
    assert "25" in s["detail"] and "89" in s["detail"]
    assert "shown" in s["detail"].lower() or "listed" in s["detail"].lower()


def test_a_full_page_does_not_add_noise_about_truncation():
    s = dl.summarise(WINDOW, returned=89)
    assert s["returned"] == 89
    assert "shown" not in s["detail"].lower()


def test_blockers_are_tallied_over_the_window_not_the_page():
    rows = [row(rules="kill_condition") for _ in range(80)] + \
           [row(rules="rsi_out_of_band") for _ in range(9)]
    s = dl.summarise(rows, returned=9)
    assert s["top_blockers"][0] == {"rule": "kill_condition", "count": 80}


def test_a_capped_count_is_named_as_a_floor_not_reported_as_the_total():
    """A window bigger than the safety cap must never read as an exact
    total - that is the same lie one layer down."""
    s = dl.summarise(WINDOW, returned=25, capped=True)
    assert s["capped"] is True
    assert "at least" in s["detail"].lower()


def test_an_uncapped_summary_says_nothing_about_caps():
    s = dl.summarise(WINDOW, returned=89)
    assert s.get("capped") is False
    assert "at least" not in s["detail"].lower()


def test_an_empty_window_is_still_its_own_finding():
    s = dl.summarise([])
    assert s["decisions"] == 0
    assert "itself a finding" in s["detail"]


def test_the_endpoint_summarises_the_window_and_pages_the_rows():
    """Asserted on the parsed endpoint, not on a docstring claiming it."""
    import ast
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "routers", "trading_dashboard.py")
    src = open(p, encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "mandate_decisions_endpoint")
    body = ast.get_source_segment(src, fn)
    # The summary must not be built from the limited page.
    assert "summarise(dicts)" not in body, (
        "the summary is still computed from the limited page")
    assert "returned=" in body, "the page size must be passed through"
    assert ".limit(" in body, "the page itself must still be limited"
