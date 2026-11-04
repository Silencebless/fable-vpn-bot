from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_size=10, max_overflow=20, pool_pre_ping=True, pool_recycle=1800)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Создаёт таблицы, если их ещё нет."""
    from app.db import models  # noqa: F401  (регистрирует модели в metadata)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
