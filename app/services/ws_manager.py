import asyncio
from datetime import UTC, datetime

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        # exercise_id -> list of (websocket, user_id, role, teams, last_ping)
        self._rooms: dict[int, list[dict]] = {}

    async def connect(
        self,
        ws: WebSocket,
        exercise_id: int,
        user_id: int,
        role: str,
        team: str | None,
    ) -> None:
        await ws.accept()
        self._rooms.setdefault(exercise_id, []).append(
            {
                "ws": ws,
                "user_id": user_id,
                "role": role,
                "team": team,
                "last_ping": datetime.now(UTC),
            }
        )

    def disconnect(self, ws: WebSocket, exercise_id: int) -> None:
        conns = self._rooms.get(exercise_id, [])
        self._rooms[exercise_id] = [c for c in conns if c["ws"] is not ws]

    def ping(self, ws: WebSocket, exercise_id: int) -> None:
        for c in self._rooms.get(exercise_id, []):
            if c["ws"] is ws:
                c["last_ping"] = datetime.now(UTC)
                return

    async def broadcast_to_exercise(self, exercise_id: int, message: dict) -> None:
        await self._send_to_many(self._rooms.get(exercise_id, []), message)

    async def broadcast_to_teams(
        self, exercise_id: int, teams: list[str], message: dict
    ) -> None:
        conns = [
            c
            for c in self._rooms.get(exercise_id, [])
            if c["team"] in teams or c["role"] == "facilitator"
        ]
        await self._send_to_many(conns, message)

    async def send_to_facilitators(self, exercise_id: int, message: dict) -> None:
        conns = [
            c for c in self._rooms.get(exercise_id, []) if c["role"] == "facilitator"
        ]
        await self._send_to_many(conns, message)

    async def _send_to_many(self, conns: list[dict], message: dict) -> None:
        dead: list[dict] = []
        for c in conns:
            try:
                await c["ws"].send_json(message)
            except Exception:
                dead.append(c)
        for c in dead:
            for room in self._rooms.values():
                try:
                    room.remove(c)
                except ValueError:
                    pass

    async def prune_stale(self, max_idle_seconds: int = 90) -> None:
        """Close and remove connections that haven't pinged recently."""
        now = datetime.now(UTC)
        for exercise_id, conns in list(self._rooms.items()):
            live: list[dict] = []
            for c in conns:
                idle = (now - c["last_ping"]).total_seconds()
                if idle > max_idle_seconds:
                    try:
                        await c["ws"].close()
                    except Exception:
                        pass
                else:
                    live.append(c)
            self._rooms[exercise_id] = live


manager = ConnectionManager()


async def heartbeat_task() -> None:
    """Background task: prune stale connections every 30 s."""
    while True:
        await asyncio.sleep(30)
        await manager.prune_stale()
