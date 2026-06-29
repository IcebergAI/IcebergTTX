from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.models.user import User, UserRole
from app.services.access_control import (
    exercise_member_for_user,
    is_actual_facilitator,
    require_exercise_access,
)
from app.services.auth_service import decode_access_token
from app.services.role_preview import apply_role_preview
from app.services.ws_manager import manager

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.websocket("/ws/exercises/{exercise_id}")
async def exercise_ws(
    ws: WebSocket,
    exercise_id: int,
    session: SessionDep,
    token: Annotated[str, Query()],
    view_role: Annotated[str | None, Query()] = None,
    view_team: Annotated[str | None, Query()] = None,
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

    user = (await session.exec(select(User).where(User.email == email))).first()
    if not user or not user.is_active:
        await ws.close(code=4001)
        return
    user = apply_role_preview(user, view_role, view_team)
    try:
        await require_exercise_access(session, exercise_id, user)
    except Exception:
        await ws.close(code=4003)
        return
    assert user.id is not None
    member = await exercise_member_for_user(session, exercise_id, user.id)
    group_id = member.group_id if member else None
    if user.role == UserRole.participant:
        # A real participant is bucketed strictly by their enrolled group; only a
        # facilitator *previewing* as a participant derives the group from the
        # (validated-via-apply_role_preview) preview team. This prevents a genuine
        # participant from subscribing to another team's broadcasts via view_team (#30).
        if is_actual_facilitator(user):
            group_id = group_id or user.team
        else:
            group_id = member.group_id if member else user.team

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
