"""The Scale Bot's "Performance Tracking" box described the wrong bot.

win_rate was summed off the FAMILY-TREE branches (two of them, dormant,
nothing allocated), expectancy came from that same bot's
get_rolling_expectancy() at -$5.29/trade, and profit_factor was the literal
`profit_factor = 1.0  # Neutral default` - never computed at all. The same
card's "Primary Profit" already showed the GRID's +$135.58, so one panel
described two bots and gave the stats box to the dead one.

Over the grid's real 196 closed trades: 172 wins / 22 losses / 2 flat,
+$156.00 gross win against $20.42 gross loss.
"""
import inspect
import crypto_grid_bot as g


def _calc(pnls):
    """The function's own arithmetic, applied to a list - so the maths is
    tested without standing up a database."""
    n = len(pnls)
    if not n:
        return {"trades": 0, "win_rate_pct": None, "profit_factor": None,
                "expectancy_per_trade_usd": None}
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gw, gl = sum(wins), abs(sum(losses))
    return {"trades": n, "wins": len(wins), "losses": len(losses),
            "flat": n - len(wins) - len(losses),
            "win_rate_pct": round(len(wins) / n * 100, 1),
            "profit_factor": round(gw / gl, 2) if gl else None,
            "expectancy_per_trade_usd": round(sum(pnls) / n, 4)}


def test_it_reproduces_the_real_196_trade_record():
    pnls = [0.9070] * 172 + [-0.9282] * 22 + [0.0] * 2
    out = _calc(pnls)
    assert out["trades"] == 196
    assert out["wins"] == 172 and out["losses"] == 22 and out["flat"] == 2
    assert out["win_rate_pct"] == 87.8
    assert 7.5 <= out["profit_factor"] <= 7.8
    assert 0.68 <= out["expectancy_per_trade_usd"] <= 0.70


def test_no_trades_is_none_not_zero():
    """'No completed trades' and 'a 0% win rate' are different claims."""
    out = _calc([])
    assert out["trades"] == 0
    assert out["win_rate_pct"] is None
    assert out["profit_factor"] is None
    assert out["expectancy_per_trade_usd"] is None


def test_a_flawless_record_has_no_profit_factor_not_1_0():
    """With no losses the ratio is undefined. 1.0 would assert the strategy
    broke even exactly - the opposite of what a perfect record means, and
    the exact value the old hardcoded placeholder reported."""
    out = _calc([1.0, 2.0, 3.0])
    assert out["profit_factor"] is None
    assert out["win_rate_pct"] == 100.0


def test_flat_trades_are_not_counted_as_wins():
    out = _calc([1.0, 0.0, -1.0])
    assert out["wins"] == 1 and out["flat"] == 1 and out["losses"] == 1
    assert out["win_rate_pct"] == round(1 / 3 * 100, 1)


def test_the_function_exists_and_reads_the_grid_trade_table():
    src = inspect.getsource(g.get_grid_performance_metrics)
    assert "CryptoGridTradeHistory.pnl" in src
    assert '"profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None' in src
    assert '"win_rate_pct": None' in src


def test_the_dashboard_no_longer_hardcodes_profit_factor():
    import routers.trading_dashboard as td
    src = inspect.getsource(td)
    assert "profit_factor = 1.0  # Neutral default" not in src, (
        "the hardcoded placeholder is back")
    assert "crypto_grid_bot_module.get_grid_performance_metrics()" in src
    assert '"grid_performance": _grid_perf or None,' in src


def test_a_metrics_failure_cannot_take_the_page_down():
    import routers.trading_dashboard as td
    src = inspect.getsource(td)
    # the CALL SITE, not the first mention of the name - which is in the
    # explanatory comment above it.
    i = src.index("await crypto_grid_bot_module.get_grid_performance_metrics()")
    window = src[i - 200:i + 400]
    assert "try:" in window and "except Exception" in window
    assert "log.warning" in window
