"""Scenario definition validation: cycle detection and size limits (#18)."""

import pytest
from pydantic import ValidationError

from app.schemas.scenario_json import MAX_INJECTS, ScenarioDefinition


def _linear_chain(n: int) -> dict:
    """A straight-line scenario of n injects chained via node-level next_inject_id."""
    injects = []
    for i in range(n):
        node = {"id": f"n{i}", "title": f"Inject {i}", "content": "x", "options": []}
        if i < n - 1:
            node["next_inject_id"] = f"n{i + 1}"
        injects.append(node)
    return {
        "title": "Chain",
        "injects": injects,
        "start_inject_id": "n0",
    }


def test_deep_linear_chain_validates_without_recursion_error():
    # Far beyond the old recursive DFS limit; must validate, not raise RecursionError.
    definition = ScenarioDefinition.model_validate(_linear_chain(3000))
    assert len(definition.injects) == 3000


def test_cycle_is_reported_as_validation_error():
    data = {
        "title": "Cyclic",
        "injects": [
            {"id": "a", "title": "A", "content": "x", "next_inject_id": "b", "options": []},
            {"id": "b", "title": "B", "content": "x", "next_inject_id": "a", "options": []},
        ],
        "start_inject_id": "a",
    }
    with pytest.raises(ValidationError, match="Cycle detected"):
        ScenarioDefinition.model_validate(data)


def test_option_cycle_is_detected():
    data = {
        "title": "Cyclic options",
        "injects": [
            {
                "id": "a",
                "title": "A",
                "content": "x",
                "options": [{"id": "o", "label": "go", "next_inject_id": "a"}],
            },
        ],
        "start_inject_id": "a",
    }
    with pytest.raises(ValidationError, match="Cycle detected"):
        ScenarioDefinition.model_validate(data)


def test_too_many_injects_is_rejected():
    with pytest.raises(ValidationError, match="max"):
        ScenarioDefinition.model_validate(_linear_chain(MAX_INJECTS + 1))


def test_duplicate_inject_id_is_rejected():
    data = {
        "title": "Duplicates",
        "injects": [
            {"id": "a", "title": "A", "content": "x", "options": []},
            {"id": "a", "title": "A again", "content": "y", "options": []},
        ],
        "start_inject_id": "a",
    }
    with pytest.raises(ValidationError, match="duplicate inject id"):
        ScenarioDefinition.model_validate(data)


def test_cycle_in_unreachable_island_is_detected():
    # b <-> c form a cycle but are not reachable from the start inject 'a'.
    data = {
        "title": "Unreachable cycle",
        "injects": [
            {"id": "a", "title": "A", "content": "x", "options": []},
            {"id": "b", "title": "B", "content": "x", "next_inject_id": "c", "options": []},
            {"id": "c", "title": "C", "content": "x", "next_inject_id": "b", "options": []},
        ],
        "start_inject_id": "a",
    }
    with pytest.raises(ValidationError, match="Cycle detected"):
        ScenarioDefinition.model_validate(data)
