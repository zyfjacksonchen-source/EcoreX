from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ecorex.runtime.image_execution import ImageExecutionPool


class _Executions:
    def __init__(self) -> None:
        self.completed: dict[str, object] = {}
        self.failed: dict[str, str] = {}

    def complete(self, execution_id: str, value: object) -> None:
        self.completed[execution_id] = value

    def fail(self, execution_id: str, *, error_code: str) -> None:
        self.failed[execution_id] = error_code


class _Jobs:
    def get(self, _job_id: str):
        return SimpleNamespace(status=SimpleNamespace(value="retry_scheduled"))


def test_two_blocked_images_do_not_consume_the_ordinary_execution_lane() -> None:
    async def scenario() -> None:
        executions = _Executions()
        pool = ImageExecutionPool(
            executions,  # type: ignore[arg-type]
            _Jobs(),
            concurrency=2,
            queue_capacity=2,
            timeout_seconds=5,
            cancellation_poll_seconds=0.05,
        )
        started = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()

        async def blocked(index: int):
            started[index].set()
            await release.wait()
            return SimpleNamespace(value={"image": index})

        assert await pool.submit(
            execution_id="image-0",
            job_id="job-0",
            invoke=lambda: blocked(0),
        ) == "accepted"
        assert await pool.submit(
            execution_id="image-1",
            job_id="job-1",
            invoke=lambda: blocked(1),
        ) == "accepted"
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in started)), timeout=1
        )

        # This models the ordinary AgentTurnWorker lane.  Both image slots are
        # blocked, but no image awaitable or semaphore is held by this task.
        ordinary_completed = asyncio.Event()

        async def ordinary_turn() -> None:
            await asyncio.sleep(0)
            ordinary_completed.set()

        await asyncio.wait_for(ordinary_turn(), timeout=0.2)
        assert ordinary_completed.is_set()
        assert pool.snapshot().running == 2

        release.set()
        await asyncio.wait_for(pool._queue.join(), timeout=1)
        assert executions.completed == {
            "image-0": {"image": 0},
            "image-1": {"image": 1},
        }
        assert executions.failed == {}
        await pool.close()

    asyncio.run(scenario())


def test_image_queue_has_explicit_backpressure_and_deduplicates_retries() -> None:
    async def scenario() -> None:
        executions = _Executions()
        pool = ImageExecutionPool(
            executions,  # type: ignore[arg-type]
            _Jobs(),
            concurrency=1,
            queue_capacity=1,
            timeout_seconds=5,
            cancellation_poll_seconds=0.05,
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked():
            started.set()
            await release.wait()
            return {"ok": True}

        assert await pool.submit(
            execution_id="running",
            job_id="job-running",
            invoke=blocked,
        ) == "accepted"
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await pool.submit(
            execution_id="queued",
            job_id="job-queued",
            invoke=blocked,
        ) == "accepted"
        assert await pool.submit(
            execution_id="queued",
            job_id="job-queued",
            invoke=blocked,
        ) == "already_pending"
        assert await pool.submit(
            execution_id="rejected",
            job_id="job-rejected",
            invoke=blocked,
        ) == "queue_full"
        snapshot = pool.snapshot()
        assert snapshot.queue_capacity == 1
        assert snapshot.queued == 1
        assert snapshot.running == 1
        assert snapshot.rejected == 1

        release.set()
        await asyncio.wait_for(pool._queue.join(), timeout=1)
        await pool.close()

    asyncio.run(scenario())


def test_started_image_execution_is_resubmitted_after_pool_restart() -> None:
    async def scenario() -> None:
        executions = _Executions()
        jobs = _Jobs()
        first = ImageExecutionPool(
            executions,  # type: ignore[arg-type]
            jobs,
            concurrency=1,
            queue_capacity=1,
            timeout_seconds=5,
            cancellation_poll_seconds=0.05,
        )
        started = asyncio.Event()
        never = asyncio.Event()

        async def interrupted():
            started.set()
            await never.wait()
            return {"unreachable": True}

        assert await first.submit(
            execution_id="durable-started",
            job_id="job-restart",
            invoke=interrupted,
        ) == "accepted"
        await asyncio.wait_for(started.wait(), timeout=1)
        await first.close()
        assert executions.completed == {}
        assert executions.failed == {}

        recovered = ImageExecutionPool(
            executions,  # type: ignore[arg-type]
            jobs,
            concurrency=1,
            queue_capacity=1,
            timeout_seconds=5,
            cancellation_poll_seconds=0.05,
        )
        assert await recovered.submit(
            execution_id="durable-started",
            job_id="job-restart",
            invoke=lambda: asyncio.sleep(0, result={"recovered": True}),
        ) == "accepted"
        await asyncio.wait_for(recovered._queue.join(), timeout=1)
        assert executions.completed == {
            "durable-started": {"recovered": True}
        }
        await recovered.close()

    asyncio.run(scenario())
