"""The census, exercised - because the bug it exists to fix passed every
source-reading test that already existed.

capital_census.py is CORRECT when you read it. It prices each coin, and an
unpriceable one is reported with a value of None and excluded "rather than
guessed at". The docstring says so, the comment says so, and the logic
matches. Its live output was:

    coins held    $0.00
      ZEC   1.82953300  (no price - not counted)

for an $11,219 account, because it prices from api.exchange.coinbase.com
while its balances come from api.coinbase.com - and only the balance host
answers from production. Every price returned None, every asset took the
honest-looking exclusion branch, and the total came out zero.

No test that reads source catches that. These execute the census against a
fake venue and assert on what it RETURNS.

Run: python3 test_account_census.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_passed = _failed = 0


def ok(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


import account_census as C
C._auth_headers = lambda m, p: {"Authorization": "test"}


class Resp:
    def __init__(self, payload, status=200): self._p, self.status = payload, status
    async def json(self): return self._p
    async def text(self): return "err"
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class Venue:
    """Balances on one host, prices on another - the shape that caused this."""
    def __init__(self, held, prices, price_host_up=True, pages=1):
        self.held, self.prices = held, prices
        self.price_host_up, self.pages = price_host_up, pages
        self.page_calls = 0
    def get(self, url, **kw):
        if "/accounts" in url:
            self.page_calls += 1
            n = self.page_calls
            accts = [{"currency": c, "available_balance": {"value": str(v)}}
                     for c, v in self.held.items()] if n == 1 else []
            return Resp({"accounts": accts,
                         "has_next": n < self.pages,
                         "cursor": "next" if n < self.pages else ""})
        if "/products/" in url:
            if not self.price_host_up:
                return Resp({}, status=503)
            asset = url.split("/products/")[1].split("-")[0]
            p = self.prices.get(asset)
            return Resp({"price": str(p)} if p else {}, status=200 if p else 404)
        return Resp({}, status=404)


HELD = {"USD": 79.30, "ZEC": 1.829533, "XRP": 1558.65, "SHIB": 180658036.99, "GAL": 12.0}
PRICES = {"ZEC": 1532.24, "XRP": 1.5489, "SHIB": 0.00000593}   # GAL unpriceable


def run(v, tracked=None):
    return asyncio.run(C.census(v, tracked_usd=tracked))


print("\nit totals the WHOLE account, not the part with a branch")
r = run(Venue(HELD, PRICES))
ok("it returns a result", r.get("available") is True, str(r)[:200])
ok("every held asset is seen", r["assets_held"] == 5, str(r["assets_held"]))
ok("the total is the real one, not zero",
   abs(r["total_usd"] - (79.30 + 1.829533*1532.24 + 1558.65*1.5489 + 180658036.99*0.00000593)) < 1.0,
   str(r["total_usd"]))
ok("USD is counted as cash, not as a coin", r["cash_usd"] == 79.30, str(r["cash_usd"]))
ok("and coins are counted apart from it", r["coin_usd"] > 5000, str(r["coin_usd"]))

print("\nan unpriced asset is a hole in the answer, never a zero")
ok("it is reported, not dropped silently", r["assets_unpriced"] == 1, str(r["assets_unpriced"]))
ok("and named", r["unpriced"][0]["asset"] == "GAL", str(r["unpriced"]))
ok("with its units, so it can be priced by hand", r["unpriced"][0]["units"] == 12.0)
ok("a warning is attached to the RESULT, not left to the reader",
   "warning" in r and "NOT in total_usd" in r["warning"], r.get("warning"))
ok("and it says the real total is higher", "higher than the figure shown" in r["warning"])

print("\nthe failure that produced $0.00 on an $11,219 account")
dead = run(Venue(HELD, PRICES, price_host_up=False))
ok("with the price host down, nothing is priced",
   dead["assets_priced"] == 1,     # only USD, which is par
   str(dead["assets_priced"]))
ok("it does NOT report a confident total of zero",
   "warning" in dead and dead["assets_unpriced"] == 4, str(dead.get("warning"))[:120])
ok("stablecoins are still counted - a broken ticker must not hide real cash",
   dead["cash_usd"] == 79.30, str(dead["cash_usd"]))
ok("and the coin total is honestly zero, with four assets flagged missing",
   dead["coin_usd"] == 0.0 and dead["assets_unpriced"] == 4)

print("\nthe GAP is the product")
r = run(Venue(HELD, PRICES), tracked=572.47)
ok("it reports what the bot-facing figure believes", r["tracked_usd"] == 572.47)
ok("and the untracked remainder", r["untracked_usd"] > 5000, str(r["untracked_usd"]))
ok("as a share of the account", r["tracked_share_pct"] < 10, str(r["tracked_share_pct"]))
ok("and says in words that nothing monitors it",
   "belongs to no branch" in r["reconciliation"], r["reconciliation"][:120])

print("\nit does not stop at page one")
v = Venue(HELD, PRICES, pages=3)
r = run(v)
ok("pagination is followed", v.page_calls == 3, f"{v.page_calls} pages")
ok("and reported", r["accounts_pages"] == 3)

print("\nedge cases do not raise")
ok("an empty account", run(Venue({}, {}))["total_usd"] == 0.0)
ok("a venue that refuses the balance call",
   run(Venue({}, {}, pages=0)).get("available") in (True, False))
z = run(Venue({"FOO": 0.0}, {}))
ok("a zero balance is not held", z["assets_held"] == 0, str(z["assets_held"]))

print("\nthe endpoint and the panel put the GAP first")
HERE = os.path.dirname(os.path.abspath(__file__))
DASH = open(os.path.join(HERE, "routers", "trading_dashboard.py"), encoding="utf-8").read()
HTML = open(os.path.join(HERE, "family_tree_dashboard.html"), encoding="utf-8").read()
ok("the endpoint exists and is a GET", '@router.get("/account-census")' in DASH)
ok("it passes what the bots track, so a gap can be computed",
   "tracked_usd=tracked" in DASH)
ok("the panel is mounted", 'id="account-census-panel"' in HTML)
ok("and rendered", "renderAccountCensus()" in HTML)
ok("it fetches on its own, off the status path",
   "/api/trading-dashboard/account-census" in HTML,
   "pricing dozens of assets must never sit in the path of the liveness page")
ok("the warning renders ABOVE the total",
   HTML.index("d.warning") < HTML.index("Coinbase total"),
   "a confident total printed over a half-priced account is the original bug")
ok("the untracked figure is its own block, not a footnote",
   "belongs to no branch" in HTML)
ok("a census that is unavailable renders nothing rather than a zero",
   "if (!d || !d.available) { el.innerHTML = ''; return; }" in HTML)

print("\nit cannot trade")
SRC = open(os.path.join(HERE, "account_census.py"), encoding="utf-8").read()
ok("no order path is imported", "place_market" not in SRC and "place_order" not in SRC)
ok("the only write is none", "db.add" not in SRC and "commit" not in SRC)
ok("stablecoins are held at par rather than looked up",
   'STABLE = {' in SRC and '"stablecoin par"' in SRC,
   "an unreachable ticker must not drop real cash out of the total")
ok("pricing tries the host that returned the balances FIRST",
   SRC.index("advanced_trade") < SRC.index("public_feed"),
   "capital_census priced from a host that does not answer from production")

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
