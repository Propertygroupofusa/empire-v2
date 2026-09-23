"""Select the process owned by this Railway service."""

import os
import sys


def get_entrypoint() -> str:
    if os.getenv("SERVICE_ROLE", "").strip().lower() == "crypto-trading":
        return "bot_runner.py"
    return "main.py"


def main() -> None:
    entrypoint = get_entrypoint()
    os.execv(sys.executable, [sys.executable, entrypoint])


if __name__ == "__main__":
    main()