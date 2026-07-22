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


async def test_drain_folds_in_collect_extra_tasks():
    """Tasks surfaced by ``collect_extra`` (e.g. schedule workers) drain in the same pass."""
    done: list[str] = []

    async def work() -> None:
        await asyncio.sleep(0)
        done.append("extra")

    extra = asyncio.ensure_future(work())

    await background.drain(collect_extra=lambda: [extra])

    assert extra.done() and extra.exception() is None
    assert done == ["extra"]


async def test_drain_awaits_children_spawned_during_the_drain():
    """A task that spawns more work while finishing must not outlive the drain (#250).

    Encodes the review's probe: a child spawned by an already-draining parent (mirroring a
    mid-release worker firing audit writes / arming comm timers) is re-collected and awaited
    rather than left pending after ``drain`` returns — which would let ``engine.dispose``
    run out from under it.
    """
    child_done: list[bool] = []

    async def child() -> None:
        await asyncio.sleep(0.01)
        child_done.append(True)

    async def parent() -> None:
        await asyncio.sleep(0.01)
        background.spawn(child())  # spawned after the drain's first snapshot

    background.spawn(parent())
    await asyncio.sleep(0)

    await background.drain()

    assert child_done == [True]


async def test_drain_stays_bounded_against_a_cancellation_resistant_task():
    """A task that swallows CancelledError cannot hang shutdown past the bound (#250).

    Encodes the review's other probe: the post-cancel wait is bounded, so ``drain`` returns
    within the grace window even though the task keeps running well past it.
    """
    started = asyncio.Event()

    async def stubborn() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(30)  # ignore the cancel and keep going past the grace

    task = background.spawn(stubborn())
    await asyncio.wait_for(started.wait(), timeout=5)

    # Bounded: returns within the cancel grace, not blocked on the resistant task.
    await asyncio.wait_for(
        background.drain(timeout=0.02), timeout=background._DRAIN_CANCEL_GRACE + 2
    )

    task.cancel()  # cleanup; the second sleep is not guarded, so this one lands


async def test_drain_is_a_noop_with_nothing_pending():
    """With no in-flight work the drain returns immediately without error."""
    await background.drain()
