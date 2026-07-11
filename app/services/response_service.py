from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.inject import Inject, InjectState
from app.models.response import Response
from app.schemas.api import ResponsePublic
from app.schemas.scenario_json import InjectNode, ScenarioDefinition
from app.services.access_control import inject_matches_group
from app.services.scenario_service import (
    definition_for_exercise,
    get_inject_node,
    get_next_inject_ids,
    resolve_branch,
)


def response_validation_error(
    node: InjectNode | None,
    *,
    content: str,
    selected_option: str | None,
) -> str | None:
    """Return a field-specific error when a response does not satisfy its inject.

    Option-bearing injects always require an exact option ID from their scenario
    node. ``free_text_response`` controls whether those injects additionally
    require prose; injects without options always remain free-text responses.
    """
    option_ids = {option.id for option in node.options} if node else set()

    if option_ids:
        if not selected_option:
            return "selected_option is required for this inject"
        if selected_option not in option_ids:
            return "selected_option is not valid for this inject"
    elif selected_option is not None:
        return "selected_option is not valid for this inject"

    content_required = node is None or not option_ids or node.free_text_response
    if content_required and not content.strip():
        return "content is required for this inject"
    return None


def response_next_inject_ids(
    definition: ScenarioDefinition,
    scenario_node_id: str,
    selected_option: str | None,
) -> list[str]:
    """Resolve one selected branch, or the successor of a non-branching node."""
    node = get_inject_node(definition, scenario_node_id)
    if node is None:
        return []
    if node.options:
        if not selected_option:
            return []
        next_id = resolve_branch(definition, scenario_node_id, selected_option)
        return [next_id] if next_id else []
    if selected_option is not None:
        return []
    return get_next_inject_ids(definition, scenario_node_id)


async def submit_response(
    session: AsyncSession,
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
    try:
        await session.flush()

        next_ids = await _compute_next_inject_ids(
            session, exercise_id, inject_id, selected_option
        )
        inject = await session.get(Inject, inject_id)
        assert inject is not None
        from app.services.progression_service import resolve_response_progression

        await resolve_response_progression(
            session,
            inject=inject,
            group_id=group_id,
            actor_id=user_id,
            next_node_id=next_ids[0] if len(next_ids) == 1 else None,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Response already submitted for this inject",
        ) from exc
    await session.refresh(response)

    return response, await _pending_next_injects(session, exercise_id, next_ids, group_id)


async def _compute_next_inject_ids(
    session: AsyncSession,
    exercise_id: int,
    inject_id: int,
    selected_option: str | None,
) -> list[str]:
    definition = await definition_for_exercise(session, exercise_id)
    if not definition:
        return []

    inject = await session.get(Inject, inject_id)
    if not inject or not inject.scenario_node_id:
        return []

    return response_next_inject_ids(definition, inject.scenario_node_id, selected_option)


async def response_next_inject_suggestions(session: AsyncSession, response: Response) -> list[dict]:
    next_ids = await _compute_next_inject_ids(
        session,
        response.exercise_id,
        response.inject_id,
        response.selected_option,
    )
    return await _pending_next_injects(session, response.exercise_id, next_ids, response.group_id)


async def _pending_next_injects(
    session: AsyncSession,
    exercise_id: int,
    scenario_node_ids: list[str],
    group_id: str | None,
) -> list[dict]:
    if not scenario_node_ids:
        return []
    injects = (
        await session.exec(
            select(Inject)
            .where(Inject.exercise_id == exercise_id)
            .where(Inject.state == InjectState.pending)
        )
    ).all()
    ordered_ids = {node_id: i for i, node_id in enumerate(scenario_node_ids)}
    matches = [
        inject
        for inject in injects
        if inject.scenario_node_id in ordered_ids and inject_matches_group(inject, group_id)
    ]
    matches.sort(key=lambda inject: ordered_ids.get(inject.scenario_node_id or "", 9999))
    return [_next_inject_payload(inject) for inject in matches]


async def broadcast_response_submitted(
    response: Response,
    next_injects: list[dict],
    progression: dict | None = None,
) -> None:
    from app.services.ws_manager import manager

    message = {
        "type": "response_submitted",
        "exercise_id": response.exercise_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": {
            "response": response_payload(response),
            "next_inject_ids": [item["scenario_node_id"] for item in next_injects],
            "next_injects": next_injects,
            "progression": progression,
        },
    }
    await manager.send_to_facilitators(response.exercise_id, message)


def response_payload(
    r: Response,
    next_injects: list[dict] | None = None,
    progression: dict | None = None,
) -> dict:
    """Canonical response serialization (HTTP + WS) via the ResponsePublic schema (#31)."""
    assert r.id is not None
    model = ResponsePublic(
        id=r.id,
        inject_id=r.inject_id,
        exercise_id=r.exercise_id,
        user_id=r.user_id,
        group_id=r.group_id,
        content=r.content,
        selected_option=r.selected_option,
        submitted_at=r.submitted_at.isoformat(),
        assessment_id=r.assessment_id,
        next_injects=next_injects,
        next_inject_ids=(
            [item["scenario_node_id"] for item in next_injects]
            if next_injects is not None
            else None
        ),
        progression=progression,
    )
    return model.model_dump(mode="json")


def _next_inject_payload(inject: Inject) -> dict:
    return {
        "id": inject.id,
        "scenario_node_id": inject.scenario_node_id,
        "title": inject.title,
        "group_id": inject.group_id,
    }
