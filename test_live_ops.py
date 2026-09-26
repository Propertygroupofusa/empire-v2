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
# _live_ops_config is ASYNC. The start marker below deliberately includes
# "async ", because "def _live_ops_config():" also matches as a SUBSTRING of
# the async line - which silently sliced the "async " off the front and
# produced a sync function full of awaits. That is a SyntaxError at exec
# time, so this whole file stopped running the moment the function was made
# async, and every check below went unenforced without one FAIL being
# printed. A test that cannot fail is worse than no test.
config_src = _extract(ROUTER, "async def _live_ops_config():", "\nasync def _live_ops_gate_feed")
assert config_src.startswith("async def "), "the slice lost its async prefix again"
# The one await inside reaches the grid module for the net-edge switch; the
# panel already falls back to True when that raises, so a stub that raises
# exercises the documented path without a database.
class _NoGridModule:
    def __getattr__(self, name):
        raise RuntimeError("no grid module in this test")
ns = {"os": os, "crypto_grid_bot_module": _NoGridModule()}
exec(config_src, ns)
_live_ops_config_async = ns["_live_ops_config"]


def _live_ops_config():
    return asyncio.run(_live_ops_config_async())


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
for key in ["runner", "gate", "config", "grid", "trades", "cash", "capital", "reconciliation"]:
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


# --- runner / pipeline / money contracts ----------------------------------
ok("runner section is gathered before anything that depends on it",
   router_src.index('_section("runner"') < router_src.index('_section("gate"'))
ok("runner reads the engine's own credential verdict, not guessed env names",
   'getattr(_engine, "cdp_configured", False)' in router_src)
ok("runner never reads a credential VALUE",
   'os.getenv("COINBASE_API_PRIVATE_KEY")' not in router_src)
ok("every runner gate ships a concrete fix", router_src.count('"fix":') >= 3)
# Match the CODE, not the prose - the comment above the fix quotes the old
# expression to explain it, and a substring test flagged that as the bug.
ok("the runner does NOT hardcode grid_fleet as the only valid mode",
   '"ok": mode == "grid_fleet"' not in router_src)
ok("it accepts every supported mode", '"ok": mode in known' in router_src)
ok("it names which service runs the mode in effect", '"mode_owner"' in router_src)
ok("it knows both services want opposite values",
   "bot_runner.py) exits unless" in router_src and "family_tree" in router_src)
ok("a retired tree is surfaced, since the loop would run and do nothing",
   '"tree_retired"' in router_src and "is_crypto_passive_mode" in router_src)
# Was: "the retire flag is described as not cleared by this repo". That was
# true until the un-retire was built, and this check correctly failed the
# moment it shipped. Replaced with what must be true now - the gate has to
# hand the operator the fix, not a dead end.
ok("a retired tree's gate points at the fix rather than a dead end",
   "resume-active-trading" in router_src
   and "nothing in this repo clears it" not in router_src)
ok("runner tracks any activity, not only gate verdicts",
   "last_activity_age_seconds" in router_src and "last_activity_age_seconds" in page)
ok("the page renders the runner gates", "renderRunner" in page)
ok("the page renders the pipeline", "renderPipeline" in page)
ok("the pipeline names where flow stops", "Flow stops at" in page)
ok("the pipeline separates 'no branches' from 'no dips'",
   "no branches exist" in page and "no coin has dipped" in page)
ok("the pipeline explains an all-rejected cycle as the gate working",
   "doing its job" in page)
ok("the page renders realized money", "renderMoney" in page and "total_realized_pnl" in page)

# --- the headline is TOTAL, not realized ----------------------------------
# Realized is positive nearly all of the time - a NORMAL grid exit only sells
# above its own entry - while the total also reflects open slices. These
# checks pin the ordering so it cannot quietly invert back.
#
# "Nearly all", not "by construction". That overstatement was corrected on
# 2026-09-25 against this account's own trade log: id 80 closed DOGE at -$2.27
# (entry 0.09112, exit 0.08964) in the 2026-09-09 forced liquidation. A forced
# close ignores the sell-above-entry rule, so realized CAN go negative and an
# empty losers column must never be presented as a guarantee.
ok("the page has a headline block", "renderHeadline" in page and 'id="p-headline"' in page)
ok("the headline is fed by its own server section", "d.headline" in page)
ok("the headline is labelled as taken plus still open",
   "Total P&amp;L — taken plus still open" in page)
ok("the headline sits ABOVE the runner panel",
   page.index('id="p-headline"') < page.index('id="p-runner"'))
ok("and above the realized money panel",
   page.index('id="p-headline"') < page.index('id="p-money"'))
ok("an unmeasurable total says so rather than showing a number",
   "not measurable" in page)
ok("the total is never backfilled from the realized half",
   "d.measurable ? signed(d.total_usd)" in page)
ok("realized is captioned as a component of the total, not the headline",
   'the &quot;taken&quot; half of the total above' in page
   or 'the "taken" half of the total above' in page)
ok("the win rate caption explains WHY it is high, without overclaiming",
   "high by design" in page)
ok("and warns that a FORCED close can still book a loss",
   "FORCED close" in page and "can book a loss" in page)
ok("no page claims wins are guaranteed by construction",
   "winners by construction" not in page)
ok("the gap warning is rendered when the server flags one", "d.warning" in page)
ok("a negative dollar figure never renders as '$-'",
   "(n < 0 ? '-$' : '$')" in page)

# The server half: one place computes the total, so this page and the
# terminal view cannot disagree about it.
ok("the server builds the headline section", "_live_ops_headline" in router_src)
ok("and it is attached to the live-ops payload", 'results["headline"]' in router_src)
ok("and it delegates to the one function that defines a total",
   "total_pnl_stats" in router_src)
ok("the metrics report is given the unrealized leg",
   "unrealized_net_usd=grid.get(" in router_src)

# --- the Coinbase total can explain its own blank --------------------------
ok("the total stays all-or-nothing",
   "real_balance is not None and tree_holdings_complete and grid_holdings_complete" in router_src)
ok("but a breakdown ships alongside it",
   '"real_crypto_net_worth_breakdown"' in router_src)
ok("and names which piece was unreadable",
   '"real_crypto_net_worth_missing"' in router_src)
ok("each component carries an availability flag", router_src.count('"available":') >= 3)

# --- inert tree controls are labelled, not left to be discovered by pressing
tree = open(os.path.join(HERE, "family_tree_dashboard.html")).read()
ok("the server says which crypto loop actually runs",
   '"family_tree_loop_running"' in router_src and '"crypto_strategy_mode"' in router_src)
ok("the tree page warns when its trading controls are inert",
   "renderTreeLoopBanner" in tree and "inert right now" in tree)
ok("the warning is data-driven, never hardcoded",
   "data.family_tree_loop_running !== false" in tree)
ok("and it fails safe on an older server that omits the field",
   "!== false" in tree)
ok("the tree page points at the page that IS live", '"/live-ops"' in tree or "/live-ops" in tree)


# --- the un-retire ---------------------------------------------------------
#
# Retirement was one-way: set_crypto_passive_mode(False) had no caller
# anywhere, so a retired tree given a running loop still did nothing at
# all, for ever. These pin the missing half of the switch.
ok("an endpoint exists to clear the retire flag",
   '@router.post("/family-tree-status/resume-active-trading")' in router_src)
ok("it actually clears the flag", "set_crypto_passive_mode(False)" in router_src)
ok("it READS THE FLAG BACK rather than trusting the write",
   router_src.count("is_crypto_passive_mode()") >= 2 and "did not clear the flag" in router_src)
ok("a failed clear is an error, not a success", "status_code=500" in router_src)
ok("it reports whether anything actually changed", '"was_passive"' in router_src)
ok("it says whether the loop that would act on it is even running",
   '"family_tree_loop_running"' in router_src and '"next_step"' in router_src)
ok("and names which service to change, and what not to break",
   "on the WEB service" in router_src and "grid_fleet, or its runner exits" in router_src)
ok("it states plainly that nothing sold is bought back",
   "NOT restored" in router_src)
ok("the retired banner offers the un-retire",
   "confirmResumeCryptoTrading" in tree and "Let the tree trade again" in tree)
ok("the un-retire asks once - it sells nothing, so a second prompt is ceremony",
   tree.count("if (!confirm(") >= 1)
ok("Live Ops points at the fix instead of calling it impossible",
   "resume-active-trading" in router_src and "nothing in this repo clears it" not in router_src)


# --- the cash ceiling ------------------------------------------------------
#
# Two loops, one Coinbase wallet, no coordination: whichever looked first
# took everything. Observed live 2026-09-24 - btc_compound converted a
# ~$577 account into BTC and the healthy grid fleet ran against $0.29.
grid_bot = open(os.path.join(HERE, "crypto_grid_bot.py")).read()
fam = open(os.path.join(HERE, "crypto_family_tree_bot.py")).read()
ok("an allocator module exists",
   os.path.exists(os.path.join(HERE, "crypto_cash_allocator.py")))
ok("the grid sweep is bounded by its share, not just by free cash",
   "get_grid_spend_ceiling_usd()" in grid_bot
   and "min(real_free_cash - GRID_CASH_RESERVE_USD, ceiling)" in grid_bot)
ok("the grid says so when the SHARE is what held it, not the wallet",
   "auto-deploy holding on its cash share" in grid_bot)
# Was: assertions on an inline cap at ONE tree buy site. That shape was
# the bug - there are five buy paths and four were uncapped. Replaced with
# the stronger property: every competitive buy routes through one
# chokepoint. test_tree_cash_ceiling.py enforces the full coverage guard.
ok("the tree caps its buys at a single chokepoint, not per call site",
   "async def capped_market_buy(" in fam
   and fam.count("allocator.spend_ceiling(allocator.TREE") == 1)
ok("the tree's main buy paths all route through it",
   "capped_market_buy(session, spend, branch.product_id" in fam
   and "capped_market_buy(session, usd_amount, target_branch.product_id" in fam
   and "capped_market_buy(session, spend, product_id" in fam)
ok("the ceiling is computed from the WALLET, never from the request",
   "free_cash_usd" in fam and "tree_spend_ceiling(free_cash_usd)" in fam)
ok("both fail closed when the ceiling cannot be computed",
   grid_bot.count("if ceiling is None:") >= 1 and fam.count("if ceiling is None:") >= 1)
ok("the endpoint serves every bot's ceiling", '_section("cash"' in router_src)
ok("the page renders it", "renderCash" in page and "Who may spend the wallet" in page)
ok("the page explains an unspent share instead of showing it as a bug",
   "stays unspent by design" in page)

# --- closing the position that caused it -----------------------------------
ok("an endpoint exists to close btc_compound's position",
   '@router.post("/btc-compound/close-position")' in router_src)
ok("it is imported, not just referenced",
   "import crypto_btc_compound_bot as crypto_btc_compound_bot_module" in router_src)
ok("it defaults to a dry run", "dry_run: bool = True" in router_src)
ok("it uses the bot's OWN exit path so the bot's state stays true",
   "_sell_and_settle(" in router_src)
ok("a sell that does not fill is an error, not a silent success",
   "The market sell did not fill" in router_src)
ok("the button shows real figures before asking",
   "confirmCloseBtcCompound" in tree and "estimated_gross_pnl_usd" in tree)
ok("and dry-runs before it ever sells",
   "dry_run=true" in tree and "dry_run=false" in tree)


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
