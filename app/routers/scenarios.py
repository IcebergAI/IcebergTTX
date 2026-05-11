import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel as _BaseModel
from pydantic import ValidationError
from sqlmodel import Session, select

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.scenario import Scenario
from app.models.user import User, UserRole
from app.schemas.scenario_json import ScenarioDefinition
from app.services.scenario_service import (
    create_scenario,
    export_definition,
    parse_definition,
    update_scenario,
)


class _ImportBody(_BaseModel):
    definition: ScenarioDefinition


router = APIRouter(prefix="/scenarios", tags=["scenarios"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[Session, Depends(get_session)]


def _scenario_summary(scenario: Scenario) -> dict:
    return {
        "id": scenario.id,
        "title": scenario.title,
        "description": scenario.description,
        "version": scenario.version,
        "tags": json.loads(scenario.tags) if scenario.tags else [],
        "created_by": scenario.created_by,
        "created_at": scenario.created_at.isoformat(),
        "updated_at": scenario.updated_at.isoformat(),
    }


def _scenario_detail(scenario: Scenario) -> dict:
    summary = _scenario_summary(scenario)
    summary["definition"] = json.loads(scenario.definition)
    return summary


def _get_or_404(session: Session, scenario_id: int) -> Scenario:
    scenario = session.get(Scenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scenario not found")
    return scenario


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("")
def list_scenarios(_: CurrentUserDep, session: SessionDep):
    scenarios = session.exec(select(Scenario)).all()
    return [_scenario_summary(s) for s in scenarios]


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: ScenarioDefinition, current_user: FacilitatorDep, session: SessionDep):
    scenario = create_scenario(session, definition=body, created_by=current_user.id)
    return _scenario_detail(scenario)


# ── Import from JSON body ─────────────────────────────────────────────────────

@router.post("/import", status_code=status.HTTP_201_CREATED)
def import_scenario(body: _ImportBody, current_user: FacilitatorDep, session: SessionDep):
    scenario = create_scenario(session, definition=body.definition, created_by=current_user.id)
    return _scenario_detail(scenario)


# ── Get ───────────────────────────────────────────────────────────────────────

@router.get("/{scenario_id}")
def get_scenario(scenario_id: int, _: CurrentUserDep, session: SessionDep):
    return _scenario_detail(_get_or_404(session, scenario_id))


# ── Update ────────────────────────────────────────────────────────────────────

@router.put("/{scenario_id}")
def update(scenario_id: int, body: ScenarioDefinition, _: FacilitatorDep, session: SessionDep):
    scenario = _get_or_404(session, scenario_id)
    scenario = update_scenario(session, scenario, definition=body)
    return _scenario_detail(scenario)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(scenario_id: int, _: FacilitatorDep, session: SessionDep):
    scenario = _get_or_404(session, scenario_id)
    session.delete(scenario)
    session.commit()


# ── Export as downloadable JSON ───────────────────────────────────────────────

@router.get("/{scenario_id}/export")
def export(scenario_id: int, _: FacilitatorDep, session: SessionDep):
    scenario = _get_or_404(session, scenario_id)
    definition = export_definition(scenario)
    import re
    safe = re.sub(r"[^\w\-]", "_", scenario.title.lower())
    filename = f"{safe}.json"
    return JSONResponse(
        content=definition.model_dump(),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Validate ──────────────────────────────────────────────────────────────────

@router.get("/{scenario_id}/validate")
def validate(scenario_id: int, _: FacilitatorDep, session: SessionDep):
    scenario = _get_or_404(session, scenario_id)
    try:
        parse_definition(scenario.definition)
        return {"valid": True, "errors": []}
    except (ValidationError, ValueError) as exc:
        return {"valid": False, "errors": str(exc)}
