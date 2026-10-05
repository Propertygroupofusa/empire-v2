"""The selector is back, and the cap it never had is in front of it.

THE OWNER, 2026-10-05: "It knew how to find the coins that we needed to make
money off of... I want that deployed right now." Plus the new constraint:
"I just don't want it to put so much money into one coin like it was doing."

THE RECORD THAT MAKES THE CAP THE POINT. The family tree's own ledger, read
live: 167 trades across 12 coins for -$508.44, every coin negative. POL
alone took 87 of them and -$392.43 at a 14.9% win rate, and single coins
reached 23% and 27% of the account. The selector's gates are restored as
the owner asked; the thing that is NEW is that a coin over the cap is
removed before quality is scored at all, so no amount of looking good can
carry it past "split it up".

Run: python3 test_coin_quality.py
"""
import os
import sys

import coin_quality as cq

checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


def C(pid, alloc, atr, rsi=40.0, cret=0.05, bullish=True, trend=True,
      slices=1, levels=3, name=None):
    return {"product_id": pid, "bot_name": name or ("bot_" + pid.split("-")[0]),
            "allocated_usd": alloc, "atr_pct": atr, "rsi": rsi,
            "coin_return": cret, "is_bullish": bullish, "trend_ok": trend,
            "open_slices": slices, "num_levels": levels}


FLEET = 1000.0
BTC_RET = 0.01

# --- the cap comes first, and it is the owner's whole point ---------------
BIG = C("POL-USD", 250.0, 0.09, rsi=20, cret=0.50)        # 25% - the best coin by far
SMALL = C("ALGO-USD", 50.0, 0.02)
t, why = cq.best([BIG, SMALL], btc_return=BTC_RET, fleet_allocated_usd=FLEET,
                 amount_usd=1.0)
ok("the biggest-ATR coin does NOT win if it is over the cap", t == "bot_ALGO")
ok("and it is rejected for size, not for quality",
   any(r["product_id"] == "POL-USD" and "cap" in r["why"] for r in why["rejected"]))
ok("the rejection quotes the share it would reach",
   any("25.1%" in r["why"] or "25.0%" in r["why"]
       for r in why["rejected"] if r["product_id"] == "POL-USD"))

# the same coin under the cap is allowed to win on quality
OK_BIG = C("POL-USD", 150.0, 0.09, rsi=20, cret=0.50)      # 15%
t2, _ = cq.best([OK_BIG, SMALL], btc_return=BTC_RET, fleet_allocated_usd=FLEET,
                amount_usd=1.0)
ok("under the cap, the most volatile coin wins as the tree intended",
   t2 == "bot_POL")

# --- the four gates, each on its own --------------------------------------
HOT = C("HOT-USD", 50.0, 0.20, rsi=70)
t3, why3 = cq.best([HOT, SMALL], btc_return=BTC_RET, fleet_allocated_usd=FLEET)
ok("an overbought coin is skipped even with the best ATR", t3 == "bot_ALGO")
ok("and the reason names RSI",
   any("overbought" in r["why"] for r in why3["rejected"]))

LAGGARD = C("LAG-USD", 50.0, 0.20, cret=0.005)             # below BTC's 0.01
t4, why4 = cq.best([LAGGARD, SMALL], btc_return=BTC_RET, fleet_allocated_usd=FLEET)
ok("a coin not beating BTC is skipped", t4 == "bot_ALGO")
ok("and the reason compares it to BTC",
   any("not beating BTC" in r["why"] for r in why4["rejected"]))

DOWN = C("DWN-USD", 50.0, 0.20, trend=False)
t5, why5 = cq.best([DOWN, SMALL], btc_return=BTC_RET, fleet_allocated_usd=FLEET)
ok("a confirmed hourly downtrend is skipped", t5 == "bot_ALGO")
ok("and the reason names the SMA pair",
   any("SMA20/SMA50" in r["why"] for r in why5["rejected"]))

FULL = C("FUL-USD", 50.0, 0.20, slices=3, levels=3)
t6, why6 = cq.best([FULL, SMALL], btc_return=BTC_RET, fleet_allocated_usd=FLEET)
ok("a branch with every rung full is skipped", t6 == "bot_ALGO")

# --- the two gates that must FAIL OPEN, exactly as the tree had them ------
NO_RSI = C("NRS-USD", 50.0, 0.20, rsi=None)
ok("an unreadable RSI does NOT block a coin",
   cq.best([NO_RSI], btc_return=BTC_RET, fleet_allocated_usd=FLEET)[0] == "bot_NRS")
NO_RET = C("NRT-USD", 50.0, 0.20, cret=None)
ok("an unreadable coin return does NOT block it",
   cq.best([NO_RET], btc_return=BTC_RET, fleet_allocated_usd=FLEET)[0] == "bot_NRT")
NO_TREND = C("NTR-USD", 50.0, 0.20, trend=None)
ok("an UNKNOWN trend does not block, only a confirmed DOWN does",
   cq.best([NO_TREND], btc_return=BTC_RET, fleet_allocated_usd=FLEET)[0] == "bot_NTR")
ok("and with BTC itself unreadable the alpha gate stands down",
   cq.best([LAGGARD], btc_return=None, fleet_allocated_usd=FLEET)[0] == "bot_LAG")

# --- but an unreadable ALLOCATION must block, because the cap needs it ----
NO_ALLOC = C("NAL-USD", None, 0.20)
t7, why7 = cq.best([NO_ALLOC], btc_return=BTC_RET, fleet_allocated_usd=FLEET)
ok("an unreadable allocation IS refused - the cap cannot be checked", t7 is None)
ok("and says why that one is different",
   any("unchecked cap" in r["why"] for r in why7["rejected"]))

# --- bullish before volatile, the tree's own order ------------------------
WILD_FLAT = C("WLD-USD", 50.0, 0.30, bullish=False)
CALM_UP = C("CLM-USD", 50.0, 0.03, bullish=True)
t8, _ = cq.best([WILD_FLAT, CALM_UP], btc_return=BTC_RET, fleet_allocated_usd=FLEET)
ok("a bullish coin outranks a more volatile non-bullish one", t8 == "bot_CLM")
NO_ATR = C("NAT-USD", 50.0, None)
r9, _ = cq.judge([NO_ATR, CALM_UP], btc_return=BTC_RET, fleet_allocated_usd=FLEET)
ok("a coin with no readable ATR sorts LAST, never first",
   r9[-1]["product_id"] == "NAT-USD")

# --- nothing eligible is a normal answer ----------------------------------
t10, why10 = cq.best([BIG], btc_return=BTC_RET, fleet_allocated_usd=FLEET,
                     amount_usd=1.0)
ok("every coin refused returns None, not an exception", t10 is None)
ok("and says no coin cleared", "no coin cleared" in why10["reason"])
ok("an empty fleet is handled", cq.best([])[0] is None)

# --- the flag -------------------------------------------------------------
os.environ.pop(cq.ENV_FLAG, None)
ok("off unless switched on", cq.enabled() is False)
os.environ[cq.ENV_FLAG] = "true"
ok("and on when it is", cq.enabled() is True)
os.environ.pop(cq.ENV_FLAG, None)

# --- it can never trade ---------------------------------------------------
SRC = open("coin_quality.py").read()
CODE = "\n".join(l for l in SRC.splitlines() if not l.lstrip().startswith("#"))
for forbidden in ("place_market_buy", "place_market_sell", "place_order",
                  "create_grid_branch", "withdraw_from_grid_branch",
                  "add_cash_to_grid_branch", "close_all_grid_slices"):
    ok(f"coin_quality never calls {forbidden}", forbidden not in CODE)

failed = [l for l, c in checks if not c]
for l, c in checks:
    print(f"  {'PASS' if c else 'FAIL'}  {l}")
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
sys.exit(1 if failed else 0)
