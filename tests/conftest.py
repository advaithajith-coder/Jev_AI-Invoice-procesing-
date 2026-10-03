import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jev_invoice.config import Settings
from jev_invoice.db.base import Base
from jev_invoice.db.seed import seed
from jev_invoice.db.session import create_engine, create_session_factory
from jev_invoice.main import create_app

# Live tests read real keys from .env; ordinary tests must never reach real APIs.
for _name in ("GROQ_API_KEY", "TYPESAFE_API_KEY"):
    os.environ.pop(_name, None)


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "log_json": False,
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        "storage_dir": tmp_path / "files",
        "groq_api_key": None,
        "typesafe_api_key": None,
        "redis_url": None,
        "approval_limit": Decimal("5000.00"),
        "background_processing": False,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


async def create_schema(settings: Settings, *, with_seed: bool = True) -> None:
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        if with_seed:
            async with create_session_factory(engine)() as session, session.begin():
                await seed(session, settings)
    finally:
        await engine.dispose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def seeded_settings(settings: Settings) -> Settings:
    asyncio.run(create_schema(settings))
    return settings


@pytest.fixture
async def session(settings: Settings) -> AsyncIterator[AsyncSession]:
    """A session on a fresh, seeded database."""
    await create_schema(settings)
    engine = create_engine(settings)
    factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture
def app(seeded_settings: Settings) -> FastAPI:
    return create_app(seeded_settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
