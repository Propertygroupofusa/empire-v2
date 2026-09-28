"""Is every model table actually on the live database?

Both creation paths fail QUIETLY. database.py wraps
Base.metadata.create_all in `except Exception` and prints "failed
(non-critical)"; main.py's foreign-key validator catches per-table
creation errors into a log warning. A model can ship, its table can fail
to appear, and the only evidence is a line in stdout that rotates.

The cost is a silent WRONG ANSWER rather than an error: the new model's
endpoint returns an empty list, which reads exactly like "nothing has
written here yet". Those two are indistinguishable from outside - the
only way to tell them apart for trade_decisions was to filter on one of
its columns and see whether the query 500ed.
"""
import ast
import pathlib

ROUTER = (pathlib.Path(__file__).with_name("routers")
          / "trading_dashboard.py").read_text()


def _fn(name):
    for n in ast.walk(ast.parse(ROUTER)):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return ast.get_source_segment(ROUTER, n)
    raise AssertionError(f"{name} not found")


def _code_only(src):
    """The function with its docstring and # comments removed.

    The docstring below quotes create_all verbatim while explaining why
    this endpoint exists, so a read-only assertion over the raw source
    matches the PROSE and fails on a function that never calls it. Assert
    on what runs.
    """
    tree = ast.parse(src)
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    return ast.unparse(fn)


SRC = _fn("schema_health_endpoint")
CODE = _code_only(SRC)


def test_it_names_the_missing_tables_not_just_a_count():
    """"3 tables missing" sends the reader digging - the exact cost this
    whole family of checks exists to remove."""
    assert '"missing": missing' in SRC
    assert "', '.join(missing)" in SRC


def test_an_unreadable_schema_is_not_reported_as_an_empty_one():
    """A gap is not a zero. Failing to READ the schema must never render
    as every table being absent."""
    assert "status_code=503" in SRC
    assert "not the same as" in SRC


def test_it_compares_against_every_registered_model():
    assert "Base.metadata.sorted_tables" in SRC
    assert "import models" in SRC, "models must be imported to register the tables"


def test_the_comparison_is_case_insensitive():
    """Postgres folds unquoted identifiers to lower case; a case-sensitive
    compare would report every table missing."""
    assert ".lower()" in SRC


def test_it_is_read_only():
    for writes in ("create(", "create_all", "db.add(", "commit()", "DROP"):
        assert writes not in CODE, writes


def test_trade_decisions_is_among_the_tables_it_would_check():
    """The table this endpoint was written to answer a question about."""
    import models
    names = {t.name for t in models.Base.metadata.sorted_tables}
    assert "trade_decisions" in names


def test_the_creation_paths_really_do_swallow_failures():
    """Pinned, because this endpoint is only worth having while that is
    true. If either path starts raising, this assertion should fail and
    somebody should reconsider whether this check is still needed."""
    db = pathlib.Path(__file__).with_name("database.py").read_text()
    assert "create_all() failed (non-critical)" in db
    main = pathlib.Path(__file__).with_name("main.py").read_text()
    assert "Could not create {table.name}" in main
