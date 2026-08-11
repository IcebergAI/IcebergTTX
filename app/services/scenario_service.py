import hashlib
import json
from datetime import UTC, datetime

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseRunSnapshot
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
    updated_by: int | None = None,
) -> Scenario:
    # Exercises capture a scenario at creation time semantically, so never rewrite
    # an in-use definition. Preserve the collaborative library by returning a new
    # editor-owned revision for future exercises instead of denying non-owner edits.
    in_use = (
        await session.exec(select(Exercise.id).where(Exercise.scenario_id == scenario.id))
    ).first()
    if in_use is not None:
        return await create_scenario(
            session,
            definition=definition,
            created_by=updated_by or scenario.created_by,
        )
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
_RUN_SNAPSHOT_ATTR = "_run_snapshot"
_PARSED_RUN_SNAPSHOT_ATTR = "_parsed_run_snapshot"


RUN_SNAPSHOT_SCHEMA_VERSION = 1


def canonical_definition_json(definition: ScenarioDefinition) -> str:
    """Serialize a validated definition as stable bytes for a run evidence record."""
    return json.dumps(
        definition.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def snapshot_content_sha256(
    *, definition_json: str, configuration: dict[str, object], schema_version: int
) -> str:
    """Hash exactly the canonical payload a launched run is interpreted from."""
    payload = json.dumps(
        {
            "configuration": configuration,
            "definition": json.loads(definition_json),
            "schema_version": schema_version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def material_definition_changes(
    earlier: ScenarioDefinition, later: ScenarioDefinition
) -> dict[str, object]:
    """Return an explainable, bounded summary of material scenario changes.

    This intentionally reports stable node/team identities and changed node payloads,
    not an opaque JSON patch. It is used when cloning a historical run into a new
    draft so facilitators can review what changed before launching again.
    """
    old_nodes = {node.id: node.model_dump(mode="json") for node in earlier.injects}
    new_nodes = {node.id: node.model_dump(mode="json") for node in later.injects}
    old_teams = {team.id: team.model_dump(mode="json") for team in earlier.participant_teams}
    new_teams = {team.id: team.model_dump(mode="json") for team in later.participant_teams}
    return {
        "injects_added": sorted(new_nodes.keys() - old_nodes.keys()),
        "injects_removed": sorted(old_nodes.keys() - new_nodes.keys()),
        "injects_changed": sorted(
            node_id
            for node_id in old_nodes.keys() & new_nodes.keys()
            if old_nodes[node_id] != new_nodes[node_id]
        ),
        "teams_added": sorted(new_teams.keys() - old_teams.keys()),
        "teams_removed": sorted(old_teams.keys() - new_teams.keys()),
        "teams_changed": sorted(
            team_id
            for team_id in old_teams.keys() & new_teams.keys()
            if old_teams[team_id] != new_teams[team_id]
        ),
        "metadata_changed": (
            earlier.model_dump(mode="json", exclude={"injects", "participant_teams"})
            != later.model_dump(mode="json", exclude={"injects", "participant_teams"})
        ),
    }


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
    cached = getattr(scenario, _PARSED_DEFINITION_ATTR, None)
    if cached is not None and cached[0] is scenario.definition:
        return cached[1]
    parsed = ScenarioDefinition.model_validate_json(scenario.definition)
    object.__setattr__(scenario, _PARSED_DEFINITION_ATTR, (scenario.definition, parsed))
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
    """Resolve an exercise from its immutable run record once launched.

    Drafts intentionally retain the reusable Scenario editing experience. A snapshot
    exists from the first successful launch onwards, so active/completed exercise
    services never reinterpret historical behavior through a source template.
    """
    exercise = await session.get(Exercise, exercise_id)
    if not exercise:
        return None
    snapshot = await snapshot_for_exercise(session, exercise_id)
    if snapshot is not None:
        cached = getattr(exercise, _PARSED_RUN_SNAPSHOT_ATTR, None)
        if cached is not None and cached[0] is snapshot.definition:
            return cached[1]
        definition = ScenarioDefinition.model_validate_json(snapshot.definition)
        object.__setattr__(exercise, _PARSED_RUN_SNAPSHOT_ATTR, (snapshot.definition, definition))
        return definition
    return await get_scenario_definition(session, exercise.scenario_id)


async def snapshot_for_exercise(
    session: AsyncSession, exercise_id: int
) -> ExerciseRunSnapshot | None:
    exercise = await session.get(Exercise, exercise_id)
    if exercise is None:
        return None
    cached = getattr(exercise, _RUN_SNAPSHOT_ATTR, None)
    if cached is not None:
        return cached
    snapshot = (
        await session.exec(
            select(ExerciseRunSnapshot).where(ExerciseRunSnapshot.exercise_id == exercise_id)
        )
    ).first()
    if snapshot is not None:
        object.__setattr__(exercise, _RUN_SNAPSHOT_ATTR, snapshot)
    return snapshot


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


async def titles_for(session: AsyncSession, scenario_ids: set[int]) -> dict[int, str]:
    """id -> title for the given scenarios — one query, not an N+1 per exercise row."""
    if not scenario_ids:
        return {}
    rows = await session.exec(
        select(Scenario.id, Scenario.title).where(col(Scenario.id).in_(scenario_ids))
    )
    return {scenario_id: title for scenario_id, title in rows.all() if scenario_id is not None}
