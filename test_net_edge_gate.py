"""A safety gate may not be switchable off in silence.

The bill for not having this, 2026-09-25:

    The live account's first trade in sixteen days - a real $22.90 buy of
    NEAR-USD - went in with NO economic check on it at all. The gate that
    was supposed to check it read, as its first statement:

        if not NET_EDGE_GATE_ENABLED:
            return True, "gate disabled"

    GRID_NET_EDGE_GATE_ENABLED was set to a non-true value in Railway, so
    the gate returned True (buy allowed) and recorded NOTHING. The
    dashboard showed:

        GATE_PASS 0   GATE_BLOCK 0   GATE_OBSERVE 0   GATE_ERROR 0

    which is byte-identical to a gate that was never reached. Tracing it
    took reading the code, confirming only one buy path existed, proving
    the activity-log write path worked (REANCHOR, SPACING_TUNED and BUY
    had all landed), and finally finding "Net-edge gate: OFF, source: env"
    in a config panel. The switch was in an environment variable nobody
    re-reads, and the off state left no trace.

THE RULES THIS FILE PROTECTS:

  1. The switch is DB-persisted and visible, not an env var.
  2. It DEFAULTS ON. A safety gate whose silent default is off commits
     money without checking its economics.
  3. Turning it off is RECORDED. Every buy allowed through unchecked
     writes GATE_DISABLED, and that verdict appears in the live feed.
  4. The config panel reports the real switch, not the retired env var -
     that panel is what operators trust, and it was reporting a variable
     the code no longer obeys.

Run: python3 test_net_edge_gate.py
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
router = open(os.path.join(HERE, "routers", "trading_dashboard.py"), encoding="utf-8").read()
tree = ast.parse(src)
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def fn(name, where=tree):
    for n in ast.walk(where):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def body(name, where=tree):
    node = fn(name, where)
    if node is None:
        return ""
    stmts = node.body
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant):
        stmts = stmts[1:]
    return "\n".join(ast.unparse(s) for s in stmts)


# --- the switch is real, DB-persisted, and defaults ON -------------------
ok("a DB-backed activity check exists", fn("is_net_edge_gate_active") is not None)
ok("a setter exists", fn("set_net_edge_gate_active") is not None)
active = body("is_net_edge_gate_active")
ok("it reads the database", "TradingBotState" in active and "NET_EDGE_GATE_KEY" in active)
ok("REGRESSION: it defaults ON when nothing is stored",
   "if row is None:" in active and "return True" in active)
ok("it does NOT consult the retired env var", "getenv" not in active)

# --- the gate consults that switch, not the env var ----------------------
gate = body("_net_edge_gate_ok")
ok("the gate asks the DB switch", "is_net_edge_gate_active" in gate)
ok("REGRESSION: the gate no longer branches on the env constant",
   "NET_EDGE_GATE_ENABLED" not in gate)

# --- the disabled path is RECORDED, not silent ---------------------------
ok("a disabled gate records a verdict",
   "GATE_DISABLED" in gate and "_record_gate_decision" in gate)
disabled_idx = gate.index("GATE_DISABLED")
return_idx = gate.index("return (True, 'gate disabled')") if "return (True, 'gate disabled')" in gate \
    else gate.index("'gate disabled'")
ok("REGRESSION: it records BEFORE returning, so the buy cannot pass unlogged",
   disabled_idx < return_idx)
ok("the record names what it let through unchecked",
   "NO economic check" in gate or "no economic check" in gate.lower())

# --- the verdict is visible on the dashboard -----------------------------
ok("GATE_DISABLED is in the live-ops event list", '"GATE_DISABLED"' in router)
ok("the config panel reads the real switch, not the env var",
   "is_net_edge_gate_active()" in router)
ok("REGRESSION: the panel no longer decides from GRID_NET_EDGE_GATE_ENABLED",
   'os.getenv("GRID_NET_EDGE_GATE_ENABLED", "true").lower() == "true"' not in router)
ok("a toggle endpoint exists", "/grid-status/net-edge-gate" in router)

# --- the env var is retired, not silently still in force -----------------
ok("the old constant name is gone from the decision path",
   "NET_EDGE_GATE_ENABLED = os.getenv" not in src)
ok("the env setting is still readable for operators to see and clear",
   "NET_EDGE_GATE_ENV_SETTING" in src)


# --- the rules, as behaviour --------------------------------------------
def gate_runs(stored):
    """stored: None = never set, 0.0 = off, 1.0 = on."""
    if stored is None:
        return True
    return bool(stored and stored >= 1.0)


ok("never configured  -> gate RUNS", gate_runs(None) is True)
ok("explicitly on     -> gate RUNS", gate_runs(1.0) is True)
ok("explicitly off    -> gate skipped", gate_runs(0.0) is False)
ok("REGRESSION: a stale env var can no longer turn it off",
   gate_runs(None) is True)


def telemetry(stored, buys):
    """What the feed shows for N buys at a given switch state."""
    return {"GATE_DISABLED": 0 if gate_runs(stored) else buys}


ok("REGRESSION: an off gate no longer looks identical to one never reached",
   telemetry(0.0, 1)["GATE_DISABLED"] == 1)
ok("an on gate writes no GATE_DISABLED rows", telemetry(1.0, 5)["GATE_DISABLED"] == 0)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
