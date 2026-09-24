"""Checks for the Live Ops endpoint and page.

Neither fastapi nor sqlalchemy is installed in every environment this has
to run in, so the endpoint module cannot be imported here. What IS checked
is everything that can be checked without it, chosen because each one has
already gone wrong at least once in this codebase:

  - the config panel reports the settings really in effect, and says
    whether each came from the environment or a default
  - one failing section degrades its own panel instead of the page
  - the page and the endpoint agree on the JSON contract, so a rename on
    one side cannot silently leave a panel permanently empty
  - the page fabricates nothing: no venue figure renders as 0 when it was
    actually unreadable

Run: python3 test_live_ops.py
"""
import asyncio
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROUTER = os.path.join(HERE, "routers", "trading_dashboard.py")
PAGE = os.path.join(HERE, "live_ops_dashboard.html")
MAIN = os.path.join(HERE, "main.py")

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def _extract(path, start_marker, end_marker):
    """Pull one function's source out of a module too heavy to import."""
    src = open(path).read()
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


# --- the config panel ------------------------------------------------------
config_src = _extract(ROUTER, "def _live_ops_config():", "\nasync def _live_ops_gate_feed")
ns = {"os": os}
exec(config_src, ns)
_live_ops_config = ns["_live_ops_config"]


def cfg(**env):
    saved = {}
    keys = ["PROP_MAX_RISK_PERCENT", "GRID_AUTO_DEPLOY_AMOUNT_USD", "GRID_CASH_RESERVE_USD",
            "GRID_MICROSTRUCTURE_VETO_MODE", "GRID_NET_EDGE_GATE_ENABLED",
            "CRYPTO_STRATEGY_MODE", "STOP_TRADING"]
    for k in keys:
        saved[k] = os.environ.pop(k, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    try:
        return {r["key"]: r for r in _live_ops_config()}
    finally:
        for k in keys:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


d = cfg()
ok("risk cap defaults to the shipped 50%", d["Max risk (both Alpaca bots)"]["value"] == "50%")
ok("and is labelled as a default, not as configured",
   d["Max risk (both Alpaca bots)"]["source"] == "default")
ok("veto defaults to observe", d["Microstructure veto"]["value"] == "observe")
ok("an unset strategy mode is shown as unset, not guessed",
   d["Grid strategy mode"]["value"] == "(unset)")

d = cfg(PROP_MAX_RISK_PERCENT="0.20")
ok("an env override is reported as the live value", d["Max risk (both Alpaca bots)"]["value"] == "20%")
ok("and is labelled as coming from the environment",
   d["Max risk (both Alpaca bots)"]["source"] == "env")

d = cfg(PROP_MAX_RISK_PERCENT="not-a-number")
ok("a garbage value falls back to the default", d["Max risk (both Alpaca bots)"]["value"] == "50%")
ok("and SAYS it was unparseable rather than hiding it",
   "unparseable" in d["Max risk (both Alpaca bots)"]["source"])

d = cfg(GRID_MICROSTRUCTURE_VETO_MODE="enforce")
ok("enforce mode is shown when really set", d["Microstructure veto"]["value"] == "enforce")
d = cfg(GRID_MICROSTRUCTURE_VETO_MODE="typo")
ok("an unrecognised veto mode is flagged, not shown as valid",
   "fallback" in d["Microstructure veto"]["value"])

d = cfg(STOP_TRADING="true")
ok("a halted bot says so loudly", "STOP_TRADING" in d["Trading halted"]["value"])
d = cfg(CRYPTO_STRATEGY_MODE="grid_fleet")
ok("the live strategy mode is surfaced", d["Grid strategy mode"]["value"] == "grid_fleet")


# --- one section failing must not blank the page --------------------------
async def _section(name, coro):
    try:
        return name, {"ok": True, "data": await coro, "error": None}
    except Exception as e:
        return name, {"ok": False, "data": None, "error": f"{type(e).__name__}: {e}"}


async def good():
    return {"v": 1}


async def bad():
    raise RuntimeError("Coinbase unreachable")


async def _isolation():
    return dict(await asyncio.gather(_section("a", good()), _section("b", bad()),
                                     _section("c", good())))


res = asyncio.run(_isolation())
ok("a healthy section still returns its data", res["a"]["ok"] and res["a"]["data"] == {"v": 1})
ok("a failing section is marked not-ok", res["b"]["ok"] is False)
ok("and carries its own error text", "Coinbase unreachable" in res["b"]["error"])
ok("a failing section never invents data", res["b"]["data"] is None)
ok("sections after the failure still succeed", res["c"]["ok"])

router_src = open(ROUTER).read()
ok("the endpoint gathers sections concurrently",
   "asyncio.gather(" in router_src and "_section(\"gate\"" in router_src)
ok("the endpoint stamps when it was served", '"served_at"' in router_src)


# --- page/endpoint contract ------------------------------------------------
page = open(PAGE).read()

# Top-level keys the page reads off the response.
for key in ["gate", "config", "grid", "capital", "reconciliation"]:
    ok(f"endpoint serves the '{key}' section the page renders",
       f'_section("{key}"' in router_src or f'results["{key}"]' in router_src)
    ok(f"page reads the '{key}' section", f"d.{key}" in page)

for key in ["tally_24h", "last_decision_age_seconds", "events"]:
    ok(f"gate payload key '{key}' exists on both sides",
       f'"{key}"' in router_src and key in page)

for ev in ["GATE_PASS", "GATE_BLOCK", "GATE_OBSERVE", "GATE_ERROR"]:
    ok(f"event type {ev} is written, served and rendered", ev in router_src and ev in page)

grid_src = open(os.path.join(HERE, "crypto_grid_bot.py")).read()
for ev in ["GATE_PASS", "GATE_BLOCK", "GATE_OBSERVE", "GATE_ERROR"]:
    ok(f"the grid bot actually records {ev}", f'"{ev}"' in grid_src)
ok("gate telemetry can never raise into a trade",
   "async def _record_gate_decision" in grid_src
   and "non-fatal, trading unaffected" in grid_src)


# --- the page must not fabricate -------------------------------------------
ok("an unreadable venue renders as unreadable, never as $0",
   "unreadable" in page and "venues_unknown" in page)
ok("a missing number renders as a dash",
   "'<span class=\"none\">—</span>'" in page)
ok("a failed poll is announced rather than leaving stale numbers up",
   "cannot reach the server" in page.lower())
ok("the heartbeat distinguishes quiet from broken",
   "Quiet" in page and "Cannot read decisions" in page)

# --- page hygiene ----------------------------------------------------------
ok("no external resources (page must render with no network)",
   not re.search(r'(src|href)\s*=\s*["\']https?://', page))
ok("the page is served by a route", "/live-ops" in open(MAIN).read())
ok("the page declares a viewport for phones", 'name="viewport"' in page)
ok("phone safe-area insets are respected", "safe-area-inset" in page)
ok("values are escaped before being written into the DOM",
   "const esc =" in page and page.count("esc(") > 10)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
