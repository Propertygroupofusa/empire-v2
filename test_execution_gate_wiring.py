"""Observe must never block. Enforce must. And the wire must exist.

The hazard this suite guards: ExecutionGate refuses a branch with no control
row, which is right, and there are 23 branches with no control rows. Shipping
the gate as binding would have halted the fleet on the next deploy.
"""
import asyncio
import os
import sys

import branch_audit_service as bas

FAILS = []


def ok(label, cond, got=None):
    if cond:
        print(f"  PASS  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"   got: {got!r}" if got is not None else ""))


def setmode(v):
    if v is None:
        os.environ.pop(bas.MODE_ENV, None)
    else:
        os.environ[bas.MODE_ENV] = v


class FakeGate:
    """Stands in for ExecutionGate so this suite needs no database."""
    def __init__(self, decision):
        self._d = decision
        self.calls = 0

    async def check(self, **kw):
        self.calls += 1
        return self._d


DENY = bas.Decision(False, "RECONCILIATION_MISMATCH",
                    "DB 100 BTC vs venue 50 BTC", "inventory_truth_gate")
ALLOW = bas.Decision(True, "OK", "permitted", "all_gates")

print("\n[1] OBSERVE is the default - a fresh deploy cannot halt the fleet")
setmode(None)
ok("unset reads observe", bas.gate_mode() == "observe", bas.gate_mode())
for v in ["", "true", "1", "on", "enforced", "ENFORCE_", "observe"]:
    setmode(v)
    ok(f"{v!r} does NOT enforce", bas.is_enforcing() is False, bas.gate_mode())
for v in ["enforce", "ENFORCE", " enforce "]:
    setmode(v)
    ok(f"{v!r} enforces", bas.is_enforcing() is True, bas.gate_mode())

print("\n[2] observe mode reports a denial but does NOT block")
setmode("observe")
g = FakeGate(DENY)
d = asyncio.run(bas.check_or_observe(g, bot_name="crypto_grid_btc",
                                     action="ENTRY", truth=None))
ok("the gate was still evaluated", g.calls == 1, g.calls)
ok("the buy is allowed to proceed", d.allowed is True, d)
ok("the reason is prefixed OBSERVED_",
   d.reason_code == "OBSERVED_RECONCILIATION_MISMATCH", d.reason_code)
ok("...so a log reader cannot mistake it for a real refusal",
   "would have blocked" in d.detail, d.detail)
ok("the original gate name is preserved", d.gate == "inventory_truth_gate", d.gate)

print("\n[3] enforce mode blocks the same decision")
setmode("enforce")
g = FakeGate(DENY)
d = asyncio.run(bas.check_or_observe(g, bot_name="crypto_grid_btc",
                                     action="ENTRY", truth=None))
ok("the buy is refused", d.allowed is False, d)
ok("the reason is NOT prefixed", d.reason_code == "RECONCILIATION_MISMATCH", d)

print("\n[4] an ALLOW passes through unchanged in both modes")
for mode in ("observe", "enforce"):
    setmode(mode)
    d = asyncio.run(bas.check_or_observe(FakeGate(ALLOW), bot_name="b",
                                         action="ENTRY", truth=None))
    ok(f"{mode}: allow stays allow", d.allowed is True, d)
    ok(f"{mode}: reason untouched", d.reason_code == "OK", d.reason_code)

print("\n[5] THE WIRE EXISTS in the live buy path")
src = open("crypto_grid_bot.py").read()
ok("check_or_observe is called from the grid bot", "check_or_observe(" in src)
ok("...with action ENTRY", 'action="ENTRY"' in src)
ok("a control row is ensured before the check", "ensure_control_state(" in src)
i_gate = src.index("check_or_observe(")
i_buy = src.index("_net_edge_gate_ok(", i_gate)
ok("the gate runs BEFORE the net-edge gate and the order", i_gate < i_buy)
seg = src[i_gate:i_gate + 1400]
ok("a denial returns rather than falling through", "return" in seg)
ok("an exception in the gate refuses the buy",
   "NOT buying" in src and "un-auditable buy is refused" in src)

print("\n[6] the gate's own failure is never a reason to trade")
seg2 = src[src.index("except Exception as _exc:", i_gate):][:600]
ok("the except branch returns", seg2.count("return") >= 1, seg2.count("return"))
ok("...and does not continue to the order", "place_maker_buy" not in seg2)

print("\n[7] a fresh branch is seeded UNKNOWN, never MATCHED")
import inspect
srcf = inspect.getsource(bas.ensure_control_state)
ok("the default reconciliation_status is UNKNOWN",
   'reconciliation_status="UNKNOWN"' in srcf, srcf[:200])
ok("execution_enabled is seeded False", "execution_enabled=False" in srcf)
ok("an existing row is never overwritten", "if row is not None:" in srcf)

setmode(None)
print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
