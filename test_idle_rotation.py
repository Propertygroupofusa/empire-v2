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

# --- the sweep must not compete with the trading loop for the rate limit ---
#
# It reads allocated_usd, bot_name, product_id, open_slices and created_at -
# all stored columns. get_grid_status fetches a live price per product, a
# wallet balance and an adaptive stop per product: ~45 venue calls to answer
# a question no live price takes part in. Production already logs "HTTP 429
# fetching USD", and the calls this would crowd out belong to the trading
# loop on the same outbound IP.
DB = fn("_branches_from_db")
PLAN = fn("plan_now")

ok("there is a database path for the branch read", "_branches_from_db" in SRC)
ok("it selects branches and slice counts, not prices",
   "CryptoGridBranch" in DB and "CryptoGridSlice" in DB)
# Asserted on CALLS in the parsed function, not its text: the docstring
# names get_grid_status to explain what it avoids, and a substring search
# reads that explanation as the offence. Fifth occurrence of this trap in
# this session's tests - the rule is now simply "never grep a docstring".
_dbfn = next(x for x in ast.walk(TREE)
             if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
             and x.name == "_branches_from_db")
_dbcalls = set()
for _n in ast.walk(_dbfn):
    if isinstance(_n, ast.Call):
        _f = _n.func
        if isinstance(_f, ast.Name):
            _dbcalls.add(_f.id)
        elif isinstance(_f, ast.Attribute):
            _dbcalls.add(_f.attr)
ok("it never fetches a price, a balance or a stop",
   not (_dbcalls & {"get_price_and_volatility", "get_usd_balance",
                    "_resolve_branch_stop", "get_grid_status",
                    "get_best_bid_ask"}), f"{sorted(_dbcalls)}")
ok("a missing slice count reads as flat, never as holding",
   "counts.get(b.bot_name, 0)" in DB)
ok("the loop takes the cheap path", "cheap=require_arm" in ROT)
ok("plan_now defaults to cheap", "cheap: bool = True" in PLAN)
ok("the expensive path still exists for the human-facing preview",
   "get_grid_status()" in PLAN)

# --- the interval ----------------------------------------------------------
#
# The condition moves on a 72-hour clock, so a faster sweep changes only how
# soon a branch is noticed after the mark passes - never whether it
# qualifies. 15 minutes is a worst-case 0.35% delay on a 4,320-minute
# condition, which is why this is a preference rather than a tradeoff once
# the pass is cheap.
ok("the sweep is every 15 minutes", w.CHECK_SECONDS == 900)
ok("the interval is settable", "GRID_IDLE_ROTATION_CHECK_SECONDS" in SRC)
ok("a pass can never run tighter than a minute", "max(CHECK_SECONDS, 60)" in SRC)

import idle_capital as _ic  # noqa: E402
_stale_minutes = _ic.STALE_AFTER_HOURS * 60
ok("the sweep is far faster than the condition it watches",
   w.CHECK_SECONDS / 60.0 < _stale_minutes / 100.0,
   f"sweep {w.CHECK_SECONDS / 60:.0f}min vs condition {_stale_minutes:.0f}min")

_failed = [l for l, p in _checks if not p]
print(f"\n{len(_checks) - len(_failed)} passed, {len(_failed)} failed")
sys.exit(1 if _failed else 0)
