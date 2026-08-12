"""The Alembic chain actually applies, on a real, empty database.

The rest of the suite builds its schema with ``metadata.create_all`` and never runs
Alembic, so nothing else here would notice a migration that cannot run — and a broken
one only shows up on a fresh production database, which is the worst place to find out.

Each test provisions a throwaway database on the same server the test container already
provides, so this costs a `CREATE DATABASE` rather than a container.
"""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.database import make_async_url, make_asyncpg_dsn

ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_migration_captures_available_legacy_attachment(tmp_path, monkeypatch):
    """Backfilled snapshots embed bytes while explicitly reporting old-file gaps."""
    import base64
    import importlib.util

    migration_path = ROOT / "alembic/versions/a9c4e7f1b2d6_add_exercise_run_snapshots.py"
    module_spec = importlib.util.spec_from_file_location("snapshot_migration", migration_path)
    assert module_spec is not None and module_spec.loader is not None
    migration = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(migration)
    capture_attachment = migration._capture_attachment

    monkeypatch.chdir(tmp_path)
    attachment = tmp_path / "uploads" / "inject_attachments" / "7" / "brief.txt"
    attachment.parent.mkdir(parents=True)
    raw = b"legacy evidence"
    attachment.write_bytes(raw)

    encoded, digest, state = capture_attachment(str(attachment), len(raw))

    assert base64.b64decode(encoded or "", validate=True) == raw
    assert digest is not None
    assert state == "captured"
    assert capture_attachment(str(attachment.with_name("missing.txt")), len(raw))[2] == (
        "unavailable"
    )


def _dsn(database: str | None = None) -> str:
    dsn = make_asyncpg_dsn(os.environ["DATABASE_URL"])
    if database is None:
        return dsn
    base, _, _ = dsn.rpartition("/")
    return f"{base}/{database}"


@pytest.fixture
async def empty_database():
    """A fresh database, dropped afterwards."""
    name = f"migration_check_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(_dsn())
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    try:
        yield name
    finally:
        admin = await asyncpg.connect(_dsn())
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


def _run_alembic(command_name: str, database: str) -> None:
    """Run an Alembic command against ``database`` in a worker thread.

    Alembic's async ``env.py`` calls ``asyncio.run``, which cannot be invoked from a
    running loop — the same reason ``run_migrations`` uses ``asyncio.to_thread``. The URL
    is set on the config rather than the environment: ``settings`` is bound at import and
    would otherwise send these migrations at the suite's own database.
    """
    from alembic.config import Config

    from alembic import command

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    # The async driver explicitly: env.py only normalises the URL it derives itself.
    config.set_main_option("sqlalchemy.url", make_async_url(_dsn(database)))
    if command_name == "upgrade":
        command.upgrade(config, "head")
    else:
        getattr(command, command_name)(config)


async def test_the_chain_applies_to_an_empty_database(empty_database):
    """Every revision, in order, on nothing — what a first deploy does."""
    await asyncio.to_thread(_run_alembic, "upgrade", empty_database)

    connection = await asyncpg.connect(_dsn(empty_database))
    try:
        tables = {
            row["tablename"]
            for row in await connection.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
    finally:
        await connection.close()

    # A domain table, the queue's, and the limiter's: one from each of the three
    # sources that now contribute schema.
    assert "exercise" in tables
    assert "exerciserunsnapshot" in tables
    assert "procrastinate_jobs" in tables
    assert "rate_limit_hits" in tables


async def test_the_migrated_schema_matches_the_models(empty_database):
    """``alembic check`` is the only drift detector this project has, since the test
    suite builds its schema from the models rather than from migrations.

    It also pins the ``include_name`` filter: procrastinate's tables are installed
    verbatim by their own revision and are deliberately absent from SQLModel's metadata,
    so without the filter this would report them as removals on a correct database.
    """
    await asyncio.to_thread(_run_alembic, "upgrade", empty_database)

    await asyncio.to_thread(_run_alembic, "check", empty_database)


async def test_the_rate_limit_table_is_unlogged(empty_database):
    """UNLOGGED is the reason a write per login attempt is acceptable — these rows stay
    out of the WAL. An autogenerated replacement would silently make it a normal table."""
    await asyncio.to_thread(_run_alembic, "upgrade", empty_database)

    connection = await asyncpg.connect(_dsn(empty_database))
    try:
        persistence = await connection.fetchval(
            "SELECT relpersistence FROM pg_class WHERE relname = 'rate_limit_hits'"
        )
    finally:
        await connection.close()

    # pg_class.relpersistence is Postgres's internal "char" type, so asyncpg hands it
    # back as bytes rather than str.
    assert persistence == b"u"
