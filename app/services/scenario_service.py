from datetime import UTC, datetime

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise
from app.models.scenario import Scenario
from app.schemas.scenario_json import ScenarioDefinition


def parse_definition(definition_json: str) -> ScenarioDefinition:
    """Parse and validate a scenario definition JSON string. Raises ValidationError on failure."""
    return ScenarioDefinition.model_validate_json(definition_json)


async def create_scenario(
    session: AsyncSession,
    *,
    definition: ScenarioDefinition,
    created_by: int,
) -> Scenario:
    definition_json = definition.model_dump_json()
    scenario = Scenario(
        title=definition.title,
        description=definition.description,
        tags=definition.tags or None,
        definition=definition_json,
        created_by=created_by,
    )
    session.add(scenario)
    await session.commit()
    await session.refresh(scenario)
    return scenario


async def update_scenario(
    session: AsyncSession,
    scenario: Scenario,
    *,
    definition: ScenarioDefinition,
) -> Scenario:
    scenario.title = definition.title
    scenario.description = definition.description
    scenario.tags = definition.tags or None
    scenario.definition = definition.model_dump_json()
    scenario.updated_at = datetime.now(UTC)
    session.add(scenario)
    await session.commit()
    await session.refresh(scenario)
    return scenario


_PARSED_DEFINITION_ATTR = "_parsed_definition"


def export_definition(scenario: Scenario) -> ScenarioDefinition:
    """Parse a scenario's stored definition JSON, memoised on the instance (#22).

    List endpoints resolve the same scenario definition once per row (per inject,
    per response, …). Because the ``Scenario`` row is identity-mapped within a
    session, all those calls share one instance, so caching the parsed
    ``ScenarioDefinition`` here collapses O(rows) JSON parses to one per request.

    The cache is keyed on the identity of the source ``definition`` string, so it
    self-invalidates whenever the column is reassigned (``update_scenario``) or
    reloaded from the DB (``session.refresh``) — those produce a new string object.
    """
    cached = scenario.__dict__.get(_PARSED_DEFINITION_ATTR)
    if cached is not None and cached[0] is scenario.definition:
        return cached[1]
    parsed = ScenarioDefinition.model_validate_json(scenario.definition)
    scenario.__dict__[_PARSED_DEFINITION_ATTR] = (scenario.definition, parsed)
    return parsed


async def get_scenario_definition(
    session: AsyncSession, scenario_id: int | None
) -> ScenarioDefinition | None:
    """Load and parse the definition for a scenario id, or None if absent."""
    if not scenario_id:
        return None
    scenario = await session.get(Scenario, scenario_id)
    return export_definition(scenario) if scenario else None


async def definition_for_exercise(
    session: AsyncSession, exercise_id: int
) -> ScenarioDefinition | None:
    """Load the scenario definition for an exercise, or None if either is missing."""
    exercise = await session.get(Exercise, exercise_id)
    if not exercise:
        return None
    return await get_scenario_definition(session, exercise.scenario_id)


def get_inject_node(definition: ScenarioDefinition, inject_id: str):
    """Return the inject node with the given id, or None."""
    return next((inj for inj in definition.injects if inj.id == inject_id), None)


def get_next_inject_ids(definition: ScenarioDefinition, current_inject_id: str) -> list[str]:
    """Return a linear successor; branch nodes require an explicit option selection."""
    node = get_inject_node(definition, current_inject_id)
    if node is None or node.options:
        return []
    return [node.next_inject_id] if node.next_inject_id is not None else []


def resolve_branch(
    definition: ScenarioDefinition,
    current_inject_id: str,
    selected_option_id: str,
) -> str | None:
    """Return the next inject_id for the chosen option, or None if leaf."""
    node = get_inject_node(definition, current_inject_id)
    if node is None:
        return None
    for opt in node.options:
        if opt.id == selected_option_id:
            return opt.next_inject_id
    return None
