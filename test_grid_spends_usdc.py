"""The grid buys whether the owner's cash sits as USD or as USDC.

THE OWNER, 2026-10-13/14: "Make it to where it can pull from either USDC or
USD. Because somehow I keep getting switched and I'm not doing it." Twice in
one day the account's cash moved from USD to USDC with no fill and nobody
pressing anything in this bot, and both times every grid buy was refused:
$3,404.83 of cash read as $12.06.

What is pinned here, against fakes - no venue is ever called:
  * the conversion only commits a quote that is one for one and fee-free
  * the buy path converts only the shortfall, only after every gate, and
    only when GRID_SPEND_USDC is not switched off
  * a failed or doubtful conversion is a skipped buy, never a guessed one
"""
import asyncio
import json
import os

import pytest

import crypto_btc_compound_bot as engine
import crypto_grid_bot as grid


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- fakes
class _Resp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Answers each POST from a queue and records what was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts = []

    def post(self, url, headers=None, data=None, timeout=None, json=None):
        self.posts.append({"url": url, "body": data})
        return self.responses.pop(0)


def _quote(amount="50.00", total="50.00", fee="0", trade_id="T1"):
    t = {"id": trade_id, "status": "TRADE_STATUS_CREATED"}
    if total is not None:
        t["total"] = {"value": total, "currency": "USD"}
    if fee is not None:
        t["total_fee"] = {"amount": {"value": fee, "currency": "USD"}}
    return _Resp(200, {"trade": t})


def _commit(status="TRADE_STATUS_COMPLETED"):
    return _Resp(200, {"trade": {"id": "T1", "status": status}})


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch):
    # Signing needs keys this test never has; the request shape is what is
    # under test, not the signature.
    monkeypatch.setattr(engine, "_auth_headers", lambda m, p, b="": {"Authorization": "test"})
    monkeypatch.delenv("GRID_SPEND_USDC", raising=False)


# ------------------------------------------------- the conversion itself
def test_a_clean_quote_is_committed_for_exactly_the_amount():
    s = _Session(_quote(), _commit())
    engine._BALANCES_CACHE["by_currency"] = {"USD": 1.0}
    got, err = run(engine.convert_usdc_to_usd(s, 50.0))
    assert (got, err) == (50.0, None)
    assert len(s.posts) == 2
    q = json.loads(s.posts[0]["body"])
    assert s.posts[0]["url"].endswith("/api/v3/brokerage/convert/quote")
    assert q == {"from_account": "USDC", "to_account": "USD", "amount": "50.00"}
    assert s.posts[1]["url"].endswith("/api/v3/brokerage/convert/trade/T1")
    assert json.loads(s.posts[1]["body"]) == {"from_account": "USDC", "to_account": "USD"}
    # the next balance read goes to the venue, not to a pre-conversion cache
    assert engine._BALANCES_CACHE["by_currency"] is None


def test_the_amount_is_rounded_up_to_the_cent():
    s = _Session(_quote(total="12.35"), _commit())
    got, _ = run(engine.convert_usdc_to_usd(s, 12.341))
    assert got == 12.35 and json.loads(s.posts[0]["body"])["amount"] == "12.35"


def test_a_quote_that_charges_a_fee_is_never_committed():
    s = _Session(_quote(fee="0.50"))
    got, err = run(engine.convert_usdc_to_usd(s, 50.0))
    assert got is None and "not committed" in err
    assert len(s.posts) == 1          # quote only - nothing moved


def test_a_quote_that_is_not_one_for_one_is_never_committed():
    s = _Session(_quote(total="49.00", fee=None))
    got, err = run(engine.convert_usdc_to_usd(s, 50.0))
    assert got is None and "one for one" in err and len(s.posts) == 1


def test_a_quote_showing_neither_fee_nor_total_is_never_committed():
    s = _Session(_quote(total=None, fee=None))
    got, err = run(engine.convert_usdc_to_usd(s, 50.0))
    assert got is None and len(s.posts) == 1


def test_a_refused_quote_moves_nothing():
    s = _Session(_Resp(400, {"error": "INVALID_ARGUMENT"}))
    got, err = run(engine.convert_usdc_to_usd(s, 50.0))
    assert got is None and "HTTP 400" in err and len(s.posts) == 1


def test_a_canceled_conversion_is_reported_as_not_done():
    s = _Session(_quote(), _commit("TRADE_STATUS_CANCELED"))
    got, err = run(engine.convert_usdc_to_usd(s, 50.0))
    assert got is None and "canceled" in err


# ------------------------------------------------------- the buy path
class _FakeEngine:
    """USD and USDC balances, and a converter that moves USDC into USD."""

    def __init__(self, usd, usdc, convert_ok=True, usd_readable=True):
        self.usd, self.usdc = usd, usdc
        self.convert_ok, self.usd_readable = convert_ok, usd_readable
        self.converted = []

    async def get_usd_balance(self, session):
        return (self.usd, None) if self.usd_readable else (None, "HTTP 429")

    async def get_usdc_balance(self, session):
        return self.usdc, None

    async def convert_usdc_to_usd(self, session, amount):
        self.converted.append(amount)
        if not self.convert_ok:
            return None, "convert quote HTTP 403: forbidden"
        self.usdc -= amount
        self.usd += amount
        return amount, None


@pytest.fixture
def fake(monkeypatch):
    def make(**kw):
        e = _FakeEngine(**kw)
        monkeypatch.setattr(grid, "engine", e)
        return e
    return make


def test_on_unless_switched_off(monkeypatch):
    assert grid.spend_usdc_enabled() is True
    for v in ("0", "false", "no", "off", "OFF", '"off"'):
        monkeypatch.setenv("GRID_SPEND_USDC", v)
        assert grid.spend_usdc_enabled() is False, v
    for v in ("1", "on", "true", "", "yes"):
        monkeypatch.setenv("GRID_SPEND_USDC", v)
        assert grid.spend_usdc_enabled() is True, v


def test_tonights_account_counts_its_usdc_as_cash(fake):
    fake(usd=12.06, usdc=3404.83)
    usd, usdc, err = run(grid.wallet_cash(None))
    assert (usd, usdc, err) == (12.06, 3404.83, None)


def test_switched_off_the_cash_reading_is_usd_only(fake, monkeypatch):
    fake(usd=12.06, usdc=3404.83)
    monkeypatch.setenv("GRID_SPEND_USDC", "off")
    assert run(grid.wallet_cash(None))[1] == 0.0


def test_the_shortfall_includes_room_for_the_fee():
    assert grid.usd_short_for(50.0, 20.0) == 31.0      # 50 + $1 - 20
    assert grid.usd_short_for(500.0, 0.0) == 505.0     # 1% above $100
    assert grid.usd_short_for(50.0, 60.0) == 0.0


def test_a_buy_with_usd_on_hand_converts_nothing(fake):
    e = fake(usd=500.0, usdc=3000.0)
    assert run(grid.ensure_usd_for_buy(None, 50.0)) == (True, None)
    assert e.converted == []


def test_a_buy_short_of_usd_converts_only_the_shortfall(fake):
    e = fake(usd=12.06, usdc=3404.83)
    ok, note = run(grid.ensure_usd_for_buy(None, 35.0))
    assert ok is True and e.converted == [23.94]       # 35 + 1 - 12.06
    assert "converted $23.94" in note
    assert e.usdc == pytest.approx(3380.89)


def test_not_enough_usdc_means_no_buy_and_no_conversion(fake):
    e = fake(usd=5.0, usdc=10.0)
    ok, note = run(grid.ensure_usd_for_buy(None, 35.0))
    assert ok is False and e.converted == [] and "no buy" in note


def test_a_failed_conversion_is_a_skipped_buy(fake):
    e = fake(usd=0.0, usdc=3000.0, convert_ok=False)
    ok, note = run(grid.ensure_usd_for_buy(None, 35.0))
    assert ok is False and "not done" in note and e.converted == [36.0]


def test_an_unreadable_usd_balance_is_a_skipped_buy(fake):
    e = fake(usd=0.0, usdc=3000.0, usd_readable=False)
    ok, _ = run(grid.ensure_usd_for_buy(None, 35.0))
    assert ok is False and e.converted == []


def test_switched_off_the_buy_goes_exactly_as_before(fake, monkeypatch):
    e = fake(usd=0.0, usdc=3000.0)
    monkeypatch.setenv("GRID_SPEND_USDC", "off")
    assert run(grid.ensure_usd_for_buy(None, 35.0)) == (True, None)
    assert e.converted == []


def test_the_conversion_happens_after_every_gate_and_before_the_order():
    """Structural: the cycle needs the venue and the database to run."""
    src = open(grid.__file__, encoding="utf-8").read()
    cyc = src[src.index("async def run_grid_branch_cycle("):]
    i_wallet = cyc.index("await wallet_cash(session)")
    i_gate = cyc.index("await _net_edge_gate_ok(")
    i_ensure = cyc.index("await ensure_usd_for_buy(session, spend)")
    i_order = cyc.index("fill = await grid_buy(session, spend")
    assert i_wallet < i_gate < i_ensure < i_order


# --- the two budgets the rotation bug inflated hold until corrected ---------

def test_ondo_and_tia_hold_at_exactly_the_inflated_figure():
    hold, why = grid.inflated_budget_hold("ONDO-USD", 1666.25)
    assert hold is True and "10-13/14" in why and "117" in why     # ~$117 is real
    assert grid.inflated_budget_hold("TIA-USD", 1649.43)[0] is True
    assert grid.inflated_budget_hold("TIA-USD", 1649.44)[0] is True     # inside a cent


def test_the_hold_lifts_the_moment_the_budget_is_corrected():
    # a withdraw back to the real figures, a partial one, or any trade-driven change
    for alloc in (117.29, 100.47, 1000.0, 1666.30, 1650.00):
        assert grid.inflated_budget_hold("ONDO-USD", alloc)[0] is False, alloc
    assert grid.inflated_budget_hold("TIA-USD", 1666.25)[0] is False     # not TIA's figure


def test_no_other_coin_is_ever_held():
    for pid, alloc in (("APE-USD", 96.81), ("XRP-USD", 1268.48), ("BTC-USD", 1666.25)):
        assert grid.inflated_budget_hold(pid, alloc)[0] is False


def test_unreadable_budget_is_not_a_hold():
    assert grid.inflated_budget_hold("ONDO-USD", None)[0] is False
    assert grid.inflated_budget_hold("ONDO-USD", "x")[0] is False


def test_the_hold_sits_before_any_conversion_and_any_order():
    src = open(grid.__file__, encoding="utf-8").read()
    cyc = src[src.index("async def run_grid_branch_cycle("):]
    i_hold = cyc.index("inflated_budget_hold(branch.product_id")
    i_ensure = cyc.index("await ensure_usd_for_buy(session, spend)")
    i_order = cyc.index("fill = await grid_buy(session, spend")
    assert i_hold < i_ensure < i_order
    assert "return" in cyc[i_hold:i_ensure]        # a held buy converts nothing


def test_the_budget_check_job_still_sees_usd_only():
    """allocation_backing feeds the claim reconciliation job. Counting USDC
    there would let it trim every branch ~24% to make room for the inflated
    ONDO/TIA budget."""
    src = open(grid.__file__, encoding="utf-8").read()
    st = src[src.index("async def get_grid_status("):]
    i = st.index("wallet_cash_usd, _wallet_err = await engine.get_usd_balance(session)")
    assert "wallet_cash(" not in st[i - 400:i + 200]


def test_the_free_cash_read_keeps_the_original_single_account_read():
    """The Oct 9 rate-limit fix pins the shape of this read; USDC rides on the
    same cached listing instead of a second walk."""
    src = open(grid.__file__, encoding="utf-8").read()
    fn = src[src.index("async def get_real_free_cash_usd("):]
    fn = fn[:fn.index("\nasync def ", 10)]
    assert "await engine.get_usd_balance(session)" in fn and "await usdc_cash(session)" in fn
