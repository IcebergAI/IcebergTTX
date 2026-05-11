from typing import Annotated

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from app.database import get_session
from app.dependencies import require_role
from app.models.user import User, UserRole

router = APIRouter(prefix="/users", tags=["users"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
SessionDep = Annotated[Session, Depends(get_session)]


def _user_out(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role,
        "team": u.team,
        "is_active": u.is_active,
    }


@router.get("")
def list_users(_: FacilitatorDep, session: SessionDep):
    return [_user_out(u) for u in session.exec(select(User)).all()]
