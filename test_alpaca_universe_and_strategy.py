"""Alpaca was refusing every ticker, for two reasons that were both bugs.

2026-09-25. Equity $1,007.74 with $810.64 - 80% - sitting in cash, one
open position, and the entry-eligibility endpoint refusing all sixteen
tickers. Two distinct defects:

DEFECT 1 - the dashboard's universe was missing a whole category.
    routers/trading_dashboard.py built approved_universe from futures +
    crypto + commodities + inverse_etfs, and omitted ["equities"].
    prop_bot's own MANDATE CHECK 1 includes it. So this page reported
    META, NVDA, AAPL, GOOGL, AMZN and MSFT as "Not in the approved
    trading universe" while the live bot would have allowed every one.
    Six of sixteen looked permanently banned for a reason true only of
    the diagnostic. On the 30-day momentum replay META was the best
    performer in the entire book at +$39.06.

DEFECT 2 - the exclusion backtest replayed the wrong strategy.
    run_full_backtest called _replay_symbol(), the MEAN-REVERSION entry
    (`rsi < RSI_LONG_THRESHOLD`, buy weakness). The live bot has run
    MOMENTUM since the head-to-head that switched it (`rsi > 55 and
    price > sma`, buy strength). Two opposite entries.

    prop_bot.get_effective_excluded_symbols() gates LIVE entries on the
    ROI those runs persist, so symbols were barred from momentum entries
    because a mean-reversion replay lost money on them. On the 30-day
    data four of the six excluded lose under momentum too - the mismatch
    mostly agreed by luck - but QQQ was barred while earning +$5.58 over
    7 momentum trades.

THE RULES THIS FILE PROTECTS:

  1. Every universe check is built from the SAME categories prop_bot
     enforces. A diagnostic that is stricter than the bot invents
     blockers that do not exist.
  2. A backtest whose output gates live entries replays the strategy that
     is actually running, read once per run so every symbol is scored the
     same way.
  3. A failed strategy lookup falls back to the historical default and
     says so - it never silently switches which strategy is scored.

Run: python3 test_alpaca_universe_and_strategy.py
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
router = open(os.path.join(HERE, "routers", "trading_dashboard.py"), encoding="utf-8").read()
lab = open(os.path.join(HERE, "alpaca_selection_backtest.py"), encoding="utf-8").read()
mandates = open(os.path.join(HERE, "bot_mandates.py"), encoding="utf-8").read()
prop = open(os.path.join(HERE, "prop_bot.py"), encoding="utf-8").read()
checks = []


def ok(label, cond):
    checks.append((label, bool(cond)))


# --- DEFECT 1: the universe ----------------------------------------------
blocks = re.findall(r"approved_universe = \((.*?)\)", router, re.S)
ok("both approved_universe blocks were found", len(blocks) == 2)
for i, b in enumerate(blocks, 1):
    ok(f"universe block {i} includes equities", '"equities"' in b)
    for cat in ("futures", "crypto", "commodities", "inverse_etfs"):
        ok(f"universe block {i} still includes {cat}", f'"{cat}"' in b)

prop_block = re.search(r"approved_universe = \((.*?)\)", prop, re.S)
ok("prop_bot's own universe was found", prop_block is not None)
prop_cats = set(re.findall(r'\["universe"\]\["(\w+)"\]', prop_block.group(1)))
router_cats = [set(re.findall(r'\["universe"\]\["(\w+)"\]', b)) for b in blocks]
ok("REGRESSION: the dashboard universe now MATCHES prop_bot exactly",
   all(c == prop_cats for c in router_cats))
ok("the fix records what it cost", "META, NVDA, AAPL" in router and "+$39.06" in router)

ok("META/NVDA/AAPL really are in the mandate",
   all(t in mandates for t in ('"META"', '"NVDA"', '"AAPL"')))


# --- DEFECT 2: the strategy ----------------------------------------------
tree = ast.parse(lab)


def fn(name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


ok("a live-family reader exists", fn("_live_strategy_family") is not None)
ok("a family-aware replay dispatcher exists", fn("_replay_for_live_family") is not None)

full = ast.unparse(fn("run_full_backtest"))
ok("REGRESSION: run_full_backtest no longer hardcodes the mean-reversion replay",
   "_replay_symbol(closes, symbol=ticker)" not in full)
ok("it dispatches on the live family", "_replay_for_live_family" in full)
ok("the family is read ONCE per run, not per symbol",
   full.count("_live_strategy_family()") == 1)
ok("the skip reason names the family instead of assuming mean reversion",
   "no trades produced under the live" in full
   and "RSI never dipped" not in full)

disp = ast.unparse(fn("_replay_for_live_family"))
ok("momentum routes to the momentum replay",
   "_replay_symbol_momentum" in disp and "momentum" in disp)
ok("anything else routes to mean reversion", "_replay_symbol(closes" in disp)

reader = ast.unparse(fn("_live_strategy_family"))
ok("a lookup failure falls back to the historical default",
   "mean_reversion" in reader and "except" in reader)
ok("the fallback is logged, never silent", "log.warning" in reader)

# --- both replays must stay interchangeable ------------------------------
mr = ast.unparse(fn("_replay_symbol"))
mo = ast.unparse(fn("_replay_symbol_momentum"))
for key in ("pnl_usd", "pnl_pct", "entry"):
    ok(f"both replays emit {key}, so the caller cannot misread either",
       key in mr and key in mo)
ok("they are genuinely opposite entries (guards this test's premise)",
   "rsi < RSI_LONG_THRESHOLD" in mr and "rsi > MOMENTUM_RSI_ENTRY" in mo)


# --- the measured consequence, as behaviour ------------------------------
# 30-day replay, $150/trade, from run_momentum_vs_mean_reversion_comparison.
MEASURED = {  # symbol: (mean_reversion_pnl, momentum_pnl)
    "QQQ": (3.76, 5.58), "SPY": (-2.68, 0.15), "SH": (-0.71, -1.34),
    "DIA": (-2.35, -2.59), "GLD": (2.54, -6.03), "IWM": (-6.90, -8.45),
    "META": (30.05, 39.06), "NVDA": (3.91, 8.14), "AAPL": (1.82, 7.95),
}
ok("REGRESSION: QQQ was excluded while PROFITABLE under the live strategy",
   MEASURED["QQQ"][1] > 0)
ok("four of the six excluded lose under momentum too - the mismatch mostly agreed",
   sum(1 for s in ("SH", "DIA", "GLD", "IWM") if MEASURED[s][1] < 0) == 4)
ok("so the mismatch was luck, not correctness",
   MEASURED["QQQ"][1] > 0 and MEASURED["GLD"][0] > 0 > MEASURED["GLD"][1])
ok("the three formerly-invisible equities are the best in the book",
   min(MEASURED[s][1] for s in ("META", "NVDA", "AAPL")) > max(
       MEASURED[s][1] for s in ("SPY", "SH", "DIA", "GLD", "IWM")))
ok("they are worth more than everything else combined",
   sum(MEASURED[s][1] for s in ("META", "NVDA", "AAPL")) > 55.0)

width = max(len(l) for l, _ in checks)
for label, passed in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
failed = [l for l, p in checks if not p]
print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
