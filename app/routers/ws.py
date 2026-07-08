from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import resolve_user_from_token
from app.middleware import origin_allowed
from app.models.user import UserRole
from app.services.access_control import (
    exercise_member_for_user,
    is_actual_facilitator,
    require_exercise_access,
)
from app.services.ws_manager import manager

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.websocket("/ws/exercises/{exercise_id}")
async def exercise_ws(
    ws: WebSocket,
    exercise_id: int,
    session: SessionDep,
    token: Annotated[str | None, Query()] = None,
    view_role: Annotated[str | None, Query()] = None,
    view_team: Annotated[str | None, Query()] = None,
):
    # Auth source (#68): browsers can't set headers on a WS upgrade, so they rely
    # on the httpOnly `access_token` cookie the browser already sends — keeping the
    # JWT out of the URL (and out of proxy access logs). An explicit `?token=` is a
    # fallback for non-browser clients. The cookie path is ambient, so it gets a
    # CSWSH Origin check (mirroring CSRFOriginMiddleware); the explicit-token path,
    # like a Bearer header, is exempt.
    if token:
        auth_token = token
    elif cookie_token := ws.cookies.get("access_token"):
        if not origin_allowed(ws.headers.get("origin"), ws.headers.get("host")):
            await ws.close(code=4003)
            return
        auth_token = cookie_token
    else:
        await ws.close(code=4001)
        return

    user = await resolve_user_from_token(auth_token, session, view_role, view_team)
    if user is None:
        await ws.close(code=4001)
        return
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
    user_id = user.id
    role = user.role.value

    # The receive loop below never touches the DB, but a WebSocket handler's
    # dependency-injected session stays open until the socket disconnects (up to
    # 24h). Release the pooled connection now so long-lived sockets can't exhaust
    # the pool under normal concurrency (#35).
    await session.close()

    await manager.connect(ws, exercise_id, user_id=user_id, role=role, group_id=group_id)
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
