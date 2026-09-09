from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings


def create_engine(settings: Settings) -> AsyncEngine:
    kwargs: dict[str, object] = {"echo": settings.db_echo, "pool_pre_ping": True}
    if settings.async_database_url.startswith("sqlite+aiosqlite:///:memory:"):
        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    elif not settings.async_database_url.startswith("sqlite"):
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
        )
        if settings.db_sslmode in {"require", "verify-ca", "verify-full"}:
            kwargs["connect_args"] = {"ssl": settings.db_sslmode}
    return create_async_engine(settings.async_database_url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def default_engine_and_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_engine(get_settings())
    return engine, create_session_factory(engine)
