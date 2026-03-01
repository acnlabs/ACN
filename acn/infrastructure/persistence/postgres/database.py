"""PostgreSQL async engine and session factory.

Usage:
    from .database import get_engine, get_session_factory

    engine = get_engine(database_url)
    async_session = get_session_factory(engine)

    async with async_session() as session:
        ...
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def get_engine(database_url: str) -> AsyncEngine:
    """Create an async SQLAlchemy engine from DATABASE_URL.

    Accepts both postgres:// and postgresql+asyncpg:// URL formats.
    """
    # Normalise Railway/Heroku postgres:// → postgresql+asyncpg://
    url = database_url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return create_async_engine(
        url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        echo=False,
    )


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to the given engine."""
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
