from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from app.database import get_session
from app.models.user import User, UserRole
from app.services.access_control import exercise_member_for_user, require_exercise_access
from app.services.auth_service import decode_access_token
from app.services.ws_manager import manager

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


@router.websocket("/ws/exercises/{exercise_id}")
async def exercise_ws(
    ws: WebSocket,
    exercise_id: int,
    session: SessionDep,
    token: str = Query(...),
    view_role: str | None = Query(default=None),
    view_team: str | None = Query(default=None),
):
    try:
        payload = decode_access_token(token)
    except Exception:
        await ws.close(code=4001)
        return

    email: str | None = payload.get("sub")
    if not email:
        await ws.close(code=4001)
        return

    user = session.exec(select(User).where(User.email == email)).first()
    if not user or not user.is_active:
        await ws.close(code=4001)
        return
    if user.role == UserRole.facilitator and view_role is not None:
        actual_role = user.role
        actual_team = user.team
        try:
            effective_role = UserRole(view_role)
        except ValueError:
            effective_role = user.role
        user = user.model_copy(
            update={
                "role": effective_role,
                "team": view_team.strip() if view_team and view_team.strip() else user.team,
            }
        )
        object.__setattr__(user, "actual_role", actual_role)
        object.__setattr__(user, "actual_team", actual_team)
        object.__setattr__(user, "can_switch_roles", True)
    try:
        require_exercise_access(session, exercise_id, user)
    except Exception:
        await ws.close(code=4003)
        return
    assert user.id is not None
    member = exercise_member_for_user(session, exercise_id, user.id)
    group_id = member.group_id if member else None
    if user.role == UserRole.participant:
        group_id = view_team.strip() if view_team and view_team.strip() else group_id
        group_id = group_id or user.team

    await manager.connect(
        ws, exercise_id, user_id=user.id, role=user.role.value, group_id=group_id
    )
    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ping":
                manager.ping(ws, exercise_id)
                await ws.send_json(
                    {
                        "type": "pong",
                        "exercise_id": exercise_id,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "payload": {},
                    }
                )
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws, exercise_id)
