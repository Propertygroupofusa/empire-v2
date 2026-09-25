"""Every Coinbase CDP JWT in this repo must be signed the same, correct way.

The bill for not having this test, 2026-09-25:

    capital_census.py reported "Coinbase: UNKNOWN / HTTP 401 - credentials
    rejected" for weeks. That was read as the API key being revoked, and
    the account owner was asked more than once to reissue it.

    The key was fine. It traded and read balances all day through
    crypto_btc_compound_bot - a real market sell of 0.00684381 BTC filled,
    and get_real_free_cash_usd() returned a live wallet balance. What was
    broken was the census's own signature: it put the query string inside
    the JWT 'uri' claim.

A 401 says "this signature did not validate". It does NOT say which half
was wrong, and the instinct is always to blame the credential. Three
separate hand-rolled implementations in this repo were malformed:

    capital_census.py          signed the query string into 'uri'
    routers/trading_dashboard  'uri' had no method and no host; iss was
                               "cdp_service" instead of "cdp"
    routers/payments.py        'uri' had no host, and the JWT header was
                               missing both "kid" and "nonce"

None of them could ever have authenticated. Each was written separately
instead of reusing the one that works, so each invented its own bug.

2026-09-25 UPDATE - the "reference" had the bug too.

This file fixed three hand-rolled implementations and pointed them at
crypto_btc_compound_bot._build_jwt as the one that worked. It did not.
Both bot modules interpolated the raw `path` into the claim, and every
caller that passed parameters - get_best_bid_ask, get_book_depth,
get_recent_market_trades, the historical/fills reconciliation - failed
its signature check and returned 401.

The cost was invisible because each caller failed closed and quietly:
get_best_bid_ask returned (None, None), place_maker_buy opens with
"if bid is None: return None", and grid_buy then fell through to a market
order. Not one maker order was ever placed. Every fill paid the 1.50%
taker round trip instead of 0.70% maker, which held the fee floor at
1.70%, which held the grid step at 2.00%.

THE CONTRACT (crypto_btc_compound_bot._build_jwt is the reference):
    payload["uri"]  == "<METHOD> <host><path>"   and NEVER a query string
    payload["iss"]  == "cdp"
    headers         carry "kid" (the key name) and a per-request "nonce"

Query parameters go on the REQUEST URL, never into the signature.

Run: python3 test_coinbase_jwt.py
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# Files that build a Coinbase CDP JWT. scripts/ are one-off operator tools
# and already use the reference f"{method} {HOST}{path}" form.
TARGETS = [
    "capital_census.py",
    "routers/trading_dashboard.py",
    "routers/payments.py",
    "crypto_btc_compound_bot.py",
    "crypto_coinbase_bot.py",
]


def uri_claim_strings(path):
    """Every literal/f-string assigned to a 'uri' key in a dict literal."""
    src = open(os.path.join(HERE, path), encoding="utf-8").read()
    found = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "uri":
                found.append(ast.unparse(v))
    return found


# --- the 'uri' claim ------------------------------------------------------
for path in TARGETS:
    for uri in uri_claim_strings(path):
        label = f"{path}: uri {uri[:52]}"
        # A query string in the signed uri is THE bug that caused the 401.
        #
        # This check used to be `"?" not in uri`, scanning the SOURCE text.
        # That could only ever catch a query string written as a literal -
        # and the one that actually cost money arrived at RUNTIME, through
        # the `path` variable: get_best_bid_ask() passes
        # "...product_book?product_id=X&limit=1" into f"{method} {HOST}{path}".
        # The source held no "?", so this test passed for months while every
        # parameterised request 401'd, every maker order fell through to a
        # market order, and the fleet paid taker on every single fill.
        #
        # So the claim is clean if it carries no literal query string AND
        # either interpolates no raw path at all, or explicitly strips one.
        strips_query = "split('?'" in uri or 'split("?"' in uri
        interpolates_path = "{path}" in uri
        no_literal_query = "?" not in uri.replace("split('?'", "").replace('split("?"', "")
        ok(f"{label} - no literal query string", no_literal_query)
        ok(f"{label} - a runtime path is stripped before signing",
           strips_query or not interpolates_path)
        # Must carry a method. Either literal or an f-string {method} slot.
        has_method = ("{method}" in uri
                      or re.search(r"\b(GET|POST|DELETE|PUT)\b", uri) is not None)
        ok(f"{label} - has an HTTP method", has_method)
        # Must carry the host. Either a {HOST}-style slot or the literal host.
        has_host = ("HOST" in uri.upper().replace("SIGN_PATH", "")
                    or "api.coinbase.com" in uri
                    or "{host}" in uri)
        ok(f"{label} - has the api host", has_host)

# --- the issuer -----------------------------------------------------------
# Checked against CODE only. The comment explaining the fix names the wrong
# value, so scanning raw source matches the explanation rather than the bug.
def _iss_values(path):
    src = open(os.path.join(HERE, path), encoding="utf-8").read()
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "iss"
                        and isinstance(v, ast.Constant)):
                    out.append(v.value)
    return out


for path in TARGETS:
    for iss in _iss_values(path):
        ok(f"{path}: iss is 'cdp', not {iss!r}", iss == "cdp")

# --- the JWT header: kid + nonce -----------------------------------------
# A kid-less or nonce-less CDP JWT is rejected. Checked per ENCLOSING
# FUNCTION, not by a forward window from the call: the reference
# implementation builds `headers = {...}` on the line BEFORE encode() and
# passes it by name, which a forward-only scan wrongly flags.
def encode_sites(path):
    """(function name, function source) for each fn containing a jwt encode."""
    src = open(os.path.join(HERE, path), encoding="utf-8").read()
    tree = ast.parse(src)
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.unparse(fn)
        if re.search(r"\b(py)?jwt\.encode\(", body):
            out.append((fn.name, body))
    return out


for path in TARGETS:
    for fn_name, body in encode_sites(path):
        ok(f"{path}:{fn_name}() sets kid in the JWT header", '"kid"' in body or "'kid'" in body)
        ok(f"{path}:{fn_name}() sets a nonce", "nonce" in body)

# --- the specific regressions, named ------------------------------------
def code_only(path):
    """Source with comments and docstrings stripped.

    The comments in these files quote the OLD broken values to explain why
    they changed. Scanning raw source therefore matches the bug's own
    description and fails on a correct file - which this test did on its
    first run, exactly the mistake it exists to catch.
    """
    src = open(os.path.join(HERE, path), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef, ast.Module))
                and ast.get_docstring(node)):
            node.body = node.body[1:] or [ast.Pass()]
    return ast.unparse(tree)  # unparse drops comments entirely


census = open(os.path.join(HERE, "capital_census.py"), encoding="utf-8").read()
census_code = code_only("capital_census.py")
ok("census signs a bare SIGN_PATH constant",
   "SIGN_PATH = '/api/v3/brokerage/accounts'" in census_code
   or 'SIGN_PATH = "/api/v3/brokerage/accounts"' in census_code)
ok("census never signs a query string (code, not comments)",
   not re.search(r"uri['\"]:\s*f?['\"][^'\"]*\?", census_code))
ok("census still REQUESTS with the query string",
   "?limit=250" in census_code)
ok("census follows pagination rather than stopping at page one",
   "has_next" in census and "cursor" in census)

dash = open(os.path.join(HERE, "routers/trading_dashboard.py"), encoding="utf-8").read()
ok("dashboard uri now names the host",
   "GET api.coinbase.com/api/v3/brokerage/accounts" in dash)

pay = open(os.path.join(HERE, "routers/payments.py"), encoding="utf-8").read()
ok("payments uri now names the host",
   "GET api.coinbase.com/api/v3/brokerage/accounts" in pay)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
