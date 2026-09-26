"""A signed request must not sign the query string. Coinbase rejects it.

This one bug is why the fleet trades a few times a week instead of
several times a day.

Coinbase CDP signs the URI claim as "METHOD host/path" with NO query
string. Both _build_jwt implementations wrote f"{method} {HOST}{path}"
where path already carried "?product_id=...&limit=1", so every
parameterised request failed its own signature check and came back 401.

The chain that cost:

  get_best_bid_ask()      -> "...product_book?product_id=X&limit=1" -> 401
                          -> returns (None, None) silently
  place_maker_buy()       -> "if bid is None: return None"
  grid_buy()              -> falls through to place_market_buy()
  every fill              -> TAKER at 1.50% round trip, never 0.70% maker
  fee_safe_floor_pct()    -> prices taker, so the floor sits at 1.70%
  every branch's step     -> cannot go below 1.70%, so it sits at 2.00%
  the fleet               -> waits for a 2% move that rarely comes

Two more callers failed the same way and just as quietly:
get_recent_market_trades() (returns None) and the historical/fills
reconciliation that confirms an accepted order really filled.

None of it logged an error. A 401 became (None, None) became a market
order became a 1.50% fee.

Run: python3 test_jwt_uri_claim.py
"""

import ast
import io
import re
import sys

FAILURES = []
CHECKS = 0


def ok(label, cond, detail=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        FAILURES.append(label)


MODULES = ["crypto_btc_compound_bot.py", "crypto_coinbase_bot.py"]

print("test_jwt_uri_claim.py")
print()

# ── 1. the claim itself ────────────────────────────────────────────────
print("-- the uri claim never carries a query string --")
for mod in MODULES:
    src = io.open(mod, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_build_jwt"), None)
    ok(f"{mod}: _build_jwt exists", fn is not None)
    if fn is None:
        continue
    body = ast.get_source_segment(src, fn) or ""
    # Find the uri value however it is spelled, ignoring comments.
    uri_lines = [ln for ln in body.split("\n")
                 if '"uri"' in ln and not ln.strip().startswith("#")]
    ok(f"{mod}: the uri claim is set exactly once", len(uri_lines) == 1, str(uri_lines))
    if uri_lines:
        line = uri_lines[0]
        ok(f"{mod}: the query string is stripped before signing",
           "split('?'" in line or 'split("?"' in line, line.strip())
        ok(f"{mod}: it no longer interpolates the raw path",
           "{path}" not in line, line.strip())

# ── 2. the behaviour, on the real paths that were failing ──────────────
print()
print("-- the real parameterised paths now sign correctly --")

HOST = "api.coinbase.com"


def uri_claim(method, path):
    """Mirror of the fixed expression."""
    return f"{method} {HOST}{path.split('?', 1)[0]}"


REAL_PATHS = [
    ("get_best_bid_ask", "GET",
     "/api/v3/brokerage/product_book?product_id=BTC-USD&limit=1",
     "/api/v3/brokerage/product_book"),
    ("get_book_depth", "GET",
     "/api/v3/brokerage/product_book?product_id=NEAR-USD&limit=25",
     "/api/v3/brokerage/product_book"),
    ("get_recent_market_trades", "GET",
     "/api/v3/brokerage/products/DOGE-USD/ticker?limit=50",
     "/api/v3/brokerage/products/DOGE-USD/ticker"),
    ("order reconciliation", "GET",
     "/api/v3/brokerage/orders/historical/fills?order_id=abc-123",
     "/api/v3/brokerage/orders/historical/fills"),
    ("fee reality", "GET",
     "/api/v3/brokerage/orders/historical/fills?limit=250",
     "/api/v3/brokerage/orders/historical/fills"),
]
for name, method, path, expected in REAL_PATHS:
    got = uri_claim(method, path)
    ok(f"{name}: signs {expected}", got == f"{method} {HOST}{expected}", got)
    ok(f"{name}: the claim contains no '?'", "?" not in got, got)

# A path with no query string must be completely unchanged.
for method, path in [("GET", "/api/v3/brokerage/accounts"),
                     ("POST", "/api/v3/brokerage/orders")]:
    ok(f"unparameterised {path} is untouched",
       uri_claim(method, path) == f"{method} {HOST}{path}")

# ── 3. the request URL still keeps its parameters ──────────────────────
print()
print("-- only the SIGNATURE drops the query, never the request --")
for mod in MODULES:
    src = io.open(mod, encoding="utf-8").read()
    # Every call site sends COINBASE_BASE_URL + path, with the full path.
    sends_full = set(re.findall(r"COINBASE_BASE_URL \+ (\w+)", src))
    # The point is that the REQUEST carries an unstripped path variable -
    # any *_path name qualifies. The first version of this check listed the
    # names it happened to know and failed on "detail_path", which is a
    # perfectly ordinary one.
    ok(f"{mod}: requests are still built from the full path",
       len(sends_full) > 0 and all(v.endswith("path") or v == "url" for v in sends_full),
       str(sends_full))
    ok(f"{mod}: no request URL is built from a stripped path",
       "COINBASE_BASE_URL + path.split" not in src)
    ok(f"{mod}: nothing strips '?' outside the jwt builder",
       src.count("split('?'") <= 1, str(src.count("split('?'")))

# ── 4. the silent-failure path that hid this ───────────────────────────
print()
print("-- the failure that hid it --")
src = io.open("crypto_btc_compound_bot.py", encoding="utf-8").read()
tree = ast.parse(src)
fn = next(n for n in ast.walk(tree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
          and n.name == "get_best_bid_ask")
body = ast.get_source_segment(src, fn)
ok("get_best_bid_ask still fails closed rather than inventing a price",
   "return None, None" in body)
maker = ast.get_source_segment(src, next(
    n for n in ast.walk(tree)
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    and n.name == "place_maker_buy"))
ok("place_maker_buy still refuses to price against an unreadable book",
   "if bid is None" in maker)
ok("a 401 on the book no longer silently means 'no maker order'",
   "split('?'" in src)

print()
print(f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("All checks passed.")
