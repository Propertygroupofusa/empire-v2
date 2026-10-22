"""STATUS.md says what each number is.

The owner pasted the 2026-10-22 17:09Z snapshot. Three of its figures read as
losses that were not what they looked like:

  * Grid "Real Realized P&L -$158.20" - the dashboard's banked profit for the
    same 306 trades was +$450.82. Four inherited ZEC sales were booked against
    their adoption mark; get_grid_trade_history already publishes them
    restated at the real cost, and this file ignored that.
  * Family tree "Total Profit (realized + unrealized)" and Alpaca "Total
    Profit" - closed results and paper on stocks still held, in one number.
  * "Real free cash available to fund a branch: -$856.76" - US dollars only,
    printed beside $3,305.35 of USDC without saying so.

And forty identical RECONCILE_REFUSED rows filled the whole activity table.

Fakes only - no database, no venue, no git.

Run as written: python3 test_status_snapshot_honest.py
"""
import asyncio
import sys

import pytest

import status_snapshot as snap


def run(c):
    return asyncio.run(c)


class _DB:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        raise RuntimeError("no database in this test")


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    monkeypatch.setattr(snap, "get_session_factory", lambda: (lambda: _DB()))


# ------------------------------------------------------------- the formatter
@pytest.mark.parametrize("n,want", [
    (450.82, "+$450.82 profit"), (-18.59, "-$18.59 loss"), (0, "$0.00"),
    (None, "—"), (-0.004, "$0.00"),
])
def test_every_result_says_profit_or_loss(n, want):
    assert snap._fmt_pl(n) == want


# -------------------------------------------------------------------- grid
def _grid(monkeypatch, history):
    import crypto_grid_bot as grid

    async def status():
        return {"mode_active": True, "branches": [], "total_allocated_usd": 4731.69,
                "real_free_cash_usd": 2448.59, "drawdown_breaker_pct": 0.25}

    async def trades(*a, **k):
        return history
    monkeypatch.setattr(grid, "get_grid_status", status)
    monkeypatch.setattr(grid, "get_grid_trade_history", trades)
    return run(snap._build_grid_section())


TODAY = {"branches": [{"total_pnl": 153.04}, {"total_pnl": -311.24}],
         "total_trade_count": 306, "realized_own_usd": 153.04,
         "realized_adopted_usd": -311.24, "realized_adopted_restated_usd": 297.78,
         "realized_adopted_restated_count": 4}


def test_the_grid_leads_with_what_the_dashboard_calls_banked(monkeypatch):
    out = _grid(monkeypatch, TODAY)
    assert "Banked profit (every closed sale, at what was really paid):** +$450.82 profit over 306 trades" in out
    assert "Real Realized P&L" not in out


def test_the_raw_booking_is_still_shown_and_says_why_it_differs(monkeypatch):
    out = _grid(monkeypatch, TODAY)
    line = next(l for l in out.splitlines() if "Booked before restating" in l)
    assert "4 inherited sale(s)" in line and "-$158.20" in line and "+$297.78 profit" in line


def test_nothing_restated_means_no_restatement_line(monkeypatch):
    h = dict(TODAY, branches=[{"total_pnl": 153.04}], realized_adopted_usd=0.0,
             realized_adopted_restated_usd=None, realized_adopted_restated_count=0)
    out = _grid(monkeypatch, h)
    assert "+$153.04 profit" in out and "Booked before restating" not in out


def test_an_unreadable_history_shows_a_dash_not_a_zero(monkeypatch):
    import crypto_grid_bot as grid

    async def boom(*a, **k):
        raise RuntimeError("db down")
    _grid(monkeypatch, TODAY)
    monkeypatch.setattr(grid, "get_grid_trade_history", boom)
    out = run(snap._build_grid_section())
    assert "Banked profit (every closed sale, at what was really paid):** —" in out


# ------------------------------------------------------------ family tree
def _tree(monkeypatch, branches, coins, **status):
    import routers.trading_dashboard as td

    async def fts(db=None):
        return dict({"branches": branches, "spendable_for_spawn": -856.76,
                     "real_usd_balance": 0.98, "real_usdc_balance": 3305.35}, **status)

    async def hist(db=None):
        return {"coins": coins}
    monkeypatch.setattr(td, "get_family_tree_status", fts)
    monkeypatch.setattr(td, "get_coin_trade_history", hist)
    return run(snap._build_crypto_section())


def test_closed_and_paper_are_two_lines_never_one_sum(monkeypatch):
    coins = [{"product_id": "POL-USD", "trade_count": 87, "win_rate": 14.9, "total_pnl": -392.43},
             {"product_id": "BTC-USD", "trade_count": 12, "win_rate": 33.3, "total_pnl": -116.01}]
    held = [{"bot_name": "b", "product_id": "X-USD", "allocated_usd": 10.0,
             "position": {"qty": 2.0, "entry_price": 10.0, "current_price": 9.0}}]
    out = _tree(monkeypatch, held, coins)
    assert "Closed trades (this older family-tree bot, all time):** -$508.44 loss" in out
    assert "Open positions on paper:** -$2.00 (nothing sold" in out
    assert "realized + unrealized" not in out and "-$510.44" not in out


def test_the_cash_line_says_it_counts_us_dollars_only(monkeypatch):
    out = _tree(monkeypatch, [], [])
    assert "US-dollar cash left after every branch's unspent budget (USDC not counted):** -$856.76" in out
    assert "Real free cash available to fund a branch" not in out
    assert "$3,305.35" in out


# ------------------------------------------------------------------ alpaca
def test_alpaca_splits_paper_from_everything_else(monkeypatch):
    import routers.trading_dashboard as td

    async def overview(db=None):
        return {"equity": 947.47, "cash": 582.26, "session_pl": -14.44, "locked_usd": 0.0,
                "bots": [{"name": f"bot_{i}", "capital": 118.43, "pl": -4.03} for i in range(1, 9)],
                "positions": [
                    {"symbol": "MSFT", "side": "long", "qty": 0.231633, "avg_entry_price": 540.138, "current_price": 524.2},
                    {"symbol": "NVDA", "side": "long", "qty": 0.563193, "avg_entry_price": 224.148, "current_price": 212.53},
                    {"symbol": "SLV", "side": "long", "qty": 2.510264, "avg_entry_price": 50.796221, "current_price": 49.44}]}
    monkeypatch.setattr(td, "get_alpaca_overview", overview)
    out = run(snap._build_alpaca_section())
    # -32.24 in the buckets; -13.64 of it is the three stocks still held at
    # these prices, so -18.60 is everything else
    assert "On paper in stocks still held:** -$13.64 (nothing sold" in out
    assert "since the first recorded balance:** -$18.60 loss" in out
    assert "**Total Profit:**" not in out


# ---------------------------------------------------------------- activity
def _rows():
    rows = [{"created_at": f"2026-10-22T16:{m:02d}:00", "bot_name": "grid",
             "event_type": "RECONCILE_REFUSED", "message": f"REFUSED {m}"} for m in (54, 39, 24)]
    rows.append({"created_at": "2026-10-22T11:03:19", "bot_name": "coin_picker",
                 "event_type": "BUY", "message": "coin picker bought DRV"})
    rows += [{"created_at": f"2026-10-22T10:{m:02d}:00", "bot_name": "grid",
              "event_type": "RECONCILE_REFUSED", "message": f"REFUSED {m}"} for m in (52, 37)]
    rows += [{"created_at": f"2026-10-22T09:{m:02d}:00", "bot_name": "coin_picker",
              "event_type": "BUY", "message": f"buy {m}"} for m in (5, 1)]
    return rows


def test_a_repeating_refusal_folds_into_one_row_with_a_count():
    out = snap.collapse_repeats(_rows())
    assert [r["event_type"] for r in out] == ["RECONCILE_REFUSED", "BUY", "RECONCILE_REFUSED", "BUY", "BUY"]
    assert out[0]["repeats"] == 3 and out[0]["message"] == "REFUSED 54"
    assert out[0]["first_at"] == "2026-10-22T16:24:00"
    assert out[2]["repeats"] == 2


def test_buys_and_sells_are_never_folded():
    out = snap.collapse_repeats(_rows())
    assert [r["message"] for r in out if r["event_type"] == "BUY"] == [
        "coin picker bought DRV", "buy 5", "buy 1"]
    assert all("repeats" not in r for r in out if r["event_type"] == "BUY")


def test_folding_leaves_the_input_untouched():
    rows = _rows()
    snap.collapse_repeats(rows)
    assert all("repeats" not in r for r in rows)


def test_the_activity_table_shows_the_count(monkeypatch):
    import crypto_family_tree_bot as tree

    async def feed(limit=50):
        assert limit == snap.ACTIVITY_FETCH
        return _rows()
    monkeypatch.setattr(tree, "get_activity_feed", feed)
    out = run(snap._build_activity_section())
    assert "RECONCILE_REFUSED ×3" in out and "since 2026-10-22 16:24:00" in out
    assert "coin picker bought DRV" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
