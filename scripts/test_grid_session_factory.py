"""Regression check for Grid's lazy database session initialization."""

import asyncio
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main():
    with tempfile.TemporaryDirectory() as temp_dir:
        database_path = pathlib.Path(temp_dir) / "grid-session.db"
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path.as_posix()}"

        import database
        import crypto_grid_bot

        assert database.AsyncSessionLocal is None

        async def check():
            await database.init_db()
            history = await crypto_grid_bot.get_grid_trade_history(limit_recent=1)
            branches = await crypto_grid_bot.get_grid_branches()

            assert history["total_trade_count"] == 0
            assert branches == []

        asyncio.run(check())

    print("PASS: Grid resolves the session factory after lazy database initialization")


if __name__ == "__main__":
    main()
