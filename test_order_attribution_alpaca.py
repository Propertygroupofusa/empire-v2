"""Every Alpaca order must say which code path sent it.

WHY. On 28 Sep, SIX separate orders for META - 0.163475 shares and
$748.91 each, submitted inside 48 MILLISECONDS at 13:30:03Z - put 73%
of a $1,007 account into one name. That concentration is 100% of the
day's realised stock loss.

Nothing anywhere could say what sent them. Every order this bot has
ever placed went out with no client_order_id, so the only way to
attribute one was to reason backwards from its size - which is exactly
how "six of eight bots bought META" was asserted from $122.42 matching
$122.52, and then had to be retracted. A coincidence that matches to
the cent is still a coincidence.

An order that cannot be traced to its caller cannot be debugged, and a
duplicate-order bug is the kind you only find after it has cost money.

NOT the Coinbase plan that was abandoned. Coinbase's fills feed carries
order_id and no client_order_id, so tagging there bought nothing.
Alpaca accepts client_order_id on submit and returns it on the order.
"""
import ast
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "prop_bot.py"), encoding="utf-8") as fh:
    BOT_SRC = fh.read()
BOT_TREE = ast.parse(BOT_SRC)
with open(os.path.join(HERE, "routers", "trading_dashboard.py"), encoding="utf-8") as fh:
    ROUTER_SRC = fh.read()
ROUTER_TREE = ast.parse(ROUTER_SRC)

# The router imports fastapi, which this test environment does not have.
# Exec just the two definitions under test, straight out of the real
# file, so the test still runs the shipped code and not a copy of it.
_ns = {}
for _node in ROUTER_TREE.body:
    if isinstance(_node, ast.Assign) and any(
            getattr(t, "id", "") == "_KNOWN_ORDER_SOURCES" for t in _node.targets):
        exec(ast.get_source_segment(ROUTER_SRC, _node), _ns)
    if isinstance(_node, ast.FunctionDef) and _node.name == "_order_source":
        exec(ast.get_source_segment(ROUTER_SRC, _node), _ns)
_order_source = _ns["_order_source"]
_KNOWN_ORDER_SOURCES = _ns["_KNOWN_ORDER_SOURCES"]


def _fn(tree, src, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node, ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found")


# ── the order carries the tag ──────────────────────────────────────────

def test_the_submitted_order_carries_a_client_order_id():
    _, src = _fn(BOT_TREE, BOT_SRC, "execute_futures_trade")
    assert '"client_order_id": client_order_id' in src


def test_the_tag_starts_with_the_caller_name():
    _, src = _fn(BOT_TREE, BOT_SRC, "execute_futures_trade")
    assert re.search(r'client_order_id = f"\{source\}-', src), \
        "the source must lead the id, or it cannot be read back off a fill"


def test_the_tag_is_unique_per_order():
    """Alpaca rejects a duplicate client_order_id. A tag built only from
    source and contract would collide on the second order of the day."""
    _, src = _fn(BOT_TREE, BOT_SRC, "execute_futures_trade")
    assert "uuid.uuid4()" in src


def test_the_tag_respects_alpacas_length_limit():
    _, src = _fn(BOT_TREE, BOT_SRC, "execute_futures_trade")
    assert "[:128]" in src


def test_an_untagged_call_is_labelled_unlabelled_not_left_blank():
    """A default of None would put anonymous orders back in the account
    silently. 'unlabelled' is visible in the record."""
    fn, _ = _fn(BOT_TREE, BOT_SRC, "execute_futures_trade")
    defaults = {a.arg: d for a, d in
                zip(fn.args.args[-len(fn.args.defaults):], fn.args.defaults)}
    assert isinstance(defaults["source"].value, str)
    assert defaults["source"].value == "unlabelled"


# ── every caller names itself ──────────────────────────────────────────

def _order_calls():
    out = []
    for node in ast.walk(BOT_TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "execute_futures_trade":
            out.append(node)
    return out


def test_there_are_order_calls_to_check():
    assert len(_order_calls()) >= 7


def test_every_order_call_passes_a_source():
    """An untagged call site is an order nobody can attribute - the whole
    condition this exists to end."""
    for call in _order_calls():
        kwargs = {k.arg for k in call.keywords}
        assert "source" in kwargs, (
            f"execute_futures_trade call at line {call.lineno} passes no "
            f"source - its orders would be anonymous")


def test_every_source_is_one_the_reader_knows():
    """A source the dashboard cannot decode reads as unattributable,
    which would be a lie about an order that WAS tagged."""
    for call in _order_calls():
        for kw in call.keywords:
            if kw.arg == "source":
                assert isinstance(kw.value, ast.Constant)
                assert kw.value.value in _KNOWN_ORDER_SOURCES, (
                    f"source {kw.value.value!r} at line {call.lineno} is not "
                    f"in the dashboard's known list")


def test_entry_and_exit_are_told_apart():
    """The six META orders were entries. An attribution that cannot
    distinguish a buy path from a sell path answers nothing."""
    sources = {kw.value.value for call in _order_calls()
               for kw in call.keywords
               if kw.arg == "source" and isinstance(kw.value, ast.Constant)}
    assert any("entry" in s for s in sources)
    assert any("exit" in s for s in sources)


# ── reading it back ────────────────────────────────────────────────────

@pytest.mark.parametrize("source", [s for s in _KNOWN_ORDER_SOURCES])
def test_a_tagged_order_reads_back_as_its_source(source):
    assert _order_source(f"{source}-META-0123456789abcdef") == source


def test_an_untagged_order_is_unattributable_not_defaulted():
    """Orders placed before tagging shipped carry Alpaca's own id. They
    really are unattributable, and saying so is the honest answer - a
    gap is not a zero, and it is certainly not 'entry_pass'."""
    assert _order_source("a1b2c3d4-e5f6-7890-abcd-ef0123456789") is None


def test_a_missing_id_is_unattributable():
    assert _order_source(None) is None
    assert _order_source("") is None


def test_an_unknown_prefix_does_not_invent_a_source():
    assert _order_source("somethingelse-META-abc") is None


def test_the_trades_endpoint_exposes_the_attribution():
    _, src = _fn(ROUTER_TREE, ROUTER_SRC, "get_todays_trades")
    assert '"client_order_id"' in src
    assert "_order_source(" in src


def test_the_trades_endpoint_exposes_submitted_at():
    """The six orders were 48ms apart. Without a submit timestamp that
    is invisible - fill times can be reordered by the venue."""
    _, src = _fn(ROUTER_TREE, ROUTER_SRC, "get_todays_trades")
    assert '"submitted_at"' in src
