"""The rate limiter's storage: one row per counted attempt (#11, #67, #117, #213).

Declared as a plain Core table rather than a SQLModel entity because nothing ever loads
it as an object — every access is an aggregate or a bulk delete, and a throttle counter
has no identity worth mapping. It is attached to ``SQLModel.metadata`` regardless, so the
test harness (which builds its schema from the models, never from Alembic) gets it for
free.

**UNLOGGED** is deliberate. These rows are expendable: they are not written to the WAL,
they are not replicated, and Postgres truncates the table after an unclean shutdown. The
worst case is that a crash forgives some in-progress lockouts, which is a fair price for
keeping a per-login-attempt write off the WAL. The corresponding Alembic revision spells
the keyword out; ``create_all`` honours the ``prefixes`` below.
"""

from sqlalchemy import Column, DateTime, Index, Integer, String, Table
from sqlmodel import SQLModel

rate_limit_hits = Table(
    "rate_limit_hits",
    SQLModel.metadata,
    Column("id", Integer, primary_key=True),
    # The limiter this row belongs to ("login", "registration", "password_reset"), so
    # the three share a table without being able to count each other's attempts.
    Column("scope", String(32), nullable=False),
    # Opaque and caller-defined: an IP, or "ip:email" for login. Attacker-controlled, so
    # it is only ever a bind parameter and never interpolated.
    Column("key", String(320), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
    Index("ix_rate_limit_hits_scope_key_at", "scope", "key", "at"),
    prefixes=["UNLOGGED"],
)
