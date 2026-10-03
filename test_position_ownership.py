"""One broker position, three systems that can hold it.

Found auditing the account-wide auto-close loop: (1) the scan's broker
reconciliation adopted branch / opening-bar positions, putting two systems'
exits on the same shares; (2) a branch whose shares were sold elsewhere kept
"holding" them forever - every exit refused by Alpaca, never buying again.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest import mock

import prop_bot as pb


class _B:
    def __init__(self, name, contract): self.bot_name, self.contract, self.active = name, contract, True


class _R:
    def __init__(self, body, status=200): self.status, self._b = status, body
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def json(self): return self._b


class _S:
    def __init__(self, body, status=200): self.body, self.status = body, status
    def get(self, *a, **k): return _R(self.body, self.status)


OLD = datetime.now(timezone.utc) - timedelta(hours=2)
USO = pb.FUTURES["MCL"]["symbol"]
SPY = pb.FUTURES["MES"]["symbol"]


def _run(coro_fn, *, branches, branch_pos, leg_pos, branch_mode=True, bar_mode=True):
    deleted = []

    async def get_br(): return branches
    async def bm(): return branch_mode
    async def om(): return bar_mode
    async def dele(n, c): deleted.append((n, c))

    with mock.patch.dict(pb.open_alpaca_branch_positions, branch_pos, clear=True), \
         mock.patch.dict(pb.open_opening_bar_positions, leg_pos, clear=True), \
         mock.patch.object(pb, "get_alpaca_branches", get_br), \
         mock.patch.object(pb, "is_alpaca_branch_mode_active", bm), \
         mock.patch.object(pb, "is_opening_bar_live_active", om), \
         mock.patch.object(pb, "_db_delete_branch_open", dele):
        out = asyncio.run(coro_fn())
        return out, dict(pb.open_alpaca_branch_positions), dict(pb.open_opening_bar_positions), deleted


def test_running_owners_are_excluded_from_adoption():
    out, *_ = _run(pb._contracts_owned_elsewhere,
                   branches=[_B("alpaca_branch_1", "MCL")],
                   branch_pos={"alpaca_branch_1": {"qty": 1, "open_time": OLD}},
                   leg_pos={"MES": {"qty": 1, "open_time": OLD}})
    assert out == {"MCL", "MES"}


def test_owner_switched_off_does_not_block_adoption():
    # With branch mode off nothing manages that branch's exits; the scan's
    # adoption is the only protection left, so it must stay possible.
    out, *_ = _run(pb._contracts_owned_elsewhere,
                   branches=[_B("alpaca_branch_1", "MCL")],
                   branch_pos={"alpaca_branch_1": {"qty": 1, "open_time": OLD}},
                   leg_pos={}, branch_mode=False)
    assert out == set()


def test_vanished_branch_position_is_released():
    session = _S([{"symbol": SPY, "qty": "1"}])           # USO is gone at the broker
    out, br, legs, deleted = _run(
        lambda: pb.drop_vanished_branch_and_leg_positions(session),
        branches=[_B("alpaca_branch_1", "MCL")],
        branch_pos={"alpaca_branch_1": {"qty": 1, "open_time": OLD}},
        leg_pos={"MES": {"qty": 1, "open_time": OLD}})
    assert out == ["alpaca_branch_1"]
    assert "alpaca_branch_1" not in br and deleted == [("alpaca_branch_1", "MCL")]
    assert "MES" in legs                                    # SPY still held: untouched


def test_fresh_position_is_not_dropped():
    session = _S([])
    out, br, *_ = _run(lambda: pb.drop_vanished_branch_and_leg_positions(session),
                       branches=[_B("alpaca_branch_1", "MCL")],
                       branch_pos={"alpaca_branch_1": {"qty": 1, "open_time": datetime.now(timezone.utc)}},
                       leg_pos={})
    assert out == [] and "alpaca_branch_1" in br


def test_unreadable_broker_drops_nothing():
    session = _S({}, status=503)
    out, br, legs, _ = _run(lambda: pb.drop_vanished_branch_and_leg_positions(session),
                            branches=[_B("alpaca_branch_1", "MCL")],
                            branch_pos={"alpaca_branch_1": {"qty": 1, "open_time": OLD}},
                            leg_pos={"MES": {"qty": 1, "open_time": OLD}})
    assert out == [] and br and legs


def test_vanished_opening_bar_leg_is_released():
    session = _S([])
    out, _, legs, _ = _run(lambda: pb.drop_vanished_branch_and_leg_positions(session),
                           branches=[], branch_pos={},
                           leg_pos={"MES": {"qty": 1, "open_time": OLD}})
    assert out == ["MES"] and legs == {}


def test_reconcile_skips_owned_contracts():
    import inspect
    src = inspect.getsource(pb.reconcile_positions_with_broker)
    assert "owned_elsewhere = await _contracts_owned_elsewhere()" in src
    assert "if owned_elsewhere is None or contract in owned_elsewhere:" in src
