from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession

from jev_invoice.config import Settings
from jev_invoice.db.seed import seed

ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["configure_logger"] = False
    return config


def test_migrations_upgrade_downgrade_and_match_models(tmp_path: Path) -> None:
    config = _alembic_config(f"sqlite+aiosqlite:///{(tmp_path / 'migrated.db').as_posix()}")

    command.upgrade(config, "head")
    command.check(config)  # raises if the models and the migrations disagree
    command.downgrade(config, "base")
    command.upgrade(config, "head")


async def test_seed_is_idempotent(session: AsyncSession, settings: Settings) -> None:
    # The session fixture already seeded once.
    counts = await seed(session, settings)
    again = await seed(session, settings)

    assert counts == again
    assert counts["vendors"] == 11
    assert counts["purchase_orders"] == 9
