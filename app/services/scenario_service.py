import hashlib
import json
from datetime import UTC, datetime

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseRunSnapshot, ExerciseState
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
    # an in-use library definition. A clone is the deliberate exception: it owns a
    # private Scenario row and is still a draft, so editing that row must update the
    # cloned exercise's actual source rather than creating an unreferenced fork.
    in_use = (await session.exec(select(Exercise).where(Exercise.scenario_id == scenario.id))).all()
    private_clone_draft = (
        len(in_use) == 1
        and in_use[0].state == ExerciseState.draft
        and in_use[0].cloned_from_exercise_id is not None
        and updated_by is not None
        and in_use[0].created_by == updated_by
        and scenario.created_by == updated_by
    )
    if in_use and not private_clone_draft:
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
    if private_clone_draft:
        # Clone creation materializes Inject and ExerciseProgress rows for the
        # original definition. Replacing only the Scenario would leave the draft
        # runnable against stale inject content/cursors, so rebuild that derived
        # state in the same transaction as the edit.
        from sqlalchemy import delete

        from app.models.exercise import ExerciseProgress
        from app.models.inject import Inject, InjectProgress
        from app.services.inject_service import seed_injects_from_scenario
        from app.services.progression_service import seed_progression

        clone = in_use[0]
        assert clone.id is not None
        await session.exec(
            delete(InjectProgress).where(col(InjectProgress.exercise_id) == clone.id)
        )
        await session.exec(delete(Inject).where(col(Inject.exercise_id) == clone.id))
        await session.exec(
            delete(ExerciseProgress).where(col(ExerciseProgress.exercise_id) == clone.id)
        )
        clone.current_node_id = definition.start_inject_id
        session.add(clone)
        await session.flush()
        await seed_injects_from_scenario(session, clone.id, scenario)
        await seed_progression(
            session,
            exercise_id=clone.id,
            start_node_id=definition.start_inject_id,
            group_ids=[team.id for team in definition.participant_teams],
        )
    await session.commit()
    await session.refresh(scenario)
    return scenario


_PARSED_DEFINITION_ATTR = "_parsed_definition"


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
        return ScenarioDefinition.model_validate_json(snapshot.definition)
    return await get_scenario_definition(session, exercise.scenario_id)


async def snapshot_for_exercise(
    session: AsyncSession, exercise_id: int
) -> ExerciseRunSnapshot | None:
    return (
        await session.exec(
            select(ExerciseRunSnapshot).where(ExerciseRunSnapshot.exercise_id == exercise_id)
        )
    ).first()


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
