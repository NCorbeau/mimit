"""Explicit engine lifecycle and transaction ownership."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    """Create a lazy connection pool. The caller must eventually dispose it."""
    return create_async_engine(database_url, pool_pre_ping=True)


def get_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessions never implicitly commit; callers own commit/rollback boundaries."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
