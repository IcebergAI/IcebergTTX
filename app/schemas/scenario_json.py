from __future__ import annotations

from pydantic import BaseModel, field_validator, model_validator

# Sanity bound on scenario size. Real tabletop scenarios have at most dozens of
# injects; this guards against pathological or malicious payloads (#18). Set well
# above any realistic scenario so legitimate authoring is never blocked.
MAX_INJECTS = 5000


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
    next_inject_id: str | None = None
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
        if len(self.injects) > MAX_INJECTS:
            raise ValueError(
                f"scenario has {len(self.injects)} injects (max {MAX_INJECTS})"
            )

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
            if inj.next_inject_id is not None and inj.next_inject_id not in inject_ids:
                raise ValueError(
                    f"inject '{inj.id}': next_inject_id '{inj.next_inject_id}' not found"
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
    """Raises ValueError if there is a cycle reachable from start_id.

    Iterative DFS (explicit stack) so deep linear ``next_inject_id`` chains cannot
    overflow the recursion limit and surface as an opaque 500 (#18).
    """
    adjacency: dict[str, list[str]] = {}
    for inj in injects:
        next_ids = [opt.next_inject_id for opt in inj.options if opt.next_inject_id is not None]
        if inj.next_inject_id is not None:
            next_ids.append(inj.next_inject_id)
        adjacency[inj.id] = next_ids

    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = dict.fromkeys(adjacency, WHITE)

    colour[start_id] = GREY
    stack: list[tuple[str, object]] = [(start_id, iter(adjacency.get(start_id, [])))]
    while stack:
        node, neighbours = stack[-1]
        descended = False
        for neighbour in neighbours:  # type: ignore[attr-defined]
            state = colour.get(neighbour, WHITE)
            if state == GREY:
                raise ValueError(f"Cycle detected in scenario graph at inject '{neighbour}'")
            if state == WHITE:
                colour[neighbour] = GREY
                stack.append((neighbour, iter(adjacency.get(neighbour, []))))
                descended = True
                break
        if not descended:
            colour[node] = BLACK
            stack.pop()
