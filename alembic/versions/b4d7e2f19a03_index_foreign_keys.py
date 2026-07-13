"""index the foreign keys the read paths filter on

Revision ID: b4d7e2f19a03
Revises: f8a9b0c1d2e3
Create Date: 2026-07-13 00:00:00.000000

PostgreSQL indexes the column a foreign key *references* (the PK), never the
referencing column, so every `where(X.exercise_id == ...)` — the shape of nearly
every read path in the app — was a sequential scan whose cost grew with the total
rows across every exercise ever run, not with the exercise being viewed.

Scope rule: index a foreign key only where it is not already covered *and* is
either filtered by a real query or on the cascade path of a routinely-deleted
parent (Exercise, Inject). Two categories are deliberately left unindexed:

  - Columns already served by the leading column of an existing index or unique
    constraint: response.exercise_id (uq_response_exercise_inject_user),
    communication.exercise_id (uq_communication_exercise_trigger_key and the
    composite added here), exerciseprogress.exercise_id, injectprogress.inject_id,
    exercisestatetransition.exercise_id, and all of communicationread (composite
    PK + ix_communicationread_user_communication).
  - The actor columns — *.created_by, *.released_by, *.resolved_by, *.reviewed_by,
    *.advanced_by, exercisestatetransition.actor_id, communication.sender_id,
    communication.triggered_by_inject_id, exerciseprogress.current_inject_id,
    injectcomment.user_id, response.user_id. No query filters on any of them and
    their parent User rows are not deleted in bulk, so an index would be pure
    write overhead.

Plain CREATE INDEX, not CONCURRENTLY: startup migrations run inside a transaction
(database.run_migrations), which CONCURRENTLY cannot. The brief lock is free
because this lands before any deployment carries real data.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b4d7e2f19a03"
down_revision: str | None = "f8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_inject_exercise_id", "inject", ["exercise_id"])
    op.create_index("ix_injectprogress_exercise_id", "injectprogress", ["exercise_id"])
    op.create_index("ix_response_inject_id", "response", ["inject_id"])
    op.create_index("ix_injectcomment_inject_id", "injectcomment", ["inject_id"])
    op.create_index("ix_injectcomment_exercise_id", "injectcomment", ["exercise_id"])
    op.create_index("ix_suggestedinject_exercise_id", "suggestedinject", ["exercise_id"])
    op.create_index("ix_exercise_scenario_id", "exercise", ["scenario_id"])

    # The pair is the hottest lookup in the app (exercise_group_for_user, sender-team
    # resolution); its leading column also serves the exercise_id-only roster scan.
    op.create_index(
        "ix_exercisemember_exercise_user",
        "exercisemember",
        ["exercise_id", "user_id"],
    )
    op.create_index("ix_exercisemember_user_id", "exercisemember", ["user_id"])

    # The inbox filters on exercise_id and orders by sent_at, so one index serves the
    # whole query. Ascending: btree scans backwards to satisfy the DESC ordering.
    op.create_index(
        "ix_communication_exercise_sent_at",
        "communication",
        ["exercise_id", "sent_at"],
    )

    op.create_index("ix_authtoken_user_id", "authtoken", ["user_id"])
    op.create_index("ix_authtoken_exercise_id", "authtoken", ["exercise_id"])


def downgrade() -> None:
    op.drop_index("ix_authtoken_exercise_id", table_name="authtoken")
    op.drop_index("ix_authtoken_user_id", table_name="authtoken")
    op.drop_index("ix_communication_exercise_sent_at", table_name="communication")
    op.drop_index("ix_exercisemember_user_id", table_name="exercisemember")
    op.drop_index("ix_exercisemember_exercise_user", table_name="exercisemember")
    op.drop_index("ix_exercise_scenario_id", table_name="exercise")
    op.drop_index("ix_suggestedinject_exercise_id", table_name="suggestedinject")
    op.drop_index("ix_injectcomment_exercise_id", table_name="injectcomment")
    op.drop_index("ix_injectcomment_inject_id", table_name="injectcomment")
    op.drop_index("ix_response_inject_id", table_name="response")
    op.drop_index("ix_injectprogress_exercise_id", table_name="injectprogress")
    op.drop_index("ix_inject_exercise_id", table_name="inject")
