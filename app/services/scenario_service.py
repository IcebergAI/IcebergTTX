import hashlib
import json
from datetime import UTC, datetime

from fastapi import HTTPException, status
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
    target_exercise_id: int | None = None,
) -> Scenario:
    # Exercises capture a scenario at creation time semantically, so never rewrite
    # an in-use library definition. A clone is the deliberate exception: it owns a
    # private Scenario row and is still a draft, so editing that row must update the
    # cloned exercise's actual source rather than creating an unreferenced fork.
    # Serialize scenario edits with new exercise creation. Both operations read
    # the same Scenario row before deciding whether clone edits may be in place.
    locked_scenario = (
        await session.exec(
            select(Scenario)
            .where(Scenario.id == scenario.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()
    if locked_scenario is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scenario not found")
    scenario = locked_scenario
    in_use = (
        await session.exec(
            select(Exercise).where(col(Exercise.scenario_id) == scenario.id).with_for_update()
        )
    ).all()
    private_clone_draft = (
        len(in_use) == 1
        and in_use[0].state == ExerciseState.draft
        and in_use[0].cloned_from_exercise_id is not None
        and updated_by is not None
        and in_use[0].created_by == updated_by
        and scenario.created_by == updated_by
    )
    private_clone_reused = (
        updated_by is not None
        and scenario.created_by == updated_by
        and any(
            exercise.state == ExerciseState.draft
            and exercise.cloned_from_exercise_id is not None
            and exercise.created_by == updated_by
            for exercise in in_use
        )
        and not private_clone_draft
    )
    previous_definition = export_definition(scenario)
    if private_clone_reused:
        target = next((exercise for exercise in in_use if exercise.id == target_exercise_id), None)
        if (
            target is None
            or target.created_by != updated_by
            or target.state is not ExerciseState.draft
            or target.cloned_from_exercise_id is None
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This clone scenario is shared; supply an owned draft clone "
                    "exercise_id to fork and edit that clone"
                ),
            )
        fork = Scenario(
            title=scenario.title,
            description=scenario.description,
            version=scenario.version,
            tags=scenario.tags,
            definition=scenario.definition,
            created_by=updated_by or scenario.created_by,
        )
        session.add(fork)
        await session.flush()
        assert fork.id is not None
        target.scenario_id = fork.id
        session.add(target)
        scenario = fork
        in_use = [target]
        private_clone_draft = True
    if in_use and not private_clone_draft:
        return await create_scenario(
            session,
            definition=definition,
            created_by=updated_by or scenario.created_by,
        )
    if private_clone_draft:
        from app.models.inject import Inject

        clone = in_use[0]
        assert clone.id is not None
        legacy_unknown = (
            await session.exec(
                select(Inject)
                .where(col(Inject.exercise_id) == clone.id)
                .where(col(Inject.scenario_seeded).is_(None))
            )
        ).all()
        changes = material_definition_changes(previous_definition, definition)
        if legacy_unknown and any(
            changes[key]
            for key in (
                "injects_added",
                "injects_removed",
                "injects_changed",
                "inject_order_changed",
                "teams_added",
                "teams_removed",
                "teams_changed",
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This legacy clone contains injects with unknown provenance. "
                    "Resolve or manually edit those injects before changing runnable "
                    "scenario content."
                ),
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

        from app.models.communication import Communication
        from app.models.exercise import ExerciseMember, ExerciseProgress
        from app.models.inject import Inject
        from app.services.inject_service import (
            sync_seeded_injects_from_scenario,
        )
        from app.services.progression_service import seed_progression

        clone = in_use[0]
        assert clone.id is not None
        old_team_ids = {team.id for team in previous_definition.participant_teams}
        team_ids = {team.id for team in definition.participant_teams}
        members = (
            await session.exec(
                select(ExerciseMember)
                .where(col(ExerciseMember.exercise_id) == clone.id)
                .where(col(ExerciseMember.group_id).is_not(None))
            )
        ).all()
        invalid_member_groups = sorted(
            {
                member.group_id
                for member in members
                if member.group_id is not None and member.group_id not in team_ids
            }
        )
        if invalid_member_groups:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Scenario teams cannot remove enrolled groups; reassign or remove "
                    f"members first: {', '.join(invalid_member_groups)}"
                ),
            )
        custom_injects = (
            await session.exec(
                select(Inject)
                .where(col(Inject.exercise_id) == clone.id)
                .where(col(Inject.scenario_seeded).is_not(True))
            )
        ).all()
        invalid_custom_groups = sorted(
            {
                group
                for inject in custom_injects
                for group in [inject.group_id, *(inject.target_teams or [])]
                if group is not None and group not in team_ids
            }
        )
        if invalid_custom_groups:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Scenario teams cannot remove groups targeted by custom injects; "
                    f"edit those injects first: {', '.join(invalid_custom_groups)}"
                ),
            )
        communications = (
            await session.exec(
                select(Communication).where(col(Communication.exercise_id) == clone.id)
            )
        ).all()
        for communication in communications:
            visible = set(communication.visible_to_teams or [])
            if not visible:
                continue
            if communication.audience_explicit is False and visible == old_team_ids:
                # Inbound injects with no explicit audience are expanded to all
                # teams at creation; keep that semantic as the definition changes.
                communication.visible_to_teams = sorted(team_ids) or None
                session.add(communication)
            elif not visible.issubset(team_ids):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Scenario teams cannot remove communication audiences; "
                        "edit or remove explicitly targeted communications first"
                    ),
                )
        await sync_seeded_injects_from_scenario(session, clone.id, scenario)
        await session.exec(
            delete(ExerciseProgress).where(col(ExerciseProgress.exercise_id) == clone.id)
        )
        clone.current_node_id = definition.start_inject_id
        session.add(clone)
        await session.flush()
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
    # The materializer uses definition order as the fallback sequence for nodes
    # without an explicit sequence_order. Preserve this as an explicit comparison
    # result so a facilitator cannot miss a runnable-order change that leaves each
    # node's individual payload unchanged.
    inject_order_changed = [node.id for node in earlier.injects] != [
        node.id for node in later.injects
    ]
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
        "inject_order_changed": inject_order_changed,
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
