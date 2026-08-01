"""Install the procrastinate task-queue schema (#213).

The DDL is procrastinate's own, read from the installed package rather than copied here,
so upgrading the library is a new revision wrapping its migration script instead of a
hand-merged diff of ours.

Revision ID: c7d8e9f0a1b2
Revises: b3c4d5e6f7a8
"""

import inspect
from collections.abc import Sequence

from sqlalchemy.util import await_only

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _run_script(sql: str) -> None:
    """Execute a multi-statement script on the migration's own connection.

    SQLAlchemy's asyncpg adapter routes every execute through a prepared statement, and
    Postgres refuses to prepare more than one command at a time — so ``exec_driver_sql``
    cannot run this. asyncpg's own ``execute`` uses the simple query protocol, which
    can; reaching it directly also keeps the DDL inside the migration's transaction, so
    a failure rolls back the schema and the version stamp together.
    """
    connection = op.get_bind()
    raw = connection.connection.driver_connection
    if inspect.iscoroutinefunction(getattr(raw, "execute", None)):
        await_only(raw.execute(sql))
    else:  # a synchronous driver (psycopg) needs none of the above
        connection.exec_driver_sql(sql)


def upgrade() -> None:
    from procrastinate.schema import SchemaManager

    _run_script(SchemaManager.get_schema())


def downgrade() -> None:
    _run_script(
        """
        DROP TABLE IF EXISTS procrastinate_events CASCADE;
        DROP TABLE IF EXISTS procrastinate_periodic_defers CASCADE;
        DROP TABLE IF EXISTS procrastinate_jobs CASCADE;
        DROP TABLE IF EXISTS procrastinate_workers CASCADE;
        DROP TYPE IF EXISTS procrastinate_job_status CASCADE;
        DROP TYPE IF EXISTS procrastinate_job_event_type CASCADE;
        DROP TYPE IF EXISTS procrastinate_job_to_defer_v1 CASCADE;
        """
    )
