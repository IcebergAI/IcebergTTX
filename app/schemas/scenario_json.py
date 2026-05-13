from __future__ import annotations

from pydantic import BaseModel, field_validator, model_validator


class ParticipantTeam(BaseModel):
    id: str
    label: str


class TriggerComm(BaseModel):
    external_entity: str
    direction: str  # "inbound" | "outbound"
    subject: str
    body: str
    delay_after_release_seconds: int = 0

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: str) -> str:
        if v not in ("inbound", "outbound"):
            raise ValueError("direction must be 'inbound' or 'outbound'")
        return v


class InjectOption(BaseModel):
    id: str
    label: str
    next_inject_id: str | None = None


class InjectNode(BaseModel):
    id: str
    title: str
    content: str
    target_teams: list[str] = []
    sequence_order: int = 0
    options: list[InjectOption] = []
    free_text_response: bool = True
    triggers_communications: list[TriggerComm] = []
    expected_actions: list[str] = []


class ScenarioMetadata(BaseModel):
    author: str | None = None
    estimated_duration_minutes: int | None = None


class ScenarioDefinition(BaseModel):
    schema_version: str = "1.0"
    title: str
    description: str | None = None
    tags: list[str] = []
    metadata: ScenarioMetadata = ScenarioMetadata()
    participant_teams: list[ParticipantTeam] = []
    injects: list[InjectNode]
    start_inject_id: str
    debrief_notes: str | None = None

    @model_validator(mode="after")
    def validate_structure(self) -> ScenarioDefinition:
        inject_ids = {inj.id for inj in self.injects}
        team_ids = {t.id for t in self.participant_teams}

        # start_inject_id must exist
        if self.start_inject_id not in inject_ids:
            raise ValueError(f"start_inject_id '{self.start_inject_id}' not found in injects")

        for inj in self.injects:
            # All target_teams must be defined
            for team in inj.target_teams:
                if team_ids and team not in team_ids:
                    raise ValueError(
                        f"inject '{inj.id}': target_team '{team}' not in participant_teams"
                    )
            # All next_inject_id references must exist
            for opt in inj.options:
                if opt.next_inject_id is not None and opt.next_inject_id not in inject_ids:
                    raise ValueError(
                        f"inject '{inj.id}', option '{opt.id}': "
                        f"next_inject_id '{opt.next_inject_id}' not found"
                    )

        # Detect cycles using DFS
        _check_no_cycles(self.injects, self.start_inject_id)

        return self


def _check_no_cycles(injects: list[InjectNode], start_id: str) -> None:
    """Raises ValueError if there is a cycle reachable from start_id."""
    adjacency: dict[str, list[str]] = {}
    for inj in injects:
        adjacency[inj.id] = [
            opt.next_inject_id for opt in inj.options if opt.next_inject_id is not None
        ]

    visited: set[str] = set()
    in_stack: set[str] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        in_stack.add(node)
        for neighbour in adjacency.get(node, []):
            if neighbour not in visited:
                dfs(neighbour)
            elif neighbour in in_stack:
                raise ValueError(f"Cycle detected in scenario graph at inject '{neighbour}'")
        in_stack.discard(node)

    dfs(start_id)
