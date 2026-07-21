"""Lifecycle owner for the durable Agent Turn worker pool."""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .worker import AgentTurnWorker, WorkerOutcome, WorkerRunResult


@dataclass(frozen=True, slots=True)
class WorkerSupervisorSnapshot:
    running: bool
    concurrency: int
    desired_workers: int
    live_workers: int
    restarted_slots: int
    completed_runs: int
    failed_runs: int
    last_outcome: WorkerOutcome | None
    last_error: str | None


class AgentWorkerSupervisor:
    """Runs a bounded worker pool and owns its ASGI shutdown boundary.

    Durable jobs and lease fencing remain the source of truth.  If shutdown
    interrupts an in-flight request, the lease expires and a later process can
    recover it without pretending that the request completed.
    """

    def __init__(
        self,
        worker: AgentTurnWorker,
        *,
        concurrency: int = 2,
        idle_poll_seconds: float = 0.25,
        shutdown_timeout_seconds: float = 5.0,
        close_gateway_on_stop: bool = True,
        restart_backoff_initial_seconds: float = 0.05,
        restart_backoff_max_seconds: float = 5.0,
    ) -> None:
        if not 1 <= concurrency <= 8:
            raise ValueError("Agent worker concurrency must be between one and eight")
        if not 0.01 <= idle_poll_seconds <= 60:
            raise ValueError("Agent worker poll interval is invalid")
        if not 0.1 <= shutdown_timeout_seconds <= 120:
            raise ValueError("Agent worker shutdown timeout is invalid")
        if not 0.01 <= restart_backoff_initial_seconds <= 60:
            raise ValueError("Agent worker restart backoff is invalid")
        if not restart_backoff_initial_seconds <= restart_backoff_max_seconds <= 60:
            raise ValueError("Agent worker maximum restart backoff is invalid")
        self.worker = worker
        self.concurrency = concurrency
        self.idle_poll_seconds = idle_poll_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.close_gateway_on_stop = close_gateway_on_stop
        self.restart_backoff_initial_seconds = restart_backoff_initial_seconds
        self.restart_backoff_max_seconds = restart_backoff_max_seconds
        self._worker_tasks: dict[int, asyncio.Task[None]] = {}
        self._slot_supervisors: dict[int, asyncio.Task[None]] = {}
        self._slot_ready: dict[int, asyncio.Event] = {}
        self._wake = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._stop_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._closed = False
        self._restarted_slots = 0
        self._completed_runs = 0
        self._failed_runs = 0
        self._last_outcome: WorkerOutcome | None = None
        self._last_error: str | None = None

    @property
    def live_workers(self) -> int:
        return sum(not task.done() for task in tuple(self._worker_tasks.values()))

    @property
    def running(self) -> bool:
        return (
            not self._stopping
            and not self._closed
            and self.live_workers == self.concurrency
        )

    def snapshot(self) -> WorkerSupervisorSnapshot:
        return WorkerSupervisorSnapshot(
            running=self.running,
            concurrency=self.concurrency,
            desired_workers=self.concurrency,
            live_workers=self.live_workers,
            restarted_slots=self._restarted_slots,
            completed_runs=self._completed_runs,
            failed_runs=self._failed_runs,
            last_outcome=self._last_outcome,
            last_error=self._last_error,
        )

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Agent worker supervisor has already been closed")
        if any(not task.done() for task in self._slot_supervisors.values()):
            return
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._slot_ready = {
            index: asyncio.Event() for index in range(self.concurrency)
        }
        self._slot_supervisors = {
            index: asyncio.create_task(
                self._supervise_slot(index),
                name=f"ecorex-agent-worker-slot-{index}",
            )
            for index in range(self.concurrency)
        }
        await asyncio.gather(
            *(ready.wait() for ready in self._slot_ready.values())
        )

    def notify(self) -> None:
        """Wake idle workers after an in-process producer queues work."""

        self._wake.set()

    async def stop(self) -> None:
        if self._closed:
            return
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(
                self._stop_owned_tasks(),
                name="ecorex-agent-worker-stop",
            )
        await asyncio.shield(self._stop_task)

    async def _stop_owned_tasks(self) -> None:
        self._stopping = True
        self._stop_event.set()
        self._wake.set()
        for ready in self._slot_ready.values():
            ready.set()
        owned_tasks = {
            *tuple(self._slot_supervisors.values()),
            *tuple(self._worker_tasks.values()),
        }
        if owned_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*owned_tasks, return_exceptions=True),
                    timeout=self.shutdown_timeout_seconds,
                )
            except TimeoutError:
                for task in owned_tasks:
                    task.cancel()
                await asyncio.gather(*owned_tasks, return_exceptions=True)
        self._slot_supervisors.clear()
        self._worker_tasks.clear()
        self._slot_ready.clear()
        try:
            close_worker = getattr(self.worker, "close", None)
            if callable(close_worker):
                worker_result = close_worker()
                if inspect.isawaitable(worker_result):
                    await asyncio.wait_for(
                        worker_result,
                        timeout=self.shutdown_timeout_seconds,
                    )
            if self.close_gateway_on_stop:
                await asyncio.wait_for(
                    self._close_gateway(),
                    timeout=self.shutdown_timeout_seconds,
                )
        except TimeoutError:
            self._last_error = "gateway_close_timeout"
        except Exception as error:
            self._last_error = self._safe_error_code(
                "gateway_close_failed", error
            )
        finally:
            self._closed = True

    async def _supervise_slot(self, index: int) -> None:
        failure_streak = 0
        generation = 0
        while not self._stopping and not self._closed:
            if generation:
                self._restarted_slots += 1
            started_at = asyncio.get_running_loop().time()
            task = asyncio.create_task(
                self._worker_loop(index),
                name=f"ecorex-agent-worker-{index}-g{generation}",
            )
            self._worker_tasks[index] = task
            ready = self._slot_ready.get(index)
            if ready is not None:
                ready.set()
            exit_error: Exception | None = None
            try:
                await task
            except asyncio.CancelledError:
                if self._stopping or self._closed:
                    raise
                exit_error = RuntimeError("worker slot was unexpectedly cancelled")
            except Exception as error:
                exit_error = error
            finally:
                if self._worker_tasks.get(index) is task:
                    self._worker_tasks.pop(index, None)

            if self._stopping or self._closed:
                return
            self._failed_runs += 1
            self._last_error = (
                "worker_slot_exited"
                if exit_error is None
                else self._safe_error_code("worker_slot_failed", exit_error)
            )
            lived_for = asyncio.get_running_loop().time() - started_at
            if lived_for >= self.restart_backoff_max_seconds:
                failure_streak = 0
            failure_streak += 1
            delay = self._restart_delay(failure_streak)
            if await self._stop_requested_before_restart(delay):
                return
            generation += 1

    async def _stop_requested_before_restart(self, delay: float) -> bool:
        """Wait until a monotonic deadline without trusting timer granularity.

        Windows timer ticks may wake ``asyncio.wait_for`` slightly before the
        requested timeout.  Restarting at that point can collapse bounded
        backoff into a restart storm.  Rechecking the absolute deadline makes
        early wakeups harmless while preserving immediate shutdown.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + delay
        while True:
            if self._stop_event.is_set() or self._stopping or self._closed:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
            except TimeoutError:
                continue
            return True

    def _restart_delay(self, failure_streak: int) -> float:
        """Return a capped delay without evaluating an unbounded exponent."""

        maximum_exponent = max(
            0,
            int(
                math.ceil(
                    math.log2(
                        self.restart_backoff_max_seconds
                        / self.restart_backoff_initial_seconds
                    )
                )
            ),
        )
        return min(
            self.restart_backoff_initial_seconds
            * 2 ** min(max(0, failure_streak - 1), maximum_exponent),
            self.restart_backoff_max_seconds,
        )

    async def _close_gateway(self) -> None:
        close = getattr(self.worker.gateway, "aclose", None)
        if not callable(close):
            return
        if inspect.iscoroutinefunction(close):
            result = close()
        else:
            result = await self._call_sync_close(close)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    async def _call_sync_close(close: Callable[[], Any]) -> Any:
        """Invoke an untrusted synchronous closer without blocking ASGI.

        A daemon thread is intentional: Python cannot cancel an arbitrary
        blocking native call. The asyncio ownership boundary still completes
        at its timeout, and a late coroutine result is explicitly closed.
        """

        loop = asyncio.get_running_loop()
        completed: asyncio.Future[Any] = loop.create_future()

        def settle(result: Any = None, error: BaseException | None = None) -> None:
            if completed.done():
                if inspect.iscoroutine(result):
                    result.close()
                return
            if error is None:
                completed.set_result(result)
            else:
                completed.set_exception(error)

        def invoke() -> None:
            try:
                result = close()
            except BaseException as error:
                try:
                    loop.call_soon_threadsafe(settle, None, error)
                except RuntimeError:
                    return
            else:
                try:
                    loop.call_soon_threadsafe(settle, result, None)
                except RuntimeError:
                    if inspect.iscoroutine(result):
                        result.close()

        threading.Thread(
            target=invoke,
            name="ecorex-agent-gateway-close",
            daemon=True,
        ).start()
        return await completed

    @staticmethod
    def _safe_error_code(prefix: str, error: BaseException) -> str:
        return f"{prefix}:{error.__class__.__name__.casefold()}"[:128]

    async def _worker_loop(self, index: int) -> None:
        worker_id = f"runtime-{id(self):x}-{index}"
        while not self._stopping:
            try:
                result = await self.worker.run_once(worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # fail one loop, never the whole pool
                self._failed_runs += 1
                self._last_error = error.__class__.__name__.casefold()
                await self._wait_for_work()
                continue
            self._record(result)
            if result.outcome is WorkerOutcome.IDLE:
                await self._wait_for_work()

    def _record(self, result: WorkerRunResult) -> None:
        self._last_outcome = result.outcome
        self._last_error = result.reason
        if result.outcome is WorkerOutcome.COMPLETED:
            self._completed_runs += 1
        elif result.outcome is WorkerOutcome.FAILED:
            self._failed_runs += 1

    async def _wait_for_work(self) -> None:
        if self._stopping:
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(
                self._wake.wait(), timeout=self.idle_poll_seconds
            )
        except TimeoutError:
            pass
