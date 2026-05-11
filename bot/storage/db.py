"""Async SQLite engine + session factory. Schema is created/migrated via alembic."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from bot.storage.models import Base


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, expire_on_commit=False, class_=AsyncSession
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield an AsyncSession; commit on clean exit, rollback on error.

        Tests that intentionally trigger IntegrityError on commit should use `raw_session()`
        instead so the auto-commit doesn't re-raise during cleanup.
        """
        async with self._session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()

    @asynccontextmanager
    async def raw_session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session with no automatic commit/rollback. Caller controls the flow."""
        async with self._session_factory() as session:
            yield session

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def aclose(self) -> None:
        await self._engine.dispose()


def _normalize_url(url: str | Path) -> str:
    if isinstance(url, Path):
        return f"sqlite+aiosqlite:///{url.as_posix()}"
    s = str(url)
    if s == ":memory:":
        return "sqlite+aiosqlite:///:memory:"
    if s.startswith("sqlite") and "aiosqlite" not in s:
        return s.replace("sqlite:", "sqlite+aiosqlite:", 1)
    return s


def get_database(url: str | Path, *, echo: bool = False) -> Database:
    """Construct a Database from a SQLite URL (or path).

    Uses an in-memory shared StaticPool when url == ':memory:' or '...:memory:'.
    """
    normalized = _normalize_url(url)
    if ":memory:" in normalized:
        engine = create_async_engine(
            normalized,
            echo=echo,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_async_engine(normalized, echo=echo, future=True)
    return Database(engine)


async def init_database(url: str | Path, *, echo: bool = False) -> Database:
    """Convenience: construct + create_all. Used for tests and the first dry-run boot."""
    db = get_database(url, echo=echo)
    await db.create_all()
    return db


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)
