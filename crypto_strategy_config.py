import os


SUPPORTED_CRYPTO_STRATEGIES = frozenset({
    "btc_compound",
    "family_tree",
    "grid_fleet",
    "multi_pair",
})


def get_crypto_strategy_mode() -> str:
    return os.getenv("CRYPTO_STRATEGY_MODE", "btc_compound").strip().strip('"').strip("'").strip()


def is_grid_fleet_mode() -> bool:
    return get_crypto_strategy_mode() == "grid_fleet"