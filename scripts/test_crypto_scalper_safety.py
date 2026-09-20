"""
Test that the revived crypto scalper cannot spend money by accident.

WHAT IT REPLACED
----------------
bot_2_crypto_scalper.py was retired for four reasons (main.py's lifespan
comment): it traded crypto through Alpaca, which is blocked for this
account's state, so every order failed silently - 0 wins, 0 losses, ever;
its state lived in bot2_state.json on an ephemeral filesystem and reset on
every redeploy; it sized orders off the SAME Alpaca balance prop_bot.py
trades stocks from; and crypto belongs on Coinbase.

The revival is only worth having if those four stay fixed, and if the
thing ships inert. This asserts both.

WHAT IS ASSERTED
----------------
  1. both switches default OFF - a fresh deploy neither runs nor trades
  2. PAPER MODE PLACES NO ORDERS. Asserted by counting real invocations
     against a fake place_order, not by trusting a return value. This is
     the load-bearing one: the whole point of paper mode is that a
     backtest claim can be tested forward at zero risk.
  3. the bot is on COINBASE, not Alpaca - no ALPACA env var is read
  4. state is database-backed - no JSON file is opened for reading or
     writing state
  5. capital comes from its own envelope (SCALPER_CAPITAL_USD), not from
     a shared broker balance, and defaults to 0
  6. the break-even win rate is COMPUTED from the live fee constant, not
     hardcoded - so it cannot silently go stale
  7. the strategy is preserved: the original's exit thresholds and its
     score>=2 entry rule still decide

No network, no database, no keys.
"""
import ast
import asyncio
import os
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
MODULE = REPO / "crypto_scalper_bot.py"


def load(enabled, live, capital="1000"):
    """Import the bot fresh with the switches in a known state.

    Raises ImportError with a clear message rather than a bare traceback.
    A test that dies before printing a verdict reads as "the test errored",
    which is very different from "the safety gate is gone" - and the
    difference matters most in exactly the case where someone is deciding
    whether it is safe to arm this bot.
    """
    for name in ("crypto_scalper_bot",):
        sys.modules.pop(name, None)
    os.environ["SCALPER_ENABLED"] = "true" if enabled else "false"
    os.environ["SCALPER_LIVE"] = "true" if live else "false"
    os.environ["SCALPER_CAPITAL_USD"] = capital
    import importlib
    return importlib.import_module("crypto_scalper_bot")


def require_deps():
    """The dynamic half needs the bot to import at all.

    Actually imports it, rather than probing a hand-picked list of
    packages: the module's dependency chain reaches jwt -> cryptography ->
    cffi, and an earlier version of this check tested only aiohttp and
    sqlalchemy, so the script still died on a C-extension failure three
    frames deeper. Catches BaseException because a broken cryptography
    build raises pyo3_runtime.PanicException, which does not inherit from
    Exception and would otherwise take the whole script down.
    """
    try:
        load(enabled=False, live=False)
        return None
    except BaseException as e:
        return f"{type(e).__name__}: {e}"


class CountingVenue:
    """Records every order instead of placing one."""

    def __init__(self):
        self.orders = []

    async def place_order(self, session, symbol, side, qty, price, **kw):
        self.orders.append((symbol, side, qty, price))
        return True


def fake_candles(n=60, base=100.0):
    """A deep, steadily-falling series: RSI low, price at the lower band.
    Chosen so get_signal returns BUY - a paper-mode test that never
    produces a buy signal proves nothing about whether paper mode blocks
    buys."""
    closes = [base * (1 - 0.004 * i) for i in range(n)]
    highs = [c * 1.001 for c in closes]
    lows = [c * 0.999 for c in closes]
    volumes = [10.0] * (n - 1) + [500.0]     # trailing volume spike
    return closes, highs, lows, volumes


def main():
    failures = []
    print("Crypto scalper safety\n")
    src = MODULE.read_text()
    tree = ast.parse(src)

    # ── 1. both switches default off ───────────────────────────────────
    for var, flag in (("ENABLED", "SCALPER_ENABLED"), ("LIVE", "SCALPER_LIVE")):
        if f'os.getenv("{flag}", "false")' not in src:
            failures.append(f"{flag} does not default to \"false\"")
    if f'os.getenv("SCALPER_CAPITAL_USD", "0")' not in src:
        failures.append("SCALPER_CAPITAL_USD does not default to 0 - a bot with an "
                        "implicit capital allocation is defect 3 all over again")
    if not [f for f in failures]:
        print("  SCALPER_ENABLED, SCALPER_LIVE and SCALPER_CAPITAL_USD all default to off/0")

    # ── 3. Coinbase, not Alpaca ────────────────────────────────────────
    alpaca = [n.args[0].value for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "getenv" and n.args
              and isinstance(n.args[0], ast.Constant)
              and "ALPACA" in str(n.args[0].value)]
    if alpaca:
        failures.append(f"reads Alpaca env vars {alpaca} - Alpaca crypto is blocked "
                        f"for this account's state, which is why the original never "
                        f"filled a single order")
    else:
        print("  reads no ALPACA_* config - the venue really did move to Coinbase")

    # ── 4. no JSON state file ──────────────────────────────────────────
    # Walk the AST for real file/serialisation calls. A substring search for
    # ".json" matches `await r.json()` - an HTTP response decode, not a
    # state file - and would fail this check on correct code.
    file_ops = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Name) and n.func.id == "open":
            file_ops.append(f"open() at line {n.lineno}")
        if (isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "json"
                and n.func.attr in ("load", "dump")):
            file_ops.append(f"json.{n.func.attr}() at line {n.lineno}")
    # Deliberately NOT a substring search for "bot2_state.json": this
    # module's docstring names it when explaining the defect it fixed, and
    # an earlier version of this check failed on that explanation.
    if file_ops:
        failures.append(f"persists state to a file ({', '.join(file_ops)}) - state "
                        f"must be in the database, or a redeploy wipes it "
                        f"(defect 2, which PR #109 fixed for the other bots)")
    else:
        print("  no file-backed state - positions persist via BotPosition")

    # ── 6. break-even rate is computed, not hardcoded ──────────────────
    if "TAKER_FEE_RATE" not in src:
        failures.append("does not reference TAKER_FEE_RATE - the break-even win "
                        "rate must be derived from the live fee constant")
    else:
        print("  break-even win rate derives from TAKER_FEE_RATE")

    # ── 2. PAPER MODE PLACES NO ORDERS ─────────────────────────────────
    missing = require_deps()
    if missing:
        print(f"\n  SKIPPED the runtime half - {missing}")
        print("  The static checks above passed, but whether paper mode actually")
        print("  places zero orders was NOT verified. Skipping is not a pass.")
        print()
        if failures:
            for f in failures:
                print(f"  FAIL  {f}")
            return 1
        return 1   # incomplete verification is not success for a money path

    bot = load(enabled=True, live=False)
    venue = CountingVenue()
    bot.place_order = venue.place_order

    async def _no_net(session, symbol, limit=60):
        return fake_candles()
    bot.fetch_candles = _no_net
    bot.load_positions = lambda: asyncio.sleep(0, result={})
    bot.save_position = lambda *a, **k: asyncio.sleep(0)
    bot.update_peak = lambda *a, **k: asyncio.sleep(0)
    bot.delete_position = lambda *a, **k: asyncio.sleep(0)

    # Confirm the fixture really does generate a BUY, or the next assertion
    # is vacuous.
    c, h, l, v = fake_candles()
    action, reason = bot.get_signal(c, h, l, v, False, 0, 0, "BTC/USD")
    if action != "BUY":
        failures.append(f"test fixture produced {action}, not BUY ({reason}) - the "
                        f"paper-mode assertion below would prove nothing")
    else:
        print(f"  fixture generates a real BUY signal ({reason})")

    try:
        asyncio.run(bot.run_cycle())
    except Exception as e:
        failures.append(f"paper cycle raised {type(e).__name__}: {e}")

    if venue.orders:
        failures.append(f"PAPER MODE PLACED {len(venue.orders)} ORDER(S): "
                        f"{venue.orders} - paper mode is the only thing standing "
                        f"between an unvalidated strategy and real money")
    else:
        print(f"  paper mode: {len(bot.LAST_CYCLE['signals'])} signals computed, "
              f"0 orders placed")

    if not bot.LAST_CYCLE["signals"]:
        failures.append("paper mode recorded no signals at all - it must still "
                        "compute, or it cannot test anything forward")
    if bot.LAST_CYCLE.get("live") is not False:
        failures.append("LAST_CYCLE does not report live=False in paper mode - the "
                        "dashboard badge is driven by this flag")

    # ── 7. strategy preserved ──────────────────────────────────────────
    cfg = bot.CONFIG
    for k, want in (("stop_loss_pct", 0.8), ("take_profit_pct", 2.4),
                    ("trailing_stop_pct", 1.2), ("max_positions", 4)):
        if cfg.get(k) != want:
            failures.append(f"CONFIG[{k!r}] is {cfg.get(k)}, original was {want} - "
                            f"changing the rules and the venue at once makes the "
                            f"result uninterpretable")
    # A steadily RISING series: RSI high, price at the upper band, no
    # oversold condition anywhere. Nothing here should score 2.
    rising = [100.0 * (1 + 0.004 * i) for i in range(30)]
    act, why = bot.get_signal(rising, rising, rising, [1.0] * 30, False, 0, 0, "BTC/USD")
    if act == "BUY":
        failures.append(f"an overbought, rising series produced BUY ({why}) - the "
                        f"score>=2 entry rule is not discriminating")
    else:
        print(f"  a rising/overbought series correctly does not buy ({why})")

    # Zero-volatility regression. The original bought here, because the
    # Bollinger bands collapse onto the mean and `px <= bb_lo * 1.005`
    # becomes trivially true - +2 on its own clears the entry rule, so a
    # dead-quiet market was an unconditional BUY.
    flat = [100.0] * 25
    act, why = bot.get_signal(flat, flat, flat, [1.0] * 25, False, 0, 0, "BTC/USD")
    if act == "BUY":
        failures.append(f"a ZERO-VOLATILITY series produced BUY ({why}) - the "
                        f"collapsed-Bollinger-band bug is back: the bot buys "
                        f"because there is no range")
    else:
        print(f"  zero-volatility series does not buy ({why})")

    # ── 5 & 1. disabled means disabled ─────────────────────────────────
    off = load(enabled=False, live=False)
    if off.ENABLED:
        failures.append("SCALPER_ENABLED=false still reports enabled")
    if off.CAPITAL_USD != 1000.0:
        pass  # capital is independent of the switches; not asserted here
    off_venue = CountingVenue()
    off.place_order = off_venue.place_order
    asyncio.run(off.run_forever())   # must return immediately, not loop
    if off_venue.orders:
        failures.append("a disabled bot placed orders")
    else:
        print("  disabled: run_forever() returns without trading")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  off by default, paper mode places nothing, Coinbase venue, "
          "DB state, strategy preserved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
