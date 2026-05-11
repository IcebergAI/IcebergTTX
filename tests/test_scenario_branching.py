"""Unit tests for ScenarioDefinition validation and scenario_service branching logic."""

import pytest
from pydantic import ValidationError

from app.schemas.scenario_json import InjectNode, InjectOption, ScenarioDefinition
from app.services.scenario_service import (
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

def test_valid_scenario_parses(sample_definition: ScenarioDefinition):
    assert sample_definition.start_inject_id == "inject_01"
    assert len(sample_definition.injects) == 2


def test_missing_start_inject_id_raises():
    with pytest.raises(ValidationError, match="start_inject_id"):
        _minimal(
            [InjectNode(id="inject_01", title="A", content="B")],
            start_id="DOES_NOT_EXIST",
        )


def test_bad_next_inject_id_raises():
    with pytest.raises(ValidationError, match="next_inject_id"):
        _minimal([
            InjectNode(
                id="inject_01", title="A", content="B",
                options=[InjectOption(id="opt_a", label="X", next_inject_id="MISSING")],
            )
        ])


def test_bad_target_team_raises():
    with pytest.raises(ValidationError, match="target_team"):
        _minimal(
            [InjectNode(id="inject_01", title="A", content="B", target_teams=["unknown_team"])],
            teams=[{"id": "it_ops", "label": "IT Ops"}],
        )


def test_cycle_detection_raises():
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


def test_leaf_node_valid():
    defn = _minimal([
        InjectNode(id="inject_01", title="A", content="B", options=[]),
    ])
    assert defn.injects[0].options == []


def test_empty_target_teams_means_all():
    defn = _minimal([
        InjectNode(id="inject_01", title="A", content="B", target_teams=[]),
    ])
    assert defn.injects[0].target_teams == []


# ── service: get_inject_node ──────────────────────────────────────────────────

def test_get_inject_node_found(sample_definition: ScenarioDefinition):
    node = get_inject_node(sample_definition, "inject_01")
    assert node is not None
    assert node.id == "inject_01"


def test_get_inject_node_not_found(sample_definition: ScenarioDefinition):
    assert get_inject_node(sample_definition, "NOPE") is None


# ── service: get_next_inject_ids ──────────────────────────────────────────────

def test_get_next_inject_ids(sample_definition: ScenarioDefinition):
    nexts = get_next_inject_ids(sample_definition, "inject_01")
    assert "inject_02" in nexts


def test_get_next_inject_ids_leaf(sample_definition: ScenarioDefinition):
    # inject_02 has no options → no next ids
    nexts = get_next_inject_ids(sample_definition, "inject_02")
    assert nexts == []


def test_get_next_inject_ids_unknown_inject(sample_definition: ScenarioDefinition):
    assert get_next_inject_ids(sample_definition, "nope") == []


# ── service: resolve_branch ───────────────────────────────────────────────────

def test_resolve_branch_known_option(sample_definition: ScenarioDefinition):
    result = resolve_branch(sample_definition, "inject_01", "opt_a")
    assert result == "inject_02"


def test_resolve_branch_leaf_option(sample_definition: ScenarioDefinition):
    # opt_b has next_inject_id=None
    result = resolve_branch(sample_definition, "inject_01", "opt_b")
    assert result is None


def test_resolve_branch_unknown_option(sample_definition: ScenarioDefinition):
    result = resolve_branch(sample_definition, "inject_01", "UNKNOWN_OPT")
    assert result is None


def test_resolve_branch_unknown_inject(sample_definition: ScenarioDefinition):
    result = resolve_branch(sample_definition, "NOPE", "opt_a")
    assert result is None


# ── Multi-level branching ─────────────────────────────────────────────────────

def test_multi_level_branch():
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
    assert get_next_inject_ids(defn, "a") == ["b", "c"]
