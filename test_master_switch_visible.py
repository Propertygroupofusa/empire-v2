"""The master switch turning off must never read as healthy.

2026-09-26: the fleet did not trade for ~2.5 hours. mode_active was False -
the Grid Bot master switch, flipped off by POST /grid-status/switch-to-scale-bot
("Switch to Scale Bot" on the dashboard). The loop kept stamping "entered" on
schedule, the dashboard kept serving live prices, and nothing said the bot was
off. The silent `return` had no log and no heartbeat stage at all.
"""
import ast, re, sys
_p = _f = 0
def ok(l, c, d=""):
    global _p, _f
    if c: _p += 1; print(f"  PASS  {l}")
    else: _f += 1; print(f"  FAIL  {l}" + (f"  -- {d}" if d else ""))

SRC = open("crypto_grid_bot.py").read()
TREE = ast.parse(SRC)
def fn(n):
    for x in ast.walk(TREE):
        if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) and x.name==n: return x

cyc = ast.get_source_segment(SRC, fn("run_grid_branches_cycle")) or ""

print("\nthe master switch being OFF is audible")
ok("the is_grid_bot_active() exit logs at warning",
   re.search(r"is_grid_bot_active\(\):.*?log\.warning", cyc, re.S) is not None,
   "this return previously had NO log at all")
ok("it names the flag so it can be found and flipped back",
   "crypto_grid_bot_active" in cyc)
ok("it stamps its own heartbeat stage",
   '_record_grid_heartbeat("bot_inactive")' in cyc)

m = re.search(r"_HEARTBEAT_STAGES\s*=\s*\{.*?\}", SRC, re.S)
ok("bot_inactive is a known stage", m and '"bot_inactive"' in m.group(0))
ok("it is DISTINCT from lease_refused", m and '"lease_refused"' in m.group(0)
   and '"bot_inactive"' in m.group(0),
   "'another process is working' and 'no process will' are different faults")

print("\nthe three silent exits are all now accounted for")
for stage in ("lease_refused", "bot_inactive", "no_active_branches", "cycled"):
    ok(f"cycle can report '{stage}'", f'"{stage}"' in cyc or f'"{stage}"' in (m.group(0) if m else ""))

print("\nthe switch still defaults ON, so False always means someone set it")
tog = ast.get_source_segment(SRC, fn("is_grid_bot_active")) or ""
ok("a missing row still returns True (unchanged)",
   re.search(r"if row is None:\s*return True", tog) is not None,
   "this toggle is deliberately NOT fail-closed - flipping it off is a "
   "deliberate act, so False is always information")
print(f"\n{_p} passed, {_f} failed")
sys.exit(1 if _f else 0)
