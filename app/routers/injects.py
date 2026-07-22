import json
import re
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ValidationError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.datastructures import UploadFile

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.exercise import ExerciseState
from app.models.inject import Inject, InjectState
from app.models.user import User, UserRole
from app.schemas.api import InjectPublic
from app.services import audit_service
from app.services.access_control import (
    require_exercise_access,
    require_exercise_owner,
    require_inject_visible,
    require_operational_mutability,
)
from app.services.domain_events import InjectUpdated, dispatch, record
from app.services.exercise_service import validate_group_id, validate_team_ids
from app.services.inject_service import (
    AttachmentMeta,
    create_inject,
    get_inject_or_404,
    inject_payload,
    release_inject,
)
from app.services.schedule_service import arm_inject_schedule, cancel_inject_schedule

router = APIRouter(prefix="/exercises/{exercise_id}/injects", tags=["injects"])

ATTACHMENT_ROOT = Path("uploads/inject_attachments")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
ATTACHMENT_CHUNK_BYTES = 1024 * 1024

# Content-type allowlist for inject attachments (#16). An uploaded type outside
# this set is stored/served as the safe default so the download response can
# never carry an attacker-chosen renderable type (e.g. text/html, image/svg+xml).
DEFAULT_ATTACHMENT_TYPE = "application/octet-stream"
ALLOWED_ATTACHMENT_TYPES = frozenset({
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/gif",
    "text/plain",
    "text/csv",
    "application/json",
    "application/zip",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
})


def _normalize_content_type(content_type: str | None) -> str:
    """Confine a stored attachment type to the allowlist (#16)."""
    base = content_type.split(";", 1)[0].strip().lower() if content_type else ""
    return base if base in ALLOWED_ATTACHMENT_TYPES else DEFAULT_ATTACHMENT_TYPE

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class CreateInjectRequest(BaseModel):
    title: str
    content: str
    scenario_node_id: str | None = None
    target_teams: list[str] | None = None
    group_id: str | None = None
    sequence_order: int = 0


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
) -> AttachmentMeta | None:
    if file is None:
        return None
    original_filename = Path(file.filename or "attachment").name
    safe_filename = _safe_filename(original_filename)
    storage_dir = ATTACHMENT_ROOT / str(exercise_id)
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / f"{uuid4().hex}_{safe_filename}"
    # Stream to disk in chunks and abort as soon as the running total exceeds the
    # cap, so an oversized upload is never fully buffered in memory (#39).
    size = 0
    try:
        with storage_path.open("wb") as out:
            while chunk := await file.read(ATTACHMENT_CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_ATTACHMENT_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="Attachment is too large",
                    )
                out.write(chunk)
    except HTTPException:
        storage_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return AttachmentMeta(
        filename=original_filename,
        content_type=_normalize_content_type(file.content_type),
        path=str(storage_path),
        size=size,
    )


def _delete_attachment_path(attachment_path: str | None) -> None:
    if not attachment_path:
        return
    path = Path(attachment_path)
    try:
        root = ATTACHMENT_ROOT.resolve()
        resolved = path.resolve()
        if root not in resolved.parents:
            raise ValueError("attachment path escapes storage root")
        resolved.unlink(missing_ok=True)
        # Keep the hierarchy tidy after successful post-commit deletion; failure is
        # harmless and reconciliation can retry later.
        resolved.parent.rmdir()
    except (OSError, ValueError):
        pass


def _delete_attachment_file(inject: Inject) -> None:
    _delete_attachment_path(inject.attachment_path)


@router.get("", response_model=list[InjectPublic])
async def list_injects(exercise_id: int, current_user: CurrentUserDep, session: SessionDep):
    await require_exercise_access(session, exercise_id, current_user)
    injects = (
        await session.exec(
            select(Inject)
            .where(Inject.exercise_id == exercise_id)
            .order_by(cast(Any, Inject.sequence_order))
        )
    ).all()
    is_facilitator = current_user.role == UserRole.facilitator
    visible = [
        i
        for i in injects
        if is_facilitator or await require_visible_bool(session, i, current_user)
    ]
    # Only facilitators get branch topology (next_inject_id); participants/observers
    # get the redacted payload so they can't read the branch map ahead of choosing (#266).
    return [await inject_payload(session, i, include_progression=is_facilitator) for i in visible]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InjectPublic)
async def create(
    exercise_id: int,
    request: Request,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    try:
        body, attachment = await _request_body_and_attachment(request)
    except (json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    exercise = await require_exercise_access(session, exercise_id, current_user)
    require_operational_mutability(exercise)
    target_teams = await validate_team_ids(
        session, exercise, body.target_teams, field="target_teams"
    )
    group_id = await validate_group_id(session, exercise, body.group_id)
    if group_id is None and target_teams and len(target_teams) == 1:
        group_id = target_teams[0]
    attachment_meta = await _save_attachment(attachment, exercise_id)
    try:
        inject = await create_inject(
            session,
            exercise_id=exercise_id,
            title=body.title,
            content=body.content,
            scenario_node_id=body.scenario_node_id,
            target_teams=target_teams,
            group_id=group_id,
            sequence_order=body.sequence_order,
            attachment=attachment_meta,
        )
    except Exception:
        if attachment_meta:
            _delete_attachment_path(attachment_meta.path)
        raise
    return await inject_payload(session, inject, include_progression=True)


@router.get("/{inject_id}", response_model=InjectPublic)
async def get_inject(
    exercise_id: int, inject_id: int, current_user: CurrentUserDep, session: SessionDep
):
    await require_exercise_access(session, exercise_id, current_user)
    inject = await get_inject_or_404(session, exercise_id, inject_id)
    await require_inject_visible(session, inject, current_user)
    return await inject_payload(
        session, inject, include_progression=current_user.role == UserRole.facilitator
    )


@router.delete("/{inject_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_inject(
    exercise_id: int, inject_id: int, current_user: FacilitatorDep, session: SessionDep
):
    exercise = await require_exercise_owner(session, exercise_id, current_user)
    require_operational_mutability(exercise)
    inject = await get_inject_or_404(session, exercise_id, inject_id)
    await session.delete(inject)
    await session.commit()
    _delete_attachment_file(inject)
    audit_service.emit(
        "inject.delete",
        actor=current_user,
        target_type="inject",
        target_id=inject_id,
        reason=f"exercise={exercise_id}",
        severity="warning",
    )


@router.get("/{inject_id}/attachment")
async def download_attachment(
    exercise_id: int,
    inject_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    await require_exercise_access(session, exercise_id, current_user)
    inject = await get_inject_or_404(session, exercise_id, inject_id)
    await require_inject_visible(session, inject, current_user)
    if not inject.attachment_path or not inject.attachment_filename:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    path = Path(inject.attachment_path)
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    return FileResponse(
        path,
        media_type=_normalize_content_type(inject.attachment_content_type),
        filename=inject.attachment_filename,
        # Prevent content sniffing / inline rendering of the served type (#16).
        # Content-Disposition: attachment is already implied by `filename`.
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/{inject_id}/release", response_model=InjectPublic)
async def release(
    exercise_id: int,
    inject_id: int,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    assert current_user.id is not None
    exercise = await require_exercise_access(session, exercise_id, current_user)
    require_operational_mutability(exercise)
    if exercise.state != ExerciseState.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only active exercises can release injects",
        )
    inject = await get_inject_or_404(session, exercise_id, inject_id)
    released = await release_inject(session, inject, released_by=current_user.id)
    audit_service.emit(
        "inject.release",
        actor=current_user,
        target_type="inject",
        target_id=inject_id,
        reason=f"exercise={exercise_id}",
    )
    return await inject_payload(session, released, include_progression=True)


class UpdateScheduleRequest(BaseModel):
    # Minutes after exercise start; null clears the schedule (back to manual-only).
    release_offset_minutes: int | None = None


@router.patch("/{inject_id}/schedule", response_model=InjectPublic)
async def update_schedule(
    exercise_id: int,
    inject_id: int,
    body: UpdateScheduleRequest,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    """Set, change, or clear a pending inject's scheduled-release offset (#116).

    Same authz as manual release. Only pending injects can be (re)scheduled; a running
    exercise re-arms the timer immediately, a paused/draft one just persists (start or
    resume will arm it). "Pull not push" is preserved — the facilitator can still release
    early or clear the schedule at any time.
    """
    if body.release_offset_minutes is not None and body.release_offset_minutes < 0:
        raise HTTPException(status_code=422, detail="release_offset_minutes must be >= 0")
    exercise = await require_exercise_access(session, exercise_id, current_user)
    require_operational_mutability(exercise)
    inject = await get_inject_or_404(session, exercise_id, inject_id)
    if inject.state != InjectState.pending:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Inject is '{inject.state}', cannot schedule",
        )
    inject.release_offset_minutes = body.release_offset_minutes
    session.add(inject)
    record(session, InjectUpdated(exercise_id=exercise_id, inject=inject))
    await session.commit()
    await session.refresh(inject)

    # Re-arm the in-memory timer to match the new value (only affects a running exercise;
    # start/resume arm the rest). Cancel first so a cleared/edited offset can't double-fire.
    cancel_inject_schedule(exercise_id, inject_id)
    arm_inject_schedule(exercise, inject)

    audit_service.emit(
        "inject.schedule",
        actor=current_user,
        target_type="inject",
        target_id=inject_id,
        reason=f"exercise={exercise_id} offset={body.release_offset_minutes}",
    )
    await dispatch(session)
    return await inject_payload(session, inject, include_progression=True)


async def require_visible_bool(session: AsyncSession, inject: Inject, user: User) -> bool:
    try:
        await require_inject_visible(session, inject, user)
        return True
    except HTTPException:
        return False
