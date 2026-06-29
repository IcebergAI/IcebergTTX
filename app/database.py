import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings


def make_async_url(url: str) -> str:
    """Normalise a database URL to use an async driver.

    Existing deployments set ``DATABASE_URL`` with the sync ``postgresql://``
    (or ``postgres://``) scheme. Rewrite it to ``postgresql+asyncpg://`` so the
    secrets/manifests do not need to change. URLs that already name an async
    driver are returned unchanged.
    """
    if url.startswith("postgresql+") or url.startswith("postgres+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


engine = create_async_engine(make_async_url(settings.database_url), pool_pre_ping=True)


async def create_db_and_tables() -> None:
    """Create tables directly from the models (used by the test suite).

    Production schema management goes through Alembic (see ``run_migrations``);
    this remains for the test harness, which creates a throwaway schema per run.
    """
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


def _upgrade_to_head() -> None:
    from alembic.config import Config

    from alembic import command

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    # Pin the script location absolutely so migrations resolve regardless of cwd.
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, "head")


async def run_migrations() -> None:
    """Bring the database schema up to head via Alembic (#19).

    Run in a worker thread because Alembic's async ``env.py`` calls
    ``asyncio.run``, which cannot be invoked from the already-running lifespan
    event loop. Safe for the single-replica deployment; multi-replica rollouts
    should instead run ``alembic upgrade head`` as a dedicated deploy step.
    """
    await asyncio.to_thread(_upgrade_to_head)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
