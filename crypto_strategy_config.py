import logging
import os


log = logging.getLogger("crypto_strategy_config")

SUPPORTED_CRYPTO_STRATEGIES = frozenset({
    "btc_compound",
    "family_tree",
    "grid_fleet",
    "multi_pair",
})

# Returned when the variable is missing, empty, or not a known strategy.
# Deliberately not a member of SUPPORTED_CRYPTO_STRATEGIES, so every
# dispatch site falls through to its else branch and starts NO bot thread.
UNCONFIGURED = "unconfigured"


def get_crypto_strategy_mode() -> str:
    """The strategy to run, or UNCONFIGURED. Never a substitute.

    An unusable value used to fall back to 'btc_compound' "so the account is
    not left idle". That trade was made deliberately and it was wrong. Here
    is the bill, from 2026-09-25:

        CRYPTO_STRATEGY_MODE='delfina_scalping' - one typo - resolved to
        btc_compound on the web service. btc_compound is a single-position
        strategy: it converted essentially the whole Coinbase balance into
        one BTC position (0.00684381 BTC, $576.08). The grid fleet, which
        the operator had actually configured and funded, was left with
        $0.29 and could not open a slice for days. Diagnosing it took an
        entire session, and the fallback WARNING was read only at the end.

    So the old docstring's premise - "a wrong-but-announced strategy is
    recoverable in the seconds it takes to read one line" - did not hold.
    The line was not read for days, and the wrong strategy did not merely
    fail to trade: it SPENT the capital allocated to another strategy on a
    position nobody chose. An idle account is visibly idle and loses
    nothing. A silently substituted capital-deploying strategy is neither.

    So: an unusable value now returns UNCONFIGURED, which matches no
    dispatch branch anywhere, so nothing trades and nothing is bought. The
    message is logged at ERROR, names the rejected value and every accepted
    one, and says exactly what to set. The Live Ops runner panel surfaces
    the same state, so it is visible without reading logs at all.

    The stripping below is load-bearing: a value pasted into Railway with
    surrounding quotes ('"btc_compound"') is a real, previously observed
    failure, which is why quotes and whitespace come off before the
    membership check rather than after.
    """
    raw = os.getenv("CRYPTO_STRATEGY_MODE")
    mode = (raw or "").strip().strip('"').strip("'").strip()
    if mode in SUPPORTED_CRYPTO_STRATEGIES:
        return mode

    log.error(
        "CRYPTO_STRATEGY_MODE=%r is not a known strategy. NO crypto loop will "
        "start and nothing will be bought or sold. Set it to one of: %s. "
        "On this deployment: grid_fleet on the crypto-trading service (which "
        "also needs SERVICE_ROLE=crypto-trading), family_tree on the web "
        "service. Refusing to substitute a strategy - an unchosen one spends "
        "real money on positions you did not ask for.",
        raw, "/".join(sorted(SUPPORTED_CRYPTO_STRATEGIES)),
    )
    return UNCONFIGURED


def is_grid_fleet_mode() -> bool:
    return get_crypto_strategy_mode() == "grid_fleet"
