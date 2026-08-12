import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import (
    VALID_TRANSITIONS,
    Exercise,
    ExerciseMember,
    ExerciseRunSnapshot,
    ExerciseState,
    ExerciseStateTransition,
    transition_action,
)
from app.models.inject import Inject
from app.models.scenario import Scenario
from app.models.user import User
from app.services.domain_events import ExerciseStateChanged, dispatch, record
from app.services.inject_service import (
    copy_attachment_for_exercise,
    promote_legacy_inject_provenance,
    restore_snapshot_attachment,
    seed_injects_from_scenario,
)
from app.services.progression_service import seed_progression
from app.services.scenario_service import (
    RUN_SNAPSHOT_SCHEMA_VERSION,
    canonical_definition_json,
    definition_for_exercise,
    export_definition,
    snapshot_content_sha256,
    snapshot_for_exercise,
)
from app.services.team_service import scenario_team_ids as definition_team_ids
from app.services.team_service import validate_team_ids as validate_definition_team_ids


@dataclass(frozen=True)
class ExerciseTransitionResult:
    exercise: Exercise
    transition: ExerciseStateTransition
    action: str


async def _freeze_run_snapshot(session: AsyncSession, exercise: Exercise) -> ExerciseRunSnapshot:
    """Capture the one immutable interpretation record for a draft being launched.

    The Scenario row is locked while its validated definition is canonicalized. This
    is deliberately in the lifecycle transaction: callers observe either both the
    active state and snapshot or neither, including under concurrent start/edit
    requests and across replicas.
    """
    assert exercise.id is not None
    existing = (
        await session.exec(
            select(ExerciseRunSnapshot).where(ExerciseRunSnapshot.exercise_id == exercise.id)
        )
    ).first()
    if existing is not None:
        return existing
    # Lock in the same Scenario -> Exercise order used by scenario editing and
    # creation. A launch/edit race therefore serializes instead of deadlocking.
    scenario = (
        await session.exec(
            select(Scenario).where(Scenario.id == exercise.scenario_id).with_for_update()
        )
    ).first()
    if scenario is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Exercise scenario was not found",
        )
    # Re-read after the state CAS while holding the same row lock. A concurrent
    # draft-settings update either committed before this point (and is captured)
    # or waits until launch completes; no mixed configuration can be recorded.
    locked_exercise = (
        await session.exec(
            select(Exercise)
            .where(Exercise.id == exercise.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()
    assert locked_exercise is not None
    assert scenario.id is not None
    definition = export_definition(scenario)
    definition_json = canonical_definition_json(definition)
    # Schedule overrides are materialized on Inject rows and are part of the
    # runnable launch configuration, not merely the reusable Scenario template.
    # Lock them under the already-locked Exercise so the snapshot cannot omit a
    # committed override that the scheduler will use.
    schedules = (
        await session.exec(
            select(Inject)
            .where(Inject.exercise_id == exercise.id)
            .with_for_update()
            .order_by(col(Inject.id))
        )
    ).all()
    materialized_injects: list[dict[str, object]] = []
    for inject in schedules:
        attachment_bytes_b64 = None
        attachment_sha256 = None
        if inject.attachment_path:
            attachment_path = Path(inject.attachment_path)
            if not attachment_path.is_file():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="An inject attachment is missing; repair it before launch",
                )
            if attachment_path.stat().st_size > 25 * 1024 * 1024:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="An inject attachment is too large to capture in the run snapshot",
                )
            raw = attachment_path.read_bytes()
            attachment_bytes_b64 = base64.b64encode(raw).decode("ascii")
            attachment_sha256 = hashlib.sha256(raw).hexdigest()
        materialized_injects.append(
            {
                "id": inject.id,
                "scenario_node_id": inject.scenario_node_id,
                "scenario_seeded": inject.scenario_seeded,
                "title": inject.title,
                "content": inject.content,
                "target_teams": inject.target_teams,
                "group_id": inject.group_id,
                "sequence_order": inject.sequence_order,
                "release_offset_minutes": inject.release_offset_minutes,
                "release_offset_explicit": inject.release_offset_explicit,
                "state": str(inject.state),
                "released_at": inject.released_at.isoformat() if inject.released_at else None,
                "released_by": inject.released_by,
                "resolved_at": inject.resolved_at.isoformat() if inject.resolved_at else None,
                "resolved_by": inject.resolved_by,
                "resolution_reason": inject.resolution_reason,
                "attachment_filename": inject.attachment_filename,
                "attachment_content_type": inject.attachment_content_type,
                "attachment_path": inject.attachment_path,
                "attachment_size": inject.attachment_size,
                "attachment_bytes_b64": attachment_bytes_b64,
                "attachment_sha256": attachment_sha256,
            }
        )
    configuration: dict[str, object] = {
        "llm_enabled": locked_exercise.llm_enabled,
        "scenario_id": scenario.id,
        "scenario_version": scenario.version,
        # The reusable scenario definition does not contain facilitator-authored
        # draft injects. Capture every materialized row (including custom content,
        # audience, ordering, attachment metadata, and lifecycle state) so the
        # launch record is independently reproducible and content-addressed.
        "injects": materialized_injects,
        "inject_schedules": [
            {
                "inject_id": inject.id,
                "scenario_node_id": inject.scenario_node_id,
                "group_id": inject.group_id,
                "release_offset_minutes": inject.release_offset_minutes,
                "release_offset_explicit": inject.release_offset_explicit,
            }
            for inject in schedules
        ],
    }
    snapshot = ExerciseRunSnapshot(
        exercise_id=exercise.id,
        scenario_id=scenario.id,
        scenario_version=scenario.version,
        scenario_title=scenario.title,
        schema_version=RUN_SNAPSHOT_SCHEMA_VERSION,
        definition=definition_json,
        configuration_json=json.dumps(configuration, sort_keys=True, separators=(",", ":")),
        content_sha256=snapshot_content_sha256(
            definition_json=definition_json,
            configuration=configuration,
            schema_version=RUN_SNAPSHOT_SCHEMA_VERSION,
        ),
    )
    session.add(snapshot)
    return snapshot


async def create_exercise(
    session: AsyncSession,
    *,
    scenario_id: int,
    title: str,
    created_by: int,
    llm_enabled: bool = False,
) -> Exercise:
    scenario = (
        await session.exec(
            select(Scenario)
            .where(Scenario.id == scenario_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()
    if not scenario:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scenario not found")

    definition = export_definition(scenario)
    exercise = Exercise(
        scenario_id=scenario_id,
        title=title,
        created_by=created_by,
        llm_enabled=llm_enabled,
        current_node_id=definition.start_inject_id,
    )
    session.add(exercise)
    await session.flush()
    assert exercise.id is not None

    try:
        await seed_injects_from_scenario(session, exercise.id, scenario)
        await seed_progression(
            session,
            exercise_id=exercise.id,
            start_node_id=definition.start_inject_id,
            group_ids=[team.id for team in definition.participant_teams],
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await session.refresh(exercise)
    return exercise


async def clone_exercise_as_draft(
    session: AsyncSession,
    *,
    source: Exercise,
    created_by: int,
    title: str | None = None,
) -> Exercise:
    """Create a distinct draft from a run's frozen definition and preserve lineage."""
    assert source.id is not None
    definition = await definition_for_exercise(session, source.id)
    if definition is None:
        raise HTTPException(status_code=422, detail="Exercise scenario was not found")
    source_scenario = await session.get(Scenario, source.scenario_id)
    if source_scenario is None:
        raise HTTPException(status_code=422, detail="Exercise scenario was not found")
    # A clone receives a new reusable Scenario row rather than attaching a draft
    # directly to historical source content. It can be tailored for the next run
    # while the source's captured evidence remains intact.
    clone_scenario = Scenario(
        title=f"{definition.title} (copy)",
        description=definition.description,
        version=source_scenario.version,
        tags=definition.tags or None,
        definition=canonical_definition_json(definition),
        created_by=created_by,
    )
    session.add(clone_scenario)
    await session.flush()
    assert clone_scenario.id is not None
    clone = Exercise(
        scenario_id=clone_scenario.id,
        title=title or f"{source.title} (copy)",
        created_by=created_by,
        llm_enabled=source.llm_enabled,
        current_node_id=definition.start_inject_id,
        cloned_from_exercise_id=source.id,
    )
    session.add(clone)
    await session.flush()
    assert clone.id is not None
    copied_attachment_paths: list[str] = []
    try:
        # A frozen run contains the materialized launch configuration, not just
        # the reusable scenario definition. Restore authored injects and explicit
        # schedule overrides so cloning a run is a faithful, editable draft.
        snapshot = await snapshot_for_exercise(session, source.id)
        snapshot_configuration = (
            json.loads(snapshot.configuration_json) if snapshot is not None else {}
        )
        snapshot_injects = snapshot_configuration.get("injects", [])
        if not isinstance(snapshot_injects, list):
            snapshot_injects = []
        source_injects = (
            await session.exec(
                select(Inject).where(Inject.exercise_id == source.id).order_by(col(Inject.id))
            )
        ).all()
        if not snapshot_injects:
            snapshot_injects = [
                {
                    "scenario_node_id": inject.scenario_node_id,
                    "scenario_seeded": inject.scenario_seeded,
                    "title": inject.title,
                    "content": inject.content,
                    "target_teams": inject.target_teams,
                    "group_id": inject.group_id,
                    "sequence_order": inject.sequence_order,
                    "release_offset_minutes": inject.release_offset_minutes,
                    "release_offset_explicit": inject.release_offset_explicit,
                    "attachment_filename": inject.attachment_filename,
                    "attachment_content_type": inject.attachment_content_type,
                    "attachment_path": inject.attachment_path,
                    "attachment_size": inject.attachment_size,
                    "attachment_bytes_b64": None,
                    "attachment_sha256": None,
                }
                for inject in source_injects
            ]
        # Snapshots created before provenance columns were introduced contain
        # unknown (NULL) provenance.  Do not pre-seed those clones: identity
        # alone cannot distinguish a facilitator-authored inject that reused a
        # scenario node id from a scenario-derived row.  Recreate every frozen
        # row as unknown instead of silently duplicating or dropping one.
        has_unknown_provenance = any(
            isinstance(spec, dict) and spec.get("scenario_seeded") is None
            for spec in snapshot_injects
        )
        if not has_unknown_provenance:
            await seed_injects_from_scenario(session, clone.id, clone_scenario)
        clone_injects = (
            await session.exec(
                select(Inject).where(Inject.exercise_id == clone.id).order_by(col(Inject.id))
            )
        ).all()
        seeded_by_identity = {
            (inject.scenario_node_id, inject.group_id): inject
            for inject in clone_injects
            if inject.scenario_seeded
        }
        snapshot_seeded_keys: set[tuple[str | None, str | None]] = set()
        from app.services.inject_service import AttachmentMeta, create_inject

        for spec in snapshot_injects:
            if not isinstance(spec, dict):
                continue
            identity = (spec.get("scenario_node_id"), spec.get("group_id"))
            if spec.get("scenario_seeded") is True:
                snapshot_seeded_keys.add(identity)
                inject = seeded_by_identity.get(identity)
                if inject is None:
                    continue
                inject.title = str(spec.get("title", inject.title))
                inject.content = str(spec.get("content", inject.content))
                inject.target_teams = spec.get("target_teams")
                inject.sequence_order = int(spec.get("sequence_order", inject.sequence_order))
                inject.release_offset_minutes = spec.get("release_offset_minutes")
                inject.release_offset_explicit = spec.get("release_offset_explicit", False)
                session.add(inject)
                continue
            attachment = None
            if spec.get("attachment_path"):
                source_attachment = AttachmentMeta(
                    filename=str(spec.get("attachment_filename") or "attachment"),
                    content_type=str(
                        spec.get("attachment_content_type") or "application/octet-stream"
                    ),
                    path=str(spec["attachment_path"]),
                    size=int(spec.get("attachment_size") or 0),
                )
                if spec.get("attachment_bytes_b64") and spec.get("attachment_sha256"):
                    try:
                        attachment = restore_snapshot_attachment(
                            filename=source_attachment.filename,
                            content_type=source_attachment.content_type,
                            encoded_bytes=str(spec["attachment_bytes_b64"]),
                            expected_sha256=str(spec["attachment_sha256"]),
                            expected_size=source_attachment.size,
                            exercise_id=clone.id,
                        )
                    except ValueError as exc:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="The immutable snapshot attachment failed integrity validation",
                        ) from exc
                else:
                    try:
                        attachment = copy_attachment_for_exercise(source_attachment, clone.id)
                    except FileNotFoundError as exc:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=(
                                "The historical attachment is unavailable and has no "
                                "embedded snapshot bytes"
                            ),
                        ) from exc
                copied_attachment_paths.append(attachment.path)
            await create_inject(
                session,
                exercise_id=clone.id,
                title=str(spec.get("title", "Untitled inject")),
                content=str(spec.get("content", "")),
                scenario_node_id=spec.get("scenario_node_id"),
                target_teams=spec.get("target_teams"),
                group_id=spec.get("group_id"),
                sequence_order=int(spec.get("sequence_order", 0)),
                release_offset_minutes=spec.get("release_offset_minutes"),
                attachment=attachment,
                scenario_seeded=spec.get("scenario_seeded", False),
                commit=False,
            )
        # A known-provenance source may have deliberately removed a seeded row
        # before launch. Do not recreate that row merely because the reusable
        # scenario definition still contains the node.
        stale_seeded = [
            inject
            for identity, inject in seeded_by_identity.items()
            if identity not in snapshot_seeded_keys
        ]
        if stale_seeded:
            from sqlalchemy import delete

            from app.models.inject import InjectProgress

            stale_ids = [inject.id for inject in stale_seeded if inject.id is not None]
            await session.exec(
                delete(InjectProgress).where(col(InjectProgress.inject_id).in_(stale_ids))
            )
            await session.exec(delete(Inject).where(col(Inject.id).in_(stale_ids)))
        if has_unknown_provenance:
            await promote_legacy_inject_provenance(session, clone.id, clone_scenario)
        await seed_progression(
            session,
            exercise_id=clone.id,
            start_node_id=definition.start_inject_id,
            group_ids=[team.id for team in definition.participant_teams],
        )
        await session.commit()
    except Exception:
        await session.rollback()
        for attachment_path in copied_attachment_paths:
            Path(attachment_path).unlink(missing_ok=True)
        raise
    await session.refresh(clone)
    return clone


async def transition_state(
    session: AsyncSession,
    exercise: Exercise,
    new_state: ExerciseState,
    *,
    actor_id: int | None = None,
) -> Exercise:
    """Compatibility wrapper returning the updated Exercise.

    API callers that need the durable event for a post-commit projection should use
    ``transition_state_with_history`` directly.
    """
    result = await transition_state_with_history(session, exercise, new_state, actor_id=actor_id)
    return result.exercise


async def transition_state_with_history(
    session: AsyncSession,
    exercise: Exercise,
    new_state: ExerciseState,
    *,
    actor_id: int | None = None,
) -> ExerciseTransitionResult:
    """Atomically change state and append its authoritative lifecycle event.

    The conditional UPDATE is a compare-and-swap on the state observed by the
    caller. A request that waited behind or raced another transition therefore
    receives 409 instead of overwriting the newer state. The history row and
    Exercise update share one transaction; callers may safely emit external
    projections only after this function returns.
    """
    previous_state = exercise.state
    if new_state not in VALID_TRANSITIONS[previous_state]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot transition from '{previous_state}' to '{new_state}'",
        )

    # Scenario editing acquires Scenario -> Exercise locks.  A draft launch must
    # use that same order and refresh the exercise after both locks are held;
    # otherwise a concurrent clone fork can win the state CAS while the snapshot
    # still resolves through the pre-fork scenario object.
    if previous_state == ExerciseState.draft and new_state == ExerciseState.active:
        scenario_lock = (
            await session.exec(
                select(Scenario).where(Scenario.id == exercise.scenario_id).with_for_update()
            )
        ).first()
        if scenario_lock is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Exercise scenario was not found"
            )
        locked = (
            await session.exec(
                select(Exercise)
                .where(Exercise.id == exercise.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).first()
        if locked is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exercise not found")
        if locked.state != previous_state:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Exercise state changed from '{previous_state}' to '{locked.state}' "
                    "while this request was in flight; reload and try again"
                ),
            )
        exercise = locked

    now = datetime.now(UTC)
    values: dict = {"state": new_state}
    if new_state == ExerciseState.active and exercise.started_at is None:
        values["started_at"] = now
    # Include pacing fields in the same compare-and-swap update as state so a
    # stale lifecycle request cannot overwrite a completed pause calculation.
    if new_state == ExerciseState.paused:
        values["paused_at"] = now
    if new_state == ExerciseState.active and exercise.paused_at is not None:
        values["accumulated_pause_seconds"] = (
            exercise.accumulated_pause_seconds + (now - exercise.paused_at).total_seconds()
        )
        values["paused_at"] = None
    if new_state == ExerciseState.completed:
        values["ended_at"] = now

    assert exercise.id is not None
    statement = (
        update(Exercise)
        .where(col(Exercise.id) == exercise.id, col(Exercise.state) == previous_state)
        .values(**values)
        .returning(col(Exercise.id))
        .execution_options(synchronize_session=False)
    )
    result = await session.exec(statement)
    if result.scalar_one_or_none() is None:
        current = (
            await session.exec(select(Exercise.state).where(Exercise.id == exercise.id))
        ).one_or_none()
        await session.rollback()
        if current is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exercise not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Exercise state changed from '{previous_state}' to '{current}' "
                "while this request was in flight; reload and try again"
            ),
        )

    transition = ExerciseStateTransition(
        exercise_id=exercise.id,
        from_state=previous_state,
        to_state=new_state,
        actor_id=actor_id if actor_id is not None else exercise.created_by,
        transitioned_at=now,
    )
    session.add(transition)
    try:
        if previous_state == ExerciseState.draft and new_state == ExerciseState.active:
            await _freeze_run_snapshot(session, exercise)
        # Flush first: the frame names the transition, so it needs its id — and the event
        # must be recorded inside this transaction so a failed commit discards it.
        await session.flush()
        assert exercise.id is not None
        assert transition.id is not None
        record(
            session,
            ExerciseStateChanged(
                exercise_id=exercise.id,
                transition_id=transition.id,
                action=transition_action(previous_state, new_state),
            ),
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    updated = await session.get(Exercise, exercise.id, populate_existing=True)
    assert updated is not None
    # Dispatch here, not in the caller: this function owns the transaction, so it owns the
    # projection. Leaving it to the caller means every *other* caller (the sample loader,
    # a test fixture) strands a committed event in the buffer, where the next dispatch on
    # the same session adopts it and emits a stale frame out of nowhere.
    await dispatch(session)
    action = transition_action(previous_state, new_state)
    return ExerciseTransitionResult(exercise=updated, transition=transition, action=action)


async def scenario_group_ids(session: AsyncSession, exercise: Exercise) -> set[str]:
    assert exercise.id is not None
    definition = await definition_for_exercise(session, exercise.id)
    if definition is None:
        return set()
    return definition_team_ids(definition)


async def validate_group_id(
    session: AsyncSession, exercise: Exercise, group_id: str | None
) -> str | None:
    if group_id is None:
        return None
    normalized = group_id.strip()
    if not normalized:
        return None
    if normalized not in await scenario_group_ids(session, exercise):
        raise HTTPException(
            status_code=422,
            detail="group_id is not defined in this scenario",
        )
    return normalized


async def validate_team_ids(
    session: AsyncSession, exercise: Exercise, team_ids: list[str] | None, *, field: str
) -> list[str] | None:
    """Normalize and validate a non-empty, duplicate-free scenario team audience."""
    if team_ids is None:
        return None
    assert exercise.id is not None
    definition = await definition_for_exercise(session, exercise.id)
    if definition is None:
        raise HTTPException(status_code=422, detail="exercise scenario was not found")
    try:
        return validate_definition_team_ids(team_ids, definition, field=field)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def default_group_for_user(
    session: AsyncSession, exercise: Exercise, user: User
) -> str | None:
    if not user.team:
        return None
    return user.team if user.team in await scenario_group_ids(session, exercise) else None


async def enrol_member(
    session: AsyncSession,
    *,
    exercise: Exercise,
    user_id: int,
    group_id: str | None = None,
    commit: bool = True,
) -> ExerciseMember:
    assert exercise.id is not None
    from app.services.progression_service import roster_changes_allowed

    if not await roster_changes_allowed(session, exercise.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Roster changes are locked after the first inject is released",
        )
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    normalized_group_id = await validate_group_id(session, exercise, group_id)
    existing = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise.id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first()
    if existing:
        return existing  # idempotent

    resolved_group_id = (
        normalized_group_id
        if normalized_group_id is not None
        else await default_group_for_user(session, exercise, user)
    )
    member = ExerciseMember(
        exercise_id=exercise.id,
        user_id=user_id,
        group_id=resolved_group_id,
        role_at_enrolment=user.role,
    )
    session.add(member)
    try:
        if commit:
            await session.commit()
            await session.refresh(member)
        else:
            await session.flush()
    except IntegrityError:
        # A concurrent enrolment (double-click, two facilitator tabs) won the race
        # between the pre-check above and this insert. The unique constraint
        # uq_exercisemember_exercise_user (#262) turns that into a clean idempotent
        # return instead of a divergent duplicate row, matching the established
        # Response/ResponseAssessment on-conflict pattern. We can only recover when we
        # own the transaction; a caller committing later (commit=False) must roll back
        # and handle it itself.
        if not commit:
            raise
        await session.rollback()
        existing = (
            await session.exec(
                select(ExerciseMember)
                .where(ExerciseMember.exercise_id == exercise.id)
                .where(ExerciseMember.user_id == user_id)
            )
        ).first()
        if existing is not None:
            return existing
        raise
    return member


async def update_member_group(
    session: AsyncSession,
    *,
    exercise: Exercise,
    user_id: int,
    group_id: str | None,
) -> ExerciseMember:
    assert exercise.id is not None
    from app.services.progression_service import roster_changes_allowed

    if not await roster_changes_allowed(session, exercise.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Roster changes are locked after the first inject is released",
        )
    member = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise.id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first()
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in exercise"
        )
    member.group_id = await validate_group_id(session, exercise, group_id)
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


async def remove_member(session: AsyncSession, *, exercise: Exercise, user_id: int) -> None:
    assert exercise.id is not None
    from app.services.progression_service import roster_changes_allowed

    if not await roster_changes_allowed(session, exercise.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Roster changes are locked after the first inject is released",
        )
    member = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise.id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first()
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in exercise"
        )
    await session.delete(member)
    await session.commit()


async def list_members(session: AsyncSession, exercise_id: int) -> list[ExerciseMember]:
    return list(
        (
            await session.exec(
                select(ExerciseMember).where(ExerciseMember.exercise_id == exercise_id)
            )
        ).all()
    )
