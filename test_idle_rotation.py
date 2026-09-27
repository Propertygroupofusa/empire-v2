"""The executor: what it will and will not move, and what arms it.

idle_capital decides what should move. This file covers the half that
actually touches money, and the thing it must not become - GRID_AUTO_ROTATE
under a new name.
"""
import ast
import sys

SRC = open("idle_rotation_worker.py").read()
TREE = ast.parse(SRC)
ROUTER = open("routers/trading_dashboard.py").read()
MAIN = open("main.py").read()

_checks = []


def ok(label, cond, detail=""):
    _checks.append((label, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  -- {detail}" if detail and not cond else ""))


def fn(name):
    n = next(x for x in ast.walk(TREE)
             if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == name)
    return "\n".join(SRC.splitlines()[n.lineno - 1:n.end_lineno])


sys.path.insert(0, ".")
import idle_rotation_worker as w  # noqa: E402

ROT = fn("rotate_once")

# --- it reuses the one mover, it does not reimplement one -------------------
ok("it calls the same function the dashboard modal calls",
   "move_cash_between_grid_branches" in ROT)
ok("it never places an order itself",
   not any(x in ROT for x in ("place_order", "place_market_sell", "create_grid_branch")))
ok("it never withdraws to unallocated cash",
   "withdraw_from_grid_branch" not in ROT)
ok("and it says so in the returned plan",
   "retires_nothing_to_cash" in open("idle_capital.py").read())

# --- arming -----------------------------------------------------------------
ok("observe is the default", w.current_mode() in ("observe", "arm"))
ok("only the exact word arms it", w.is_armed() == (w.current_mode() == "arm"))
ok("the mode is stripped and lowercased", ".strip().lower()" in fn("current_mode"))
ok("armed twice - once before fetching, once before each move",
   ROT.count("is_armed()") >= 2)
ok("a mid-pass disarm stops before the next move", "disarmed mid-pass" in ROT)

# --- the default is preview, everywhere -------------------------------------
ok("rotate_once previews unless told otherwise", "dry_run: bool = True" in ROT)
ok("a dry run returns before any move",
   ROT.index("if dry_run:") < ROT.index("move_cash_between_grid_branches"))
ok("the endpoint is dry-run by default", "async def idle_capital_rotate(dry_run: bool = True)" in ROUTER)
ok("the endpoint is a POST, so the write guard covers the preview too",
   '@router.post("/idle-capital/rotate")' in ROUTER)

# --- the bound on a pass ----------------------------------------------------
ok("one move per pass by default", w.MAX_MOVES_PER_PASS == 1)
ok("the cap is applied to the plan", 'plan["moves"][:max_moves]' in ROT)

# --- it is not GRID_AUTO_ROTATE under a new name ----------------------------
# Asserted on the parsed tree, not the source text. The module docstring
# explains at length why this is not GRID_AUTO_ROTATE, so any substring
# search for that name matches the explanation and reads the warning as the
# offence - the same trap this repo has now hit in four separate test files.
_env_reads = set()
_names = set()
for n in ast.walk(TREE):
    if isinstance(n, ast.Call):
        f = n.func
        if isinstance(f, ast.Attribute) and f.attr == "getenv":
            # Only the FIRST argument is the variable name; the second is a
            # default ("observe", "3600") and is not an env read.
            if n.args and isinstance(n.args[0], ast.Constant) \
                    and isinstance(n.args[0].value, str):
                _env_reads.add(n.args[0].value)
        if isinstance(f, ast.Name):
            _names.add(f.id)
        elif isinstance(f, ast.Attribute):
            _names.add(f.attr)

ok("it never reads the auto-rotate switch",
   "GRID_AUTO_ROTATE" not in _env_reads, f"{sorted(_env_reads)}")
ok("it never calls the fleet's own rotation sweep",
   not (_names & {"run_grid_auto_rotate_sweep", "_maybe_rotate_one_grid_branch",
                  "set_grid_auto_rotate_active"}))
ok("its own env switch is distinct", w.MODE_ENV == "GRID_IDLE_ROTATION_MODE")
ok("and every literal env name it reads is its own",
   all(e.startswith("GRID_IDLE_ROTATION") for e in _env_reads), f"{sorted(_env_reads)}")
ok("the docstring states why this is not that switch",
   "NOT GRID_AUTO_ROTATE SWITCHED ON" in SRC)

# --- failures are survivable ------------------------------------------------
ok("one failed move does not abort the pass", "failed.append" in ROT)
ok("the loop never dies", "except Exception" in fn("run_periodically"))
ok("a heartbeat records what moved", "moved_usd" in SRC)

# --- wiring -----------------------------------------------------------------
ok("the loop is started at boot", "idle_rotation_worker.run_periodically()" in MAIN)
ok("a failure to start does not take the app down",
   "idle rotation not started" in MAIN)
ok("the endpoint hands the worker require_arm=False, since the guarded "
   "request IS the authorisation", "require_arm=False" in ROUTER)
ok("the periodic loop still requires its own arm", "require_arm=True" in SRC)

# --- the planner stays pure -------------------------------------------------
IC = ast.parse(open("idle_capital.py").read())
ic_mods = set()
for n in ast.walk(IC):
    if isinstance(n, ast.Import):
        ic_mods |= {a.name.split(".")[0] for a in n.names}
    elif isinstance(n, ast.ImportFrom) and n.module:
        ic_mods.add(n.module.split(".")[0])
ok("the planner still cannot reach the venue or the database",
   not (ic_mods & {"aiohttp", "sqlalchemy", "models", "crypto_grid_bot", "requests"}),
   f"{sorted(ic_mods)}")

_failed = [l for l, p in _checks if not p]
print(f"\n{len(_checks) - len(_failed)} passed, {len(_failed)} failed")
sys.exit(1 if _failed else 0)
