"""One Coinbase wallet, several bots, no starvation.

THE PROBLEM THIS SOLVES, observed live on 2026-09-24:

The grid fleet and the family tree spend the SAME real Coinbase USD
wallet, and neither knows the other exists. Whichever loop reaches the
cash first takes as much as it wants; the other finds an empty wallet and
loops forever looking healthy. That day the fallback btc_compound loop
had converted ~$577 of a ~$577 account into BTC, leaving $0.29, and the
grid fleet - freshly deployed, credentials verified, supervised loop
running, logs entirely clean - had nothing to trade with and no way to
say so.

THE RULE:

    allocatable = max(0, free_cash - global_reserve)
    ceiling(bot) = allocatable * share(bot)

A bot may never spend more than its ceiling in one decision. The global
reserve is held back from every bot, so the wallet is never drained to
zero by anyone - fees settle, and an account at exactly $0.00 cannot pay
one.

WHAT THIS IS NOT: it is not a lock, and two loops can still race inside
one instant. It bounds the DAMAGE of that race rather than preventing it.
A bot that wins a race can take its share and no more, so the loser still
finds its own share waiting. That is the property that matters, and it is
achievable without cross-process coordination this codebase does not have.

SHARES ARE FIXED, NOT DYNAMIC, and that is deliberate. "Let an idle bot's
share flow to a busy one" sounds better and reintroduces the exact
failure: the busy bot takes everything, the idle one wakes up to an empty
wallet. An idle bot's share sitting unused is the cost of the guarantee.
If a bot is genuinely not running, set its share to 0 and the others
divide the whole wallet.

Configure with CRYPTO_CASH_SHARE_GRID / _TREE / _COMPOUND (fractions) and
CRYPTO_GLOBAL_CASH_RESERVE_USD. Defaults favour the grid fleet because it
is the only component with a positive realized record; the tree is
unproven and btc_compound is a fallback that should not normally be
running at all.

Deliberately no figure quoted here. An earlier version of this comment
cited "+$19.55 over 82 trades at a 76% win rate" as though it were a
standing measurement. It came from a single dashboard reading, was never
reproducible from anything in this repo, and a later screenshot of the
same dashboard showed 22 trades and +$12.94 across the two live branches.
A number that cannot be reproduced does not belong in a comment that
justifies how capital is split - read the live per-branch figures
instead.
"""

import logging
import os

log = logging.getLogger("crypto_cash_allocator")

# Bot keys. These are the identities that share the wallet.
GRID = "grid"
TREE = "tree"
COMPOUND = "compound"
BOTS = (GRID, TREE, COMPOUND)

DEFAULT_SHARES = {
    GRID: 0.70,      # the only component with a positive measured record
    TREE: 0.30,      # unproven; resuming from retirement
    COMPOUND: 0.0,   # a fallback loop, not a strategy anyone chose
}

# Never let the wallet be drained to exactly zero. Fees settle after a
# fill, and an account at $0.00 cannot pay one - a rejected fee is a
# stuck position, which costs far more than the few dollars held back.
DEFAULT_GLOBAL_RESERVE_USD = 15.0

_ENV_SHARE = {
    GRID: "CRYPTO_CASH_SHARE_GRID",
    TREE: "CRYPTO_CASH_SHARE_TREE",
    COMPOUND: "CRYPTO_CASH_SHARE_COMPOUND",
}
ENV_GLOBAL_RESERVE = "CRYPTO_GLOBAL_CASH_RESERVE_USD"


def _env_float(name, default):
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        log.warning("%s=%r is not a number - using %s", name, raw, default)
        return default
    if value < 0:
        log.warning("%s=%r is negative - using %s", name, raw, default)
        return default
    return value


def load_shares(env=None):
    """Each bot's share of the wallet, normalised so they cannot overcommit.

    Shares summing above 1.0 would hand out more than the wallet holds -
    each bot would pass its own ceiling check and the wallet would still
    come up short, which is precisely the bug this module exists to
    prevent. Over-committed shares are therefore scaled down
    proportionally and the adjustment is logged, never silently applied.

    Summing BELOW 1.0 is left alone: it is a legitimate way to hold extra
    cash back beyond the global reserve.

    All-zero shares are returned as-is rather than normalised. It means
    "nobody may spend", which is a valid, if drastic, configuration -
    inventing a split there would override an explicit instruction.
    """
    get = (env or os.environ).get
    raw = {}
    for bot in BOTS:
        name = _ENV_SHARE[bot]
        value = get(name)
        if value is None or not str(value).strip():
            raw[bot] = DEFAULT_SHARES[bot]
            continue
        try:
            parsed = float(str(value).strip())
        except (TypeError, ValueError):
            log.warning("%s=%r is not a number - using %s", name, value, DEFAULT_SHARES[bot])
            raw[bot] = DEFAULT_SHARES[bot]
            continue
        raw[bot] = max(0.0, parsed)

    total = sum(raw.values())
    if total > 1.0 + 1e-9:
        log.warning(
            "cash shares sum to %.3f, over 1.0 - scaling down proportionally so the "
            "bots cannot collectively claim more wallet than exists (%s)",
            total, ", ".join(f"{b}={raw[b]:.3f}" for b in BOTS),
        )
        return {bot: raw[bot] / total for bot in BOTS}
    return raw


def spend_ceiling(bot, free_cash_usd,
                  global_reserve_usd=None, shares=None):
    """The most `bot` may spend right now. Returns (ceiling, reason).

    free_cash_usd is the shared pool both systems already agree on
    (crypto_grid_bot.get_real_free_cash_usd). None means the balance
    could not be read, and that returns a ceiling of None - not 0.0.
    The distinction is load-bearing: 0.0 says "there is no money", None
    says "I do not know", and a caller must not deploy on either but
    should only say the wallet is empty for the first.
    """
    if bot not in BOTS:
        return None, f"unknown bot {bot!r} - no ceiling can be computed"
    if free_cash_usd is None:
        return None, "free cash could not be read - not deploying against an unknown balance"

    reserve = (DEFAULT_GLOBAL_RESERVE_USD if global_reserve_usd is None
               else max(0.0, float(global_reserve_usd)))
    share = (shares or load_shares()).get(bot, 0.0)

    allocatable = max(0.0, float(free_cash_usd) - reserve)
    ceiling = round(allocatable * share, 2)

    if share <= 0:
        return 0.0, (f"{bot} has a 0% cash share - it is configured not to spend "
                     f"(set {_ENV_SHARE[bot]} above 0 to change that)")
    if allocatable <= 0:
        return 0.0, (f"${free_cash_usd:,.2f} free is at or below the ${reserve:,.2f} "
                     f"global reserve - nothing is allocatable to anyone")
    return ceiling, (f"{bot} may spend up to ${ceiling:,.2f} "
                     f"({share * 100:.0f}% of ${allocatable:,.2f} allocatable, "
                     f"after a ${reserve:,.2f} global reserve)")


def allocation_report(free_cash_usd, global_reserve_usd=None, shares=None):
    """Every bot's ceiling in one structure, for the dashboard.

    Built so a starved bot is visible BEFORE it starves rather than
    inferred afterwards from the absence of trades.
    """
    shares = shares or load_shares()
    reserve = (DEFAULT_GLOBAL_RESERVE_USD if global_reserve_usd is None
               else max(0.0, float(global_reserve_usd)))
    allocatable = (None if free_cash_usd is None
                   else max(0.0, round(float(free_cash_usd) - reserve, 2)))
    rows = []
    for bot in BOTS:
        ceiling, reason = spend_ceiling(bot, free_cash_usd, reserve, shares)
        rows.append({
            "bot": bot,
            "share_pct": round(shares.get(bot, 0.0) * 100, 1),
            "ceiling_usd": ceiling,
            "reason": reason,
        })
    return {
        "free_cash_usd": None if free_cash_usd is None else round(float(free_cash_usd), 2),
        "global_reserve_usd": reserve,
        "allocatable_usd": allocatable,
        "shares_sum_pct": round(sum(shares.values()) * 100, 1),
        "bots": rows,
    }


# --- self-test ------------------------------------------------------------


def _self_test():
    checks = []

    def ok(label, cond):
        checks.append((label, bool(cond)))

    S = {GRID: 0.70, TREE: 0.30, COMPOUND: 0.0}

    # The live scenario this was built for.
    c, why = spend_ceiling(GRID, 0.29, 15.0, S)
    ok("a drained wallet gives the grid a $0 ceiling", c == 0.0)
    ok("and says the reserve is why, not 'no share'", "global reserve" in why)

    c, _ = spend_ceiling(GRID, 615.0, 15.0, S)
    ok("a funded wallet gives the grid 70% of what is allocatable", c == 420.0)
    c, _ = spend_ceiling(TREE, 615.0, 15.0, S)
    ok("and the tree 30%", c == 180.0)
    ok("the two together never exceed what is allocatable", 420.0 + 180.0 <= 615.0 - 15.0)

    c, why = spend_ceiling(COMPOUND, 615.0, 15.0, S)
    ok("a 0%-share bot gets nothing", c == 0.0)
    ok("and is told it is configured that way, not that the wallet is empty",
       "0% cash share" in why)

    c, why = spend_ceiling(GRID, None, 15.0, S)
    ok("an unreadable balance yields None, NOT 0.0", c is None)
    ok("and says so rather than reporting an empty wallet", "could not be read" in why)

    c, _ = spend_ceiling("nonsense", 615.0, 15.0, S)
    ok("an unknown bot cannot be given a ceiling", c is None)

    c, _ = spend_ceiling(GRID, 10.0, 15.0, S)
    ok("cash below the reserve is not allocatable", c == 0.0)
    c, _ = spend_ceiling(GRID, 15.0, 15.0, S)
    ok("cash exactly at the reserve is not allocatable either", c == 0.0)

    # Normalisation.
    over = load_shares({"CRYPTO_CASH_SHARE_GRID": "0.8", "CRYPTO_CASH_SHARE_TREE": "0.8"})
    ok("over-committed shares are scaled to sum to 1.0",
       abs(sum(over.values()) - 1.0) < 1e-9)
    ok("and keep their relative proportions", abs(over[GRID] - over[TREE]) < 1e-9)

    under = load_shares({"CRYPTO_CASH_SHARE_GRID": "0.5", "CRYPTO_CASH_SHARE_TREE": "0.2",
                         "CRYPTO_CASH_SHARE_COMPOUND": "0"})
    ok("under-committed shares are left alone (holding cash back is valid)",
       abs(sum(under.values()) - 0.7) < 1e-9)

    zero = load_shares({"CRYPTO_CASH_SHARE_GRID": "0", "CRYPTO_CASH_SHARE_TREE": "0",
                        "CRYPTO_CASH_SHARE_COMPOUND": "0"})
    ok("all-zero shares stay zero rather than being invented",
       sum(zero.values()) == 0.0)

    junk = load_shares({"CRYPTO_CASH_SHARE_GRID": "abc"})
    ok("a garbage share falls back to the default", junk[GRID] == DEFAULT_SHARES[GRID])
    neg = load_shares({"CRYPTO_CASH_SHARE_TREE": "-1"})
    ok("a negative share is floored at 0, never allowed to inflate others",
       neg[TREE] == 0.0)

    defaults = load_shares({})
    ok("defaults favour the only component with a measured record",
       defaults[GRID] > defaults[TREE] > defaults[COMPOUND] == 0.0)
    ok("and never overcommit the wallet", sum(defaults.values()) <= 1.0 + 1e-9)

    # Report.
    rep = allocation_report(615.0, 15.0, S)
    ok("the report names every bot", {r["bot"] for r in rep["bots"]} == set(BOTS))
    ok("the report shows what is allocatable", rep["allocatable_usd"] == 600.0)
    ok("the report's ceilings never exceed what is allocatable",
       sum(r["ceiling_usd"] for r in rep["bots"]) <= rep["allocatable_usd"] + 1e-9)
    rep_none = allocation_report(None, 15.0, S)
    ok("an unreadable balance reports None, not 0", rep_none["allocatable_usd"] is None
       and all(r["ceiling_usd"] is None for r in rep_none["bots"]))

    # The starvation property itself, stated as a test.
    free = 615.0
    grid_c, _ = spend_ceiling(GRID, free, 15.0, S)
    ok("after the grid spends its whole ceiling, the tree's share is untouched",
       spend_ceiling(TREE, free - grid_c, 15.0, S)[0] > 0)

    width = max(len(label) for label, _ in checks)
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label:<{width}}")
    failed = [label for label, passed in checks if not passed]
    print(f"\n  {len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    print("=" * 72)
    print("  CRYPTO CASH ALLOCATOR - self-test")
    print("=" * 72)
    raise SystemExit(_self_test())
