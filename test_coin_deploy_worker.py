"""The worker creates branches with real money, so its guards are the test."""
import ast
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "coin_deploy_worker.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)


def fn(name):
    return next(n for n in ast.walk(TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def body(name):
    n = fn(name)
    return "\n".join(SRC.splitlines()[n.lineno - 1:n.end_lineno])


def code_only(name):
    """Parsed body with the docstring stripped - the docstrings here name
    every trap, so a text search matches them and proves nothing."""
    node = fn(name)
    stmts = [s for s in node.body
             if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                     and isinstance(s.value.value, str))]
    return ast.dump(ast.Module(body=stmts, type_ignores=[]))


def test_the_targets_are_the_three_that_were_verified():
    import coin_deploy_worker as W
    assert W.TARGET_COINS == ["PRIME-USD", "TON-USD", "APE-USD"]


def test_the_default_is_dry_run():
    node = fn("run_once")
    defaults = dict(zip([a.arg for a in node.args.args][-len(node.args.defaults):],
                        node.args.defaults))
    assert defaults["dry_run"].value is True


def test_dry_run_creates_nothing():
    b = body("run_once")
    assert b.index("if dry_run or not rows:") < b.index("create_grid_branch")


def test_it_rechecks_for_an_existing_branch_at_write_time():
    """Another pass, or a human, may have created it since the plan."""
    c = code_only("run_once")
    assert "exists" in c
    assert c.index("exists") < c.index("create_grid_branch")


def test_one_coin_failing_does_not_cost_the_others_their_turn():
    b = body("run_once")
    i = b.index("for r in rows:")
    loop_body = b[i:]
    assert "try:" in loop_body and "except Exception as e:" in loop_body


def test_an_unreadable_switch_stays_off():
    assert "staying OFF" in body("armed_now")
    c = code_only("armed_now")
    i = c.index("ExceptHandler")
    assert "ARMED_BY_DEFAULT" not in c[i:], "the error path must not use the default"


def test_it_places_no_orders():
    """It creates branches. A new branch buys its own dip through the gates
    that already exist - which is why those gates were verified first."""
    for forbidden in ("grid_buy", "grid_sell", "place_order", "CryptoGridSlice("):
        assert forbidden not in SRC, forbidden


def test_it_never_sells_anything():
    for forbidden in ("db.delete", "close_all", "force_close"):
        assert forbidden not in SRC, forbidden


def test_the_plan_is_recomputed_from_live_cash_every_pass():
    """Never from a figure carried over - the quoted $193.26 was $137.79 by
    the time branches could be created."""
    c = code_only("run_once")
    assert "get_real_free_cash_usd" in c
    assert "GRID_CASH_RESERVE_USD" in c


def test_held_coins_are_passed_so_the_pass_is_idempotent():
    c = code_only("run_once")
    assert "held_product_ids" in c
    assert "held" in c


def test_a_failed_pass_creates_nothing_and_keeps_going():
    b = body("loop")
    assert "nothing created" in b
    assert "await asyncio.sleep(CHECK_SECONDS)" in b


@pytest.mark.parametrize("raw,expected", [
    ("arm", True), ("on", True), ("1", True),
    ("observe", False), ("off", False), ("0", False),
    ("", None), ("maybe", None),
])
def test_env_switch_parsing(raw, expected):
    import coin_deploy_worker as W
    saved = os.environ.get("GRID_COIN_DEPLOY_MODE")
    try:
        os.environ["GRID_COIN_DEPLOY_MODE"] = raw
        assert W.env_mode() is expected
    finally:
        if saved is None:
            os.environ.pop("GRID_COIN_DEPLOY_MODE", None)
        else:
            os.environ["GRID_COIN_DEPLOY_MODE"] = saved


def test_removing_the_existing_branch_check_would_allow_a_duplicate():
    old = "if exists is not None:"
    assert SRC.count(old) == 1, "mutation anchor moved - this test is blind"
    assert SRC.replace(old, "if False:", 1) != SRC
