import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.exercise import Exercise, ExerciseMember, ExerciseState
from app.models.inject import Inject
from app.models.inject_comment import InjectComment
from app.models.response import Response
from app.models.user import User, UserRole
from app.schemas.api import ExercisePublic, MemberPublic
from app.services import audit_service
from app.services.access_control import (
    get_exercise_or_404,
    is_actual_facilitator,
    require_exercise_access,
)
from app.services.exercise_service import (
    create_exercise,
    enrol_member,
    remove_member,
    transition_state,
    update_member_group,
)
from app.services.scenario_service import get_scenario_definition

router = APIRouter(prefix="/exercises", tags=["exercises"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Request schemas ───────────────────────────────────────────────────────────

class CreateExerciseRequest(BaseModel):
    scenario_id: int
    title: str
    llm_enabled: bool = False


class UpdateExerciseRequest(BaseModel):
    title: str | None = None
    llm_enabled: bool | None = None


class EnrolMemberRequest(BaseModel):
    user_id: int
    group_id: str | None = None


class UpdateMemberRequest(BaseModel):
    group_id: str | None = None


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _exercise_out(ex: Exercise) -> dict:
    return ExercisePublic.from_model(ex).model_dump(mode="json")


def _member_out(m: ExerciseMember) -> dict:
    return MemberPublic.from_model(m).model_dump(mode="json")


async def _get_or_404(session: AsyncSession, exercise_id: int) -> Exercise:
    return await get_exercise_or_404(session, exercise_id)


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[ExercisePublic])
async def list_exercises(current_user: CurrentUserDep, session: SessionDep):
    q = select(Exercise)
    if current_user.role != UserRole.facilitator and not is_actual_facilitator(current_user):
        q = q.join(ExerciseMember).where(ExerciseMember.user_id == current_user.id)
    return [_exercise_out(ex) for ex in (await session.exec(q)).all()]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ExercisePublic)
async def create(body: CreateExerciseRequest, current_user: FacilitatorDep, session: SessionDep):
    assert current_user.id is not None
    ex = await create_exercise(
        session,
        scenario_id=body.scenario_id,
        title=body.title,
        created_by=current_user.id,
        llm_enabled=body.llm_enabled,
    )
    return _exercise_out(ex)


@router.get("/{exercise_id}", response_model=ExercisePublic)
async def get_exercise(exercise_id: int, current_user: CurrentUserDep, session: SessionDep):
    return _exercise_out(await require_exercise_access(session, exercise_id, current_user))


@router.get("/{exercise_id}/teams")
async def list_exercise_teams(exercise_id: int, current_user: CurrentUserDep, session: SessionDep):
    exercise = await require_exercise_access(session, exercise_id, current_user)
    definition = await get_scenario_definition(session, exercise.scenario_id)
    if not definition:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scenario not found")
    return [team.model_dump() for team in definition.participant_teams]


@router.put("/{exercise_id}", response_model=ExercisePublic)
async def update_exercise(
    exercise_id: int, body: UpdateExerciseRequest, _: FacilitatorDep, session: SessionDep
):
    ex = await _get_or_404(session, exercise_id)
    ex.sqlmodel_update(body.model_dump(exclude_unset=True))
    session.add(ex)
    await session.commit()
    await session.refresh(ex)
    return _exercise_out(ex)


@router.delete("/{exercise_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_exercise(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    ex = await _get_or_404(session, exercise_id)
    if ex.state != ExerciseState.draft:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only draft exercises can be deleted",
        )
    await session.delete(ex)
    await session.commit()
    audit_service.emit(
        "exercise.delete",
        actor=current_user,
        target_type="exercise",
        target_id=exercise_id,
        severity="warning",
    )


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def _transition(
    exercise_id: int, current_user: User, session: AsyncSession, target: ExerciseState, action: str
) -> dict:
    ex = await _get_or_404(session, exercise_id)
    result = await transition_state(session, ex, target)
    audit_service.emit(
        action, actor=current_user, target_type="exercise", target_id=exercise_id
    )
    return _exercise_out(result)


@router.post("/{exercise_id}/start", response_model=ExercisePublic)
async def start(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    return await _transition(
        exercise_id, current_user, session, ExerciseState.active, "exercise.start"
    )


@router.post("/{exercise_id}/pause", response_model=ExercisePublic)
async def pause(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    return await _transition(
        exercise_id, current_user, session, ExerciseState.paused, "exercise.pause"
    )


@router.post("/{exercise_id}/resume", response_model=ExercisePublic)
async def resume(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    return await _transition(
        exercise_id, current_user, session, ExerciseState.active, "exercise.resume"
    )


@router.post("/{exercise_id}/complete", response_model=ExercisePublic)
async def complete(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    return await _transition(
        exercise_id, current_user, session, ExerciseState.completed, "exercise.complete"
    )


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/{exercise_id}/members", response_model=list[MemberPublic])
async def list_members(exercise_id: int, current_user: CurrentUserDep, session: SessionDep):
    await require_exercise_access(session, exercise_id, current_user)
    members = (
        await session.exec(
            select(ExerciseMember).where(ExerciseMember.exercise_id == exercise_id)
        )
    ).all()
    return [_member_out(m) for m in members]


@router.post(
    "/{exercise_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=MemberPublic,
)
async def add_member(
    exercise_id: int, body: EnrolMemberRequest, current_user: FacilitatorDep, session: SessionDep
):
    ex = await _get_or_404(session, exercise_id)
    member = await enrol_member(session, exercise=ex, user_id=body.user_id, group_id=body.group_id)
    audit_service.emit(
        "member.enrol",
        actor=current_user,
        target_type="user",
        target_id=body.user_id,
        reason=f"exercise={exercise_id} group={body.group_id}",
    )
    return _member_out(member)


@router.patch("/{exercise_id}/members/{user_id}", response_model=MemberPublic)
async def patch_member(
    exercise_id: int,
    user_id: int,
    body: UpdateMemberRequest,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    ex = await _get_or_404(session, exercise_id)
    member = await update_member_group(
        session, exercise=ex, user_id=user_id, group_id=body.group_id
    )
    audit_service.emit(
        "member.group_change",
        actor=current_user,
        target_type="user",
        target_id=user_id,
        reason=f"exercise={exercise_id} group={body.group_id}",
    )
    return _member_out(member)


@router.delete("/{exercise_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_member(
    exercise_id: int, user_id: int, current_user: FacilitatorDep, session: SessionDep
):
    ex = await _get_or_404(session, exercise_id)
    await remove_member(session, exercise=ex, user_id=user_id)
    audit_service.emit(
        "member.remove",
        actor=current_user,
        target_type="user",
        target_id=user_id,
        reason=f"exercise={exercise_id}",
        severity="warning",
    )


# ── Export ────────────────────────────────────────────────────────────────────

# Exports use deliberately slim row projections (not the full *Public schemas) —
# a data dump, not the live API shape. Kept as named helpers so the projection is
# explicit and stays consistent across the JSON and CSV exports.

def _export_inject_row(i: Inject) -> dict:
    return {
        "id": i.id,
        "scenario_node_id": i.scenario_node_id,
        "title": i.title,
        "state": i.state,
        "group_id": i.group_id,
        "released_at": i.released_at.isoformat() if i.released_at else None,
    }


def _export_response_row(r: Response) -> dict:
    return {
        "id": r.id,
        "inject_id": r.inject_id,
        "user_id": r.user_id,
        "group_id": r.group_id,
        "content": r.content,
        "selected_option": r.selected_option,
        "submitted_at": r.submitted_at.isoformat(),
    }


def _export_comment_row(c: InjectComment) -> dict:
    return {
        "id": c.id,
        "inject_id": c.inject_id,
        "user_id": c.user_id,
        "group_id": c.group_id,
        "content": c.content,
        "created_at": c.created_at.isoformat(),
    }


async def _build_export(session: AsyncSession, exercise_id: int) -> dict:
    ex = await _get_or_404(session, exercise_id)
    injects = (
        await session.exec(select(Inject).where(Inject.exercise_id == exercise_id))
    ).all()
    responses = (
        await session.exec(select(Response).where(Response.exercise_id == exercise_id))
    ).all()
    comments = (
        await session.exec(select(InjectComment).where(InjectComment.exercise_id == exercise_id))
    ).all()
    members = (
        await session.exec(select(ExerciseMember).where(ExerciseMember.exercise_id == exercise_id))
    ).all()
    return {
        "exercise": _exercise_out(ex),
        "members": [_member_out(m) for m in members],
        "injects": [_export_inject_row(i) for i in injects],
        "responses": [_export_response_row(r) for r in responses],
        "inject_comments": [_export_comment_row(c) for c in comments],
    }


@router.get("/{exercise_id}/export")
async def export_json(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    data = await _build_export(session, exercise_id)
    audit_service.emit(
        "exercise.export",
        actor=current_user,
        target_type="exercise",
        target_id=exercise_id,
        reason="format=json",
        severity="warning",
    )
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f'attachment; filename="exercise_{exercise_id}.json"'},
    )


@router.get("/{exercise_id}/export.csv")
async def export_csv(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    data = await _build_export(session, exercise_id)
    audit_service.emit(
        "exercise.export",
        actor=current_user,
        target_type="exercise",
        target_id=exercise_id,
        reason="format=csv",
        severity="warning",
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    cols = ["inject_id", "inject_title", "user_id", "selected_option", "content", "submitted_at"]
    writer.writerow(cols)
    inject_map = {i["id"]: i["title"] for i in data["injects"]}
    for r in data["responses"]:
        writer.writerow([
            r["inject_id"],
            inject_map.get(r["inject_id"], ""),
            r["user_id"],
            r["selected_option"] or "",
            r["content"],
            r["submitted_at"],
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="exercise_{exercise_id}.csv"'},
    )
