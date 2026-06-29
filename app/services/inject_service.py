import json
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.inject import Inject, InjectState
from app.models.scenario import Scenario
from app.services.scenario_service import export_definition


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
    attachment_filename: str | None = None,
    attachment_content_type: str | None = None,
    attachment_path: str | None = None,
    attachment_size: int | None = None,
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
        target_teams=json.dumps(normalized_targets) if normalized_targets else None,
        group_id=normalized_group_id,
        sequence_order=sequence_order,
        attachment_filename=attachment_filename,
        attachment_content_type=attachment_content_type,
        attachment_path=attachment_path,
        attachment_size=attachment_size,
    )
    session.add(inject)
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
    released_by: int,
) -> Inject:
    if inject.state != InjectState.pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Inject is already '{inject.state}', cannot release",
        )

    inject.state = InjectState.released
    inject.released_at = datetime.now(UTC)
    inject.released_by = released_by
    session.add(inject)
    await session.commit()
    await session.refresh(inject)

    await _broadcast_inject_released(session, inject)
    await _trigger_communications(session, inject)
    return inject


async def _trigger_communications(session: AsyncSession, inject: Inject) -> None:
    """If the inject's scenario node declares triggered comms, schedule them."""
    from app.services.communication_service import schedule_triggered_comms

    if not inject.scenario_node_id:
        return
    from app.models.exercise import Exercise
    from app.models.scenario import Scenario

    exercise = await session.get(Exercise, inject.exercise_id)
    if not exercise:
        return
    scenario = await session.get(Scenario, exercise.scenario_id)
    if not scenario:
        return

    from app.services.scenario_service import export_definition, get_inject_node

    node = get_inject_node(export_definition(scenario), inject.scenario_node_id)
    if node and node.triggers_communications:
        schedule_triggered_comms(inject, node.triggers_communications)


async def _broadcast_inject_released(session: AsyncSession, inject: Inject) -> None:
    from app.services.ws_manager import manager

    target_groups = _inject_target_groups(inject)
    message = {
        "type": "inject_released",
        "exercise_id": inject.exercise_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": await _inject_payload(session, inject),
    }

    if target_groups:
        await manager.broadcast_to_groups(inject.exercise_id, target_groups, message)
    else:
        await manager.broadcast_to_exercise(inject.exercise_id, message)


def _inject_target_groups(inject: Inject) -> list[str] | None:
    if inject.group_id:
        return [inject.group_id]
    return json.loads(inject.target_teams) if inject.target_teams else None


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
    from app.models.exercise import Exercise
    from app.models.scenario import Scenario
    from app.services.scenario_service import export_definition, get_inject_node

    exercise = await session.get(Exercise, inject.exercise_id)
    if not exercise:
        return None
    scenario = await session.get(Scenario, exercise.scenario_id)
    if not scenario:
        return None
    return get_inject_node(export_definition(scenario), inject.scenario_node_id)


async def _inject_options(session: AsyncSession, inject: Inject) -> list[dict]:
    node = await _inject_node(session, inject)
    if not node:
        return []
    return [
        {"id": option.id, "label": option.label, "next_inject_id": option.next_inject_id}
        for option in node.options
    ]


async def _inject_payload(session: AsyncSession, inject: Inject) -> dict:
    node = await _inject_node(session, inject)
    return {
        "id": inject.id,
        "exercise_id": inject.exercise_id,
        "scenario_node_id": inject.scenario_node_id,
        "title": inject.title,
        "content": inject.content,
        "target_teams": json.loads(inject.target_teams) if inject.target_teams else None,
        "group_id": inject.group_id,
        "sequence_order": inject.sequence_order,
        "state": inject.state,
        "released_at": inject.released_at.isoformat() if inject.released_at else None,
        "released_by": inject.released_by,
        "options": await _inject_options(session, inject),
        "next_inject_id": node.next_inject_id if node else None,
        "free_text_response": node.free_text_response if node else True,
        "attachment": inject_attachment_payload(inject),
    }


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
            )
