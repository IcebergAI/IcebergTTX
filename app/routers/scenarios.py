import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel as _BaseModel
from pydantic import ValidationError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user, require_actual_role
from app.models.exercise import Exercise
from app.models.scenario import Scenario
from app.models.user import User, UserRole
from app.schemas.api import ScenarioDetail, ScenarioSummary
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

FacilitatorDep = Annotated[User, Depends(require_actual_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _scenario_summary(scenario: Scenario) -> dict:
    definition = json.loads(scenario.definition)
    injects = definition.get("injects", [])
    return {
        "id": scenario.id,
        "title": scenario.title,
        "description": scenario.description,
        "version": scenario.version,
        "tags": scenario.tags or [],
        "inject_count": len(injects),
        "branch_count": sum(1 for inj in injects if len(inj.get("options", [])) > 1),
        "created_by": scenario.created_by,
        "created_at": scenario.created_at.isoformat(),
        "updated_at": scenario.updated_at.isoformat(),
    }


def _scenario_detail(scenario: Scenario) -> dict:
    summary = _scenario_summary(scenario)
    summary["definition"] = json.loads(scenario.definition)
    return summary


async def _get_or_404(session: AsyncSession, scenario_id: int) -> Scenario:
    scenario = await session.get(Scenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scenario not found")
    return scenario


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[ScenarioSummary])
async def list_scenarios(_: FacilitatorDep, session: SessionDep):
    scenarios = (await session.exec(select(Scenario))).all()
    return [_scenario_summary(s) for s in scenarios]


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", status_code=status.HTTP_201_CREATED, response_model=ScenarioDetail)
async def create(body: ScenarioDefinition, current_user: FacilitatorDep, session: SessionDep):
    assert current_user.id is not None
    scenario = await create_scenario(session, definition=body, created_by=current_user.id)
    return _scenario_detail(scenario)


# ── Import from JSON body ─────────────────────────────────────────────────────

@router.post("/import", status_code=status.HTTP_201_CREATED, response_model=ScenarioDetail)
async def import_scenario(body: _ImportBody, current_user: FacilitatorDep, session: SessionDep):
    assert current_user.id is not None
    scenario = await create_scenario(
        session, definition=body.definition, created_by=current_user.id
    )
    return _scenario_detail(scenario)


# ── Get ───────────────────────────────────────────────────────────────────────

@router.get("/{scenario_id}", response_model=ScenarioDetail)
async def get_scenario(scenario_id: int, _: FacilitatorDep, session: SessionDep):
    return _scenario_detail(await _get_or_404(session, scenario_id))


# ── Update ────────────────────────────────────────────────────────────────────

@router.put("/{scenario_id}", response_model=ScenarioDetail)
async def update(
    scenario_id: int, body: ScenarioDefinition, current_user: FacilitatorDep, session: SessionDep
):
    scenario = await _get_or_404(session, scenario_id)
    assert current_user.id is not None
    scenario = await update_scenario(
        session,
        scenario,
        definition=body,
        updated_by=current_user.id,
    )
    return _scenario_detail(scenario)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{scenario_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(scenario_id: int, _: FacilitatorDep, session: SessionDep):
    scenario = await _get_or_404(session, scenario_id)
    in_use = (
        await session.exec(select(Exercise.id).where(Exercise.scenario_id == scenario_id))
    ).first()
    if in_use is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scenario is in use by one or more exercises",
        )
    await session.delete(scenario)
    await session.commit()


# ── Export as downloadable JSON ───────────────────────────────────────────────

@router.get("/{scenario_id}/export")
async def export(scenario_id: int, _: FacilitatorDep, session: SessionDep):
    scenario = await _get_or_404(session, scenario_id)
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
async def validate(scenario_id: int, _: FacilitatorDep, session: SessionDep):
    scenario = await _get_or_404(session, scenario_id)
    try:
        parse_definition(scenario.definition)
        return {"valid": True, "errors": []}
    except (ValidationError, ValueError) as exc:
        return {"valid": False, "errors": str(exc)}
