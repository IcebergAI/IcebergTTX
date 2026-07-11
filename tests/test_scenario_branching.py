"""Unit tests for ScenarioDefinition validation and scenario_service branching logic."""

import pytest
from pydantic import ValidationError

from app.models.scenario import Scenario
from app.schemas.scenario_json import InjectNode, InjectOption, ScenarioDefinition
from app.services.scenario_service import (
    export_definition,
    get_inject_node,
    get_next_inject_ids,
    resolve_branch,
)


def _minimal(injects, start_id="inject_01", teams=None):
    return ScenarioDefinition(
        title="Test",
        participant_teams=teams or [],
        injects=injects,
        start_inject_id=start_id,
    )


# ── Schema validation ─────────────────────────────────────────────────────────

async def test_valid_scenario_parses(sample_definition: ScenarioDefinition):
    assert sample_definition.start_inject_id == "inject_01"
    assert len(sample_definition.injects) == 2


async def test_missing_start_inject_id_raises():
    with pytest.raises(ValidationError, match="start_inject_id"):
        _minimal(
            [InjectNode(id="inject_01", title="A", content="B")],
            start_id="DOES_NOT_EXIST",
        )


async def test_bad_next_inject_id_raises():
    with pytest.raises(ValidationError, match="next_inject_id"):
        _minimal([
            InjectNode(
                id="inject_01", title="A", content="B",
                options=[InjectOption(id="opt_a", label="X", next_inject_id="MISSING")],
            )
        ])


async def test_bad_linear_next_inject_id_raises():
    with pytest.raises(ValidationError, match="next_inject_id"):
        _minimal([
            InjectNode(id="inject_01", title="A", content="B", next_inject_id="MISSING"),
        ])


async def test_bad_target_team_raises():
    with pytest.raises(ValidationError, match="target_team"):
        _minimal(
            [InjectNode(id="inject_01", title="A", content="B", target_teams=["unknown_team"])],
            teams=[{"id": "it_ops", "label": "IT Ops"}],
        )


async def test_cycle_detection_raises():
    with pytest.raises(ValidationError, match="Cycle"):
        _minimal([
            InjectNode(
                id="a", title="A", content=".",
                options=[InjectOption(id="opt1", label="Go to B", next_inject_id="b")],
            ),
            InjectNode(
                id="b", title="B", content=".",
                options=[InjectOption(id="opt2", label="Back to A", next_inject_id="a")],
            ),
        ], start_id="a")


async def test_linear_cycle_detection_raises():
    with pytest.raises(ValidationError, match="Cycle"):
        _minimal([
            InjectNode(id="a", title="A", content=".", next_inject_id="b"),
            InjectNode(id="b", title="B", content=".", next_inject_id="a"),
        ], start_id="a")


async def test_leaf_node_valid():
    defn = _minimal([
        InjectNode(id="inject_01", title="A", content="B", options=[]),
    ])
    assert defn.injects[0].options == []


async def test_empty_target_teams_means_all():
    defn = _minimal([
        InjectNode(id="inject_01", title="A", content="B", target_teams=[]),
    ])
    assert defn.injects[0].target_teams == []


# ── service: get_inject_node ──────────────────────────────────────────────────

async def test_get_inject_node_found(sample_definition: ScenarioDefinition):
    node = get_inject_node(sample_definition, "inject_01")
    assert node is not None
    assert node.id == "inject_01"


async def test_get_inject_node_not_found(sample_definition: ScenarioDefinition):
    assert get_inject_node(sample_definition, "NOPE") is None


# ── service: get_next_inject_ids ──────────────────────────────────────────────

async def test_get_next_inject_ids_requires_choice_for_branch(
    sample_definition: ScenarioDefinition,
):
    nexts = get_next_inject_ids(sample_definition, "inject_01")
    assert nexts == []


async def test_get_next_inject_ids_leaf(sample_definition: ScenarioDefinition):
    # inject_02 has no options → no next ids
    nexts = get_next_inject_ids(sample_definition, "inject_02")
    assert nexts == []


async def test_get_next_inject_ids_linear():
    defn = _minimal([
        InjectNode(id="a", title="A", content=".", next_inject_id="b"),
        InjectNode(id="b", title="B", content="."),
    ], start_id="a")
    assert get_next_inject_ids(defn, "a") == ["b"]


async def test_get_next_inject_ids_unknown_inject(sample_definition: ScenarioDefinition):
    assert get_next_inject_ids(sample_definition, "nope") == []


# ── service: resolve_branch ───────────────────────────────────────────────────

async def test_resolve_branch_known_option(sample_definition: ScenarioDefinition):
    result = resolve_branch(sample_definition, "inject_01", "opt_a")
    assert result == "inject_02"


async def test_resolve_branch_leaf_option(sample_definition: ScenarioDefinition):
    # opt_b has next_inject_id=None
    result = resolve_branch(sample_definition, "inject_01", "opt_b")
    assert result is None


async def test_resolve_branch_unknown_option(sample_definition: ScenarioDefinition):
    result = resolve_branch(sample_definition, "inject_01", "UNKNOWN_OPT")
    assert result is None


async def test_resolve_branch_unknown_inject(sample_definition: ScenarioDefinition):
    result = resolve_branch(sample_definition, "NOPE", "opt_a")
    assert result is None


# ── Multi-level branching ─────────────────────────────────────────────────────

async def test_multi_level_branch():
    defn = _minimal([
        InjectNode(
            id="a", title="A", content=".",
            options=[
                InjectOption(id="go_b", label="B", next_inject_id="b"),
                InjectOption(id="go_c", label="C", next_inject_id="c"),
            ],
        ),
        InjectNode(id="b", title="B", content="."),
        InjectNode(id="c", title="C", content="."),
    ], start_id="a")

    assert resolve_branch(defn, "a", "go_b") == "b"
    assert resolve_branch(defn, "a", "go_c") == "c"
    assert get_next_inject_ids(defn, "a") == []


# ── export_definition memoisation (#22) ───────────────────────────────────────

async def test_export_definition_memoised_on_instance(sample_definition: ScenarioDefinition):
    """Repeated parses of one Scenario row collapse to a single JSON parse (#22)."""
    scenario = Scenario(
        title=sample_definition.title,
        definition=sample_definition.model_dump_json(),
        created_by=1,
    )
    first = export_definition(scenario)
    second = export_definition(scenario)
    # Same parsed object returned — not re-parsed per call.
    assert first is second


async def test_export_definition_reparses_after_definition_change(
    sample_definition: ScenarioDefinition,
):
    """Reassigning the source JSON must invalidate the memoised parse (#22)."""
    scenario = Scenario(
        title=sample_definition.title,
        definition=sample_definition.model_dump_json(),
        created_by=1,
    )
    first = export_definition(scenario)

    changed = sample_definition.model_copy(update={"title": "Renamed"})
    scenario.definition = changed.model_dump_json()
    second = export_definition(scenario)

    assert second is not first
    assert second.title == "Renamed"
