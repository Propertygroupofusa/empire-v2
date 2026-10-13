"""The day/swing trader sells only at a win, like prop_bot.

WHY THIS FILE EXISTS. prop_bot got "sell only at a win" on 2026-10-12 at
the owner's instruction; alpaca_swing_bot.py did not, so on the same Alpaca
account it still sold at its -0.5% intraday and -2% swing hard stops, and on
an RSI exit taken below cost. Its live log on 2026-10-13 still read
"Stops: HARD STOP-LOSS ENABLED (2.0%)" beside prop_bot's
"ALPACA_SELL_ONLY_AT_PROFIT armed".

The gate is behavioural (real function, real env reads); the wiring is
structural, since "does every sell pass through it" is a question about the
code.
"""
import ast
import os
import sys

failures = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label} {detail}")


os.environ.pop("ALPACA_SELL_ONLY_AT_PROFIT", None)
import alpaca_swing_bot as sb  # noqa: E402

floor = sb.min_exit_pnl_pct()
print(f"--- win floor {floor:.2f}% ---")
ok("floor is prop_bot's round trip plus margin (0.32%)", abs(floor - 0.32) < 1e-9, floor)

print("--- armed by default (variable unset) ---")
ok("unset means armed", sb.sell_only_at_profit() is True)
e, r = sb.gate_exit_at_win(True, "HARD STOP-LOSS -0.55% hit", -0.55)
ok("intraday hard stop below cost is HELD", e is False and "Hold at -0.55%" in r, r)
e, r = sb.gate_exit_at_win(True, "HARD STOP-LOSS -2.10% hit", -2.10)
ok("swing hard stop below cost is HELD", e is False, r)
e, r = sb.gate_exit_at_win(True, "15-min RSI 70 > 65 (overbought)", 0.10)
ok("RSI exit under the floor is HELD", e is False and "win floor" in r, r)
e, r = sb.gate_exit_at_win(True, "15-min RSI 70 > 65 (overbought)", 0.40)
ok("RSI exit above the floor still sells", e is True and r.startswith("15-min RSI"), r)
e, r = sb.gate_exit_at_win(True, "Profit target +1.05% hit", 1.05)
ok("profit target still sells", e is True, r)
e, r = sb.gate_exit_at_win(False, None, -3.0)
ok("no exit wanted stays no exit", e is False and r is None)

print("--- the off switch is an explicit negative only ---")
for v in ("false", "0", "no", "OFF", '"false"'):
    os.environ["ALPACA_SELL_ONLY_AT_PROFIT"] = v
    e, _ = sb.gate_exit_at_win(True, "HARD STOP-LOSS -0.55% hit", -0.55)
    ok(f"{v!r} turns it off (stop sells)", e is True)
for v in ("", "true", "flase", "1"):
    os.environ["ALPACA_SELL_ONLY_AT_PROFIT"] = v
    e, _ = sb.gate_exit_at_win(True, "HARD STOP-LOSS -0.55% hit", -0.55)
    ok(f"{v!r} leaves it armed (held)", e is False)
os.environ.pop("ALPACA_SELL_ONLY_AT_PROFIT", None)

print("--- every sell order is placed only after the gate ---")
src = open(sb.__file__, encoding="utf-8").read()
tree = ast.parse(src)
sells, gated = 0, 0
for fn in ast.walk(tree):
    if not isinstance(fn, ast.AsyncFunctionDef):
        continue
    body = ast.get_source_segment(src, fn) or ""
    n_sell = body.count('place_order(session, proxy, qty, "sell")')
    if not n_sell:
        continue
    sells += n_sell
    if body.count("gate_exit_at_win(should_exit, reason, pnl_pct)") >= n_sell \
            and body.index("gate_exit_at_win(") < body.index('"sell")'):
        gated += n_sell
ok("found the exit sells", sells == 2, sells)
ok("each one comes after the gate in its function", gated == sells, f"{gated}/{sells}")

print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED'}")
sys.exit(1 if failures else 0)
