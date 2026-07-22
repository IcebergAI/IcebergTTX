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
from collections.abc import Coroutine, Iterable
from typing import Any

_tasks: set[asyncio.Task] = set()
_limited_tasks: dict[str, set[asyncio.Task]] = {}


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


async def drain(*, timeout: float = 10.0, extra: Iterable[asyncio.Task] = ()) -> None:
    """Wait for in-flight spawned tasks to settle, then cancel any stragglers (#250).

    Called by the lifespan shutdown so queued background work — mail delivery, audit
    persistence, SIEM forwarding, LLM runs — and any schedule worker already past its
    sleep (passed via ``extra``) can commit before the loop closes. Bounded by ``timeout``
    so a wedged task can't hang shutdown; folding ``extra`` in keeps everything inside one
    grace period rather than serialising two waits.
    """
    pending = [t for t in _tasks if not t.done()]
    pending.extend(
        task for tasks in _limited_tasks.values() for task in tasks if not task.done()
    )
    pending.extend(t for t in extra if not t.done())
    if not pending:
        return
    _, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in still_pending:
        task.cancel()
    if still_pending:
        await asyncio.gather(*still_pending, return_exceptions=True)
