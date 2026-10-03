"""Does out_of_reach tell the truth about what it cannot see?

The bug it documents is a substitution: "nobody told us" reported as
"there is none". Every test below is written to fail if that substitution
comes back, including through the convenient path where an unset env var
reads as 0.0 and every ratio quietly stays wrong.
"""
import out_of_reach as M

FAILED = []


def ok(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        FAILED.append(label)


# ---- the live case this module was built from -------------------------
# ETH 2.8e-09 units, ATOM and ADA at a confirmed zero, SOL partly staked.
LIVE_BRANCHES = [{"product_id": p} for p in (
    "ETH-USD", "SOL-USD", "XRP-USD", "BTC-USD", "TIA-USD", "PRIME-USD")]
LIVE_ALL = {"ETH": 2.803169192e-09, "SOL": 1.03460007, "XRP": 1396.790632,
            "BTC": 0.01814921, "TIA": 0.0, "PRIME": 0.0, "ATOM": 0.0,
            "ADA": 0.0}
LIVE_AVAIL = {"SOL": 0.25865002, "XRP": 1396.790632, "BTC": 0.01814921}

r = M.assess(LIVE_BRANCHES, LIVE_ALL, LIVE_AVAIL,
             readable_total_usd=10195.63, env={})
print("\n-- live shape, nothing declared")
ok("it is readable", r["readable"] is True)
ok("verdict is UNKNOWN, not a number", r["verdict"] == "UNKNOWN")
ok("out_of_reach_usd is None, NOT 0.0", r["out_of_reach_usd"] is None)
ok("and None is not 0.0", r["out_of_reach_usd"] != 0.0)
ok("it says unset means unknown", r["unset_means_unknown_not_zero"] is True)
empt = {e["asset"] for e in r["empty_trading_balance"]}
ok("ETH at 2.8e-09 is flagged (dust is not a position)", "ETH" in empt)
ok("TIA at a confirmed zero is flagged", "TIA" in empt)
ok("PRIME at a confirmed zero is flagged", "PRIME" in empt)
ok("SOL with 1.03 units is NOT flagged empty", "SOL" not in empt)
ok("XRP with real coin is NOT flagged", "XRP" not in empt)
ok("count matches the list", r["branches_on_an_empty_trading_balance"] == len(empt))
ok("ETH is not called a confirmed zero (it is dust, 2.8e-09 != 0)",
   next(e for e in r["empty_trading_balance"] if e["asset"] == "ETH")
   ["confirmed_zero"] is False)
ok("TIA IS called a confirmed zero",
   next(e for e in r["empty_trading_balance"] if e["asset"] == "TIA")
   ["confirmed_zero"] is True)
ok("ATOM/ADA have no branch so they are not branch findings",
   not ({"ATOM", "ADA"} & empt))
ok("ratios are called upper bounds while undeclared",
   "UPPER BOUND" in r["ratios_from_the_readable_total_are"].upper())
ok("the detail names the empty assets", "ETH" in r["detail"])
ok("no true_total is invented from an unknown", "true_total_usd" not in r)
ok("it says staking is unstaked in the app, not here",
   "Coinbase app" in next(e for e in r["empty_trading_balance"]
                          if e["asset"] == "ETH")["why"])

# ---- UNKNOWN vs ZERO: a currency the venue never mentioned -----------
print("\n-- a currency the read never mentioned")
r2 = M.assess([{"product_id": "QNT-USD"}], {"BTC": 1.0}, {"BTC": 1.0}, env={})
ok("QNT is UNKNOWN, not empty", r2["unread_count"] == 1)
ok("and it is NOT in the empty list",
   not any(e["asset"] == "QNT" for e in r2["empty_trading_balance"]))
ok("its verdict says UNKNOWN", r2["unread"][0]["verdict"] == "UNKNOWN")
ok("the reason says unread, not zero", "not zero" in r2["unread"][0]["why"])

# ---- no reading at all ----------------------------------------------
print("\n-- no balance reading at all")
r3 = M.assess(LIVE_BRANCHES, {}, {}, env={})
ok("unreadable, not 'all empty'", r3["readable"] is False)
ok("out_of_reach stays None", r3["out_of_reach_usd"] is None)
ok("it does not claim 6 empty branches",
   "branches_on_an_empty_trading_balance" not in r3)
r3b = M.assess(LIVE_BRANCHES, None, None, env={})
ok("None balances are unreadable too", r3b["readable"] is False)

# ---- the declared path ----------------------------------------------
print("\n-- declared $4,093.14 against a readable $10,195.63")
r4 = M.assess(LIVE_BRANCHES, LIVE_ALL, LIVE_AVAIL,
              readable_total_usd=10195.63,
              env={"GRID_OUT_OF_REACH_USD": "4093.14"})
ok("verdict flips to DECLARED", r4["verdict"] == "DECLARED")
ok("the declared figure survives", r4["out_of_reach_usd"] == 4093.14)
ok("true total is the sum", r4["true_total_usd"] == round(10195.63 + 4093.14, 2))
ok("reachable share is computed, ~71.3%",
   abs(r4["reachable_share_pct"] - 71.35) < 0.05)
ok("out-of-reach share ~28.65%",
   abs(r4["out_of_reach_share_pct"] - 28.65) < 0.05)
ok("the two shares sum to 100",
   abs(r4["reachable_share_pct"] + r4["out_of_reach_share_pct"] - 100.0) < 0.02)
ok("it still says the figure is declared, not measured",
   r4["out_of_reach_is_declared_not_measured"] is True)
ok("the overstatement factor appears in the detail", "1.40" in r4["detail"])
ok("the empty-balance findings survive declaration",
   r4["branches_on_an_empty_trading_balance"] == len(empt))

print("\n-- declared but no readable total")
r5 = M.assess(LIVE_BRANCHES, LIVE_ALL, LIVE_AVAIL,
              env={"GRID_OUT_OF_REACH_USD": "4093.14"})
ok("still DECLARED", r5["verdict"] == "DECLARED")
ok("no share invented without a denominator", "reachable_share_pct" not in r5)
ok("the detail says the shares were not computed", "not computed" in r5["detail"])

# ---- a declared value that is not a value ---------------------------
print("\n-- junk and edge values in the env var")
for raw, label in (("", "empty string"), ("   ", "whitespace"),
                   ("abc", "not a number"), ("-5", "negative"),
                   ("nan", "NaN"), ("None", "the literal 'None'")):
    rr = M.assess(LIVE_BRANCHES, LIVE_ALL, LIVE_AVAIL,
                  readable_total_usd=10195.63,
                  env={"GRID_OUT_OF_REACH_USD": raw})
    ok(f"{label} -> UNKNOWN, not 0.0",
       rr["verdict"] == "UNKNOWN" and rr["out_of_reach_usd"] is None)
r6 = M.assess(LIVE_BRANCHES, LIVE_ALL, LIVE_AVAIL,
              readable_total_usd=10195.63,
              env={"GRID_OUT_OF_REACH_USD": "0"})
ok("an EXPLICIT '0' is declared (the owner said none), not unknown",
   r6["verdict"] == "DECLARED" and r6["out_of_reach_usd"] == 0.0)
ok("and with 0 declared the shares read 100 / 0",
   r6["reachable_share_pct"] == 100.0 and r6["out_of_reach_share_pct"] == 0.0)
ok("' 4093.14 ' with spaces still parses",
   M.assess([], LIVE_ALL, LIVE_AVAIL,
            env={"GRID_OUT_OF_REACH_USD": " 4093.14 "})["out_of_reach_usd"]
   == 4093.14)

# ---- the note -------------------------------------------------------
print("\n-- the optional note")
r7 = M.assess([], LIVE_ALL, LIVE_AVAIL,
              env={"GRID_OUT_OF_REACH_USD": "4093.14",
                   "GRID_OUT_OF_REACH_NOTE": "ETH 99% / SOL 96% / ATOM / ADA staked"})
ok("the note is carried", "ATOM" in r7["declared_note"])
ok("no note -> None, not ''",
   M.assess([], LIVE_ALL, LIVE_AVAIL, env={})["declared_note"] is None)

# ---- it changes nothing --------------------------------------------
print("\n-- it is a measurement")
br = [{"product_id": "ETH-USD", "allocated_usd": 406.0}]
snap = [dict(b) for b in br]
M.assess(br, LIVE_ALL, LIVE_AVAIL, env={"GRID_OUT_OF_REACH_USD": "4093.14"})
ok("the branch dicts it was handed are untouched", br == snap)
ok("no module-level env read when env= is passed",
   M.declared_out_of_reach_usd({}) is None)
ok("it says so in the payload",
   M.assess(br, LIVE_ALL, LIVE_AVAIL, env={})["is_a_measurement_not_a_change"]
   is True)

# ---- no branches at all --------------------------------------------
print("\n-- no branches")
r8 = M.assess([], LIVE_ALL, LIVE_AVAIL, readable_total_usd=10195.63, env={})
ok("readable with zero branches", r8["readable"] is True)
ok("zero empty findings", r8["branches_on_an_empty_trading_balance"] == 0)
ok("it still counts the currencies the venue listed",
   r8["currencies_the_venue_listed"] == len(LIVE_ALL))
r9 = M.assess(None, LIVE_ALL, LIVE_AVAIL, env={})
ok("None branches do not raise", r9["branches_measured"] == 0)
r10 = M.assess([{"allocated_usd": 1.0}], LIVE_ALL, LIVE_AVAIL, env={})
ok("a branch with no product_id is skipped, not crashed",
   r10["branches_on_an_empty_trading_balance"] == 0 and r10["unread_count"] == 0)

# ---- case and junk units -------------------------------------------
print("\n-- messy inputs")
r11 = M.assess([{"product_id": "eth-usd"}], {"eth": 0.0}, {}, env={})
ok("lowercase product and currency still match",
   any(e["asset"] == "ETH" for e in r11["empty_trading_balance"]))
r12 = M.assess([{"product_id": "ETH-USD"}], {"ETH": "not a number"}, {}, env={})
ok("an unparseable unit count reads 0.0 and is flagged empty",
   r12["branches_on_an_empty_trading_balance"] == 1)
r13 = M.assess([{"product_id": "ETH-USD"}], {"ETH": float("nan")}, {}, env={})
ok("NaN units read 0.0, not NaN",
   r13["empty_trading_balance"][0]["trading_balance_units"] == 0.0)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for f in FAILED:
        print("  -", f)
    raise SystemExit(1)
print("all checks passed")
