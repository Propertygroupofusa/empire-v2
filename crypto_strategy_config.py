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


# Names that were real once and are not strategies any more. Calling one of
# these a typo sends the operator looking for a spelling mistake that is not
# there; naming it as retired points at the real fix, which is deleting the
# variable.
RETIRED_STRATEGY_NAMES = {"delfina_scalping", "scalping", "delfina"}

# What actually started, registered by whoever started it. This module is
# sync and reads only the environment, so without this it cannot tell
# "misconfigured and dead" from "misconfigured but running from a DB
# override" - and those two deserve very different log levels.
_RUNTIME_MODE = None
_RUNTIME_SOURCE = None


def note_runtime_mode(mode, source):
    """Record the strategy a caller actually started, and where it came from."""
    global _RUNTIME_MODE, _RUNTIME_SOURCE
    _RUNTIME_MODE, _RUNTIME_SOURCE = mode, source


def _is_retired_name():
    raw = (os.getenv("CRYPTO_STRATEGY_MODE") or "").strip().strip('"').strip("'").strip()
    return raw.lower() in RETIRED_STRATEGY_NAMES


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
    # CRYPTO_STRATEGY_MODE_OVERRIDE is checked FIRST and is an escape hatch,
    # not a feature. On 2026-09-25 the deployment reached a state where
    # CRYPTO_STRATEGY_MODE could not be corrected through the Railway UI at
    # all: the variable was deleted (confirmed - /health reported "(unset)"),
    # re-added as family_tree, and a fresh process six minutes later still
    # read the old 'delfina_scalping'. A deleted value came back on its own,
    # most likely a redeploy of an earlier deployment restoring that
    # deployment's variable snapshot.
    #
    # Every other explanation had already been eliminated by measurement:
    # the environment was production, /health listed exactly one key named
    # CRYPTO_STRATEGY_MODE, uptime proved the process had restarted, and the
    # commit proved the running build was current.
    #
    # So this accepts a SECOND name with no deployment history to restore.
    # Setting a brand-new variable sidesteps whatever is pinning the old one.
    # It is deliberately checked first so it can win without the stuck value
    # having to be removed. Same validation - it is a new name, not a new
    # trust level, and an unusable value here still yields UNCONFIGURED
    # rather than a substitute.
    for var in ("CRYPTO_STRATEGY_MODE_OVERRIDE", "CRYPTO_STRATEGY_MODE"):
        raw = os.getenv(var)
        mode = (raw or "").strip().strip('"').strip("'").strip()
        if mode in SUPPORTED_CRYPTO_STRATEGIES:
            if var != "CRYPTO_STRATEGY_MODE":
                log.warning(
                    "Using %s=%r. This overrides CRYPTO_STRATEGY_MODE=%r and exists "
                    "only because that variable could not be corrected through the "
                    "Railway UI. Remove it once the underlying variable is fixed.",
                    var, mode, os.getenv("CRYPTO_STRATEGY_MODE"),
                )
            return mode

    # A DB strategy override can start a loop that this function cannot see:
    # it is async and DB-backed, this is sync and env-only. Before this
    # check existed the message below asserted "NO crypto loop will start
    # and nothing will be bought or sold" while grid_fleet was running from
    # exactly such an override and had bought a real NEAR-USD slice. A false
    # alarm at ERROR level is not harmless - it teaches the operator to
    # scroll past the one message that would matter if it were ever true.
    if _RUNTIME_MODE is not None:
        log.warning(
            "CRYPTO_STRATEGY_MODE=%r is not a known strategy%s, but %r is "
            "running from %s, so trading is NOT stopped. Clear the stale "
            "variable when Railway allows it; nothing is broken meanwhile.",
            os.getenv("CRYPTO_STRATEGY_MODE"),
            " (a retired name, not a typo)" if _is_retired_name() else "",
            _RUNTIME_MODE, _RUNTIME_SOURCE or "another source",
        )
        return UNCONFIGURED

    log.error(
        "CRYPTO_STRATEGY_MODE=%r is not a known strategy. No crypto loop will "
        "start FROM THIS VARIABLE. Set it to one of: %s. "
        "On this deployment: grid_fleet on the crypto-trading service (which "
        "also needs SERVICE_ROLE=crypto-trading), family_tree on the web "
        "service. Refusing to substitute a strategy - an unchosen one spends "
        "real money on positions you did not ask for. If this variable cannot "
        "be corrected through the Railway UI, set CRYPTO_STRATEGY_MODE_OVERRIDE "
        "to the same value instead - it is checked first and wins.",
        os.getenv("CRYPTO_STRATEGY_MODE"),
        "/".join(sorted(SUPPORTED_CRYPTO_STRATEGIES)),
    )
    return UNCONFIGURED


def is_grid_fleet_mode() -> bool:
    return get_crypto_strategy_mode() == "grid_fleet"
