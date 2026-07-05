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


class ExercisePublic(BaseModel):
    id: int
    scenario_id: int
    title: str
    state: ExerciseState
    current_node_id: str | None = None
    llm_enabled: bool
    started_at: str | None = None
    ended_at: str | None = None
    created_by: int
    created_at: str

    @classmethod
    def from_model(cls, ex: Exercise) -> ExercisePublic:
        return cls(
            id=ex.id,
            scenario_id=ex.scenario_id,
            title=ex.title,
            state=ex.state,
            current_node_id=ex.current_node_id,
            llm_enabled=ex.llm_enabled,
            started_at=ex.started_at.isoformat() if ex.started_at else None,
            ended_at=ex.ended_at.isoformat() if ex.ended_at else None,
            created_by=ex.created_by,
            created_at=ex.created_at.isoformat(),
        )


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
    read_by: list[int]


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
