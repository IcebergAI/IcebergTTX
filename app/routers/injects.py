import json
import re
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError
from sqlmodel import Session, select
from starlette.datastructures import UploadFile

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.exercise import ExerciseState
from app.models.inject import Inject
from app.models.user import User, UserRole
from app.services import audit_service
from app.services.access_control import (
    require_exercise_access,
    require_inject_visible,
)
from app.services.exercise_service import validate_group_id
from app.services.inject_service import (
    create_inject,
    get_inject_or_404,
    inject_attachment_payload,
    release_inject,
)

router = APIRouter(prefix="/exercises/{exercise_id}/injects", tags=["injects"])

ATTACHMENT_ROOT = Path("uploads/inject_attachments")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[Session, Depends(get_session)]


class CreateInjectRequest(BaseModel):
    title: str
    content: str
    scenario_node_id: str | None = None
    target_teams: list[str] | None = None
    group_id: str | None = None
    sequence_order: int = 0


def _inject_node(session: Session, inject: Inject):
    if not inject.scenario_node_id:
        return None
    from app.models.exercise import Exercise
    from app.models.scenario import Scenario
    from app.services.scenario_service import export_definition, get_inject_node

    exercise = session.get(Exercise, inject.exercise_id)
    if not exercise:
        return None
    scenario = session.get(Scenario, exercise.scenario_id)
    if not scenario:
        return None
    return get_inject_node(export_definition(scenario), inject.scenario_node_id)


def _inject_options(session: Session, inject: Inject) -> list[dict]:
    node = _inject_node(session, inject)
    if not node:
        return []
    return [
        {"id": option.id, "label": option.label, "next_inject_id": option.next_inject_id}
        for option in node.options
    ]


def _inject_out(inject: Inject, session: Session | None = None) -> dict:
    node = _inject_node(session, inject) if session is not None else None
    data = {
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
        "attachment": inject_attachment_payload(inject),
    }
    if session is not None:
        data["options"] = _inject_options(session, inject)
        data["next_inject_id"] = node.next_inject_id if node else None
        data["free_text_response"] = node.free_text_response if node else True
    return data


def _safe_filename(filename: str | None) -> str:
    original = Path(filename or "attachment").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", original).strip("._")
    return cleaned or "attachment"


def _parse_target_teams(raw: object) -> list[str] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, list):
        return [str(team).strip() for team in raw if str(team).strip()]
    text = str(raw).strip()
    if not text:
        return None
    if text.startswith("["):
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("target_teams must be a list")
        return [str(team).strip() for team in parsed if str(team).strip()]
    return [team.strip() for team in text.split(",") if team.strip()]


async def _request_body_and_attachment(
    request: Request,
) -> tuple[CreateInjectRequest, UploadFile | None]:
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        return CreateInjectRequest.model_validate(await request.json()), None

    form = await request.form()
    target_teams = [
        str(team).strip() for team in form.getlist("target_teams") if str(team).strip()
    ]
    if not target_teams:
        target_teams = _parse_target_teams(form.get("target_teams"))
    body = CreateInjectRequest(
        title=str(form.get("title") or ""),
        content=str(form.get("content") or ""),
        scenario_node_id=str(form.get("scenario_node_id") or "") or None,
        target_teams=target_teams or None,
        group_id=str(form.get("group_id") or "") or None,
        sequence_order=int(str(form.get("sequence_order") or "0")),
    )
    attachment = form.get("attachment")
    if not isinstance(attachment, UploadFile) or not attachment.filename:
        return body, None
    return body, attachment


async def _save_attachment(
    file: UploadFile | None,
    exercise_id: int,
) -> dict[str, str | int | None]:
    if file is None:
        return {}
    original_filename = Path(file.filename or "attachment").name
    safe_filename = _safe_filename(original_filename)
    storage_dir = ATTACHMENT_ROOT / str(exercise_id)
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{uuid4().hex}_{safe_filename}"
    data = await file.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Attachment is too large",
        )
    storage_path.write_bytes(data)
    await file.close()
    return {
        "attachment_filename": original_filename,
        "attachment_content_type": file.content_type or "application/octet-stream",
        "attachment_path": str(storage_path),
        "attachment_size": len(data),
    }


def _delete_attachment_file(inject: Inject) -> None:
    if not inject.attachment_path:
        return
    try:
        Path(inject.attachment_path).unlink(missing_ok=True)
    except OSError:
        pass


@router.get("")
def list_injects(exercise_id: int, current_user: CurrentUserDep, session: SessionDep):
    require_exercise_access(session, exercise_id, current_user)
    injects = session.exec(
        select(Inject)
        .where(Inject.exercise_id == exercise_id)
        .order_by(cast(Any, Inject.sequence_order))
    ).all()
    visible = [
        i
        for i in injects
        if current_user.role == UserRole.facilitator
        or require_visible_bool(session, i, current_user)
    ]
    return [_inject_out(i, session) for i in visible]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(
    exercise_id: int,
    request: Request,
    _: FacilitatorDep,
    session: SessionDep,
):
    try:
        body, attachment = await _request_body_and_attachment(request)
    except (json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    exercise = require_exercise_access(session, exercise_id, _)
    group_id = validate_group_id(session, exercise, body.group_id)
    if group_id is None and body.target_teams and len(body.target_teams) == 1:
        group_id = validate_group_id(session, exercise, body.target_teams[0])
    attachment_fields = await _save_attachment(attachment, exercise_id)
    inject = create_inject(
        session,
        exercise_id=exercise_id,
        title=body.title,
        content=body.content,
        scenario_node_id=body.scenario_node_id,
        target_teams=body.target_teams,
        group_id=group_id,
        sequence_order=body.sequence_order,
        **attachment_fields,
    )
    return _inject_out(inject, session)


@router.get("/{inject_id}")
def get_inject(exercise_id: int, inject_id: int, current_user: CurrentUserDep, session: SessionDep):
    require_exercise_access(session, exercise_id, current_user)
    inject = get_inject_or_404(session, exercise_id, inject_id)
    require_inject_visible(session, inject, current_user)
    return _inject_out(inject, session)


@router.delete("/{inject_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_inject(exercise_id: int, inject_id: int, _: FacilitatorDep, session: SessionDep):
    inject = get_inject_or_404(session, exercise_id, inject_id)
    _delete_attachment_file(inject)
    session.delete(inject)
    session.commit()
    audit_service.emit(
        "inject.delete",
        actor=_,
        target_type="inject",
        target_id=inject_id,
        reason=f"exercise={exercise_id}",
        severity="warning",
    )


@router.get("/{inject_id}/attachment")
def download_attachment(
    exercise_id: int,
    inject_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    require_exercise_access(session, exercise_id, current_user)
    inject = get_inject_or_404(session, exercise_id, inject_id)
    require_inject_visible(session, inject, current_user)
    if not inject.attachment_path or not inject.attachment_filename:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    path = Path(inject.attachment_path)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    return FileResponse(
        path,
        media_type=inject.attachment_content_type or "application/octet-stream",
        filename=inject.attachment_filename,
    )


@router.post("/{inject_id}/release")
async def release(
    exercise_id: int,
    inject_id: int,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    assert current_user.id is not None
    exercise = require_exercise_access(session, exercise_id, current_user)
    if exercise.state != ExerciseState.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only active exercises can release injects",
        )
    inject = get_inject_or_404(session, exercise_id, inject_id)
    released = await release_inject(session, inject, released_by=current_user.id)
    audit_service.emit(
        "inject.release",
        actor=current_user,
        target_type="inject",
        target_id=inject_id,
        reason=f"exercise={exercise_id}",
    )
    return _inject_out(released, session)


def require_visible_bool(session: Session, inject: Inject, user: User) -> bool:
    try:
        require_inject_visible(session, inject, user)
        return True
    except HTTPException:
        return False
