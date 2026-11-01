import asyncio

import pytest

from ns_common.errors import NsError
from ns_supervisor.lock import GpuLock


def test_free_lock_is_granted_immediately() -> None:
    async def main() -> None:
        lock = GpuLock()
        await lock.acquire("a", "llama", 1)
        assert lock.state.owner == "a" and lock.state.service == "llama"
        assert lock.release("a") is True
        assert lock.state.owner is None and lock.state.service is None

    asyncio.run(main())


def test_waiters_are_served_fifo() -> None:
    async def main() -> list[str]:
        lock = GpuLock()
        order: list[str] = []
        await lock.acquire("holder", "llama", 1)

        async def want(owner: str) -> None:
            await lock.acquire(owner, "tts", 5)
            order.append(owner)
            await asyncio.sleep(0)
            lock.release(owner)

        tasks = []
        for name in ("w1", "w2", "w3"):
            tasks.append(asyncio.create_task(want(name)))
            await asyncio.sleep(0)  # let each waiter enqueue before the next
        assert lock.waiters() == [("w1", "tts"), ("w2", "tts"), ("w3", "tts")]
        lock.release("holder")
        await asyncio.gather(*tasks)
        return order

    assert asyncio.run(main()) == ["w1", "w2", "w3"]


def test_timeout_raises_e201_and_queue_survives() -> None:
    async def main() -> None:
        lock = GpuLock()
        await lock.acquire("holder", "llama", 1)

        async def late() -> None:
            await lock.acquire("late", "tts", 5)

        slow = asyncio.create_task(late())
        await asyncio.sleep(0)
        with pytest.raises(NsError) as ei:
            await lock.acquire("impatient", "tts", 0.05)
        assert ei.value.code == "NS-E201"
        assert lock.waiters() == [("late", "tts")]
        lock.release("holder")
        await slow
        assert lock.state.owner == "late"

    asyncio.run(main())


def test_zero_timeout_fails_at_once_when_busy() -> None:
    async def main() -> None:
        lock = GpuLock()
        await lock.acquire("holder", "llama", None)
        with pytest.raises(NsError) as ei:
            await lock.acquire("other", "tts", 0)
        assert ei.value.code == "NS-E201"
        assert lock.waiters() == []

    asyncio.run(main())


def test_release_by_non_owner_is_noop() -> None:
    async def main() -> None:
        lock = GpuLock()
        await lock.acquire("a", "llama", 1)
        assert lock.release("b") is False
        assert lock.state.owner == "a"

    asyncio.run(main())


def test_same_owner_reacquire_swaps_service() -> None:
    async def main() -> None:
        lock = GpuLock()
        await lock.acquire("a", "llama", 1)
        await lock.acquire("a", "tts", 1)
        assert lock.state.owner == "a" and lock.state.service == "tts"

    asyncio.run(main())


def test_cancelled_waiter_is_skipped() -> None:
    async def main() -> None:
        lock = GpuLock()
        await lock.acquire("holder", "llama", 1)
        first = asyncio.create_task(lock.acquire("gone", "tts", 5))
        second = asyncio.create_task(lock.acquire("stays", "tts", 5))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        lock.release("holder")
        await second
        assert lock.state.owner == "stays"

    asyncio.run(main())
