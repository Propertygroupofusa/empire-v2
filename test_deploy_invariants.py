"""Properties that must hold across the whole deployment, not one module.

Every check here exists because something in this session went wrong in a
way no per-module test could have caught:

  an undefined `logger` stopped two safety loops from ever starting, and
  was invisible because the failure was logged under a message about a
  different subsystem;

  a patch anchored on a line that appears in two endpoints landed in the
  wrong one, so a heartbeat reported "not deployed" for hours while the
  deploy had in fact landed;

  a docstring claimed a trailing space would NOT arm a loop that sells,
  when the code strips whitespace and it does.

Each of those is a cross-cutting property. They belong in one file that
runs on every change rather than in whichever module happened to notice.
"""
import ast
import importlib

import pytest


VENUE_WORKERS = ["auto_trim_worker", "resting_stops_worker"]
PURE = ["auto_trim", "position_rules", "cross_rates", "walk_forward",
        "resting_stops", "grid_universe", "branch_expansion"]
FILES_WITH_TASKS = ["main.py", "auto_trim_worker.py", "resting_stops_worker.py",
                    "routers/trading_dashboard.py"]


@pytest.mark.parametrize("mod", PURE + VENUE_WORKERS)
def test_every_module_imports(mod):
    importlib.import_module(mod)


@pytest.mark.parametrize("path", FILES_WITH_TASKS)
def test_no_file_uses_an_undefined_logger(path):
    """`log` is defined in main.py; `logger` never has been.

    Three background-task blocks wrote logger.info AND logger.warning
    inside their own except handlers, so the handler raised too and the
    exception escaped, taking the next two task registrations with it.
    """
    tree = ast.parse(open(path).read())
    bad = [n for n in ast.walk(tree)
           if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
           and n.value.id == "logger"]
    assert not bad, f"{path} uses an undefined `logger` {len(bad)} time(s)"


@pytest.mark.parametrize("mod", PURE)
def test_deciding_modules_cannot_reach_a_venue(mod):
    """Sizing is testable without an account; only workers may talk."""
    tree = ast.parse(open(mod + ".py").read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None)]
            for n in names:
                assert not any(b in (n or "") for b in
                               ("aiohttp", "requests", "httpx", "urllib")), \
                    f"{mod} imports {n}"


@pytest.mark.parametrize("mod", VENUE_WORKERS)
def test_every_venue_worker_is_disarmed_by_default(mod, monkeypatch):
    m = importlib.import_module(mod)
    env = getattr(m, "MODE_ENV", None) or m.resting_stops.MODE_ENV
    monkeypatch.delenv(env, raising=False)
    assert m.current_mode() != "arm", f"{mod} arms itself with {env} unset"


@pytest.mark.parametrize("mod", VENUE_WORKERS)
@pytest.mark.parametrize("value", ["true", "yes", "1", "on", "ARMED", "armed",
                                   "enable", "", "arm arm", "a rm"])
def test_only_arm_arms(mod, value, monkeypatch):
    m = importlib.import_module(mod)
    env = getattr(m, "MODE_ENV", None) or m.resting_stops.MODE_ENV
    monkeypatch.setenv(env, value)
    assert m.current_mode() != "arm", f"{mod} armed on {value!r}"


@pytest.mark.parametrize("mod", VENUE_WORKERS)
def test_every_venue_worker_has_a_heartbeat(mod):
    """Without one, "not running", "failing every pass" and "not yet" all
    look identical from outside - which cost hours of wrong diagnosis."""
    m = importlib.import_module(mod)
    hb = getattr(m, "HEARTBEAT", None)
    assert isinstance(hb, dict)
    for k in ("started_at", "last_pass_at", "passes", "last_error"):
        assert k in hb, f"{mod} heartbeat missing {k}"


def test_state_changing_routes_are_behind_the_write_guard():
    import write_guard
    for path in ("/api/trading-dashboard/grid-universe/expand",
                 "/api/trading-dashboard/coinbase/sell-amount",
                 "/api/crypto/withdraw"):
        assert write_guard.is_protected("POST", path), f"{path} is unguarded"
    for path in ("/api/trading-dashboard/auto-trim",
                 "/api/trading-dashboard/resting-stops",
                 "/api/trading-dashboard/gate-verdict",
                 "/api/trading-dashboard/grid-universe"):
        assert not write_guard.is_protected("GET", path), f"{path} reads should be open"


def test_no_two_endpoints_end_with_the_same_cache_epilogue():
    """A patch anchored on a shared line landed in the wrong function.

    Two endpoints both ended with `out["served_from_cache"] = False`, so a
    one-count replace hit whichever came first. The fix is not vigilance;
    it is that a unique anchor must exist. If this fails, a future patch
    will land in the wrong endpoint again.
    """
    src = open("routers/trading_dashboard.py").read()
    line = 'out["served_from_cache"] = False'
    n = src.count(line)
    assert n <= 1 or src.count('_AUTO_TRIM_CACHE["at"] = _time.time()') == 1, (
        f"{line} appears {n} times with no unique neighbouring anchor")


def test_no_endpoint_awaits_a_synchronous_helper():
    """/resting-stops 500'd on every call from the moment it shipped.

    `_cached_watch` is a plain def returning a dict or None, and the new
    endpoint wrote `await _cached_watch(30)`. Awaiting a dict raises, so
    the route was dead on arrival - and the module behind it had 62
    passing tests, because the tests exercised the MODULE and nobody ever
    called the ROUTE.

    This walks the router for awaits of helpers defined without `async`,
    which is the class of mistake rather than the one instance.
    """
    src = open("routers/trading_dashboard.py").read()
    tree = ast.parse(src)

    sync_defs = {n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef)}
    async_defs = {n.name for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef)}
    sync_only = sync_defs - async_defs

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            f = node.value.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name in sync_only:
                offenders.append(name)
    assert not offenders, f"awaiting synchronous helper(s): {sorted(set(offenders))}"


def test_every_new_route_is_registered_exactly_once():
    """A duplicated @router.get path silently shadows the first handler."""
    src = open("routers/trading_dashboard.py").read()
    tree = ast.parse(src)
    paths = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for d in node.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and d.func.attr in ("get", "post", "put", "delete")
                        and d.args and isinstance(d.args[0], ast.Constant)):
                    paths.append((d.func.attr, d.args[0].value))
    dupes = {p for p in paths if paths.count(p) > 1}
    assert not dupes, f"duplicate routes shadow each other: {sorted(dupes)}"
