import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.exercise import Exercise, ExerciseMember, ExerciseState
from app.models.inject import Inject
from app.models.response import Response
from app.models.user import User, UserRole
from app.services.exercise_service import (
    create_exercise,
    enrol_member,
    remove_member,
    transition_state,
)

router = APIRouter(prefix="/exercises", tags=["exercises"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[Session, Depends(get_session)]


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


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _exercise_out(ex: Exercise) -> dict:
    return {
        "id": ex.id,
        "scenario_id": ex.scenario_id,
        "title": ex.title,
        "state": ex.state,
        "current_node_id": ex.current_node_id,
        "llm_enabled": ex.llm_enabled,
        "started_at": ex.started_at.isoformat() if ex.started_at else None,
        "ended_at": ex.ended_at.isoformat() if ex.ended_at else None,
        "created_by": ex.created_by,
        "created_at": ex.created_at.isoformat(),
    }


def _member_out(m: ExerciseMember) -> dict:
    return {
        "id": m.id,
        "exercise_id": m.exercise_id,
        "user_id": m.user_id,
        "joined_at": m.joined_at.isoformat(),
    }


def _get_or_404(session: Session, exercise_id: int) -> Exercise:
    ex = session.get(Exercise, exercise_id)
    if not ex:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exercise not found")
    return ex


# ── CRUD ──────────────────────────────────────────────────────────────────────

@router.get("")
def list_exercises(_: CurrentUserDep, session: SessionDep):
    return [_exercise_out(ex) for ex in session.exec(select(Exercise)).all()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: CreateExerciseRequest, current_user: FacilitatorDep, session: SessionDep):
    ex = create_exercise(
        session,
        scenario_id=body.scenario_id,
        title=body.title,
        created_by=current_user.id,
        llm_enabled=body.llm_enabled,
    )
    return _exercise_out(ex)


@router.get("/{exercise_id}")
def get_exercise(exercise_id: int, _: CurrentUserDep, session: SessionDep):
    return _exercise_out(_get_or_404(session, exercise_id))


@router.put("/{exercise_id}")
def update_exercise(
    exercise_id: int, body: UpdateExerciseRequest, _: FacilitatorDep, session: SessionDep
):
    ex = _get_or_404(session, exercise_id)
    if body.title is not None:
        ex.title = body.title
    if body.llm_enabled is not None:
        ex.llm_enabled = body.llm_enabled
    session.add(ex)
    session.commit()
    session.refresh(ex)
    return _exercise_out(ex)


@router.delete("/{exercise_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_exercise(exercise_id: int, _: FacilitatorDep, session: SessionDep):
    ex = _get_or_404(session, exercise_id)
    if ex.state != ExerciseState.draft:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only draft exercises can be deleted",
        )
    session.delete(ex)
    session.commit()


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@router.post("/{exercise_id}/start")
def start(exercise_id: int, _: FacilitatorDep, session: SessionDep):
    ex = _get_or_404(session, exercise_id)
    return _exercise_out(transition_state(session, ex, ExerciseState.active))


@router.post("/{exercise_id}/pause")
def pause(exercise_id: int, _: FacilitatorDep, session: SessionDep):
    ex = _get_or_404(session, exercise_id)
    return _exercise_out(transition_state(session, ex, ExerciseState.paused))


@router.post("/{exercise_id}/resume")
def resume(exercise_id: int, _: FacilitatorDep, session: SessionDep):
    ex = _get_or_404(session, exercise_id)
    return _exercise_out(transition_state(session, ex, ExerciseState.active))


@router.post("/{exercise_id}/complete")
def complete(exercise_id: int, _: FacilitatorDep, session: SessionDep):
    ex = _get_or_404(session, exercise_id)
    return _exercise_out(transition_state(session, ex, ExerciseState.completed))


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/{exercise_id}/members")
def list_members(exercise_id: int, _: CurrentUserDep, session: SessionDep):
    _get_or_404(session, exercise_id)
    members = session.exec(
        select(ExerciseMember).where(ExerciseMember.exercise_id == exercise_id)
    ).all()
    return [_member_out(m) for m in members]


@router.post("/{exercise_id}/members", status_code=status.HTTP_201_CREATED)
def add_member(
    exercise_id: int, body: EnrolMemberRequest, _: FacilitatorDep, session: SessionDep
):
    ex = _get_or_404(session, exercise_id)
    member = enrol_member(session, exercise=ex, user_id=body.user_id)
    return _member_out(member)


@router.delete("/{exercise_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_member(exercise_id: int, user_id: int, _: FacilitatorDep, session: SessionDep):
    ex = _get_or_404(session, exercise_id)
    remove_member(session, exercise=ex, user_id=user_id)


# ── Export ────────────────────────────────────────────────────────────────────

def _build_export(session: Session, exercise_id: int) -> dict:
    ex = _get_or_404(session, exercise_id)
    injects = session.exec(
        select(Inject).where(Inject.exercise_id == exercise_id)
    ).all()
    responses = session.exec(
        select(Response).where(Response.exercise_id == exercise_id)
    ).all()
    members = session.exec(
        select(ExerciseMember).where(ExerciseMember.exercise_id == exercise_id)
    ).all()
    return {
        "exercise": _exercise_out(ex),
        "members": [_member_out(m) for m in members],
        "injects": [
            {
                "id": i.id,
                "scenario_node_id": i.scenario_node_id,
                "title": i.title,
                "state": i.state,
                "released_at": i.released_at.isoformat() if i.released_at else None,
            }
            for i in injects
        ],
        "responses": [
            {
                "id": r.id,
                "inject_id": r.inject_id,
                "user_id": r.user_id,
                "content": r.content,
                "selected_option": r.selected_option,
                "submitted_at": r.submitted_at.isoformat(),
            }
            for r in responses
        ],
    }


@router.get("/{exercise_id}/export")
def export_json(exercise_id: int, _: FacilitatorDep, session: SessionDep):
    data = _build_export(session, exercise_id)
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f'attachment; filename="exercise_{exercise_id}.json"'},
    )


@router.get("/{exercise_id}/export.csv")
def export_csv(exercise_id: int, _: FacilitatorDep, session: SessionDep):
    data = _build_export(session, exercise_id)
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
