import logging
import os


log = logging.getLogger("crypto_strategy_config")

DEFAULT_CRYPTO_STRATEGY = "btc_compound"

SUPPORTED_CRYPTO_STRATEGIES = frozenset({
    "btc_compound",
    "family_tree",
    "grid_fleet",
    "multi_pair",
})


def get_crypto_strategy_mode() -> str:
    """The strategy to run, falling back to the default on an unusable value.

    An unrecognized CRYPTO_STRATEGY_MODE used to be returned verbatim, and
    main.py's dispatch would then match none of its four branches, take the
    else, log a warning and start NO bot thread at all. A single typo in a
    Railway variable therefore stopped all crypto trading silently-in-effect
    - the warning was there, but the account simply sat idle until someone
    read the logs. That is what 'delfina_scalping' did: capital untouched
    from Sep 23 onward.

    Falling back trades one risk for another and the trade is deliberate.
    Running the documented default is not what the operator typed, so it is
    logged at WARNING naming both the rejected value and the substitute -
    never silently. The alternative, which this replaces, was an idle
    account and an equally easy-to-miss log line, with no trading at all to
    show for the caution. A wrong-but-announced strategy is recoverable in
    the seconds it takes to read one line; weeks of not trading is not.

    The stripping below is also load-bearing: a value pasted into Railway
    with surrounding quotes ('"btc_compound"') is a real, previously
    observed failure, which is why quotes and whitespace come off before
    the membership check rather than after.
    """
    raw = os.getenv("CRYPTO_STRATEGY_MODE")
    if raw is None:
        return DEFAULT_CRYPTO_STRATEGY

    mode = raw.strip().strip('"').strip("'").strip()
    if not mode:
        return DEFAULT_CRYPTO_STRATEGY
    if mode in SUPPORTED_CRYPTO_STRATEGIES:
        return mode

    log.warning(
        "CRYPTO_STRATEGY_MODE=%r is not a known strategy (%s) - running %r "
        "instead so the account is not left idle. Fix the Railway variable "
        "to run something else.",
        raw, "/".join(sorted(SUPPORTED_CRYPTO_STRATEGIES)), DEFAULT_CRYPTO_STRATEGY,
    )
    return DEFAULT_CRYPTO_STRATEGY


def is_grid_fleet_mode() -> bool:
    return get_crypto_strategy_mode() == "grid_fleet"
