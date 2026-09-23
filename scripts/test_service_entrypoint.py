"""Regression check for Railway service process selection."""

import os
import pathlib
import sys
from unittest.mock import patch

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from service_entrypoint import get_entrypoint


def main() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert get_entrypoint() == "main.py"

    with patch.dict(os.environ, {"SERVICE_ROLE": "crypto-trading"}, clear=True):
        assert get_entrypoint() == "bot_runner.py"

    with patch.dict(
        os.environ,
        {"SERVICE_ROLE": "web", "CRYPTO_STRATEGY_MODE": "grid_fleet"},
        clear=True,
    ):
        assert get_entrypoint() == "main.py"

    print("PASS: Railway service role selects the expected process")


if __name__ == "__main__":
    main()