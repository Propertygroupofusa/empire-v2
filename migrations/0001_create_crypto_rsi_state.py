"""
Explicit migration to create crypto_rsi_state table if it doesn't exist.

This table is defined in models.py but may not have been created during init_db()
if the async engine wasn't fully initialized. This migration creates it explicitly.
"""
import asyncio
from database import engine
from sqlalchemy import text, inspect

async def migrate():
    """Create crypto_rsi_state table if it doesn't exist."""

    async with engine.begin() as conn:
        # Check if table already exists
        inspector = await conn.run_sync(inspect)
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())

        if "crypto_rsi_state" in tables:
            print("✅ crypto_rsi_state table already exists")
            return True

        # Create the table
        sql = """
        CREATE TABLE crypto_rsi_state (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR UNIQUE NOT NULL,
            entered_oversold BOOLEAN DEFAULT FALSE,
            armed_rsi FLOAT,
            last_rsi FLOAT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX idx_crypto_rsi_state_symbol ON crypto_rsi_state(symbol);
        CREATE INDEX idx_crypto_rsi_state_updated_at ON crypto_rsi_state(updated_at);
        """

        try:
            await conn.execute(text(sql))
            await conn.commit()
            print("✅ crypto_rsi_state table created successfully")
            return True
        except Exception as e:
            print(f"❌ Failed to create crypto_rsi_state table: {e}")
            return False

if __name__ == "__main__":
    result = asyncio.run(migrate())
    exit(0 if result else 1)
