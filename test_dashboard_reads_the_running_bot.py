"""The live dashboard was wired to a bot that is not trading.

Three panels on /live-dashboard-data, one cause. The handler read
crypto_coinbase_bot - the OLD bot - while every dollar in the account
is traded by the grid fleet:

  positions  ->  {"open": [], "count": 0, "max": 3}
                 while 21 of 23 grid branches held coin against
                 $7,885.21 of allocated capital, and "max": 3 was a
                 literal beside a 23-branch fleet
  pairs      ->  AVAX, DOGE, MATIC - none of them a branch - from that
                 bot's CRYPTO_PAIRS, while most coins the fleet holds
                 were missing
  status     ->  getattr(crypto_coinbase_bot, 'BOT_RUNNING', True), and
                 that module defines no BOT_RUNNING AT ALL (grep count:
                 0), so the call could only ever return its default.
                 The indicator was a green light soldered on.

The last one is the worst of the three and the reason this file exists:
a status light that cannot go out is not a status light. It is the
483.00 literal wearing a different shape - a value nobody measured,
displayed as though someone had.

These tests are static: they read the handler's source and assert it
does not reach for the dormant module. A behavioural test would need
live credentials and a database, which is exactly why the original bug
survived - nothing in the suite could execute that handler.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
HANDLER = "get_live_dashboard_data_v2"
ROUTER = os.path.join(ROOT, "routers", "trading_dashboard.py")

# Names on the dormant bot that a live panel must not be built from.
DORMANT_ATTRS = {"open_crypto_positions", "CRYPTO_PAIRS", "BOT_RUNNING"}


def _handler_source():
    tree = ast.parse(open(ROUTER, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == HANDLER:
            return node
    raise AssertionError(f"{HANDLER} not found in {ROUTER}")


def _attribute_reads(fn):
    """Every `<name>.<attr>` read inside the handler."""
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            out.append((node.value.id, node.attr, node.lineno))
    return out


def _getattr_calls(fn):
    out = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and node.args):
            target = node.args[0].id if isinstance(node.args[0], ast.Name) else "?"
            name = node.args[1].value if isinstance(node.args[1], ast.Constant) else "?"
            has_default = len(node.args) > 2
            out.append((target, name, has_default, node.lineno))
    return out


def test_the_handler_does_not_read_the_dormant_bots_state():
    fn = _handler_source()
    bad = [(o, a, ln) for o, a, ln in _attribute_reads(fn) if a in DORMANT_ATTRS]
    bad += [(o, n, ln) for o, n, has_d, ln in _getattr_calls(fn) if n in DORMANT_ATTRS]
    assert not bad, (
        "the live dashboard is reading state off a bot that is not trading: "
        + "; ".join(f"{o}.{a} at line {ln}" for o, a, ln in bad))


def test_the_bot_status_is_not_a_getattr_default():
    """getattr(mod, 'BOT_RUNNING', True) on a module with no such
    attribute is a hardcoded True. Any getattr in this handler whose
    default is a bare True is the same trap."""
    fn = _handler_source()
    offenders = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "getattr" and len(node.args) > 2):
            d = node.args[2]
            if isinstance(d, ast.Constant) and d.value is True:
                offenders.append(node.lineno)
    assert not offenders, (
        f"getattr(..., default=True) at line(s) {offenders}: a status that "
        f"defaults to True can never report a problem")


def test_the_position_count_ceiling_is_not_a_literal():
    """"max": 3 sat beside a 23-branch fleet.

    Walks the AST rather than grepping the text. The first draft used a
    regex over the handler's source and failed on the COMMENT that
    explains this very fix - which is the standing rule in this repo
    ("never match on source containing comments; strip them or parse
    AST") broken in the same change that cites it. A dict key is a
    syntax node; find it as one.
    """
    offenders = []
    for node in ast.walk(_handler_source()):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value == "max"
                    and isinstance(v, ast.Constant) and isinstance(v.value, (int, float))):
                offenders.append((v.value, v.lineno))
    assert not offenders, (
        '"max" is a hardcoded literal ' +
        "; ".join(f"{val} at line {ln}" for val, ln in offenders) +
        " - it must come from the fleet")


def test_that_ceiling_test_can_actually_fail():
    # A static check that cannot fail is decorative. Prove the AST walk
    # finds the literal it is looking for, on the original shape.
    tree = ast.parse('def f():\n    return {"positions": {"open": [], "max": 3}}\n')
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "max"
                        and isinstance(v, ast.Constant) and isinstance(v.value, (int, float))):
                    found.append(v.value)
    assert found == [3], found


def test_the_comment_explaining_the_fix_does_not_trip_the_checks():
    # The regression that prompted the AST rewrite: the word "max": 3
    # appears in a comment in the handler, and must not be read as code.
    src = open(ROUTER, encoding="utf-8").read()
    assert '"max": 3 was hardcoded' in src, "the explanatory comment is gone"
    # and the check above still passes with it present
    test_the_position_count_ceiling_is_not_a_literal()


def test_the_handler_asks_the_grid_for_its_branches():
    # The positive half: it must read the fleet that IS trading, not
    # merely avoid the one that is not.
    fn = _handler_source()
    reads = {(o, a) for o, a, _ in _attribute_reads(fn)}
    assert ("crypto_grid_bot_module", "get_grid_status") in reads, (
        "the handler no longer reads the dormant bot, but it does not read "
        "the grid either - the panels would just be empty")


def test_all_three_statuses_are_reachable_in_the_source():
    """active / stalled / unknown. If the source can only ever produce
    one of them the indicator is decorative again."""
    src = ast.get_source_segment(open(ROUTER, encoding="utf-8").read(), _handler_source())
    for state in ("active", "stalled", "unknown"):
        assert f'"{state}"' in src, f"the handler can never report {state!r}"


def test_the_page_renders_the_unknown_state_distinctly():
    """The payload gaining a third state is useless if the page folds it
    back into two - "cannot tell" would render identically to "dead"."""
    html = open(os.path.join(ROOT, "live_trading_dashboard.html"), encoding="utf-8").read()
    assert "'unknown'" in html, "the page never checks for the unknown state"
    assert ".status-badge.unknown" in html and ".bot-status-indicator.unknown" in html, \
        "the unknown state has no styling of its own, so it renders as inactive"


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"  FAIL {name}: {e}")
    sys.exit(1 if fails else 0)


# ── the Alpaca half: a hardcoded venue URL beside a configurable one ───
#
# /live-dashboard-data reported buying_power 0, equity 0, daily_profit 0
# and total_profit 0 at 14:52Z on 2026-09-28, while /status reported
# equity $980.18, cash $274.26, 7 trades today and 1 open position on
# the SAME account.
#
# Cause: the handler built its own request to the literal
# "https://paper-api.alpaca.markets/v2/account", while
# _fetch_alpaca_account - the helper /status goes through - reads
# ALPACA_BASE_URL, which is configurable. Point that at the live
# endpoint and the two paths read different accounts; the handler's
# `except` swallowed the mismatch and its zeros went to the page as
# figures.
#
# Third instance in one day of a literal standing in for a real value,
# after 483.00 and getattr(..., 'BOT_RUNNING', True). The generalisable
# guard is the one below: no venue hostname may be written into this
# router as a literal when a configured base URL exists for it.

# Only hosts a CONFIGURED base actually covers. The first draft of this
# list said "alpaca.markets" and "api.exchange.coinbase.com" too, and
# went red on a dozen pre-existing lines - but those are different
# services: data.alpaca.markets is market data and
# api.exchange.coinbase.com is the public price host, neither of which
# ALPACA_BASE_URL or COINBASE_HOST stands in for. A rule that fires on
# code it has no opinion about is a rule nobody can act on. The rule was
# wrong, not the code.
#
# What remained after narrowing was four real instances of
# "https://api.coinbase.com" written into this router while
# account_census.COINBASE_HOST and crypto_coinbase_bot.COINBASE_BASE_URL
# both exist - and three of them sign with account_census._auth_headers,
# whose JWT `uri` claim embeds the host. Set COINBASE_HOST and the
# signature would be for one host while the request went to another.
# They were fixed rather than exempted.
VENUE_HOSTS = ("paper-api.alpaca.markets", "api.alpaca.markets", "api.coinbase.com")


def _string_constants(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value, node.lineno


def test_no_venue_url_is_hardcoded_where_a_configured_base_exists():
    """Parses the AST, so the comment above - which names the offending
    URL on purpose - cannot trip it. That regex mistake was made once
    already today, on the "max" literal check."""
    src = open(ROUTER, encoding="utf-8").read()
    tree = ast.parse(src)

    # Which base-URL names the module defines at module level.
    configured = {t.id for node in tree.body if isinstance(node, ast.Assign)
                  for t in node.targets
                  if isinstance(t, ast.Name) and t.id.endswith("_BASE_URL")}
    assert configured, "no *_BASE_URL is configured in this router at all"

    # Module-level assignments are allowed to hold the default.
    module_level_lines = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for c, ln in _string_constants(node):
                module_level_lines.add(ln)

    offenders = []
    for value, lineno in _string_constants(tree):
        if lineno in module_level_lines:
            continue
        if any(host in value for host in VENUE_HOSTS) and value.startswith("http"):
            offenders.append((value, lineno))
    assert not offenders, (
        "venue URL hardcoded inside a function while a configured base exists "
        f"({sorted(configured)}): "
        + "; ".join(f"{v!r} at line {ln}" for v, ln in offenders))


def test_the_live_dashboard_reads_alpaca_through_the_shared_helper():
    fn = _handler_source()
    calls = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_fetch_alpaca_account" in calls, (
        "the handler does not go through _fetch_alpaca_account, so it can "
        "read a different account than /status does")


def test_the_alpaca_pnl_is_not_a_hardcoded_zero():
    """daily_profit and total_profit were literal 0 behind a TODO. On a
    live account that reads as a flat day, not as 'nobody computed it'."""
    fn = _handler_source()
    offenders = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and k.value in ("daily_profit", "total_profit",
                                                            "growth_percent", "equity",
                                                            "buying_power")
                    and isinstance(v, ast.Constant) and isinstance(v.value, (int, float))):
                offenders.append((k.value, v.value, v.lineno))
    assert not offenders, (
        "hardcoded numeric account figure(s): "
        + "; ".join(f"{k}={v} at line {ln}" for k, v, ln in offenders))


def test_the_page_renders_a_null_pnl_as_not_computed():
    html = open(os.path.join(ROOT, "live_trading_dashboard.html"), encoding="utf-8").read()
    assert "hasDaily" in html and "hasTotal" in html, \
        "the card does not distinguish a null P&L from a zero one"
    assert "account.daily_profit !== null" in html
