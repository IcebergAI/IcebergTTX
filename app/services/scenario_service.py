import json
from datetime import UTC, datetime

from sqlmodel import Session

from app.models.scenario import Scenario
from app.schemas.scenario_json import ScenarioDefinition


def parse_definition(definition_json: str) -> ScenarioDefinition:
    """Parse and validate a scenario definition JSON string. Raises ValidationError on failure."""
    return ScenarioDefinition.model_validate_json(definition_json)


def create_scenario(
    session: Session,
    *,
    definition: ScenarioDefinition,
    created_by: int,
) -> Scenario:
    definition_json = definition.model_dump_json()
    scenario = Scenario(
        title=definition.title,
        description=definition.description,
        tags=json.dumps(definition.tags) if definition.tags else None,
        definition=definition_json,
        created_by=created_by,
    )
    session.add(scenario)
    session.commit()
    session.refresh(scenario)
    return scenario


def update_scenario(
    session: Session,
    scenario: Scenario,
    *,
    definition: ScenarioDefinition,
) -> Scenario:
    scenario.title = definition.title
    scenario.description = definition.description
    scenario.tags = json.dumps(definition.tags) if definition.tags else None
    scenario.definition = definition.model_dump_json()
    scenario.updated_at = datetime.now(UTC)
    session.add(scenario)
    session.commit()
    session.refresh(scenario)
    return scenario


def export_definition(scenario: Scenario) -> ScenarioDefinition:
    return ScenarioDefinition.model_validate_json(scenario.definition)


def get_inject_node(definition: ScenarioDefinition, inject_id: str):
    """Return the inject node with the given id, or None."""
    return next((inj for inj in definition.injects if inj.id == inject_id), None)


def get_next_inject_ids(definition: ScenarioDefinition, current_inject_id: str) -> list[str]:
    """Return inject IDs reachable after a response without a selected branch option."""
    node = get_inject_node(definition, current_inject_id)
    if node is None:
        return []
    option_next_ids = [opt.next_inject_id for opt in node.options if opt.next_inject_id is not None]
    if option_next_ids:
        return option_next_ids
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
