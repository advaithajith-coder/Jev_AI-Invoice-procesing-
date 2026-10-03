"""Async engine and session factory. Creating them does not open a connection."""

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jev_invoice.config import Settings


def ensure_sqlite_directory(database_url: str) -> None:
    """SQLite creates the file but not its folder; create the folder."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def create_engine(settings: Settings) -> AsyncEngine:
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite":
        ensure_sqlite_directory(settings.database_url)
        engine = create_async_engine(url, connect_args={"timeout": 30})
        event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
        return engine
    return create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    # SQLite ignores foreign keys unless asked; WAL lets readers work during writes.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
