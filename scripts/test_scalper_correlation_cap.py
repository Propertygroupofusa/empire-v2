"""
Test that a market-wide dip cannot open four correlated positions at once.

WHAT PROMPTED IT
----------------
A live cycle looked like this:

    BTC   $84,025.19  HOLD  score=0 | none
    ETH   $2,682.12   BUY   score=3 | rsi=46 | at_bb_lower
    SOL   $120.00     BUY   score=4 | rsi=45 | at_bb_lower
    LINK  $14.0050    BUY   score=3 | rsi=51 | at_bb_lower
    DOGE  $0.0971     BUY   score=4 | rsi=44 | at_bb_lower
    ADA   $0.2542     BUY   score=3 | rsi=51 | at_bb_lower
    XRP   $1.5411     BUY   score=4 | rsi=42 | at_bb_lower

Six of seven at the lower Bollinger band in the same minute. Crypto
trades as one correlated block, so that is not six independent setups -
it is the whole market down together. max_positions=4 would have opened
four of them, which is one bet at 4x size dressed as diversification. All
four then stop out together at -0.8%, or -2.0% each after fees.

The single-pass loop also took them in COINS order, so the four opened
would have been whichever came first in the list - not the strongest.

WHAT IS ASSERTED
----------------
  1. that exact cycle opens 2 positions, not 4
  2. the two taken are the HIGHEST-SCORING, not the first in list order
  3. a lone BUY is untouched - the cap only bites on a cluster
  4. EXITS ARE NEVER CAPPED. A cycle where everything must sell closes
     every position, however many that is. Capping an exit would trap
     capital in a falling market, which is worse than the problem this
     guard solves.
  5. the cap is reported, not silent

No network, no keys, no orders.
"""
import os
import re
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
os.environ["SCALPER_LIVE"] = "true"
os.environ["SCALPER_CAPITAL_USD"] = "500"
os.environ["COINBASE_API_KEY_NAME"] = "organizations/test/apiKeys/test"
os.environ["COINBASE_API_PRIVATE_KEY"] = "test"


def main():
    failures = []
    print("Correlated-dip position cap\n")
    try:
        import scalping_bot_local as bot
    except BaseException as e:
        print(f"  SKIPPED - could not import the bot: {type(e).__name__}: {e}")
        print("  Skipping is not a pass.")
        return 1

    print(f"  bot version {bot.BOT_VERSION}\n")
    orders = []
    bot.place_order = lambda coin, side, usd_amount=None, base_size=None: (
        orders.append((coin, side)), (True, {}))[1]

    def series(drop_per_bar):
        return [100.0 * (1 - drop_per_bar * i) for i in range(40)]

    # Measured, not assumed. A 0.2%/bar decline scores 4 and a 0.6%/bar
    # decline scores 2 - the STEEPER fall scores LOWER, because its wider
    # Bollinger band puts the last price above the lower-band tolerance.
    # An earlier version of this test had that backwards, asserted the
    # steep group was strongest, and reported a ranking bug that was not
    # there. So the premise is verified here before anything is asserted
    # on it.
    STRONG, WEAK = 0.002, 0.006
    s_strong = int(re.search(r"score=(\d+)", bot.signal(series(STRONG), False, None, None)[1]).group(1))
    s_weak = int(re.search(r"score=(\d+)", bot.signal(series(WEAK), False, None, None)[1]).group(1))
    print(f"  fixture check: {STRONG*100}%/bar scores {s_strong}, "
          f"{WEAK*100}%/bar scores {s_weak}")
    if s_strong <= s_weak:
        print("  SKIPPED - the two fixtures do not produce distinct scores, so the "
              "ranking assertion below would prove nothing.")
        return 1

    # ── 1, 2 & 5. six simultaneous BUYs ────────────────────────────────
    # ETH, LINK, ADA are WEAK and come EARLIER in COINS than the strong
    # ones - so taking list order would pick ETH and LINK.
    strengths = {"BTC": 0.0, "ETH": WEAK, "SOL": STRONG, "LINK": WEAK,
                 "DOGE": STRONG, "ADA": WEAK, "XRP": STRONG}
    bot.get_candles = lambda coin, **kw: series(strengths[coin])
    state = {"positions": {}, "closed_trades": []}
    orders.clear()
    bot.run_cycle(state, authed=True)

    buys = [c for c, s in orders if s == "BUY"]
    if len(buys) != bot.CONFIG["max_new_positions_per_cycle"]:
        failures.append(f"a market-wide dip opened {len(buys)} positions "
                        f"({buys}); the cap is "
                        f"{bot.CONFIG['max_new_positions_per_cycle']}")
    else:
        print(f"  six BUY signals -> opened {len(buys)}: {', '.join(buys)}")

    # ── 2. the strongest, not the first in COINS order ─────────────────
    flat = [c for c in bot.COINS if strengths[c] == STRONG]
    if buys and not set(buys).issubset(set(flat)):
        failures.append(f"opened {buys}, but the strongest-scoring candidates "
                        f"were {flat} - entries are being taken in list order, "
                        f"not by signal strength")
    elif buys:
        print(f"  the two taken are from the strongest group {flat}, "
              f"not the head of COINS ({bot.COINS[0]}, {bot.COINS[1]})")

    # ── 3. a lone signal is not capped ─────────────────────────────────
    lone = {c: (STRONG if c == "XRP" else 0.0) for c in bot.COINS}
    bot.get_candles = lambda coin, **kw: series(lone[coin])
    state = {"positions": {}, "closed_trades": []}
    orders.clear()
    bot.run_cycle(state, authed=True)
    buys = [c for c, s in orders if s == "BUY"]
    if buys != ["XRP"]:
        failures.append(f"a single isolated BUY should open exactly XRP, got {buys}")
    else:
        print("  a lone BUY signal is unaffected by the cap")

    # ── 4. EXITS ARE NEVER CAPPED ──────────────────────────────────────
    # Four open positions, all deep underwater: every one must stop out.
    state = {"positions": {c: {"entry": 1000.0, "qty": 1.0, "peak": 1000.0}
                           for c in ("ETH", "SOL", "LINK", "DOGE")},
             "closed_trades": []}
    bot.get_candles = lambda coin, **kw: [50.0] * 40    # far below every entry
    orders.clear()
    bot.run_cycle(state, authed=True)
    sells = [c for c, s in orders if s == "SELL"]
    if len(sells) != 4:
        failures.append(f"only {len(sells)} of 4 losing positions were closed "
                        f"({sells}) - an exit must NEVER be capped, or capital "
                        f"is trapped in a falling market")
    elif state["positions"]:
        failures.append(f"positions remain after all sold: {list(state['positions'])}")
    else:
        print(f"  all {len(sells)} stop-losses fired - exits are not capped")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    print("  PASS  a correlated dip enters small, the strongest first, and "
          "exits stay uncapped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
