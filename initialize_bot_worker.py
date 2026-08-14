import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from models import Worker
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///empire.db")

async def initialize_bot_worker():
    """Create bot worker record for earnings tracking"""
    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as session:
        # Check if bot worker already exists
        from sqlalchemy import select
        result = await session.execute(
            select(Worker).where(Worker.email == "bot@pgusa.local")
        )
        existing = result.scalar_one_or_none()
        
        if existing:
            print(f"✓ Bot worker already exists: {existing.id}")
            return
        
        # Create bot worker
        bot_worker = Worker(
            name="Empire Bot",
            email="bot@pgusa.local",
            role="bot",
            status="active"
        )
        session.add(bot_worker)
        await session.commit()
        
        print(f"✓ Bot worker created: {bot_worker.id}")
        print(f"  Name: {bot_worker.name}")
        print(f"  Email: {bot_worker.email}")
    
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(initialize_bot_worker())
