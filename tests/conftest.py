"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from bot.storage.db import Database, init_database
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine


def enable_sqlite_fk(engine: AsyncEngine) -> None:
    """Turn on FK enforcement for in-memory SQLite engines used in tests."""

    @event.listens_for(engine.sync_engine, "connect")
    def _enable(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """In-memory DB with schema; always dispose engine to avoid aiosqlite thread leaks."""
    database = await init_database(":memory:")
    try:
        yield database
    finally:
        await database.aclose()
