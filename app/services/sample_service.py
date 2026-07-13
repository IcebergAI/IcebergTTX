import re
from pathlib import Path

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseState
from app.models.inject import Inject
from app.models.scenario import Scenario
from app.models.user import UserRole
from app.schemas.scenario_json import ScenarioDefinition
from app.services.exercise_service import create_exercise, enrol_member, transition_state
from app.services.inject_service import release_inject
from app.services.scenario_service import create_scenario

SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples"

# Sample ids are bare filename stems; anything else (path separators, "..",
# NUL, etc.) is rejected to prevent directory traversal (#15).
SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def sample_id_from_path(path: Path) -> str:
    return path.stem


def list_sample_definitions() -> list[tuple[str, ScenarioDefinition]]:
    samples: list[tuple[str, ScenarioDefinition]] = []
    for path in sorted(SAMPLES_DIR.glob("*.json")):
        definition = ScenarioDefinition.model_validate_json(path.read_text())
        samples.append((sample_id_from_path(path), definition))
    return samples


def get_sample_definition(sample_id: str) -> ScenarioDefinition | None:
    if not SAMPLE_ID_RE.match(sample_id):
        return None
    path = SAMPLES_DIR / f"{sample_id}.json"
    # Belt-and-braces: confine the resolved path within SAMPLES_DIR even if the
    # allowlist above is ever relaxed.
    if not path.resolve().is_relative_to(SAMPLES_DIR.resolve()):
        return None
    if not path.is_file():
        return None
    return ScenarioDefinition.model_validate_json(path.read_text())


def sample_summary(sample_id: str, definition: ScenarioDefinition) -> dict:
    return {
        "id": sample_id,
        "title": definition.title,
        "description": definition.description,
        "tags": definition.tags,
        "inject_count": len(definition.injects),
        "team_count": len(definition.participant_teams),
        "estimated_duration_minutes": definition.metadata.estimated_duration_minutes,
    }


async def load_sample_scenario(
    session: AsyncSession, *, sample_id: str, created_by: int
) -> tuple[Scenario, bool]:
    definition = get_sample_definition(sample_id)
    if definition is None:
        raise FileNotFoundError(sample_id)

    definition_json = definition.model_dump_json()
    existing = (
        await session.exec(select(Scenario).where(Scenario.definition == definition_json))
    ).first()
    if existing:
        return existing, False
    return await create_scenario(session, definition=definition, created_by=created_by), True


async def create_sample_demo_exercise(
    session: AsyncSession, *, sample_id: str, created_by: int
) -> tuple[Scenario, Exercise]:
    scenario, _ = await load_sample_scenario(session, sample_id=sample_id, created_by=created_by)
    assert scenario.id is not None
    exercise = await create_exercise(
        session,
        scenario_id=scenario.id,
        title=f"Demo: {scenario.title}",
        created_by=created_by,
    )
    definition = get_sample_definition(sample_id)
    assert definition is not None
    demo_group = definition.participant_teams[0].id if definition.participant_teams else None
    member = await enrol_member(
        session, exercise=exercise, user_id=created_by, group_id=demo_group
    )
    # A sample demo is intentionally a one-person participant preview: the
    # facilitator owns the exercise but acts as its participant audience.
    member.role_at_enrolment = UserRole.participant
    session.add(member)
    await session.commit()
    exercise = await transition_state(session, exercise, ExerciseState.active)
    start_inject = (
        await session.exec(
            select(Inject)
            .where(Inject.exercise_id == exercise.id)
            .where(Inject.scenario_node_id == exercise.current_node_id)
        )
    ).first()
    if start_inject:
        await release_inject(session, start_inject, released_by=created_by)
    return scenario, exercise
