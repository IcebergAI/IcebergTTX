from collections.abc import AsyncGenerator

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
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
