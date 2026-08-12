"""Embed retained legacy attachment bytes in immutable run snapshots (#315).

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
"""

import base64
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_SNAPSHOT_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _capture_attachment(
    path_value: str | None, expected_size: int | None
) -> tuple[str | None, str | None, str]:
    """Capture bytes only from the managed attachment root.

    Old runs may refer to files that have already been removed.  Such a reference
    is marked explicitly so cloning cannot silently use a mutable replacement.
    """
    if not path_value:
        return None, None, "not_present"
    root = Path("uploads/inject_attachments").resolve()
    candidate = Path(path_value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, None, "unavailable"
    try:
        if not candidate.is_file():
            return None, None, "unavailable"
        size = candidate.stat().st_size
        if size > MAX_SNAPSHOT_ATTACHMENT_BYTES:
            return None, None, "oversized"
        raw = candidate.read_bytes()
    except OSError:
        return None, None, "unavailable"
    if expected_size is not None and len(raw) != expected_size:
        return None, None, "invalid"
    return base64.b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest(), "captured"


def _snapshot_digest(
    *, definition: object, configuration: dict[str, object], schema_version: int
) -> str:
    payload = json.dumps(
        {
            "configuration": configuration,
            "definition": definition,
            "schema_version": schema_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, exercise_id, definition, configuration_json, schema_version "
            "FROM exerciserunsnapshot ORDER BY id"
        )
    ).mappings()
    for row in rows:
        try:
            definition = json.loads(row["definition"])
            configuration = json.loads(row["configuration_json"])
        except (TypeError, ValueError):
            # A malformed historical snapshot is already non-cloneable; leave it
            # untouched so the migration remains resumable and does not fabricate
            # a different definition.
            continue
        if not isinstance(configuration, dict):
            continue
        injects = configuration.get("injects")
        changed = False
        if isinstance(injects, list):
            for spec in injects:
                if not isinstance(spec, dict):
                    continue
                status = spec.get("attachment_snapshot_status")
                if status in {"captured", "not_present", "unavailable", "oversized", "invalid"}:
                    continue
                encoded, digest, capture_status = _capture_attachment(
                    spec.get("attachment_path"), spec.get("attachment_size")
                )
                spec["attachment_bytes_b64"] = encoded
                spec["attachment_sha256"] = digest
                spec["attachment_snapshot_status"] = capture_status
                changed = True
        if "communications" not in configuration:
            # A legacy row has no trustworthy launch/runtime discriminator.  Even
            # facilitator-created inbound messages may have been injected after
            # launch, so copying current rows would fabricate launch evidence and
            # could disclose runtime content in a new clone.
            configuration["communications"] = None
            configuration["communications_source"] = "unknown_legacy"
            changed = True
        if not changed:
            continue
        configuration_json = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        content_sha256 = _snapshot_digest(
            definition=definition,
            configuration=configuration,
            schema_version=int(row["schema_version"]),
        )
        bind.execute(
            sa.text(
                "UPDATE exerciserunsnapshot "
                "SET configuration_json=:configuration_json, content_sha256=:content_sha256 "
                "WHERE id=:id"
            ),
            {
                "id": row["id"],
                "configuration_json": configuration_json,
                "content_sha256": content_sha256,
            },
        )


def downgrade() -> None:
    # The added JSON fields are intentionally retained: removing them would make
    # historical snapshots less reproducible and reintroduce path-only cloning.
    pass
