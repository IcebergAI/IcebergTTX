"""Fire-and-forget task management (#20).

``asyncio.create_task`` only registers a *weak* reference in the event loop, so a
task with no other reference can be garbage-collected mid-flight. ``spawn`` holds
a strong reference until the task completes, which is the pattern every
background call site (LLM pipeline, mail delivery, audit persistence) needs.

Delayed exercise work is *not* one of them any more: an inject release or a triggered
communication also has to be cancellable and rehydratable, so ``schedule_service`` keeps
its own keyed registry, which holds the strong reference itself (#211).

This does not provide durability across process restarts — a delayed task is
still lost if the single process dies (see the task-queue note in CLAUDE.md). It
only guarantees the task is not dropped by the garbage collector while the
process is alive.
"""

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from typing import Any

_tasks: set[asyncio.Task] = set()
_limited_tasks: dict[str, set[asyncio.Task]] = {}

# After the grace period expires, stragglers are cancelled and then given this brief,
# fixed window to unwind. It must stay bounded: a task that swallows CancelledError must
# not be able to hang shutdown past engine.dispose (#250).
_DRAIN_CANCEL_GRACE = 1.0


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Schedule ``coro`` and retain a strong reference until it finishes."""
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def spawn_limited(
    coro: Coroutine[Any, Any, Any], *, bucket: str, limit: int
) -> asyncio.Task | None:
    """Schedule work only while its named best-effort bucket has capacity."""
    tasks = _limited_tasks.setdefault(bucket, set())
    if len(tasks) >= limit:
        coro.close()
        return None
    task = asyncio.ensure_future(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def drain(
    *,
    timeout: float = 10.0,
    collect_extra: Callable[[], Iterable[asyncio.Task]] = lambda: (),
) -> None:
    """Wait for in-flight spawned work — and anything it spawns while finishing — to settle,
    then cancel whatever remains, all within a bounded grace period (#250).

    Draining is not a single snapshot. A finishing task can spawn *more* background work: a
    schedule worker's release fires audit persistence and SIEM forwarding, and its dispatch
    can arm fresh triggered-communication timers. A one-shot snapshot would let those
    children outlive ``engine.dispose``, so the task sets are re-collected after every wait
    until they empty or the deadline passes. ``collect_extra`` is re-invoked each round to
    fold in tasks tracked elsewhere — the lifespan passes ``cancel_all_schedules``, which
    cancels still-sleeping timers (rehydration re-arms them next boot) and hands back any
    worker already mid-release so it can finish its commit and dispatch atomically (#218).
    Every task ``collect_extra`` has ever returned stays tracked, so a worker that spans
    rounds is never dropped even after its registry entry is cleared.

    The whole thing is bounded: the initial waits share one ``timeout`` deadline, and past
    it stragglers are cancelled and then waited on for a short fixed window — so a task that
    swallows ``CancelledError`` cannot hang shutdown indefinitely.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    tracked_extra: set[asyncio.Task] = set()

    def _pending() -> list[asyncio.Task]:
        tracked_extra.update(collect_extra())  # accumulate — never drop a returned worker
        pool = {
            *_tasks,
            *(task for bucket in _limited_tasks.values() for task in bucket),
            *tracked_extra,
        }
        return [task for task in pool if not task.done()]

    while pending := _pending():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.wait(pending, timeout=remaining)

    stragglers = _pending()
    if stragglers:
        for task in stragglers:
            task.cancel()
        await asyncio.wait(stragglers, timeout=_DRAIN_CANCEL_GRACE)
