#!/usr/bin/env python3
"""
Force creation of missing tables in production database.
Run this when init_db() fails to create tables on startup.
"""
import asyncio
import sys
from database import engine, Base
from models import *  # noqa: F401,F403 - imports register models on Base.metadata

async def main():
    print("[DB] Forcing table creation for all models...")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("[DB] ✅ All tables created successfully")
        return 0
    except Exception as e:
        print(f"[DB] ❌ Table creation failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await engine.dispose()

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
