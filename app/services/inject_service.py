import json
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlmodel import Session

from app.models.inject import Inject, InjectState
from app.models.scenario import Scenario
from app.services.scenario_service import export_definition


def create_inject(
    session: Session,
    *,
    exercise_id: int,
    title: str,
    content: str,
    scenario_node_id: str | None = None,
    target_teams: list[str] | None = None,
    sequence_order: int = 0,
) -> Inject:
    inject = Inject(
        exercise_id=exercise_id,
        scenario_node_id=scenario_node_id,
        title=title,
        content=content,
        target_teams=json.dumps(target_teams) if target_teams else None,
        sequence_order=sequence_order,
    )
    session.add(inject)
    session.commit()
    session.refresh(inject)
    return inject


def get_inject_or_404(session: Session, exercise_id: int, inject_id: int) -> Inject:
    inject = session.get(Inject, inject_id)
    if not inject or inject.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inject not found")
    return inject


async def release_inject(
    session: Session,
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
    session.commit()
    session.refresh(inject)

    await _broadcast_inject_released(session, inject)
    await _trigger_communications(session, inject)
    return inject


async def _trigger_communications(session: Session, inject: Inject) -> None:
    """If the inject's scenario node declares triggered comms, schedule them."""
    from app.services.communication_service import schedule_triggered_comms

    if not inject.scenario_node_id:
        return
    from app.models.exercise import Exercise
    from app.models.scenario import Scenario

    exercise = session.get(Exercise, inject.exercise_id)
    if not exercise:
        return
    scenario = session.get(Scenario, exercise.scenario_id)
    if not scenario:
        return

    from app.services.scenario_service import export_definition, get_inject_node

    node = get_inject_node(export_definition(scenario), inject.scenario_node_id)
    if node and node.triggers_communications:
        schedule_triggered_comms(session, inject, node.triggers_communications)


async def _broadcast_inject_released(session: Session, inject: Inject) -> None:
    from app.services.ws_manager import manager

    target_teams: list[str] | None = (
        json.loads(inject.target_teams) if inject.target_teams else None
    )
    message = {
        "type": "inject_released",
        "exercise_id": inject.exercise_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": _inject_payload(session, inject),
    }

    if target_teams:
        await manager.broadcast_to_teams(inject.exercise_id, target_teams, message)
    else:
        await manager.broadcast_to_exercise(inject.exercise_id, message)


def _inject_options(session: Session, inject: Inject) -> list[dict]:
    if not inject.scenario_node_id:
        return []
    from app.models.exercise import Exercise
    from app.models.scenario import Scenario
    from app.services.scenario_service import export_definition, get_inject_node

    exercise = session.get(Exercise, inject.exercise_id)
    if not exercise:
        return []
    scenario = session.get(Scenario, exercise.scenario_id)
    if not scenario:
        return []
    node = get_inject_node(export_definition(scenario), inject.scenario_node_id)
    if not node:
        return []
    return [
        {"id": option.id, "label": option.label, "next_inject_id": option.next_inject_id}
        for option in node.options
    ]


def _inject_payload(session: Session, inject: Inject) -> dict:
    return {
        "id": inject.id,
        "exercise_id": inject.exercise_id,
        "scenario_node_id": inject.scenario_node_id,
        "title": inject.title,
        "content": inject.content,
        "target_teams": json.loads(inject.target_teams) if inject.target_teams else None,
        "sequence_order": inject.sequence_order,
        "state": inject.state,
        "released_at": inject.released_at.isoformat() if inject.released_at else None,
        "released_by": inject.released_by,
        "options": _inject_options(session, inject),
    }


def seed_injects_from_scenario(session: Session, exercise_id: int, scenario: Scenario) -> None:
    """Pre-populate Inject rows from the scenario definition (all pending)."""
    definition = export_definition(scenario)
    for i, node in enumerate(definition.injects):
        create_inject(
            session,
            exercise_id=exercise_id,
            title=node.title,
            content=node.content,
            scenario_node_id=node.id,
            target_teams=node.target_teams or None,
            sequence_order=i,
        )
