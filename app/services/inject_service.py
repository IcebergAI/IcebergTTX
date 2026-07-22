from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import delete, update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import ExerciseState
from app.models.inject import Inject, InjectState
from app.models.scenario import Scenario
from app.schemas.api import InjectPublic
from app.services.domain_events import InjectReleased, dispatch, record
from app.services.scenario_service import export_definition


@dataclass(frozen=True)
class AttachmentMeta:
    """Stored attachment fields, grouped so they travel as one value (#5)."""

    filename: str
    content_type: str
    path: str
    size: int


async def create_inject(
    session: AsyncSession,
    *,
    exercise_id: int,
    title: str,
    content: str,
    scenario_node_id: str | None = None,
    target_teams: list[str] | None = None,
    group_id: str | None = None,
    sequence_order: int = 0,
    release_offset_minutes: int | None = None,
    attachment: AttachmentMeta | None = None,
    commit: bool = True,
) -> Inject:
    normalized_group_id = group_id.strip() if group_id and group_id.strip() else None
    normalized_targets = target_teams
    if normalized_group_id and not normalized_targets:
        normalized_targets = [normalized_group_id]
    inject = Inject(
        exercise_id=exercise_id,
        scenario_node_id=scenario_node_id,
        title=title,
        content=content,
        target_teams=normalized_targets or None,
        group_id=normalized_group_id,
        sequence_order=sequence_order,
        release_offset_minutes=release_offset_minutes,
        attachment_filename=attachment.filename if attachment else None,
        attachment_content_type=attachment.content_type if attachment else None,
        attachment_path=attachment.path if attachment else None,
        attachment_size=attachment.size if attachment else None,
    )
    session.add(inject)
    await session.flush()
    if commit:
        await session.commit()
        await session.refresh(inject)
    return inject


async def get_inject_or_404(session: AsyncSession, exercise_id: int, inject_id: int) -> Inject:
    inject = await session.get(Inject, inject_id)
    if not inject or inject.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inject not found")
    return inject


async def delete_pending_inject(session: AsyncSession, inject: Inject) -> None:
    """Delete an inject only while it is still pending, else 409.

    Conditional on ``state == pending`` so a release committing between the caller's read and
    this delete cannot slip through: once released, the inject carries after-action evidence
    (responses, comments, per-group resolution progress) and is on participant screens (#265).
    A released inject matches zero rows here and is left fully intact; a genuinely-pending one
    has no such dependents, so the ``ondelete="CASCADE"`` FKs never destroy evidence. Mirrors
    the compare-and-swap in ``release_inject``."""
    assert inject.id is not None
    deleted = (
        await session.exec(
            delete(Inject)
            .where(col(Inject.id) == inject.id, col(Inject.state) == InjectState.pending)
            .returning(col(Inject.id))
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if deleted is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Released injects cannot be deleted; resolve them or let them stand",
        )
    await session.commit()


async def release_inject(
    session: AsyncSession,
    inject: Inject,
    released_by: int | None,
    *,
    scheduled: bool = False,
) -> Inject:
    from app.services.progression_service import (
        lock_exercise_for_audience_snapshot,
        release_is_allowed,
        seed_inject_resolution_contexts,
    )

    # Re-read state from the locked exercise row. Both callers (manual route and scheduled
    # worker) check state == active *before* this lock, so a pause committing in that window
    # would otherwise land a release — frame broadcast, triggered comms armed — into a paused
    # exercise (#265). The lock is already held, so this costs nothing extra.
    exercise = await lock_exercise_for_audience_snapshot(session, inject.exercise_id)
    if exercise.state != ExerciseState.active:
        # Nothing has been written yet (only the row lock is held), so raise without a
        # rollback — matching the release_is_allowed guard just below. The caller's / worker's
        # transaction releases the lock on exit.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only active exercises can release injects",
        )

    if not await release_is_allowed(session, inject, scheduled=scheduled):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Inject is not the current branch for its group",
        )

    contexts = await seed_inject_resolution_contexts(session, inject)
    if not contexts:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Inject cannot be released without an eligible participant audience",
        )

    now = datetime.now(UTC)
    statement = (
        update(Inject)
        .where(col(Inject.id) == inject.id, col(Inject.state) == InjectState.pending)
        .values(state=InjectState.released, released_at=now, released_by=released_by)
        .returning(col(Inject.id))
        .execution_options(synchronize_session=False)
    )
    released_id = (await session.exec(statement)).scalar_one_or_none()
    if released_id is None:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Inject is no longer pending and cannot be released",
        )
    # Recorded inside the transaction, so the compare-and-swap loser above — which
    # rolled back — cannot emit a frame or fire triggered comms for a release that
    # never happened. Both are subscribers to this one event (see ws_projector).
    assert inject.id is not None
    record(session, InjectReleased(exercise_id=inject.exercise_id, inject=inject))
    await session.commit()
    # ``inject`` may already be in this session's identity map, so a returned ORM
    # row would retain its old pending attributes with synchronize_session=False.
    # Refresh the authoritative row after commit before constructing side effects.
    await session.refresh(inject)

    # Releasing (manually or on schedule) settles any pending scheduled-release timer only
    # after the compare-and-swap has committed. The losing racer must not cancel the winner's
    # timer or emit any side effects.
    from app.services.schedule_service import cancel_inject_schedule

    cancel_inject_schedule(inject.exercise_id, inject.id)

    await dispatch(session)
    return inject


def inject_target_groups(inject: Inject) -> list[str] | None:
    if inject.group_id:
        return [inject.group_id]
    return inject.target_teams


def inject_attachment_payload(inject: Inject) -> dict | None:
    if not inject.attachment_path or not inject.attachment_filename:
        return None
    return {
        "filename": inject.attachment_filename,
        "content_type": inject.attachment_content_type or "application/octet-stream",
        "size": inject.attachment_size,
        "url": f"/api/exercises/{inject.exercise_id}/injects/{inject.id}/attachment",
    }


async def _inject_node(session: AsyncSession, inject: Inject):
    if not inject.scenario_node_id:
        return None
    from app.services.scenario_service import definition_for_exercise, get_inject_node

    definition = await definition_for_exercise(session, inject.exercise_id)
    if not definition:
        return None
    return get_inject_node(definition, inject.scenario_node_id)


async def _inject_options(
    session: AsyncSession, inject: Inject, *, include_progression: bool = False
) -> list[dict]:
    node = await _inject_node(session, inject)
    if not node:
        return []
    options: list[dict] = []
    for option in node.options:
        entry: dict[str, str | None] = {"id": option.id, "label": option.label}
        # next_inject_id maps this option to an unreleased future node — the branch
        # topology. Omit it unless the caller opts in (facilitator paths), so a
        # participant can't read which option leads where before choosing (#266).
        if include_progression:
            entry["next_inject_id"] = option.next_inject_id
        options.append(entry)
    return options


async def inject_payload(
    session: AsyncSession, inject: Inject, *, include_progression: bool = False
) -> dict:
    """Canonical inject serialization shared by the API responses and WS broadcasts.

    Built via the ``InjectPublic`` schema so the HTTP and WebSocket payloads cannot
    drift (#21, #31).

    ``next_inject_id`` (node-level and per-option) points at unreleased future nodes —
    the exercise's branch topology. It is redacted by **default** (#266) and included
    only when ``include_progression`` is set, which facilitator-facing paths opt into.
    Redacting by default means a forgotten caller over-redacts (safe) rather than leaks.
    """
    assert inject.id is not None
    node = await _inject_node(session, inject)
    payload = InjectPublic(
        id=inject.id,
        exercise_id=inject.exercise_id,
        scenario_node_id=inject.scenario_node_id,
        title=inject.title,
        content=inject.content,
        target_teams=inject.target_teams,
        group_id=inject.group_id,
        sequence_order=inject.sequence_order,
        state=inject.state,
        released_at=inject.released_at.isoformat() if inject.released_at else None,
        released_by=inject.released_by,
        resolved_at=inject.resolved_at.isoformat() if inject.resolved_at else None,
        resolved_by=inject.resolved_by,
        resolution_reason=inject.resolution_reason,
        release_offset_minutes=inject.release_offset_minutes,
        options=await _inject_options(session, inject, include_progression=include_progression),
        next_inject_id=(node.next_inject_id if node and include_progression else None),
        free_text_response=node.free_text_response if node else True,
        attachment=inject_attachment_payload(inject),
    ).model_dump(mode="json")
    if not include_progression:
        # Drop the node-level key entirely so the WS frame (no response_model) omits it.
        # HTTP routes keep response_model=InjectPublic, which re-adds it as null — a
        # participant sees no future node either way (#266).
        payload.pop("next_inject_id", None)
    return payload


async def seed_injects_from_scenario(
    session: AsyncSession, exercise_id: int, scenario: Scenario
) -> None:
    """Pre-populate Inject rows from the scenario definition (all pending)."""
    definition = export_definition(scenario)
    for i, node in enumerate(definition.injects):
        sequence_order = node.sequence_order or i
        if node.target_teams:
            for group_id in node.target_teams:
                await create_inject(
                    session,
                    exercise_id=exercise_id,
                    title=node.title,
                    content=node.content,
                    scenario_node_id=node.id,
                    target_teams=[group_id],
                    group_id=group_id,
                    sequence_order=sequence_order,
                    release_offset_minutes=node.release_at_minutes,
                    commit=False,
                )
        else:
            await create_inject(
                session,
                exercise_id=exercise_id,
                title=node.title,
                content=node.content,
                scenario_node_id=node.id,
                target_teams=None,
                group_id=None,
                sequence_order=sequence_order,
                release_offset_minutes=node.release_at_minutes,
                commit=False,
            )


async def attachment_paths_for_exercise(session: AsyncSession, exercise_id: int) -> list[str]:
    """Every stored attachment path in the exercise, for cleanup after a cascade delete."""
    rows = await session.exec(
        select(Inject.attachment_path).where(
            Inject.exercise_id == exercise_id,
            col(Inject.attachment_path).is_not(None),
        )
    )
    return [path for path in rows.all() if path is not None]
