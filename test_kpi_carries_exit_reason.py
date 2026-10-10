"""The capital allocator must be able to see which closes were inherited.

WHY THIS TEST EXISTS, measured live on 2026-10-10.

capital_kpis.compute() excludes `adopted_exit` rows from the edge, and the
comment above ADOPTED_EXIT_REASON explains exactly why: on 2026-10-04 four
ZEC closes the grid never chose either end of flipped net_edge_per_trade_usd
negative, bottleneck() turned that into NO_EDGE, and /capital-placement
refused every capital lever at once - total_addressable_usd $0.00.

The exclusion was correct and it never fired. The dict comprehensions that
hand rows to compute() did not copy `exit_reason` off the ORM row, so every
row arrived with exit_reason None, `inherited` was always empty, and the
guard could not match anything. Measured against the real 268-row ledger:

    exit_reason dropped : 268 trades, -$166.20, edge -0.6201, NO_EDGE
    exit_reason carried : 264 trades, +$145.04, edge +0.5494, HEALTHY

A field that a filter depends on is not optional, so this test pins both
halves: the builders carry it, and carrying it changes the verdict.
"""
import ast
import capital_kpis


ROUTER = "routers/trading_dashboard.py"


def _compute_arg_names(fn):
    """Names passed as the first argument to capital_kpis.compute() inside fn."""
    names = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        target = (f.attr if isinstance(f, ast.Attribute) else
                  f.id if isinstance(f, ast.Name) else None)
        if target != "compute":
            continue
        if isinstance(f, ast.Attribute) and not (
                isinstance(f.value, ast.Name) and f.value.id == "capital_kpis"):
            continue
        if n.args:
            a = n.args[0]
            if isinstance(a, ast.Name):
                names.add(a.id)
            elif isinstance(a, ast.BoolOp):          # `trades or []`
                for v in a.values:
                    if isinstance(v, ast.Name):
                        names.add(v.id)
    return names


def _kpi_feeding_builders():
    """Every list comprehension whose result is handed to
    capital_kpis.compute(), found by following the argument name back to its
    assignment in the same function - not by shape, so a builder that merely
    looks similar is not swept in and a real one cannot hide."""
    tree = ast.parse(open(ROUTER).read())
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        wanted = _compute_arg_names(fn)
        if not wanted:
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.ListComp):
                continue
            if not any(isinstance(t, ast.Name) and t.id in wanted for t in node.targets):
                continue
            elt = node.value.elt
            if not isinstance(elt, ast.Dict):
                continue
            out.append((node.lineno,
                        {k.value for k in elt.keys if isinstance(k, ast.Constant)}))
    return out


def test_at_least_one_builder_is_found():
    """If the walk finds nothing the assertions below are vacuous."""
    found = _kpi_feeding_builders()
    assert len(found) >= 4, f"expected the known compute() feeders, found {found}"


def test_every_grid_row_builder_carries_exit_reason():
    """THE ONE THAT CATCHES IT. A row reaching compute() without exit_reason
    silently defeats the inherited-exit guard: `inherited` matches nothing,
    the four adopted_exit closes stay in the edge, and bottleneck() returns
    NO_EDGE, which refuses every capital lever at once."""
    missing = [(ln, sorted(keys)) for ln, keys in _kpi_feeding_builders()
               if "exit_reason" not in keys]
    assert not missing, (
        "these row builders feed capital_kpis.compute() without exit_reason, "
        f"so adopted_exit rows cannot be excluded: {missing}")


def test_no_alpaca_row_shape_reaches_this_path():
    """Guards the account separation, not the bug. The stocks ledger keys on
    symbol/bot; if one of those rows ever reached the Coinbase KPI path the two
    accounts would be measured as one, which the owner has ruled out outright.
    (The Alpaca builder lives behind alpaca_growth and is correctly invisible
    to the walk above - this asserts it stays that way.)"""
    for ln, keys in _kpi_feeding_builders():
        assert "symbol" not in keys and "bot" not in keys, (
            f"an Alpaca-shaped row builder at line {ln} feeds the Coinbase "
            f"capital_kpis path: {sorted(keys)}")


def _ledger(n_own=264, own_total=145.04, inherited=(-26.72, -123.21, -123.24, -38.07)):
    """The real shape of the 2026-10-10 book: a profitable own ledger plus the
    four ZEC bookkeeping exits."""
    each = own_total / n_own
    rows = [{"pnl": each, "qty": 1.0, "entry_price": 1.0, "exit_price": 1.0,
             "product_id": "XRP-USD", "closed_at": f"2026-10-0{i % 9 + 1}T00:00:00Z",
             "exit_reason": "profit_target"} for i in range(n_own)]
    rows += [{"pnl": p, "qty": 1.0, "entry_price": 1.0, "exit_price": 1.0,
              "product_id": "ZEC-USD", "closed_at": "2026-10-04T08:30:00Z",
              "exit_reason": "adopted_exit"} for p in inherited]
    return rows


def test_inherited_exits_are_excluded_and_the_edge_flips():
    rows = _ledger()
    carried = capital_kpis.compute(rows, allocated_usd=4296.43)
    assert carried["inherited_excluded"] == 4
    assert carried["trades"] == 264
    assert carried["net_edge_per_trade_usd"] > 0, \
        "the own ledger is profitable; the edge must read positive once inherited exits are out"

    dropped = [{k: v for k, v in r.items() if k != "exit_reason"} for r in rows]
    blind = capital_kpis.compute(dropped, allocated_usd=4296.43)
    assert blind["inherited_excluded"] == 0
    assert blind["trades"] == 268
    assert blind["net_edge_per_trade_usd"] < 0, \
        "this is the live failure: without the field the four exits drag the edge negative"


def test_the_verdict_that_gates_the_money_changes_with_it():
    """bottleneck() is what /capital-placement refuses on. NO_EDGE there means
    total_addressable_usd 0.00 - no dollar may be placed anywhere."""
    rows = _ledger()
    carried, _ = capital_kpis.bottleneck(capital_kpis.compute(rows, allocated_usd=4296.43))
    dropped = [{k: v for k, v in r.items() if k != "exit_reason"} for r in rows]
    blind, _ = capital_kpis.bottleneck(capital_kpis.compute(dropped, allocated_usd=4296.43))
    assert blind == "NO_EDGE"
    assert carried != "NO_EDGE"


def test_an_inherited_gain_is_excluded_too():
    """The rule is symmetric by design - an inherited WIN would unlock the
    levers just as falsely as an inherited loss locks them."""
    rows = _ledger(inherited=(+400.0,))
    k = capital_kpis.compute(rows, allocated_usd=4296.43)
    assert k["inherited_excluded"] == 1
    assert k["net_usd"] < 200, "an inherited gain must not inflate the ledger"
