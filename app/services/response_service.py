from datetime import UTC, datetime

from sqlmodel import Session, select

from app.models.inject import Inject, InjectState
from app.models.response import Response
from app.models.scenario import Scenario
from app.services.scenario_service import export_definition, get_next_inject_ids, resolve_branch


def submit_response(
    session: Session,
    *,
    inject_id: int,
    exercise_id: int,
    user_id: int,
    content: str,
    selected_option: str | None = None,
    group_id: str | None = None,
) -> tuple[Response, list[dict]]:
    """
    Save a response. Returns (response, next_inject_ids) where next_inject_ids
    is the list of valid next scenario nodes given the selected option (empty for
    free-text only responses or leaf nodes).
    """
    response = Response(
        inject_id=inject_id,
        exercise_id=exercise_id,
        user_id=user_id,
        group_id=group_id,
        content=content,
        selected_option=selected_option,
    )
    session.add(response)
    session.commit()
    session.refresh(response)

    next_ids = _compute_next_inject_ids(session, exercise_id, inject_id, selected_option)
    return response, _pending_next_injects(session, exercise_id, next_ids, group_id)


def _compute_next_inject_ids(
    session: Session,
    exercise_id: int,
    inject_id: int,
    selected_option: str | None,
) -> list[str]:
    from app.models.exercise import Exercise

    exercise = session.get(Exercise, exercise_id)
    if not exercise or not exercise.scenario_id:
        return []

    scenario = session.get(Scenario, exercise.scenario_id)
    if not scenario:
        return []

    inject = session.get(Inject, inject_id)
    if not inject or not inject.scenario_node_id:
        return []

    definition = export_definition(scenario)

    if selected_option:
        next_id = resolve_branch(definition, inject.scenario_node_id, selected_option)
        return [next_id] if next_id else []

    return get_next_inject_ids(definition, inject.scenario_node_id)


def response_next_inject_suggestions(session: Session, response: Response) -> list[dict]:
    next_ids = _compute_next_inject_ids(
        session,
        response.exercise_id,
        response.inject_id,
        response.selected_option,
    )
    return _pending_next_injects(session, response.exercise_id, next_ids, response.group_id)


def _pending_next_injects(
    session: Session,
    exercise_id: int,
    scenario_node_ids: list[str],
    group_id: str | None,
) -> list[dict]:
    if not scenario_node_ids:
        return []
    injects = session.exec(
        select(Inject)
        .where(Inject.exercise_id == exercise_id)
        .where(Inject.state == InjectState.pending)
    ).all()
    ordered_ids = {node_id: i for i, node_id in enumerate(scenario_node_ids)}
    matches = [
        inject
        for inject in injects
        if inject.scenario_node_id in ordered_ids
        and _inject_matches_group(inject, group_id)
    ]
    matches.sort(key=lambda inject: ordered_ids.get(inject.scenario_node_id or "", 9999))
    return [_next_inject_payload(inject) for inject in matches]


def _inject_matches_group(inject: Inject, group_id: str | None) -> bool:
    if inject.group_id is not None:
        return group_id == inject.group_id
    if inject.target_teams:
        import json

        teams = json.loads(inject.target_teams)
        return group_id in teams
    return True


async def broadcast_response_submitted(
    response: Response,
    next_injects: list[dict],
) -> None:
    from app.services.ws_manager import manager

    message = {
        "type": "response_submitted",
        "exercise_id": response.exercise_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": {
            "response": _response_payload(response),
            "next_inject_ids": [item["scenario_node_id"] for item in next_injects],
            "next_injects": next_injects,
        },
    }
    await manager.send_to_facilitators(response.exercise_id, message)


def _response_payload(r: Response) -> dict:
    return {
        "id": r.id,
        "inject_id": r.inject_id,
        "exercise_id": r.exercise_id,
        "user_id": r.user_id,
        "group_id": r.group_id,
        "content": r.content,
        "selected_option": r.selected_option,
        "submitted_at": r.submitted_at.isoformat(),
    }


def _next_inject_payload(inject: Inject) -> dict:
    return {
        "id": inject.id,
        "scenario_node_id": inject.scenario_node_id,
        "title": inject.title,
        "group_id": inject.group_id,
    }
