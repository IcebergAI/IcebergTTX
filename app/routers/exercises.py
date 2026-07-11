import csv
import io
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.exercise import Exercise, ExerciseMember, ExerciseState
from app.models.inject import Inject
from app.models.inject_comment import InjectComment
from app.models.report_summary import ExecutiveSummary
from app.models.response import Response
from app.models.scenario import Scenario
from app.models.user import User, UserRole
from app.schemas.api import (
    DebriefNotes,
    ExecutiveSummaryPublic,
    ExerciseProgressionPublic,
    ExercisePublic,
    ExerciseStateChange,
    MemberPublic,
    ReportSummaryState,
    TimelineEvent,
)
from app.services import audit_service
from app.services.access_control import (
    exercise_group_for_user,
    is_actual_facilitator,
    is_admin,
    require_exercise_access,
    require_exercise_owner,
)
from app.services.background import spawn
from app.services.exercise_service import (
    create_exercise,
    enrol_member,
    remove_member,
    transition_state_with_history,
    update_member_group,
)
from app.services.llm.service import active_provider
from app.services.llm_service import run_summary_pipeline
from app.services.progression_service import progression_snapshot
from app.services.report_service import build_report, render_markdown
from app.services.scenario_service import get_scenario_definition
from app.services.schedule_service import (
    cancel_exercise_schedules,
    schedule_exercise_injects,
)
from app.services.timeline_service import build_timeline
from app.services.ws_manager import manager

router = APIRouter(prefix="/exercises", tags=["exercises"])
logger = logging.getLogger(__name__)

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
    debrief_notes: str | None = None  # #112 — owner-only, editable in any live state


class UpdateSummaryRequest(BaseModel):
    summary_text: str


class EnrolMemberRequest(BaseModel):
    user_id: int
    group_id: str | None = None


class UpdateMemberRequest(BaseModel):
    group_id: str | None = None


# ── Serialisation helpers ─────────────────────────────────────────────────────


def _exercise_out(ex: Exercise, scenario_title: str | None = None) -> dict:
    return ExercisePublic.from_model(ex, scenario_title).model_dump(mode="json")


async def _scenario_titles(session: AsyncSession, exercises: list[Exercise]) -> dict[int, str]:
    """id → title for the scenarios behind ``exercises`` — one query, not N+1."""
    scenario_ids = {ex.scenario_id for ex in exercises if ex.scenario_id is not None}
    if not scenario_ids:
        return {}
    rows = await session.exec(
        select(Scenario.id, Scenario.title).where(col(Scenario.id).in_(scenario_ids))
    )
    return {scenario_id: title for scenario_id, title in rows.all() if scenario_id is not None}


def _member_out(m: ExerciseMember) -> dict:
    return MemberPublic.from_model(m).model_dump(mode="json")


# ── CRUD ──────────────────────────────────────────────────────────────────────


@router.get("", response_model=list[ExercisePublic])
async def list_exercises(current_user: CurrentUserDep, session: SessionDep):
    assert current_user.id is not None
    q = select(Exercise)
    if is_admin(current_user):
        pass  # admins see every exercise
    elif current_user.role == UserRole.facilitator or is_actual_facilitator(current_user):
        # Facilitators see exercises they created plus any they co-facilitate (are a
        # member of) — scoped per-exercise (#12), no longer a global view.
        member_ids = select(ExerciseMember.exercise_id).where(
            ExerciseMember.user_id == current_user.id
        )
        q = q.where(
            or_(col(Exercise.created_by) == current_user.id, col(Exercise.id).in_(member_ids))
        )
    else:
        q = q.join(ExerciseMember).where(ExerciseMember.user_id == current_user.id)
    # Deterministic order (#96). Without it Postgres returns heap order, which shifts
    # whenever a row is rewritten (every start/pause/resume UPDATEs the row) — so the
    # client's "first active exercise" could change with no user action. Most-recently
    # started first; drafts (started_at IS NULL) sink below anything that has ever run;
    # id DESC is a total-order tiebreaker so equal timestamps can never swap.
    q = q.order_by(col(Exercise.started_at).desc().nulls_last(), col(Exercise.id).desc())
    exercises = list((await session.exec(q)).all())
    titles = await _scenario_titles(session, exercises)
    return [_exercise_out(ex, titles.get(ex.scenario_id)) for ex in exercises]


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


@router.get("/{exercise_id}/progression", response_model=ExerciseProgressionPublic)
async def get_exercise_progression(
    exercise_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    await require_exercise_access(session, exercise_id, current_user)
    participant_view = current_user.role == UserRole.participant
    group_id = (
        await exercise_group_for_user(session, exercise_id, current_user)
        if participant_view
        else None
    )
    return await progression_snapshot(
        session,
        exercise_id,
        group_id=group_id,
        include_all_groups=not participant_view,
    )


@router.get("/{exercise_id}/teams")
async def list_exercise_teams(exercise_id: int, current_user: CurrentUserDep, session: SessionDep):
    exercise = await require_exercise_access(session, exercise_id, current_user)
    definition = await get_scenario_definition(session, exercise.scenario_id)
    if not definition:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scenario not found")
    return [team.model_dump() for team in definition.participant_teams]


@router.put("/{exercise_id}", response_model=ExercisePublic)
async def update_exercise(
    exercise_id: int,
    body: UpdateExerciseRequest,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    ex = await require_exercise_owner(session, exercise_id, current_user)
    ex.sqlmodel_update(body.model_dump(exclude_unset=True))
    session.add(ex)
    await session.commit()
    await session.refresh(ex)
    return _exercise_out(ex)


@router.delete("/{exercise_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_exercise(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    ex = await require_exercise_owner(session, exercise_id, current_user)
    if ex.state != ExerciseState.draft:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only draft exercises can be deleted",
        )
    attachment_paths = list(
        (
            await session.exec(
                select(Inject.attachment_path).where(
                    Inject.exercise_id == exercise_id,
                    col(Inject.attachment_path).is_not(None),
                )
            )
        ).all()
    )
    await session.delete(ex)
    await session.commit()
    # The database cascade is authoritative. Files are removed only after it
    # commits, so a failed transaction cannot leave live rows with broken links.
    from app.routers.injects import _delete_attachment_path

    for attachment_path in attachment_paths:
        _delete_attachment_path(attachment_path)
    audit_service.emit(
        "exercise.delete",
        actor=current_user,
        target_type="exercise",
        target_id=exercise_id,
        severity="warning",
    )


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def _transition(
    exercise_id: int, current_user: User, session: AsyncSession, target: ExerciseState
) -> dict:
    assert current_user.id is not None
    ex = await require_exercise_owner(session, exercise_id, current_user)
    result = await transition_state_with_history(session, ex, target, actor_id=current_user.id)
    audit_service.emit(
        result.action,
        actor=current_user,
        target_type="exercise",
        target_id=exercise_id,
    )

    transition = result.transition
    assert transition.id is not None
    payload = ExerciseStateChange(
        transition_id=transition.id,
        exercise_id=exercise_id,
        previous_state=transition.from_state,
        new_state=transition.to_state,
        state=transition.to_state,
        actor_id=transition.actor_id,
        transitioned_at=transition.transitioned_at.isoformat(),
        started_at=(result.exercise.started_at.isoformat() if result.exercise.started_at else None),
        ended_at=result.exercise.ended_at.isoformat() if result.exercise.ended_at else None,
        paused_at=(result.exercise.paused_at.isoformat() if result.exercise.paused_at else None),
        accumulated_pause_seconds=result.exercise.accumulated_pause_seconds,
    ).model_dump(mode="json")
    try:
        await manager.broadcast_to_exercise(
            exercise_id,
            {
                "type": "exercise_state_change",
                "exercise_id": exercise_id,
                "timestamp": transition.transitioned_at.isoformat(),
                "payload": payload,
            },
        )
    except Exception:
        # The database transition is already committed and remains authoritative.
        # One dead socket is handled by ConnectionManager; this guard covers an
        # unexpected manager failure without misreporting the committed request.
        logger.exception("failed to broadcast exercise lifecycle transition %d", transition.id)
    # Start/resume arm pending timers; pause/complete cancel them. This runs only
    # after the atomic state/history transaction and canonical WS projection.
    if target == ExerciseState.active:
        await schedule_exercise_injects(session, result.exercise)
    else:
        cancel_exercise_schedules(exercise_id)
    return _exercise_out(result.exercise)


@router.post("/{exercise_id}/start", response_model=ExercisePublic)
async def start(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    return await _transition(exercise_id, current_user, session, ExerciseState.active)


@router.post("/{exercise_id}/pause", response_model=ExercisePublic)
async def pause(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    return await _transition(exercise_id, current_user, session, ExerciseState.paused)


@router.post("/{exercise_id}/resume", response_model=ExercisePublic)
async def resume(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    return await _transition(exercise_id, current_user, session, ExerciseState.active)


@router.post("/{exercise_id}/complete", response_model=ExercisePublic)
async def complete(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    return await _transition(exercise_id, current_user, session, ExerciseState.completed)


# ── Members ───────────────────────────────────────────────────────────────────


@router.get("/{exercise_id}/members", response_model=list[MemberPublic])
async def list_members(exercise_id: int, current_user: CurrentUserDep, session: SessionDep):
    await require_exercise_access(session, exercise_id, current_user)
    members = (
        await session.exec(select(ExerciseMember).where(ExerciseMember.exercise_id == exercise_id))
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
    ex = await require_exercise_owner(session, exercise_id, current_user)
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
    ex = await require_exercise_owner(session, exercise_id, current_user)
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
    ex = await require_exercise_owner(session, exercise_id, current_user)
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
        "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
        "resolved_by": i.resolved_by,
        "resolution_reason": i.resolution_reason,
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


async def _build_export(session: AsyncSession, exercise_id: int, current_user: User) -> dict:
    ex = await require_exercise_owner(session, exercise_id, current_user)
    injects = (await session.exec(select(Inject).where(Inject.exercise_id == exercise_id))).all()
    responses = (
        await session.exec(select(Response).where(Response.exercise_id == exercise_id))
    ).all()
    comments = (
        await session.exec(select(InjectComment).where(InjectComment.exercise_id == exercise_id))
    ).all()
    members = (
        await session.exec(select(ExerciseMember).where(ExerciseMember.exercise_id == exercise_id))
    ).all()
    definition = await get_scenario_definition(session, ex.scenario_id)
    return {
        "exercise": _exercise_out(ex),
        # Debrief notes (#112) — owner-only export path, so safe to include here even
        # though they're kept out of the participant-visible ExercisePublic.
        "debrief": {
            "scenario_debrief_notes": definition.debrief_notes if definition else None,
            "debrief_notes": ex.debrief_notes,
        },
        "members": [_member_out(m) for m in members],
        "injects": [_export_inject_row(i) for i in injects],
        "responses": [_export_response_row(r) for r in responses],
        "inject_comments": [_export_comment_row(c) for c in comments],
    }


@router.get("/{exercise_id}/export")
async def export_json(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    data = await _build_export(session, exercise_id, current_user)
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


@router.get("/{exercise_id}/timeline", response_model=list[TimelineEvent])
async def exercise_timeline(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    """Merged, chronological event feed for the exercise (facilitator-owner only, #111)."""
    await require_exercise_owner(session, exercise_id, current_user)
    return await build_timeline(session, exercise_id)


@router.get("/{exercise_id}/debrief", response_model=DebriefNotes)
async def get_debrief(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    """Owner-only debrief notes (#112): the scenario author's read-only talking points
    plus the editable exercise-level notes. Never exposed to participants/observers."""
    ex = await require_exercise_owner(session, exercise_id, current_user)
    definition = await get_scenario_definition(session, ex.scenario_id)
    return DebriefNotes(
        exercise_id=exercise_id,
        scenario_debrief_notes=definition.debrief_notes if definition else None,
        debrief_notes=ex.debrief_notes,
    )


# ── After-action report (#113) ────────────────────────────────────────────────


def _summary_public(s: ExecutiveSummary) -> ExecutiveSummaryPublic:
    return ExecutiveSummaryPublic(
        exercise_id=s.exercise_id,
        summary_text=s.summary_text,
        llm_model=s.llm_model,
        edited=s.edited,
        generated_at=s.generated_at.isoformat(),
    )


async def _get_summary_row(session: AsyncSession, exercise_id: int) -> ExecutiveSummary | None:
    return (
        await session.exec(
            select(ExecutiveSummary).where(ExecutiveSummary.exercise_id == exercise_id)
        )
    ).first()


@router.get("/{exercise_id}/report")
async def report_json(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    """Structured after-action report data (facilitator-owner only, #113). Rendered by
    the print-friendly HTML view and available for programmatic use."""
    await require_exercise_owner(session, exercise_id, current_user)
    report = await build_report(session, exercise_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exercise not found")
    return report


@router.get("/{exercise_id}/report.md")
async def report_markdown(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    """Markdown after-action report download (facilitator-owner only, #113)."""
    await require_exercise_owner(session, exercise_id, current_user)
    report = await build_report(session, exercise_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exercise not found")
    audit_service.emit(
        "exercise.export",
        actor=current_user,
        target_type="exercise",
        target_id=exercise_id,
        reason="format=report.md",
        severity="warning",
    )
    return PlainTextResponse(
        render_markdown(report),
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="report_{exercise_id}.md"',
        },
    )


@router.get("/{exercise_id}/report/summary", response_model=ReportSummaryState)
async def get_report_summary(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    """Current executive summary + whether AI drafting is available (#113)."""
    ex = await require_exercise_owner(session, exercise_id, current_user)
    row = await _get_summary_row(session, exercise_id)
    available = active_provider() is not None and ex.llm_enabled
    return ReportSummaryState(available=available, summary=_summary_public(row) if row else None)


@router.post("/{exercise_id}/report/summary", status_code=status.HTTP_202_ACCEPTED)
async def draft_report_summary(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    """Kick off an LLM draft of the executive summary (#113). Gated on both a
    configured provider AND the exercise's own AI opt-in — 409 if either is off."""
    ex = await require_exercise_owner(session, exercise_id, current_user)
    if active_provider() is None or not ex.llm_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="AI summary unavailable: no provider configured or exercise AI disabled",
        )
    spawn(run_summary_pipeline(exercise_id))
    return {"status": "accepted"}


@router.patch("/{exercise_id}/report/summary", response_model=ExecutiveSummaryPublic)
async def edit_report_summary(
    exercise_id: int,
    body: UpdateSummaryRequest,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    """Facilitator edits the drafted summary before it lands in the report (#113)."""
    await require_exercise_owner(session, exercise_id, current_user)
    row = await _get_summary_row(session, exercise_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No summary to edit")
    row.summary_text = body.summary_text
    row.edited = True
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _summary_public(row)


@router.get("/{exercise_id}/export.csv")
async def export_csv(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    data = await _build_export(session, exercise_id, current_user)
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
        writer.writerow(
            [
                r["inject_id"],
                inject_map.get(r["inject_id"], ""),
                r["user_id"],
                r["selected_option"] or "",
                r["content"],
                r["submitted_at"],
            ]
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="exercise_{exercise_id}.csv"'},
    )
