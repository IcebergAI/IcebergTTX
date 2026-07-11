from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.inject import Inject, InjectState
from app.models.scenario import Scenario
from app.schemas.api import InjectPublic
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


async def release_inject(
    session: AsyncSession,
    inject: Inject,
    released_by: int | None,
) -> Inject:
    from app.services.progression_service import release_is_allowed

    if not await release_is_allowed(session, inject):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Inject is not the current branch for its group",
        )

    now = datetime.now(UTC)
    statement = (
        update(Inject)
        .where(Inject.id == inject.id, Inject.state == InjectState.pending)
        .values(state=InjectState.released, released_at=now, released_by=released_by)
        .returning(Inject.id)
        .execution_options(synchronize_session=False)
    )
    released_id = (await session.exec(statement)).scalar_one_or_none()
    if released_id is None:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Inject is no longer pending and cannot be released",
        )
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

    await _broadcast_inject_released(session, inject)
    await _trigger_communications(session, inject)
    return inject


async def _trigger_communications(session: AsyncSession, inject: Inject) -> None:
    """If the inject's scenario node declares triggered comms, schedule them."""
    from app.services.communication_service import schedule_triggered_comms

    if not inject.scenario_node_id:
        return

    from app.services.scenario_service import definition_for_exercise, get_inject_node

    definition = await definition_for_exercise(session, inject.exercise_id)
    if not definition:
        return

    node = get_inject_node(definition, inject.scenario_node_id)
    if node and node.triggers_communications:
        schedule_triggered_comms(inject, node.triggers_communications, node.id)


async def _broadcast_inject_released(session: AsyncSession, inject: Inject) -> None:
    from app.services.ws_manager import manager

    target_groups = _inject_target_groups(inject)
    message = {
        "type": "inject_released",
        "exercise_id": inject.exercise_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": await inject_payload(session, inject),
    }

    if target_groups:
        await manager.broadcast_to_groups(inject.exercise_id, target_groups, message)
    else:
        await manager.broadcast_to_exercise(inject.exercise_id, message)


async def broadcast_inject_updated(session: AsyncSession, inject: Inject) -> None:
    """Push a metadata change (e.g. edited/cancelled schedule, #116) to facilitators.

    Facilitator-only: participants only ever see *released* injects, so a pending
    inject's schedule edit is irrelevant to them and stays off their socket.
    """
    from app.services.ws_manager import manager

    await manager.send_to_facilitators(
        inject.exercise_id,
        {
            "type": "inject_updated",
            "exercise_id": inject.exercise_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": await inject_payload(session, inject),
        },
    )


def _inject_target_groups(inject: Inject) -> list[str] | None:
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


async def _inject_options(session: AsyncSession, inject: Inject) -> list[dict]:
    node = await _inject_node(session, inject)
    if not node:
        return []
    return [
        {"id": option.id, "label": option.label, "next_inject_id": option.next_inject_id}
        for option in node.options
    ]


async def inject_payload(session: AsyncSession, inject: Inject) -> dict:
    """Canonical inject serialization shared by the API responses and WS broadcasts.

    Built via the ``InjectPublic`` schema so the HTTP and WebSocket payloads cannot
    drift (#21, #31).
    """
    node = await _inject_node(session, inject)
    return InjectPublic(
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
        options=await _inject_options(session, inject),
        next_inject_id=node.next_inject_id if node else None,
        free_text_response=node.free_text_response if node else True,
        attachment=inject_attachment_payload(inject),
    ).model_dump(mode="json")


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
