from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.communication import Communication
from app.models.exercise import Exercise, ExerciseMember, ExerciseState, SnapshotProvenance
from app.models.inject import Inject
from app.models.launch_snapshot import ExerciseLaunchSnapshot
from app.models.scenario import Scenario
from app.schemas.scenario_json import ScenarioDefinition
from app.services.scenario_service import export_definition

SNAPSHOT_SCHEMA_VERSION = "1.0"


def canonical_snapshot_bytes(content: dict[str, Any]) -> bytes:
    return json.dumps(
        content,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def snapshot_digest(content: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_snapshot_bytes(content)).hexdigest()


def _canonical_scenario_definition(definition: ScenarioDefinition) -> dict[str, Any]:
    """Canonicalise set-like scenario fields without changing authored sequence."""
    content = definition.model_dump(mode="json")
    for inject in content.get("injects", []):
        inject["target_teams"] = sorted(inject.get("target_teams") or [])
    return content


def attachment_digest(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


async def lock_configuration_owner(
    session: AsyncSession, exercise: Exercise
) -> tuple[Scenario, Exercise]:
    """Acquire the one canonical configuration lock order: Scenario, then Exercise."""
    scenario = (
        await session.exec(
            select(Scenario)
            .where(Scenario.id == exercise.scenario_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if scenario is None:
        raise HTTPException(status_code=409, detail="Exercise scenario no longer exists")
    locked = (
        await session.exec(
            select(Exercise)
            .where(Exercise.id == exercise.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if locked is None:
        raise HTTPException(status_code=404, detail="Exercise not found")
    if locked.scenario_id != scenario.id:
        raise HTTPException(
            status_code=409,
            detail="Exercise scenario changed while configuration was being locked",
        )
    return scenario, locked


def require_inject_configuration_mutable(exercise: Exercise, inject: Inject) -> None:
    """Keep launch-configured rows equal to an exact snapshot after the boundary.

    Runtime-created injects remain intentionally editable. Legacy runs have unknown
    provenance and retain their pre-feature behaviour; no exactness claim is made for
    them.
    """
    if (
        exercise.launch_provenance == SnapshotProvenance.exact
        and inject.created_during_run is not True
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Launch-configured injects are frozen after launch",
        )


async def _locked_launch_rows(
    session: AsyncSession, exercise_id: int
) -> tuple[list[Inject], list[ExerciseMember], list[Communication]]:
    injects = list(
        (
            await session.exec(
                select(Inject)
                .where(Inject.exercise_id == exercise_id)
                .order_by(col(Inject.id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    members = list(
        (
            await session.exec(
                select(ExerciseMember)
                .where(ExerciseMember.exercise_id == exercise_id)
                .order_by(col(ExerciseMember.id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    communications = list(
        (
            await session.exec(
                select(Communication)
                .where(Communication.exercise_id == exercise_id)
                .order_by(col(Communication.id))
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    return injects, members, communications


async def _attachment_snapshot(inject: Inject) -> dict[str, Any] | None:
    if inject.attachment_path is None:
        return None
    try:
        digest, actual_size = await asyncio.to_thread(attachment_digest, inject.attachment_path)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Inject {inject.id} attachment is unavailable; launch was not started",
        ) from exc
    if inject.attachment_size is not None and actual_size != inject.attachment_size:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Inject {inject.id} attachment size changed; launch was not started",
        )
    inject.attachment_sha256 = digest
    return {
        "filename": inject.attachment_filename,
        "content_type": inject.attachment_content_type,
        "size": actual_size,
        "sha256": digest,
    }


async def build_launch_content(
    session: AsyncSession,
    *,
    scenario: Scenario,
    exercise: Exercise,
    injects: list[Inject],
    members: list[ExerciseMember],
    communications: list[Communication],
) -> dict[str, Any]:
    uncertain_member = next(
        (member for member in members if member.created_during_run is not False), None
    )
    if uncertain_member is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Member {uncertain_member.id} has uncertain draft provenance; "
                "launch was not started"
            ),
        )
    uncertain_communication = next(
        (
            communication
            for communication in communications
            if communication.created_during_run is not False
        ),
        None,
    )
    if uncertain_communication is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Communication {uncertain_communication.id} has uncertain draft provenance; "
                "launch was not started"
            ),
        )

    inject_content = []
    for inject in injects:
        if inject.created_during_run is not False:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Inject {inject.id} has uncertain draft provenance; launch was not started",
            )
        inject_content.append(
            {
                "scenario_node_id": inject.scenario_node_id,
                "title": inject.title,
                "content": inject.content,
                "target_teams": sorted(inject.target_teams or []),
                "group_id": inject.group_id,
                "sequence_order": inject.sequence_order,
                "release_offset_minutes": inject.release_offset_minutes,
                "attachment": await _attachment_snapshot(inject),
            }
        )
    inject_content.sort(
        key=lambda item: (
            item["sequence_order"],
            item["scenario_node_id"] or "",
            item["group_id"] or "",
            canonical_snapshot_bytes(item),
        )
    )

    member_content = [
        {
            "user_id": member.user_id,
            "group_id": member.group_id,
            "role": member.role_at_enrolment.value,
        }
        for member in members
    ]
    member_content.sort(key=lambda item: (item["user_id"], canonical_snapshot_bytes(item)))

    communication_content = [
        {
            "direction": communication.direction.value,
            "sender_id": communication.sender_id,
            "sender_team": communication.sender_team,
            "external_entity": communication.external_entity,
            "subject": communication.subject,
            "body": communication.body,
            "visible_to_teams": sorted(communication.visible_to_teams or []),
            "sent_at": communication.sent_at.isoformat(),
        }
        for communication in communications
    ]
    communication_content.sort(
        key=lambda item: (item["sent_at"], canonical_snapshot_bytes(item))
    )

    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "scenario": {
            "version": scenario.version,
            "definition": _canonical_scenario_definition(export_definition(scenario)),
        },
        "exercise": {
            "title": exercise.title,
            "llm_enabled": exercise.llm_enabled,
        },
        "injects": inject_content,
        "members": member_content,
        "prepared_communications": communication_content,
    }


async def freeze_launch_configuration(
    session: AsyncSession, exercise: Exercise
) -> Exercise:
    """Freeze draft configuration in the same transaction as draft -> active."""
    scenario, locked = await lock_configuration_owner(session, exercise)
    if locked.state != ExerciseState.draft:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a draft exercise can create a launch snapshot",
        )
    if locked.launch_snapshot_digest is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Draft already references a launch snapshot",
        )
    assert locked.id is not None
    injects, members, communications = await _locked_launch_rows(session, locked.id)
    content = await build_launch_content(
        session,
        scenario=scenario,
        exercise=locked,
        injects=injects,
        members=members,
        communications=communications,
    )
    # Attachment digests are part of the static Inject projection. Persist them while
    # the parent is still pending so the database's exact-run mutation guard can then
    # enforce the post-launch boundary for old and new application replicas alike.
    await session.flush()
    digest = snapshot_digest(content)
    await session.exec(
        pg_insert(ExerciseLaunchSnapshot)
        .values(
            digest=digest,
            schema_version=SNAPSHOT_SCHEMA_VERSION,
            content=content,
            # Core INSERTs bypass SQLModel's Python-side default factory.
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["digest"])
    )
    existing = await session.get(ExerciseLaunchSnapshot, digest)
    if existing is not None and existing.content != content:
        raise RuntimeError("launch snapshot digest collision")
    locked.launch_snapshot_digest = digest
    locked.launch_provenance = SnapshotProvenance.exact
    session.add(locked)
    for inject in injects:
        if inject.attachment_sha256 is not None:
            session.add(inject)
    await session.flush()
    return locked


async def launch_definition_for_exercise(
    session: AsyncSession, exercise: Exercise
) -> ScenarioDefinition | None:
    if exercise.launch_snapshot_digest is None:
        return None
    snapshot = await session.get(ExerciseLaunchSnapshot, exercise.launch_snapshot_digest)
    if snapshot is None:
        raise RuntimeError("exercise references a missing launch snapshot")
    definition = snapshot.content.get("scenario", {}).get("definition")
    return ScenarioDefinition.model_validate(definition)
