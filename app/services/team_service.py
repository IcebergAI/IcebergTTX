"""Scenario-team catalog helpers shared by API and untrusted LLM paths."""

from app.schemas.scenario_json import ScenarioDefinition


def scenario_team_ids(definition: ScenarioDefinition) -> set[str]:
    return {team.id for team in definition.participant_teams}


def validate_team_ids(
    team_ids: list[str] | None,
    definition: ScenarioDefinition,
    *,
    field: str,
) -> list[str] | None:
    """Normalize a non-empty, duplicate-free audience against one scenario catalog."""
    if team_ids is None:
        return None
    normalized = [team_id.strip() for team_id in team_ids]
    if any(not team_id for team_id in normalized):
        raise ValueError(f"{field} must not contain blank team ids")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field} must not contain duplicate team ids")
    unknown = sorted(set(normalized) - scenario_team_ids(definition))
    if unknown:
        raise ValueError(
            f"{field} contains team ids not in this scenario: {', '.join(unknown)}"
        )
    return normalized or None
