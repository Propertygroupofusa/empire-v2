"""The owner's retired list: SH, PSQ, DOG and RWM are out of NEW entries.

Added 2026-10-12 on the owner's instruction ("fix whatever that needs to
be fixed for it to change to it being a positive result") after a
read-only replay of the live rule set with and without the four inverse
ETFs - see the comment above _retired_symbols_from_env in prop_bot.py.

Entry-only: the set is unioned into get_effective_excluded_symbols(),
which only the entry paths consult. Exits never read it, FUTURES keeps
the four so a held position still resolves, and the mandate keeps its
inverse_etfs list so compliance history is unchanged.

No broker, no database, no network. Runnable as a script or under pytest.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prop_bot  # noqa: E402

ENV = prop_bot.ALPACA_RETIRED_SYMBOLS_ENV
HERE = os.path.dirname(os.path.abspath(__file__))


def _with_env(value):
    """Set (or unset with None) the variable for the duration of a call."""
    saved = os.environ.pop(ENV, None)
    if value is not None:
        os.environ[ENV] = value
    return saved


def _restore(saved):
    os.environ.pop(ENV, None)
    if saved is not None:
        os.environ[ENV] = saved


def test_unset_means_the_four_measured_names():
    saved = _with_env(None)
    try:
        assert prop_bot._retired_symbols_from_env() == {"SH", "PSQ", "DOG", "RWM"}
    finally:
        _restore(saved)


def test_an_explicit_empty_string_brings_them_all_back():
    saved = _with_env("")
    try:
        assert prop_bot._retired_symbols_from_env() == set()
    finally:
        _restore(saved)


def test_a_comma_list_retires_exactly_those_names():
    saved = _with_env(" sh , Dog;rwm ")
    try:
        assert prop_bot._retired_symbols_from_env() == {"SH", "DOG", "RWM"}
    finally:
        _restore(saved)


def test_the_retired_names_are_in_the_effective_excluded_set():
    """Even with both measured layers empty, the four are excluded."""
    async def _no_layers():
        return set(), set()
    real = prop_bot._exclusion_layers
    saved = _with_env(None)
    prop_bot._exclusion_layers = _no_layers
    try:
        got = asyncio.run(prop_bot.get_effective_excluded_symbols())
        assert {"SH", "PSQ", "DOG", "RWM"} <= got
        assert "AMZN" not in got and "SLV" not in got
    finally:
        prop_bot._exclusion_layers = real
        _restore(saved)


def test_the_measured_layers_still_come_through():
    async def _layers():
        return {"USO"}, {"GLD"}
    real = prop_bot._exclusion_layers
    saved = _with_env("")
    prop_bot._exclusion_layers = _layers
    try:
        assert asyncio.run(prop_bot.get_effective_excluded_symbols()) == {"USO", "GLD"}
    finally:
        prop_bot._exclusion_layers = real
        _restore(saved)


def test_the_reason_names_the_retirement_without_touching_the_database():
    saved = _with_env(None)
    try:
        reason = asyncio.run(prop_bot.describe_symbol_exclusion_reason("SH"))
        assert "retired" in reason and ENV in reason and "exits are unaffected" in reason
    finally:
        _restore(saved)


def test_exits_still_resolve_the_four_in_futures():
    """Removing them from FUTURES would KeyError an exit on a held
    position (FUTURES[contract]["symbol"] is read on the close path).
    They stay; only entries are refused."""
    for code in ("SH", "DOG", "RWM"):
        assert code in prop_bot.FUTURES and prop_bot.FUTURES[code]["symbol"] == code


def test_the_mandate_still_lists_them_so_history_is_not_rewritten():
    import bot_mandates
    assert bot_mandates.APEX_MANDATE["universe"]["inverse_etfs"] == ["SH", "PSQ", "DOG", "RWM"]


def test_only_the_excluded_set_reads_the_retired_list():
    """The retired list has exactly three readers in prop_bot: its own
    definition, the effective-excluded union, and the reason text. No
    exit function reads it."""
    src = open(os.path.join(HERE, "prop_bot.py")).read()
    assert src.count("_retired_symbols_from_env(") == 3


if __name__ == "__main__":
    names = [n for n in sorted(globals()) if n.startswith("test_")]
    failed = 0
    for n in names:
        try:
            globals()[n]()
            print("ok   ", n)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL ", n, "-", type(e).__name__, e)
    print(f"{len(names) - failed}/{len(names)} passed")
    sys.exit(1 if failed else 0)
