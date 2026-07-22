"""background.drain: shutdown lets queued work settle, bounds stragglers (#250)."""

import asyncio

from app.services import background


async def test_drain_awaits_pending_spawned_work():
    """A drain waits for in-flight spawned tasks to finish before returning."""
    done: list[int] = []

    async def work(n: int) -> None:
        await asyncio.sleep(0)
        done.append(n)

    t1 = background.spawn(work(1))
    t2 = background.spawn(work(2))

    await background.drain()

    assert t1.done() and t2.done()
    assert sorted(done) == [1, 2]


async def test_drain_cancels_a_straggler_past_the_grace():
    """A task that outlasts the grace period is cancelled rather than hanging shutdown."""
    started = asyncio.Event()

    async def wedged() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = background.spawn(wedged())
    await asyncio.wait_for(started.wait(), timeout=5)

    await background.drain(timeout=0.05)

    assert task.cancelled()


async def test_drain_folds_in_extra_tasks():
    """Tasks passed via ``extra`` (e.g. schedule workers) drain in the same grace period."""
    done: list[str] = []

    async def work() -> None:
        await asyncio.sleep(0)
        done.append("extra")

    extra = asyncio.ensure_future(work())

    await background.drain(extra=[extra])

    assert extra.done() and extra.exception() is None
    assert done == ["extra"]


async def test_drain_is_a_noop_with_nothing_pending():
    """With no in-flight work the drain returns immediately without error."""
    await background.drain()
