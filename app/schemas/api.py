"""Pydantic response models for the JSON API.

These mirror the dict shapes built by the routers' ``_out``/payload helpers.
Declaring them as ``response_model`` lets FastAPI filter (drop unexpected/secret
fields), document (OpenAPI), and serialise responses via Pydantic. Fields that
some routes omit (e.g. an inject's ``options`` is only resolved when a session is
available) are optional so the same model serves every variant. Timestamp fields
are strings because the helpers emit ``.isoformat()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.models.communication import CommDirection
from app.models.exercise import ExerciseState
from app.models.inject import InjectState
from app.models.suggested_inject import SuggestedInjectStatus
from app.models.user import UserRole

if TYPE_CHECKING:
    from app.models.exercise import Exercise, ExerciseMember


class UserPublic(BaseModel):
    id: int
    email: str
    display_name: str
    role: UserRole
    team: str | None = None
    is_active: bool
    is_admin: bool = False
    must_change_password: bool = False


class ExercisePublic(BaseModel):
    id: int
    scenario_id: int
    title: str
    # Denormalised for list views (the dashboard renders it beside the exercise title).
    # Only the list route populates it; single-exercise routes leave it None.
    scenario_title: str | None = None
    state: ExerciseState
    current_node_id: str | None = None
    llm_enabled: bool
    started_at: str | None = None
    ended_at: str | None = None
    created_by: int
    created_at: str

    @classmethod
    def from_model(cls, ex: Exercise, scenario_title: str | None = None) -> ExercisePublic:
        return cls(
            id=ex.id,
            scenario_id=ex.scenario_id,
            title=ex.title,
            scenario_title=scenario_title,
            state=ex.state,
            current_node_id=ex.current_node_id,
            llm_enabled=ex.llm_enabled,
            started_at=ex.started_at.isoformat() if ex.started_at else None,
            ended_at=ex.ended_at.isoformat() if ex.ended_at else None,
            created_by=ex.created_by,
            created_at=ex.created_at.isoformat(),
        )


class ExerciseStateChange(BaseModel):
    """Canonical post-commit WebSocket payload for a lifecycle transition (#129)."""

    transition_id: int
    exercise_id: int
    previous_state: ExerciseState
    new_state: ExerciseState
    # Compatibility alias for clients written against the original envelope.
    state: ExerciseState
    actor_id: int | None = None
    transitioned_at: str
    started_at: str | None = None
    ended_at: str | None = None


class ExecutiveSummaryPublic(BaseModel):
    """LLM-drafted (and facilitator-editable) executive summary for the report (#113)."""

    exercise_id: int
    summary_text: str
    llm_model: str
    edited: bool
    generated_at: str


class ReportSummaryState(BaseModel):
    """Executive-summary state for the review UI: whether AI drafting is available
    (a provider is configured AND the exercise opted in) plus the current summary."""

    available: bool
    summary: ExecutiveSummaryPublic | None = None


class DebriefNotes(BaseModel):
    """Owner-only debrief payload (#112): the scenario author's read-only talking
    points alongside the editable exercise-level notes. Never returned by the
    participant-visible exercise routes."""

    exercise_id: int
    scenario_debrief_notes: str | None = None  # read-only, from the scenario definition
    debrief_notes: str | None = None  # editable, facilitator's observations


class MemberPublic(BaseModel):
    id: int
    exercise_id: int
    user_id: int
    group_id: str | None = None
    joined_at: str

    @classmethod
    def from_model(cls, m: ExerciseMember) -> MemberPublic:
        return cls(
            id=m.id,
            exercise_id=m.exercise_id,
            user_id=m.user_id,
            group_id=m.group_id,
            joined_at=m.joined_at.isoformat(),
        )


class ScenarioSummary(BaseModel):
    id: int
    title: str
    description: str | None = None
    version: str
    tags: list[str]
    inject_count: int
    branch_count: int
    created_by: int
    created_at: str
    updated_at: str


class ScenarioDetail(ScenarioSummary):
    definition: dict[str, Any]


class InjectPublic(BaseModel):
    id: int
    exercise_id: int
    scenario_node_id: str | None = None
    title: str
    content: str
    target_teams: list[str] | None = None
    group_id: str | None = None
    sequence_order: int
    state: InjectState
    released_at: str | None = None
    released_by: int | None = None
    attachment: dict[str, Any] | None = None
    # Present only when the scenario node is resolved (a session was available).
    options: list[dict[str, Any]] | None = None
    next_inject_id: str | None = None
    free_text_response: bool | None = None


class ResponsePublic(BaseModel):
    id: int
    inject_id: int
    exercise_id: int
    user_id: int
    group_id: str | None = None
    content: str
    selected_option: str | None = None
    submitted_at: str
    assessment_id: int | None = None
    # Facilitator view only.
    next_injects: list[dict[str, Any]] | None = None
    next_inject_ids: list[str] | None = None


class CommunicationPublic(BaseModel):
    id: int
    exercise_id: int
    sender_id: int | None = None
    sender_team: str | None = None
    direction: CommDirection
    external_entity: str | None = None
    subject: str
    body: str
    triggered_by_inject_id: int | None = None
    visible_to_teams: list[str] | None = None
    sent_at: str
    is_read: bool = False
    read_at: str | None = None


class InjectCommentPublic(BaseModel):
    id: int
    inject_id: int
    exercise_id: int
    user_id: int
    author_name: str
    group_id: str | None = None
    content: str
    created_at: str


class AssessmentPublic(BaseModel):
    id: int
    response_id: int
    llm_model: str
    assessment_text: str
    decision_quality: str | None = None
    recommended_branch_option_id: str | None = None
    assessed_at: str


class TimelineEvent(BaseModel):
    """One event in the merged exercise timeline (#111).

    A flat model where each ``kind`` populates a different subset of the optional
    payload fields (same "one model, optional fields" convention as the *Public
    schemas above). ``at`` is an ISO timestamp string.
    """

    kind: str  # inject_released | response | communication | comment | state_change
    at: str
    # inject_released
    inject_id: int | None = None
    scenario_node_id: str | None = None
    title: str | None = None
    target_teams: list[str] | None = None
    released_by: int | None = None
    # response
    response_id: int | None = None
    selected_option: str | None = None
    decision_quality: str | None = None
    # communication
    communication_id: int | None = None
    direction: CommDirection | None = None
    external_entity: str | None = None
    subject: str | None = None
    sender_id: int | None = None
    sender_team: str | None = None
    visible_to_teams: list[str] | None = None
    triggered_by_inject_id: int | None = None
    # comment
    comment_id: int | None = None
    # response + comment
    user_id: int | None = None
    group_id: str | None = None
    content: str | None = None
    # state_change
    transition_id: int | None = None
    action: str | None = None
    actor_id: int | None = None
    previous_state: ExerciseState | None = None
    new_state: ExerciseState | None = None


class SuggestedInjectPublic(BaseModel):
    id: int
    exercise_id: int
    triggered_by_response_id: int
    title: str
    content: str
    target_teams: list[str] | None = None
    llm_model: str
    status: SuggestedInjectStatus
    reviewed_by: int | None = None
    reviewed_at: str | None = None
    generated_at: str
