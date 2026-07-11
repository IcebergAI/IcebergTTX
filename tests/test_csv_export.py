import csv
import io

import pytest
from httpx import AsyncClient

from app.services.csv_export import spreadsheet_safe_cell, spreadsheet_safe_row


@pytest.mark.parametrize(
    "value",
    [
        "=1+1",
        "+SUM(A1:A2)",
        "-1+2",
        "@SUM(A1:A2)",
        "  =1+1",
        "\tbenign-looking",
        "  \tbenign-looking",
        "\r\n@SUM(A1:A2)",
        "\u200b=1+1",
        "\x00+1+1",
        "\uff1d1+1",
        "\uff0bSUM(A1:A2)",
        "\uff20SUM(A1:A2)",
    ],
)
def test_spreadsheet_safe_cell_neutralizes_formula_variants(value: str):
    assert spreadsheet_safe_cell(value) == "'" + value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ordinary text", "ordinary text"),
        ("text = 1", "text = 1"),
        ("'=1+1", "'=1+1"),
        ("", ""),
        (42, 42),
        (None, None),
    ],
)
def test_spreadsheet_safe_cell_preserves_inert_values(value: object, expected: object):
    assert spreadsheet_safe_cell(value) == expected


def test_spreadsheet_safe_row_applies_the_boundary_to_every_cell():
    assert spreadsheet_safe_row(["safe", "=1+1", 7]) == ["safe", "'=1+1", 7]


async def test_csv_export_neutralizes_participant_response(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise,
):
    owner_headers = {"Authorization": f"Bearer {facilitator_token}"}
    participant_headers = {"Authorization": f"Bearer {participant_token}"}
    injects = (
        await client.get(f"/api/exercises/{active_exercise.id}/injects", headers=owner_headers)
    ).json()
    pending = next(inject for inject in injects if inject["state"] == "pending")
    released = await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{pending['id']}/release",
        headers=owner_headers,
    )
    assert released.status_code == 200

    formula = "\u200b  \uff1d1+1"
    submitted = await client.post(
        f"/api/exercises/{active_exercise.id}/responses",
        json={"inject_id": pending["id"], "content": formula},
        headers=participant_headers,
    )
    assert submitted.status_code == 201

    exported = await client.get(
        f"/api/exercises/{active_exercise.id}/export.csv", headers=owner_headers
    )
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.text)))
    assert len(rows) == 1
    assert rows[0]["content"] == "'" + formula
