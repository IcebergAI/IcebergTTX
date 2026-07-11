"""After-action report assembly (#113).

Builds one structured report object from the tables an exercise already fills —
released injects and their responses (with LLM decision quality), communications,
debrief notes, and the optional LLM executive summary — reused by both the Markdown
download and the print-friendly HTML view. Ownership is checked by the caller.

Sections with no data are returned as empty lists / ``None`` so the renderers can
collapse them gracefully.
"""

from datetime import datetime

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.assessment import ResponseAssessment
from app.models.communication import Communication
from app.models.exercise import Exercise, ExerciseMember
from app.models.inject import Inject, InjectProgress
from app.models.report_summary import ExecutiveSummary
from app.models.response import Response
from app.models.scenario import Scenario
from app.models.user import User, UserRole
from app.services.scenario_service import export_definition


def _fmt(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _duration(started: datetime | None, ended: datetime | None) -> str | None:
    if not (started and ended):
        return None
    total = int((ended - started).total_seconds())
    if total < 0:
        return None
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


async def build_report(session: AsyncSession, exercise_id: int) -> dict | None:
    """Assemble the structured after-action report for ``exercise_id``.

    Returns ``None`` if the exercise is missing. Caller must have checked ownership.
    """
    exercise = await session.get(Exercise, exercise_id)
    if exercise is None:
        return None

    scenario = await session.get(Scenario, exercise.scenario_id)
    definition = export_definition(scenario) if scenario else None

    # Name resolution for released_by / response authors.
    users = (await session.exec(select(User))).all()
    user_name = {u.id: (u.display_name or u.email) for u in users}

    def name(uid: int | None) -> str:
        if uid is None:
            return "system"
        return user_name.get(uid, f"User #{uid}")

    members = (
        await session.exec(select(ExerciseMember).where(ExerciseMember.exercise_id == exercise_id))
    ).all()

    role_counts = {role: 0 for role in UserRole}
    for member in members:
        role_counts[member.role_at_enrolment] += 1

    participant_team_counts = {
        team.id: 0 for team in (definition.participant_teams if definition else [])
    }
    unassigned_participant_count = 0
    for member in members:
        if member.role_at_enrolment != UserRole.participant:
            continue
        if member.group_id in participant_team_counts:
            participant_team_counts[member.group_id] += 1
        else:
            # Includes explicitly unassigned participants and legacy/removed team IDs.
            # Keeping this bucket explicit guarantees that the breakdown always sums
            # to participant_count.
            unassigned_participant_count += 1

    # Released injects, ordered as released, each with its responses + decision quality.
    injects = (
        await session.exec(
            select(Inject)
            .where(Inject.exercise_id == exercise_id, col(Inject.released_at).is_not(None))
            .order_by(col(Inject.released_at))
        )
    ).all()
    responses = (
        await session.exec(select(Response).where(Response.exercise_id == exercise_id))
    ).all()
    quality = {}
    if responses:
        assessments = (
            await session.exec(
                select(ResponseAssessment).where(
                    col(ResponseAssessment.response_id).in_([r.id for r in responses])
                )
            )
        ).all()
        quality = {a.response_id: a for a in assessments}

    responses_by_inject: dict[int, list] = {}
    for r in responses:
        responses_by_inject.setdefault(r.inject_id, []).append(r)

    resolution_rows = (
        await session.exec(select(InjectProgress).where(InjectProgress.exercise_id == exercise_id))
    ).all()
    resolutions_by_inject: dict[int, list[InjectProgress]] = {}
    for resolution in resolution_rows:
        resolutions_by_inject.setdefault(resolution.inject_id, []).append(resolution)

    inject_rows = []
    for i in injects:
        assert i.id is not None
        rows = []
        for r in sorted(responses_by_inject.get(i.id, []), key=lambda x: x.submitted_at):
            assert r.id is not None
            a = quality.get(r.id)
            rows.append(
                {
                    "author": name(r.user_id),
                    "group_id": r.group_id,
                    "selected_option": r.selected_option,
                    "content": r.content,
                    "decision_quality": a.decision_quality if a else None,
                    "assessment_text": a.assessment_text if a else None,
                    "submitted_at": _fmt(r.submitted_at),
                }
            )
        inject_rows.append(
            {
                "title": i.title,
                "scenario_node_id": i.scenario_node_id,
                "content": i.content,
                "target_teams": i.target_teams,  # None = all teams
                "released_at": _fmt(i.released_at),
                "released_by": name(i.released_by),
                "resolutions": [
                    {
                        "group_id": resolution.group_id,
                        "state": resolution.state,
                        "resolved_at": _fmt(resolution.resolved_at),
                        "resolved_by": name(resolution.resolved_by),
                        "resolution_reason": resolution.resolution_reason,
                    }
                    for resolution in resolutions_by_inject.get(i.id, [])
                ],
                "responses": rows,
            }
        )

    comms = (
        await session.exec(
            select(Communication)
            .where(Communication.exercise_id == exercise_id)
            .order_by(col(Communication.sent_at))
        )
    ).all()
    comm_rows = [
        {
            "direction": c.direction,
            "external_entity": c.external_entity,
            "subject": c.subject,
            "body": c.body,
            "sent_at": _fmt(c.sent_at),
            "visible_to_teams": c.visible_to_teams,
            "sender": name(c.sender_id) if c.sender_id else (c.sender_team or c.external_entity),
        }
        for c in comms
    ]

    summary_row = (
        await session.exec(
            select(ExecutiveSummary).where(ExecutiveSummary.exercise_id == exercise_id)
        )
    ).first()
    summary = (
        {
            "summary_text": summary_row.summary_text,
            "llm_model": summary_row.llm_model,
            "edited": summary_row.edited,
            "generated_at": _fmt(summary_row.generated_at),
        }
        if summary_row
        else None
    )

    return {
        "exercise": {
            "id": exercise.id,
            "title": exercise.title,
            "state": exercise.state,
            "started_at": _fmt(exercise.started_at),
            "ended_at": _fmt(exercise.ended_at),
            "duration": _duration(exercise.started_at, exercise.ended_at),
        },
        "scenario": {
            "title": definition.title if definition else (scenario.title if scenario else None),
            "description": definition.description if definition else None,
        },
        "teams": [
            {
                "id": t.id,
                "label": t.label,
                "participant_count": participant_team_counts[t.id],
            }
            for t in (definition.participant_teams if definition else [])
        ],
        "member_count": len(members),
        "participant_count": role_counts[UserRole.participant],
        "facilitator_count": role_counts[UserRole.facilitator],
        "observer_count": role_counts[UserRole.observer],
        "unassigned_participant_count": unassigned_participant_count,
        "injects": inject_rows,
        "communications": comm_rows,
        "debrief": {
            "scenario_debrief_notes": definition.debrief_notes if definition else None,
            "debrief_notes": exercise.debrief_notes,
        },
        "executive_summary": summary,
    }


# ── Markdown rendering ────────────────────────────────────────────────────────

_QUALITY_LABEL = {"good": "Good", "adequate": "Adequate", "poor": "Poor"}


def render_markdown(report: dict) -> str:
    """Render the structured report to a Markdown document."""
    ex = report["exercise"]
    sc = report["scenario"]
    lines: list[str] = []
    lines.append(f"# After-Action Report — {ex['title']}")
    lines.append("")

    # Metadata
    lines.append("## Overview")
    if sc.get("title"):
        lines.append(f"- **Scenario:** {sc['title']}")
    if sc.get("description"):
        lines.append(f"- **Description:** {sc['description']}")
    lines.append(f"- **Status:** {ex['state']}")
    lines.append(f"- **Started:** {ex['started_at'] or '—'}")
    lines.append(f"- **Ended:** {ex['ended_at'] or '—'}")
    if ex.get("duration"):
        lines.append(f"- **Duration:** {ex['duration']}")
    teams = ", ".join(f"{t['label']} ({t['participant_count']})" for t in report["teams"])
    if report["unassigned_participant_count"]:
        teams = ", ".join(
            part
            for part in (
                teams,
                f"Unassigned / other ({report['unassigned_participant_count']})",
            )
            if part
        )
    teams = teams or "—"
    lines.append(f"- **Teams:** {teams}")
    lines.append(f"- **Participants:** {report['participant_count']}")
    lines.append(f"- **Facilitators:** {report['facilitator_count']}")
    lines.append(f"- **Observers:** {report['observer_count']}")
    lines.append(f"- **Total enrolled:** {report['member_count']}")
    lines.append("")

    # Executive summary
    if report["executive_summary"]:
        lines.append("## Executive Summary")
        lines.append(report["executive_summary"]["summary_text"])
        lines.append("")

    # Decision summary
    lines.append("## Decision Summary")
    if not report["injects"]:
        lines.append("_No injects were released._")
    for inj in report["injects"]:
        lines.append(f"### {inj['title']}")
        if inj.get("released_at"):
            lines.append(f"*Released {inj['released_at']} by {inj['released_by']}.*")
        for resolution in inj.get("resolutions", []):
            context = resolution["group_id"] or "shared path"
            lines.append(
                f"*Resolved for {context} at {resolution['resolved_at'] or '—'} "
                f"by {resolution['resolved_by']}.*"
            )
        if not inj["responses"]:
            lines.append("")
            lines.append("_No responses recorded._")
        for r in inj["responses"]:
            opt = f" · selected **{r['selected_option']}**" if r["selected_option"] else ""
            if r["decision_quality"]:
                q_label = _QUALITY_LABEL.get(r["decision_quality"], r["decision_quality"])
                q = f" · quality: **{q_label}**"
            else:
                q = ""
            lines.append(f"- **{r['author']}**{opt}{q}")
            if r["content"]:
                lines.append(f"  - {r['content']}")
            if r["assessment_text"]:
                lines.append(f"  - _Assessment: {r['assessment_text']}_")
        lines.append("")

    # Communications
    lines.append("## Communications Log")
    if not report["communications"]:
        lines.append("_No communications were sent._")
    for c in report["communications"]:
        entity = f" ({c['external_entity']})" if c["external_entity"] else ""
        lines.append(f"- **[{c['direction']}]** {c['subject']}{entity} — {c['sent_at'] or '—'}")
        if c["body"]:
            lines.append(f"  - {c['body']}")
    lines.append("")

    # Debrief
    d = report["debrief"]
    if d["scenario_debrief_notes"] or d["debrief_notes"]:
        lines.append("## Debrief")
        if d["scenario_debrief_notes"]:
            lines.append("**Scenario prompts:**")
            lines.append(d["scenario_debrief_notes"])
            lines.append("")
        if d["debrief_notes"]:
            lines.append("**Facilitator notes:**")
            lines.append(d["debrief_notes"])
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
