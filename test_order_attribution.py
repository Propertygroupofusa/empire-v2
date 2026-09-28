"""Tagging client_order_id would not have worked. order_id does.

maker_only_holds can say a taker leg was billed after maker-only was
armed, but never WHICH of six paths placed it. The obvious fix was to
prefix the client_order_id at placement.

Checked against a real Coinbase fill before building it. The fills feed
(/orders/historical/fills) returns, per fill:

    commission, commission_detail_total, entry_id, fillSource,
    future_legs, is_rebalance, liquidity_indicator, option_legs,
    order_data_source, order_id, price, product_id, product_type,
    realized_pl, retail_portfolio_id, sequence_timestamp,
    session_realized_pl, side, size, size_in_quote, trade_id,
    trade_time, trade_type, user_id

No client_order_id. A tag written there never comes back, and a whole
build would have shipped before anyone noticed the join could not be
made.

order_id is what Coinbase mints and returns in success_response at
placement, and it is the one key both sides share.
"""
import ast
import inspect
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def src(path, name):
    s = open(os.path.join(HERE, path), encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(s))
              if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)
    return ast.get_source_segment(s, fn)


def test_the_join_key_is_order_id_not_client_order_id():
    from models import OrderAttribution
    cols = {c.name for c in OrderAttribution.__table__.columns}
    assert "order_id" in cols
    assert "client_order_id" not in cols, (
        "the fills feed carries no client_order_id - a tag there never returns")


def test_the_model_records_who_and_what_not_just_that_it_happened():
    from models import OrderAttribution
    cols = {c.name for c in OrderAttribution.__table__.columns}
    for c in ("source", "product_id", "side", "placed_at"):
        assert c in cols


def test_order_id_is_unique_so_one_order_has_one_source():
    from models import OrderAttribution
    assert OrderAttribution.__table__.columns["order_id"].unique is True


def test_the_engine_records_before_it_polls_for_the_fill():
    """A slow or failed fill poll must not lose the only link between an
    order and whoever asked for it."""
    body = src("crypto_btc_compound_bot.py", "_place_and_confirm")
    assert "_record_order_source" in body
    assert body.index("_record_order_source") < body.index("historical/"), (
        "attribution is recorded after the fill poll, where a timeout loses it")


def test_recording_can_never_cost_a_trade():
    body = src("crypto_btc_compound_bot.py", "_record_order_source")
    assert "except Exception" in body and "non-fatal" in body
    assert "raise" not in body


def test_an_untagged_caller_writes_no_row_rather_than_a_wrong_one():
    """source defaults to None everywhere, so an un-updated caller is
    recorded as absent rather than mis-attributed."""
    body = src("crypto_btc_compound_bot.py", "_record_order_source")
    assert "if not order_id or not source:" in body
    for fn in ("place_market_buy", "place_market_sell", "_place_and_confirm"):
        assert "source: str = None" in src("crypto_btc_compound_bot.py", fn), fn


def test_every_market_order_path_that_can_produce_a_taker_fill_is_tagged():
    """The four call sites that place a MARKET order and so can be billed
    as a taker. Maker orders are post_only and cannot cross, so they are
    not what this is for."""
    grid = open(os.path.join(HERE, "crypto_grid_bot.py"), encoding="utf-8").read()
    trim = open(os.path.join(HERE, "auto_trim_worker.py"), encoding="utf-8").read()
    for tag in ('source="grid_buy_market"', 'source="grid_sell"',
                'source="grid_close_branch"'):
        assert tag in grid, tag
    assert '"auto_trim"' in trim


def test_the_trimmer_records_its_own_because_it_posts_its_own_order():
    body = src("auto_trim_worker.py", "_place_market_sell")
    assert "_record_order_source" in body
    assert "success_response" in body, "it must read the id Coinbase returned"
    assert "non-fatal" in body
