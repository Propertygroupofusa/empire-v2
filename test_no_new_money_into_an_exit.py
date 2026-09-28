"""A coin on the way out takes no new dollars.

Moving the concentration ceiling onto the account book lowered every
reading - ZEC from 31.00% to 19.33%, XRP from 30.07% to 19.68% - which
is the point of the account book, and which the owner chose.

But it removed an accidental brake. ZEC is the ONE coin the owner has
said to exit; redeploy_freed_cash has named it DEFAULT_SOURCE since it
was written, and resting_stops excludes it. Until this change the
ceiling refused new ZEC buys as a side effect of a 31% reading. At
19.33% it would not, and the branch is active with buys_paused=false.
The level cap (7 slices against 3 levels) happens to stop it today, but
that is arithmetic that changes the moment a slice sells.

So the brake is made explicit instead of incidental: a coin being
exited is refused by name, not by a percentage that happens to be high.

ONE-DIRECTIONAL. It can only ever refuse a buy. It never sells, never
resizes, never unblocks anything - so it cannot realize a loss and
cannot deepen a position. Exits are read from one place, the same
constant the redeploy plan uses, so there is no second list to fall out
of step.
"""
import crypto_grid_bot_exits as ex
import redeploy_freed_cash as rfc


def test_the_exit_list_is_the_redeploy_source_not_a_second_copy():
    """Tickers, because the wallet holds one pool per coin - the redeploy
    plan names the product and this names the coin under it."""
    assert ex.exiting_coins() == {rfc.DEFAULT_SOURCE.split("-")[0].upper()}


def test_the_coin_being_exited_is_refused_by_name():
    ok, why = ex.exit_verdict("ZEC-USD", exits=("ZEC-USD",))
    assert ok is False
    assert "exit" in why.lower()
    assert "Nothing is sold" in why


def test_every_other_coin_is_untouched():
    for p in ("XRP-USD", "NEAR-USD", "BTC-USD", "JASMY-USD"):
        ok, why = ex.exit_verdict(p, exits=("ZEC-USD",))
        assert ok is True, (p, why)


def test_matching_is_by_coin_not_by_string():
    for spelling in ("ZEC", "zec-usd", "ZEC-USD", "Zec"):
        ok, _ = ex.exit_verdict(spelling, exits=("ZEC-USD",))
        assert ok is False, spelling
    for spelling in ("ZEC", "zec"):
        ok, _ = ex.exit_verdict("ZEC-USD", exits=(spelling,))
        assert ok is False, spelling


def test_an_empty_exit_list_refuses_nothing():
    """One-directional: with nothing being exited this cannot bite."""
    for e in ((), []):
        ok, _ = ex.exit_verdict("ZEC-USD", exits=e)
        assert ok is True


def test_none_means_use_the_configured_list_not_an_empty_one():
    """The distinction that decides whether a caller passing nothing gets
    the guard or silently opts out of it. None takes the default."""
    ok, _ = ex.exit_verdict("ZEC-USD", exits=None)
    assert ok is False
    ok, _ = ex.exit_verdict("ZEC-USD")
    assert ok is False


def test_it_can_only_refuse_and_never_sell_or_resize():
    """The safety property, asserted on the parsed source."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(ex))
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for forbidden in ("place", "place_market_sell", "place_market_buy",
                      "grid_sell", "cancel", "commit", "execute"):
        assert forbidden not in calls, f"{forbidden} in a refuse-only module"
    # Every public entry point returns a verdict, never performs one.
    assert {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
            and not n.name.startswith("_")} == {"exiting_coins", "exit_verdict"}


def test_the_buy_path_checks_it_before_spending():
    """An AST read of the real call site, not a docstring saying so."""
    import ast
    src = open("crypto_grid_bot.py", encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
              and n.name == "run_grid_branch_cycle")
    body = ast.get_source_segment(src, fn)
    assert "exit_verdict" in body, "the buy path does not consult the exit list"
    assert body.index("exit_verdict") < body.index("grid_buy"), (
        "the exit check must come before the order is placed")
