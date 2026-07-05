from __future__ import annotations

import graphlib

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

        ids = [inj.id for inj in self.injects]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            # Downstream code (get_inject_node, seed_injects_from_scenario, branch
            # resolution) assumes inject ids are unique; a set would hide collisions.
            raise ValueError(f"duplicate inject id(s): {', '.join(dupes)}")
        inject_ids = set(ids)
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
        _check_no_cycles(self.injects)

        return self


def _check_no_cycles(injects: list[InjectNode]) -> None:
    """Raises ValueError if the scenario graph contains a cycle.

    Delegates to the stdlib ``graphlib.TopologicalSorter``, which processes every
    node (so a cycle in an island unreachable from the start still fails, #37) and
    is iterative internally (deep linear ``next_inject_id`` chains cannot overflow
    the recursion limit, #18). Edge direction is irrelevant for cycle detection,
    so feeding successors where the sorter expects predecessors is fine.
    """
    graph: dict[str, list[str]] = {}
    for inj in injects:
        successors = [opt.next_inject_id for opt in inj.options if opt.next_inject_id is not None]
        if inj.next_inject_id is not None:
            successors.append(inj.next_inject_id)
        graph[inj.id] = successors

    try:
        graphlib.TopologicalSorter(graph).prepare()
    except graphlib.CycleError as exc:
        cycle = exc.args[1] if len(exc.args) > 1 else []
        raise ValueError(
            f"Cycle detected in scenario graph: {' -> '.join(cycle)}"
        ) from exc
