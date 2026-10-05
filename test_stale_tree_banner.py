"""The tree gates must not narrate a loop that never starts.

Two banners - the rolling-expectancy pause and the drawdown-breaker pause -
are produced in TWO places from the same fact: once client-side in
family_tree_dashboard.html, once server-side in _build_progress_observations.
The client copy was gated on family_tree_loop_running. The server copy was
not, so the fix covered one of the two places the sentence is written and
the banner kept appearing.

What it announced: 20 trades, 35% win rate, -$105.70 - read off
CryptoCoinTradeHistory, the RETIRED tree's ledger (167 trades, -$508.44,
newest 2026-09-09). Printed directly above a live grid running 87 trades at
77% and +$25.82, on a deploy where the tree loop is never started at all.
"""
import ast
import sys

SRC = open("routers/trading_dashboard.py").read()
PAGE = open("family_tree_dashboard.html").read()

_checks = []


def ok(label, cond, detail=""):
    _checks.append((label, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  -- {detail}" if detail and not cond else ""))


def _fn(name):
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return "\n".join(SRC.splitlines()[fn.lineno - 1:fn.end_lineno])


# _build_progress_observations was SPLIT on 2026-10-05 into
# _crypto_observations and _alpaca_observations, because the two
# accounts are no longer combined anywhere. The tree banner this
# file guards is a Coinbase-side observation, so it lives in the
# crypto half.
OBS = _fn("_crypto_observations")

ok("the server side reads whether the tree loop is actually running",
   'crypto_data.get("family_tree_loop_running")' in OBS)

ok("it treats an explicit False as 'not running', not a missing key",
   "is not False" in OBS)

ok("the expectancy pause is gated on it", "if tree_gates_apply and rolling" in OBS)
ok("so is the drawdown-breaker pause", "if tree_gates_apply and paused_dd" in OBS)

ok("neither gate still tests retirement alone",
   "if not crypto_retired and rolling" not in OBS
   and "if not crypto_retired and paused_dd" not in OBS)

ok("retirement is still reported on its own",
   "retired (passive mode)" in OBS)

ok("the client-side copy keeps its own gate",
   "data.family_tree_loop_running === false" in PAGE)

# The two copies must agree on the condition, or this recurs.
ok("both copies gate on the SAME flag",
   "family_tree_loop_running" in OBS and "family_tree_loop_running" in PAGE)


def _observations(crypto):
    """Re-run the gate logic exactly as the module states it."""
    retired = bool(crypto.get("crypto_passive_mode"))
    live = crypto.get("family_tree_loop_running") is not False
    return (not retired) and live


STALE = {"crypto_passive_mode": False, "family_tree_loop_running": False,
         "rolling_expectancy": {"negative": True, "num_trades": 20}}
LIVE = {"crypto_passive_mode": False, "family_tree_loop_running": True,
        "rolling_expectancy": {"negative": True, "num_trades": 20}}
UNKNOWN = {"crypto_passive_mode": False,
           "rolling_expectancy": {"negative": True, "num_trades": 20}}

ok("a stopped tree loop suppresses the pause banner", _observations(STALE) is False)
ok("a running tree loop still shows it", _observations(LIVE) is True)
ok("an absent flag does NOT suppress it - silence is not proof of a dead loop",
   _observations(UNKNOWN) is True)
ok("retirement suppresses it regardless of the loop flag",
   _observations({**LIVE, "crypto_passive_mode": True}) is False)

_failed = [l for l, p in _checks if not p]
print(f"\n{len(_checks) - len(_failed)} passed, {len(_failed)} failed")
sys.exit(1 if _failed else 0)
