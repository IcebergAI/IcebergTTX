from collections.abc import Generator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=not _is_sqlite,
)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_sqlite_columns()


def _ensure_sqlite_columns() -> None:
    """Add nullable columns for existing local SQLite databases.

    The app intentionally does not have Alembic yet; create_all() creates fresh
    schemas but does not alter old tables. These nullable columns are safe to
    add in place and keep local preview databases usable during development.
    """
    if engine.dialect.name != "sqlite":
        return

    columns = {
        "exercisemember": {"group_id": "VARCHAR"},
        "inject": {
            "group_id": "VARCHAR",
            "attachment_filename": "VARCHAR",
            "attachment_content_type": "VARCHAR",
            "attachment_path": "VARCHAR",
            "attachment_size": "INTEGER",
        },
        "response": {"group_id": "VARCHAR"},
        "communication": {"sender_team": "VARCHAR"},
    }
    with engine.begin() as conn:
        for table_name, wanted in columns.items():
            existing = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
            }
            for column_name, column_type in wanted.items():
                if column_name not in existing:
                    conn.execute(
                        text(
                            f"ALTER TABLE {table_name} "
                            f"ADD COLUMN {column_name} {column_type}"
                        )
                    )


def get_session() -> Generator[Session]:
    with Session(engine) as session:
        yield session
