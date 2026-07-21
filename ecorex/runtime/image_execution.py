"""Bounded, restart-safe execution lane for long-running image tools."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .database import json_dumps
from .tool_executions import ToolExecutionRepository


@dataclass(frozen=True, slots=True)
class ImageExecutionPoolSnapshot:
    concurrency: int
    queue_capacity: int
    queued: int
    running: int
    accepted: int
    rejected: int
    completed: int
    failed: int


@dataclass(frozen=True, slots=True)
class _ImageWork:
    execution_id: str
    job_id: str
    invoke: Callable[[], Awaitable[Any]]


class ImageExecutionPool:
    """Own an isolated bounded queue without holding an Agent Turn lease.

    Durable ``ToolExecution`` rows are the authority.  The in-memory queue is
    only an execution accelerator: after a process restart a leased Turn sees
    the still-started row and safely submits the same idempotent image call
    again.  The image backend's publication key remains unchanged.
    """

    def __init__(
        self,
        repository: ToolExecutionRepository,
        jobs: Any,
        *,
        concurrency: int = 2,
        queue_capacity: int = 8,
        timeout_seconds: float = 900.0,
        cancellation_poll_seconds: float = 0.5,
    ) -> None:
        if not 1 <= concurrency <= 8:
            raise ValueError("image execution concurrency must be between one and eight")
        if not 1 <= queue_capacity <= 64:
            raise ValueError("image execution queue capacity must be between one and 64")
        if not 1 <= timeout_seconds <= 3600:
            raise ValueError("image execution timeout is invalid")
        if not 0.05 <= cancellation_poll_seconds <= 5:
            raise ValueError("image cancellation poll interval is invalid")
        self.repository = repository
        self.jobs = jobs
        self.concurrency = concurrency
        self.queue_capacity = queue_capacity
        self.timeout_seconds = timeout_seconds
        self.cancellation_poll_seconds = cancellation_poll_seconds
        self._queue: asyncio.Queue[_ImageWork] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._workers: list[asyncio.Task[None]] = []
        self._pending: set[str] = set()
        self._running: set[str] = set()
        self._settled: OrderedDict[str, str] = OrderedDict()
        self._lock = asyncio.Lock()
        self._closing = False
        self._accepted = 0
        self._rejected = 0
        self._completed = 0
        self._failed = 0

    async def submit(
        self,
        *,
        execution_id: str,
        job_id: str,
        invoke: Callable[[], Awaitable[Any]],
    ) -> str:
        """Return ``accepted``, ``already_pending`` or ``queue_full``."""

        if self._closing:
            return "queue_full"
        await self._ensure_started()
        async with self._lock:
            if execution_id in self._pending or execution_id in self._running:
                return "already_pending"
            settled = self._settled.get(execution_id)
            if settled is not None:
                return f"already_{settled}"
            try:
                self._queue.put_nowait(
                    _ImageWork(
                        execution_id=execution_id,
                        job_id=job_id,
                        invoke=invoke,
                    )
                )
            except asyncio.QueueFull:
                self._rejected += 1
                return "queue_full"
            self._pending.add(execution_id)
            self._accepted += 1
            return "accepted"

    def snapshot(self) -> ImageExecutionPoolSnapshot:
        return ImageExecutionPoolSnapshot(
            concurrency=self.concurrency,
            queue_capacity=self.queue_capacity,
            queued=self._queue.qsize(),
            running=len(self._running),
            accepted=self._accepted,
            rejected=self._rejected,
            completed=self._completed,
            failed=self._failed,
        )

    async def close(self) -> None:
        """Cancel volatile work without falsifying durable terminal state."""

        self._closing = True
        workers = tuple(self._workers)
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        self._pending.clear()
        self._running.clear()

    async def _ensure_started(self) -> None:
        if self._workers:
            return
        async with self._lock:
            if self._workers:
                return
            if self._closing:
                return
            self._workers = [
                asyncio.create_task(
                    self._worker(index),
                    name=f"ecorex-image-execution-{index}",
                )
                for index in range(self.concurrency)
            ]

    async def _worker(self, _index: int) -> None:
        while True:
            work = await self._queue.get()
            async with self._lock:
                self._pending.discard(work.execution_id)
                self._running.add(work.execution_id)
            try:
                call = await self._invoke_bounded(work)
                value = getattr(call, "value", call)
                encoded = json_dumps(value)
                if len(encoded.encode("utf-8")) > 1024 * 1024:
                    raise RuntimeError("tool_output_too_large")
                await asyncio.to_thread(
                    self.repository.complete,
                    work.execution_id,
                    value,
                )
                self._completed += 1
                await self._remember_settled(work.execution_id, "completed")
            except asyncio.CancelledError:
                if self._closing:
                    raise
                await self._fail(work.execution_id, "image_execution_cancelled")
            except TimeoutError:
                await self._fail(work.execution_id, "image_execution_timeout")
            except Exception as error:
                code = getattr(error, "code", None)
                if not isinstance(code, str) or not code:
                    code = error.__class__.__name__.casefold()
                await self._fail(work.execution_id, str(code)[:128])
            finally:
                async with self._lock:
                    self._running.discard(work.execution_id)
                self._queue.task_done()

    async def _invoke_bounded(self, work: _ImageWork) -> Any:
        invocation = asyncio.create_task(work.invoke())
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await self._cancel_invocation(invocation)
                    raise TimeoutError
                done, _ = await asyncio.wait(
                    {invocation},
                    timeout=min(self.cancellation_poll_seconds, remaining),
                )
                if done:
                    return invocation.result()
                job = await asyncio.to_thread(self.jobs.get, work.job_id)
                status = str(getattr(job.status, "value", job.status))
                if status in {
                    "completed",
                    "failed",
                    "cancelled",
                    "dead_letter",
                }:
                    await self._cancel_invocation(invocation)
                    raise asyncio.CancelledError
        except asyncio.CancelledError:
            await self._cancel_invocation(invocation)
            raise

    async def _fail(self, execution_id: str, code: str) -> None:
        try:
            try:
                await asyncio.to_thread(
                    self.repository.fail,
                    execution_id,
                    error_code=code,
                )
            except Exception:
                # Another recovered process may have completed the same
                # idempotent execution between our invocation and commit.
                # The durable terminal record wins; never crash a pool slot.
                record = await asyncio.to_thread(self.repository.get, execution_id)
                if getattr(record, "status", None) == "completed":
                    self._completed += 1
                    await self._remember_settled(execution_id, "completed")
                    return
        except Exception:
            # Repository availability is recovered by the still-started
            # ToolExecution on the next Turn lease.  Pool workers remain live.
            return
        self._failed += 1
        await self._remember_settled(execution_id, "failed")

    @staticmethod
    async def _cancel_invocation(invocation: asyncio.Task[Any]) -> None:
        invocation.cancel()
        done, _ = await asyncio.wait({invocation}, timeout=0.25)
        if not done:
            # A broken third-party coroutine may suppress cancellation.  It
            # cannot be allowed to pin the bounded image lane indefinitely.
            invocation.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )

    async def _remember_settled(self, execution_id: str, status: str) -> None:
        async with self._lock:
            self._settled[execution_id] = status
            self._settled.move_to_end(execution_id)
            while len(self._settled) > 1024:
                self._settled.popitem(last=False)
