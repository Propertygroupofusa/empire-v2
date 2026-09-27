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


def test_every_inline_script_on_the_dashboard_parses():
    """One typo in that inline <script> kills EVERY panel, not just the new one.

    The dashboard is a single 450KB page with all of its behaviour in one
    inline script, so a stray backtick in a template literal does not
    degrade one card - it stops the whole file executing and the page
    renders as static furniture with every panel stuck on "Not loaded."
    Python's parser cannot see that, and no other test in this repo reads
    the page as code.
    """
    import re
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("no node available to parse the page's JavaScript")

    html = open("family_tree_dashboard.html", encoding="utf-8").read()
    blocks = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
    assert blocks, "found no inline script - the regex is broken, not the page"

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as fh:
        fh.write("\n;\n".join(blocks))
        path = fh.name
    r = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert r.returncode == 0, f"the dashboard's JavaScript does not parse:\n{r.stderr}"


def test_the_kpi_panel_only_reads_fields_the_endpoint_sends():
    """The panel is the one consumer of /capital-kpis and nothing type-checks
    the boundary. A renamed key renders as `undefined` on a money tile."""
    import capital_kpis
    import re

    produced = set(capital_kpis.compute([], allocated_usd=100.0))
    produced |= {"bottleneck", "bottleneck_detail", "what_would_move_it",
                 "ledger_note", "capital_note", "ledger_rows_read",
                 "ledger_row_limit", "backing_verdict", "claimed_usd",
                 "headline", "is_a_measurement_not_a_change",
                 "served_from_cache", "cache_age_seconds"}

    html = open("family_tree_dashboard.html", encoding="utf-8").read()
    start = html.index("async function loadCapitalKpis()")
    body = html[start:html.index("\n}", html.index("} catch (e)", start))]
    read = set(re.findall(r"\bd\.([A-Za-z_][A-Za-z0-9_]*)", body))
    assert read, "found no field reads - the slice is wrong, not the panel"
    assert read <= produced, read - produced


def _panel_fields(fn_name):
    """Every `d.<field>` a dashboard panel reads, straight from the page."""
    import re
    html = open("family_tree_dashboard.html", encoding="utf-8").read()
    start = html.index(f"async function {fn_name}(")
    end = html.index("\n}", html.index("} catch (e)", start))
    return set(re.findall(r"\bd\.([A-Za-z_][A-Za-z0-9_]*)", html[start:end]))


def test_the_growth_curve_panel_only_reads_fields_the_endpoint_sends():
    import growth_ledger
    from datetime import datetime, timedelta
    t0 = datetime(2026, 9, 27)
    snaps = [{"captured_at": t0 + timedelta(hours=i), "bottleneck": "CAPITAL_OUTSIDE",
              "note": None, "census_carried": False,
              **{f: 1.0 for f in growth_ledger.FIELDS}} for i in range(3)]
    produced = set(growth_ledger.summarise(snaps))
    produced |= set(growth_ledger.summarise([]))          # the no-readings shape
    produced |= {"recorder", "is_a_measurement_not_a_change", "what_happens_next"}
    read = _panel_fields("loadGrowthCurve")
    assert read, "found no field reads - the slice is wrong, not the panel"
    assert read <= produced, read - produced


def test_the_placement_panel_only_reads_fields_the_endpoint_sends():
    import capital_placement
    p = capital_placement.plan(kpis={"net_edge_per_trade_usd": 0.2, "net_usd": 19.61},
                               holdings=[], branches=[], free_cash_usd=481.52,
                               account_total_usd=11397.11, realized_usd=19.61)
    refused = capital_placement.plan(kpis={"net_edge_per_trade_usd": -1.0},
                                     holdings=[], branches=[], free_cash_usd=0.0,
                                     account_total_usd=0.0)
    produced = set(p) | set(refused) | {
        "notes", "account_total_usd", "claimed_usd", "deployed_coin_usd",
        "free_cash_usd", "served_from_cache", "cache_age_seconds"}
    read = _panel_fields("loadPlacement")
    assert read, "found no field reads - the slice is wrong, not the panel"
    assert read <= produced, read - produced


def test_every_background_worker_is_registered_exactly_once():
    """A worker imported twice runs twice - two loops placing against one
    account. A worker imported zero times is the /auto-trim failure: the
    module had tests, the route existed, and nothing ever started it."""
    src = open("main.py").read()
    for worker in ("auto_trim_worker", "resting_stops_worker", "growth_ledger_worker",
                   "coin_adoption_worker"):
        starts = src.count(f"{worker}.run_periodically(")
        assert starts == 1, f"{worker} started {starts} times in main.py"


def test_the_snapshot_writer_only_sets_columns_the_table_has():
    """A typo'd kwarg on the model raises at INSERT - inside a background
    loop, where it would be swallowed into a warning and the series would
    silently never fill."""
    import ast as _ast
    import models
    cols = {c.name for c in models.CapitalKpiSnapshot.__table__.columns}
    tree = _ast.parse(open("growth_ledger_worker.py").read())
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name)
                and node.func.id == "CapitalKpiSnapshot"):
            for kw in node.keywords:
                if kw.arg is not None:
                    assert kw.arg in cols, kw.arg
            for kw in node.keywords:
                if kw.arg is None:          # **{...} - check the literal keys
                    for d in _ast.walk(kw.value):
                        if isinstance(d, _ast.Dict):
                            for k in d.keys:
                                if isinstance(k, _ast.Constant) and isinstance(k.value, str):
                                    assert k.value in cols, k.value


def test_the_beta_check_panel_only_reads_fields_the_endpoint_sends():
    import beta_check
    out = beta_check.scan({"1.0%@6h": {"A": -1.0, "B": -1.0, "C": -1.0}},
                          {"A": 19.0, "B": 16.0, "C": -2.6})
    produced = set(out) | {"available", "reason", "window_returns_pct", "instruments",
                           "study_as_of", "study_days", "what_this_does_not_say"}
    read = _panel_fields("loadBetaCheck")
    assert read, "found no field reads - the slice is wrong, not the panel"
    assert read <= produced, read - produced


def test_the_adoption_panel_only_reads_fields_the_endpoint_sends():
    import coin_adoption
    p = coin_adoption.plan(
        [{"asset": "XLM", "units": 2641.0, "price": 0.2129, "usd": 562.34}],
        account_total_usd=11418.75, claimed_products=())
    produced = set(p) | {"notes", "account_total_usd", "coin_usd", "cash_usd",
                         "claimed_products", "is_armed", "arming", "mode", "worker",
                         "adopted_stop_pct", "served_from_cache", "cache_age_seconds"}
    read = _panel_fields("loadAdoption")
    assert read, "found no field reads - the slice is wrong, not the panel"
    assert read <= produced, read - produced


def test_no_adoption_endpoint_can_place_or_write():
    """The planner ships before the executor. Until an armed worker
    exists, nothing on this path may reach a venue or the slice table."""
    import ast as _ast
    src = open("routers/trading_dashboard.py").read()
    tree = _ast.parse(src)
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, (_ast.AsyncFunctionDef, _ast.FunctionDef))
              and n.name == "coin_adoption_preview")
    called = set()
    for node in _ast.walk(fn):
        if isinstance(node, _ast.Call):
            f = node.func
            called.add(f.id if isinstance(f, _ast.Name) else getattr(f, "attr", ""))
    for banned in ("create_grid_branch", "grid_buy", "grid_sell", "place_market_buy",
                   "place_market_sell", "commit", "add"):
        assert banned not in called, f"the adoption preview CALLS {banned}"


def test_the_league_panel_only_reads_fields_the_endpoint_sends():
    import coin_league
    out = coin_league.table({"A-USD": [{"pnl": 1.0, "qty": 1.0, "entry_price": 100.0}] * 25},
                            window_returns={"A-USD": -8.0})
    out2 = coin_league.table({"A-USD": [{"pnl": 1.0, "qty": 1.0, "entry_price": 100.0}] * 25},
                             window_returns={"A-USD": -8.0}, held_products=["A-USD"])
    produced = set(out) | set(out2) | {"notes", "window_returns_pct", "blueprint"}
    read = _panel_fields("loadLeague")
    assert read, "found no field reads - the slice is wrong, not the panel"
    assert read <= produced, read - produced


def test_the_standings_writer_only_sets_columns_the_table_has():
    """The league rows are built as a dict of literals, so a typo lands as
    a TypeError inside a background loop and the series silently never
    fills - the same failure shape as the snapshot writer."""
    import ast as _ast
    import models
    cols = {c.name for c in models.CoinLeagueSnapshot.__table__.columns}
    tree = _ast.parse(open("growth_ledger_worker.py").read())
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "_league_rows")
    keys = set()
    for node in _ast.walk(fn):
        if isinstance(node, _ast.Dict):
            for k in node.keys:
                if isinstance(k, _ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    # grid_pct / num_levels belong to the config dict, not the table row.
    keys -= {"grid_pct", "num_levels"}
    assert keys, "found no literal keys - the walk is broken, not the code"
    assert keys <= cols, keys - cols


def test_a_failed_league_never_stops_the_account_series_being_written():
    """The account KPIs answer the owner's actual question. A standings
    build that throws must degrade to a note, not take the pass down."""
    import ast as _ast
    tree = _ast.parse(open("growth_ledger_worker.py").read())
    fn = next(n for n in _ast.walk(tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "_league_rows")
    handlers = [n for n in _ast.walk(fn) if isinstance(n, _ast.ExceptHandler)]
    assert handlers, "_league_rows has no except - a throw would take the pass with it"
    # The inner handler around a single unreadable slice correctly just
    # skips it; what matters is that the OUTER one returns a note rather
    # than re-raising into the pass.
    assert any(any(isinstance(x, _ast.Return) for x in _ast.walk(h)) for h in handlers), \
        "no handler RETURNS - a throw would take the account series with it"
    for h in handlers:
        assert not any(isinstance(x, _ast.Raise) for x in _ast.walk(h)), \
            "a handler re-raises, which would take the pass down"


def test_the_loss_panel_only_reads_fields_the_endpoint_sends():
    import loss_study
    rows = [{"pnl": 1.0, "qty": 1.0, "entry_price": 100.0, "exit_reason": "profit_target"}] * 30
    produced = set(loss_study.analyse(rows, config_epoch="2026-09-26T02:45:00Z")) | {
        "stop_sweep", "verdict", "verdict_detail",
        "why_zero_losses_is_the_wrong_target", "is_a_measurement_not_a_change"}
    read = _panel_fields("loadLossStudy")
    assert read, "found no field reads - the slice is wrong, not the panel"
    assert read <= produced, read - produced
