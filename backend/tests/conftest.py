from __future__ import annotations

import os

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

os.environ.setdefault("APP_DEBUG", "true")
os.environ.setdefault("APP_SECRET_KEY", "pytest-secret-key-with-more-than-thirty-two-characters")

import app.models  # noqa: E402, F401
from app.core.config import Settings
from app.db.base import Base
from app.db.session import create_engine, create_session_factory
from app.main import create_app


@pytest_asyncio.fixture
async def app_bundle():
    settings = Settings(
        debug=True,
        secret_key="test-secret-key-with-more-than-thirty-two-characters",
        database_url="sqlite+aiosqlite:///:memory:",
        allowed_hosts=["testserver"],
        cors_allowed_origins=["http://testserver"],
        session_cookie_secure=False,
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = create_app(settings, engine=engine, session_factory=session_factory)
    yield app, session_factory, settings
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db(app_bundle):
    _, session_factory, _ = app_bundle
    async with session_factory() as session:
        assert isinstance(session, AsyncSession)
        yield session
        await session.rollback()
