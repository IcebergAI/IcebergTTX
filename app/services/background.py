"""Fire-and-forget task management (#20).

``asyncio.create_task`` only registers a *weak* reference in the event loop, so a
task with no other reference can be garbage-collected mid-flight. ``spawn`` holds
a strong reference until the task completes, which is the pattern every
background call site (LLM pipeline, delayed comms, audit persistence) needs.

This does not provide durability across process restarts — a delayed task is
still lost if the single process dies (see the task-queue note in CLAUDE.md). It
only guarantees the task is not dropped by the garbage collector while the
process is alive.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Schedule ``coro`` and retain a strong reference until it finishes."""
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
