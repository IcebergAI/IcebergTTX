import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import WebSocket


@dataclass
class Connection:
    ws: WebSocket
    user_id: int
    role: str
    group_id: str | None
    last_ping: datetime


class ConnectionManager:
    def __init__(self) -> None:
        # exercise_id -> live connections
        self._rooms: dict[int, list[Connection]] = {}

    async def connect(
        self,
        ws: WebSocket,
        exercise_id: int,
        user_id: int,
        role: str,
        group_id: str | None,
    ) -> None:
        await ws.accept()
        self._rooms.setdefault(exercise_id, []).append(
            Connection(
                ws=ws,
                user_id=user_id,
                role=role,
                group_id=group_id,
                last_ping=datetime.now(UTC),
            )
        )

    def disconnect(self, ws: WebSocket, exercise_id: int) -> None:
        conns = self._rooms.get(exercise_id, [])
        self._rooms[exercise_id] = [c for c in conns if c.ws is not ws]

    async def close_user_connections(self, user_id: int, code: int = 4003) -> None:
        """Close every live socket for a user after authorization changes.

        Remove the connections from all room indexes before sending close frames,
        so concurrent broadcasters cannot continue treating a downgraded user's
        cached WebSocket role as authoritative.
        """
        targets: dict[int, Connection] = {}
        for exercise_id, conns in self._rooms.items():
            for connection in conns:
                if connection.user_id == user_id:
                    targets[id(connection.ws)] = connection
            self._rooms[exercise_id] = [c for c in conns if c.user_id != user_id]

        for connection in targets.values():
            try:
                await connection.ws.close(code=code)
            except Exception:  # nosec B110 - best-effort close of a dead socket
                pass

    def ping(self, ws: WebSocket, exercise_id: int) -> None:
        for c in self._rooms.get(exercise_id, []):
            if c.ws is ws:
                c.last_ping = datetime.now(UTC)
                return

    def refresh_authorization(
        self, ws: WebSocket, exercise_id: int, *, role: str, group_id: str | None
    ) -> None:
        """Replace the connection's authorization snapshot after a heartbeat check."""
        for connection in self._rooms.get(exercise_id, []):
            if connection.ws is ws:
                connection.role = role
                connection.group_id = group_id
                return

    def _matching(
        self, exercise_id: int, predicate: Callable[[Connection], bool]
    ) -> list[Connection]:
        """Connections satisfying `predicate`, de-duplicated by socket identity."""
        seen: set[int] = set()
        out: list[Connection] = []
        for c in self._rooms.get(exercise_id, []):
            if not predicate(c) or id(c.ws) in seen:
                continue
            seen.add(id(c.ws))
            out.append(c)
        return out

    async def broadcast_to_exercise(self, exercise_id: int, message: dict) -> None:
        await self._send_to_many(self._rooms.get(exercise_id, []), message)

    async def broadcast_to_groups(
        self, exercise_id: int, group_ids: list[str], message: dict
    ) -> None:
        # Facilitators and observers have global read-visibility of injects/comments
        # (see is_inject_visible_to_user), so they receive group-scoped pushes too —
        # otherwise observers see a group-targeted inject on load but miss its live
        # inject_released frame (#38).
        conns = [
            c
            for c in self._rooms.get(exercise_id, [])
            if c.group_id in group_ids or c.role in ("facilitator", "observer")
        ]
        await self._send_to_many(conns, message)

    async def send_to_facilitators(self, exercise_id: int, message: dict) -> None:
        conns = self._matching(exercise_id, lambda c: c.role == "facilitator")
        await self._send_to_many(conns, message)

    async def send_to_facilitators_and_user(
        self, exercise_id: int, user_id: int | None, message: dict
    ) -> None:
        conns = self._matching(
            exercise_id, lambda c: c.role == "facilitator" or c.user_id == user_id
        )
        await self._send_to_many(conns, message)

    async def send_to_facilitators_user_and_groups(
        self,
        exercise_id: int,
        user_id: int | None,
        group_ids: list[str],
        message: dict,
    ) -> None:
        conns = self._matching(
            exercise_id,
            lambda c: (
                c.role == "facilitator"
                or c.user_id == user_id
                or c.group_id in group_ids
            ),
        )
        await self._send_to_many(conns, message)

    async def _send_to_many(self, conns: list[Connection], message: dict) -> None:
        dead: list[Connection] = []
        for c in conns:
            try:
                await c.ws.send_json(message)
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
            live: list[Connection] = []
            for c in conns:
                idle = (now - c.last_ping).total_seconds()
                if idle > max_idle_seconds:
                    try:
                        await c.ws.close()
                    # best-effort close of an already-dead socket
                    except Exception:  # nosec B110
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
