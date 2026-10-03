"""Does the cooldown stay quiet only when the answer truly cannot change?

The failure mode worth guarding is not "it skips too much" in the abstract.
It is skipping on a GAP: a rate-limited balance read refuses the sell, and
if that refusal armed a cooldown the bot would then deliberately ignore the
product for fifteen minutes over a transient 429. Several tests below exist
for that one mistake.
"""
import sys

import dust_cooldown as dc

FAILS = []


def ok(label, cond, got=None):
    if cond:
        print(f"  PASS  {label}")
    else:
        FAILS.append(label)
        print(f"  FAIL  {label}" + (f"   got: {got!r}" if got is not None else ""))


def fresh():
    dc._armed.clear()


print("\n[1] a computed DUST verdict arms, and the next attempt is skipped")
fresh()
armed = dc.note_dust("QNT-USD", "DUST", available_units=0.00097323,
                     reason="BELOW_BASE_INCREMENT", now=1000.0)
ok("note_dust reports it armed", armed is True, armed)
r = dc.skip_reason("QNT-USD", now=1030.0)   # one grid cycle later
ok("the very next cycle is skipped", r is not None, r)
ok("the reason names the real available figure", "0.00097323" in (r or ""), r)
ok("the reason names the venue code", "BELOW_BASE_INCREMENT" in (r or ""), r)

print("\n[2] AN UNREADABLE ANYTHING MUST NOT ARM - a gap is not a fact")
for label, decision in [("balance unreadable (no decision computed)", None),
                        ("a REFUSED verdict", "REFUSED"),
                        ("an empty string", ""),
                        ("lowercase 'dust' is not the constant", "dust")]:
    fresh()
    armed = dc.note_dust("QNT-USD", decision, available_units=None, now=1000.0)
    ok(f"{label} does not arm", armed is False, armed)
    ok(f"{label} leaves the next attempt free", dc.skip_reason("QNT-USD", now=1001.0) is None)

print("\n[3] an EXECUTE clears a cooldown that was already armed")
fresh()
dc.note_dust("PEPE-USD", "DUST", available_units=0.0687, now=1000.0)
ok("armed first", dc.skip_reason("PEPE-USD", now=1010.0) is not None)
dc.note_dust("PEPE-USD", "EXECUTE", available_units=5000000.0, now=1020.0)
ok("EXECUTE cleared it", dc.skip_reason("PEPE-USD", now=1030.0) is None)

print("\n[4] the cooldown expires on its own, and drops its record")
fresh()
dc.note_dust("TIA-USD", "DUST", available_units=0.0, now=1000.0)
ok("still quiet at 899s", dc.skip_reason("TIA-USD", now=1899.0) is not None)
ok("free again at exactly 900s", dc.skip_reason("TIA-USD", now=1900.0) is None)
ok("and the record is gone, not just expired", "TIA-USD" not in dc.armed_products(),
   dc.armed_products())

print("\n[5] available_units of 0.0 is reported as 0.0, never as 'unknown'")
fresh()
dc.note_dust("TIA-USD", "DUST", available_units=0.0, reason="BELOW_BASE_INCREMENT",
             now=1000.0)
r = dc.skip_reason("TIA-USD", now=1010.0)
ok("a real zero survives the falsy trap", "held 0.0 at" in (r or ""), r)
ok("and is not described as unknown", "unknown" not in (r or ""), r)

print("\n[6] a buy clears it immediately - inventory may have grown")
fresh()
dc.note_dust("QNT-USD", "DUST", available_units=0.00097323, now=1000.0)
ok("armed", dc.skip_reason("QNT-USD", now=1010.0) is not None)
ok("clear() reports it dropped one", dc.clear("QNT-USD") is True)
ok("the next sell attempt goes through", dc.skip_reason("QNT-USD", now=1011.0) is None)
ok("clearing nothing reports False", dc.clear("QNT-USD") is False)

print("\n[7] products are independent")
fresh()
dc.note_dust("QNT-USD", "DUST", available_units=0.0009, now=1000.0)
ok("QNT is quiet", dc.skip_reason("QNT-USD", now=1010.0) is not None)
ok("XRP is untouched", dc.skip_reason("XRP-USD", now=1010.0) is None)

print("\n[8] a zero or negative window disables the feature entirely")
fresh()
dc.note_dust("QNT-USD", "DUST", available_units=0.0009, now=1000.0)
ok("cooldown=0 never skips", dc.skip_reason("QNT-USD", now=1001.0, cooldown=0) is None)
fresh()
dc.note_dust("QNT-USD", "DUST", available_units=0.0009, now=1000.0)
ok("a negative window never skips",
   dc.skip_reason("QNT-USD", now=1001.0, cooldown=-5) is None)

print("\n[9] the message must not claim a sale was refused")
fresh()
dc.note_dust("QNT-USD", "DUST", available_units=0.00097323, now=1000.0)
r = dc.skip_reason("QNT-USD", now=1010.0) or ""
ok("it says it is a cooldown on the question", "cooldown on the QUESTION" in r, r)
ok("it does not say the order was cancelled", "cancelled" not in r.replace(
    "nothing is cancelled", ""), r)

print("\n[10] the default window is the 15 minutes the module documents")
ok("COOLDOWN_SECONDS is 900", dc.COOLDOWN_SECONDS == 900.0, dc.COOLDOWN_SECONDS)
ok("DUST matches execution_quantity's constant",
   __import__("execution_quantity").DUST == dc.DUST)

print("\n[11] armed_products hands back a copy, not the live store")
fresh()
dc.note_dust("QNT-USD", "DUST", available_units=0.0009, now=1000.0)
snap = dc.armed_products()
snap["QNT-USD"]["at"] = 0.0
snap["INVENTED-USD"] = {"at": 0.0}
ok("mutating the snapshot does not move the real cooldown",
   dc.skip_reason("QNT-USD", now=1010.0) is not None)
ok("and does not invent a product", dc.skip_reason("INVENTED-USD", now=1010.0) is None)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
