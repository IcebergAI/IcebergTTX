"""Alembic migration environment (async / asyncpg) — #19.

The URL and target metadata come from the application itself so migrations can
never drift from the models or the runtime connection settings. All model
modules are imported for their side effect of registering tables on
``SQLModel.metadata`` (the same import list as ``app.main``).
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from alembic import context
from app.config import settings
from app.database import make_async_url
from app.models import (  # noqa: F401
    assessment,
    audit,
    communication,
    exercise,
    inject,
    inject_comment,
    report_summary,
    response,
    scenario,
    suggested_inject,
    user,
)

config = context.config
config.set_main_option("sqlalchemy.url", make_async_url(settings.database_url))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
