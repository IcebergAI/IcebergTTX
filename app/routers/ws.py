from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlmodel import Session, select

from app.database import get_session
from app.models.user import User
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

    await manager.connect(
        ws, exercise_id, user_id=user.id, role=user.role.value, team=user.team
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
