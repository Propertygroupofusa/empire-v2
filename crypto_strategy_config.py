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

# What the services should be set to, corrected 2026-09-26 against the live
# deployment rather than against the topology this file assumed.
#
# Two versions of this text have now been wrong, in opposite directions:
#
#   "family_tree on the web service" - written when family_tree was the
#   intent. Every mode spends the SAME Coinbase balance (main.py: "Only one
#   runs at a time because all modes share the same real Coinbase account and
#   balance"), so once grid_fleet went live that instruction became "start a
#   second strategy on the money the fleet is trading", printed as the remedy
#   for a log line.
#
#   "leave CRYPTO_STRATEGY_MODE UNSET on the web service" - its replacement,
#   written on the assumption that a dedicated crypto-trading service owns the
#   loop. It does not. /api/trading-dashboard/grid-status reported
#   loop_lease.this_process='web:1' with held_by_this_process=True and a
#   5-second-old cycle: the loop is running in the WEB service's standby
#   thread (the grid_fleet branch of main.py), and it is in that branch only
#   because a DB strategy override resolves to grid_fleet. Unsetting the
#   variable therefore leaves the entire fleet hanging on one database row -
#   clear that row and the standby thread never starts and nothing says so.
#
# So the rule is not per-service at all. It is: name the strategy that should
# run, everywhere it might run. grid_fleet is idempotent across processes
# because of the lease; a different mode beside it is not.

_WIRING = (
    "Correct wiring: CRYPTO_STRATEGY_MODE=grid_fleet on every service that "
    "should run the fleet, plus SERVICE_ROLE=crypto-trading on the dedicated "
    "runner if one is deployed. grid_fleet on more than one process is SAFE: "
    "crypto_grid_bot holds a lease it renews every cycle, and a second process "
    "stays on standby until that lease goes stale, so the two cannot "
    "double-order. What is not safe is a DIFFERENT mode beside a live fleet - "
    "every mode spends the same Coinbase balance, so family_tree or "
    "btc_compound would trade the money the fleet is already using. If the "
    "variable cannot be corrected through the Railway UI, set "
    "CRYPTO_STRATEGY_MODE_OVERRIDE instead - it is checked first and wins."
)


# What actually started, registered by whoever started it. This module is
# sync and reads only the environment, so without this it cannot tell
# "misconfigured and dead" from "misconfigured but running from a DB
# override" - and those two deserve very different log levels.
_RUNTIME_MODE = None
_RUNTIME_SOURCE = None


def note_runtime_mode(mode, source):
    """Record the strategy a caller actually started. Only a REAL one counts.

    That guard is the whole point, and leaving it out turned the safety
    feature into its own failure. main.py registers whatever lifespan()
    resolved - which, with a stale variable and no DB override, is
    UNCONFIGURED. Recording that set _RUNTIME_MODE to a truthy non-strategy,
    so every later read took the "something is running" branch and logged:

        CRYPTO_STRATEGY_MODE='delfina_scalping' is not a known strategy
        (a retired name, not a typo), but 'unconfigured' is running from
        CRYPTO_STRATEGY_MODE, so trading is NOT stopped.

    Nothing was running. That branch exists to suppress a FALSE ERROR; it
    had become a false ALL-CLEAR, which is the worse of the two - a wrong
    ERROR at least sends someone to look.

    Returns True if the registration was accepted.
    """
    global _RUNTIME_MODE, _RUNTIME_SOURCE
    if mode not in SUPPORTED_CRYPTO_STRATEGIES:
        # Not a caller error. "I resolved to UNCONFIGURED and started
        # nothing" is an honest thing to report; it just is not evidence
        # that anything is trading, so it must not be stored as such.
        log.debug("not recording %r as a running strategy - it is not one", mode)
        return False
    _RUNTIME_MODE, _RUNTIME_SOURCE = mode, source
    return True


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

    # ABSENT is not the same fault as WRONG, and the old code could not tell
    # them apart - both landed on the ERROR below. That made the advice in
    # that ERROR contradict itself: it says "on the web service leave
    # CRYPTO_STRATEGY_MODE UNSET", and then an unset variable produced
    #
    #     CRYPTO_STRATEGY_MODE=None is not a known strategy. No crypto loop
    #     will start FROM THIS VARIABLE.
    #
    # So following the fix printed inside the error re-raised the error, with
    # a different value in it. Deleting the stale variable - the whole point -
    # would have swapped one red line every boot for another.
    #
    # A missing variable is a process with no crypto strategy, which on the
    # web service is the intended state and on any other process is simply
    # nothing to say. The one place where absence IS fatal announces it
    # itself: bot_runner.py takes UNCONFIGURED and logs "NO crypto loop is
    # running anywhere - not here, and not on the web service" at ERROR
    # before exiting. So nothing is hidden by stepping down here, and the
    # dashboard's runner panel still goes red when no loop is alive anywhere.
    if not any((os.getenv(v) or "").strip()
               for v in ("CRYPTO_STRATEGY_MODE", "CRYPTO_STRATEGY_MODE_OVERRIDE")):
        log.info(
            "No CRYPTO_STRATEGY_MODE set, so nothing starts FROM THIS VARIABLE. "
            "On a process that should run no crypto loop that is the intended "
            "state. But if this process IS running the fleet, it is doing so "
            "from a DB strategy override alone - one row away from a silent "
            "stop - and the variable should be set to match it. %s", _WIRING,
        )
        return UNCONFIGURED

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

    # A RETIRED name is a different fault from a typo, and one level does not
    # fit both. delfina_scalping / scalping / delfina name strategies that no
    # longer exist in this build: no module, no dispatch branch, nothing in
    # the code to repair. Nothing was substituted, so nothing was spent, and
    # on the web service "no crypto loop from this variable" is the INTENDED
    # state. An ERROR every boot, describing an intended state, is how an
    # operator learns to scroll past ERRORs - which is the exact habit this
    # module was written to stop.
    #
    # The one process where a retired name IS an emergency does not depend on
    # this level: bot_runner.py (the crypto-trading service) sees UNCONFIGURED
    # and logs its own ERROR, "NO crypto loop is running anywhere", then
    # exits. So the downgrade here cannot quiet the service that matters.
    if _is_retired_name():
        log.warning(
            "CRYPTO_STRATEGY_MODE=%r names a RETIRED strategy. There is no code "
            "behind that name anywhere in this build - it is not a typo, and "
            "there is nothing to fix in the code. Nothing was substituted and "
            "no crypto loop will start FROM THIS VARIABLE. The fix is to DELETE "
            "the stale variable. %s",
            os.getenv("CRYPTO_STRATEGY_MODE"), _WIRING,
        )
        return UNCONFIGURED

    log.error(
        "CRYPTO_STRATEGY_MODE=%r is not a known strategy - somebody typed a "
        "value meaning to run something, and it is not running. "
        "No crypto loop will start FROM THIS VARIABLE. "
        "Accepted values: %s. Refusing to substitute "
        "a strategy - an unchosen one spends real money on positions you did "
        "not ask for. %s",
        os.getenv("CRYPTO_STRATEGY_MODE"),
        "/".join(sorted(SUPPORTED_CRYPTO_STRATEGIES)),
        _WIRING,
    )
    return UNCONFIGURED


def is_grid_fleet_mode() -> bool:
    return get_crypto_strategy_mode() == "grid_fleet"
