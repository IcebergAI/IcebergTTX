from datetime import UTC, datetime

from sqlmodel import Session

from app.models.inject import Inject
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
) -> tuple[Response, list[str]]:
    """
    Save a response. Returns (response, next_inject_ids) where next_inject_ids
    is the list of valid next scenario nodes given the selected option (empty for
    free-text only responses or leaf nodes).
    """
    response = Response(
        inject_id=inject_id,
        exercise_id=exercise_id,
        user_id=user_id,
        content=content,
        selected_option=selected_option,
    )
    session.add(response)
    session.commit()
    session.refresh(response)

    next_ids = _compute_next_inject_ids(session, exercise_id, inject_id, selected_option)
    return response, next_ids


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


async def broadcast_response_submitted(
    response: Response,
    next_inject_ids: list[str],
) -> None:
    from app.services.ws_manager import manager

    message = {
        "type": "response_submitted",
        "exercise_id": response.exercise_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": {
            "response": _response_payload(response),
            "next_inject_ids": next_inject_ids,
        },
    }
    await manager.send_to_facilitators(response.exercise_id, message)


def _response_payload(r: Response) -> dict:
    return {
        "id": r.id,
        "inject_id": r.inject_id,
        "exercise_id": r.exercise_id,
        "user_id": r.user_id,
        "content": r.content,
        "selected_option": r.selected_option,
        "submitted_at": r.submitted_at.isoformat(),
    }
