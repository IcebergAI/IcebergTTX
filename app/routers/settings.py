from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_actual_role
from app.models.user import User, UserRole
from app.routers.exercises import _exercise_out
from app.routers.scenarios import _scenario_detail
from app.services.sample_service import (
    create_sample_demo_exercise,
    list_sample_definitions,
    load_sample_scenario,
    sample_summary,
)

router = APIRouter(prefix="/settings", tags=["settings"])

ActualFacilitatorDep = Annotated[User, Depends(require_actual_role(UserRole.facilitator))]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/samples/scenarios")
def list_sample_scenarios(_: ActualFacilitatorDep):
    return [
        sample_summary(sample_id, definition)
        for sample_id, definition in list_sample_definitions()
    ]


@router.post("/samples/scenarios/{sample_id}/load", status_code=status.HTTP_201_CREATED)
async def load_sample(sample_id: str, current_user: ActualFacilitatorDep, session: SessionDep):
    assert current_user.id is not None
    try:
        scenario, created = await load_sample_scenario(
            session, sample_id=sample_id, created_by=current_user.id
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sample scenario not found"
        ) from None
    return {"created": created, "scenario": _scenario_detail(scenario)}


@router.post("/samples/scenarios/{sample_id}/demo-exercise", status_code=status.HTTP_201_CREATED)
async def create_demo_exercise(
    sample_id: str,
    current_user: ActualFacilitatorDep,
    session: SessionDep,
):
    assert current_user.id is not None
    try:
        scenario, exercise = await create_sample_demo_exercise(
            session, sample_id=sample_id, created_by=current_user.id
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sample scenario not found"
        ) from None
    return {
        "scenario": _scenario_detail(scenario),
        "exercise": _exercise_out(exercise),
    }
