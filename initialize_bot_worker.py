import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from models import Worker
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///empire.db")

async def initialize_bot_worker():
    """Create bot worker record for earnings tracking (non-blocking with timeout)"""
    try:
        engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            pool_pre_ping=True,
            connect_args={"timeout": 5}  # 5 second connection timeout
        )
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Wrap in timeout to prevent startup hang
        async def _init():
            async with async_session() as session:
                from sqlalchemy import select
                result = await session.execute(
                    select(Worker).where(Worker.email == "bot@pgusa.local")
                )
                existing = result.scalar_one_or_none()

                if existing:
                    print(f"✓ Bot worker already exists: {existing.id}")
                    return

                bot_worker = Worker(
                    name="Empire Bot",
                    email="bot@pgusa.local",
                    role="bot",
                    status="active"
                )
                session.add(bot_worker)
                await session.commit()
                print(f"✓ Bot worker created: {bot_worker.id}")

            await engine.dispose()

        # 10 second timeout for entire initialization
        await asyncio.wait_for(_init(), timeout=10.0)
    except asyncio.TimeoutError:
        print("⚠️ Bot worker initialization timeout - skipping (will retry later)")
    except Exception as e:
        print(f"⚠️ Bot worker initialization failed: {e} - skipping (will retry later)")

if __name__ == "__main__":
    asyncio.run(initialize_bot_worker())
