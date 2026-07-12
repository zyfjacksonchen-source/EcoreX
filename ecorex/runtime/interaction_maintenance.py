"""Lifecycle owner for durable HITL expiry convergence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import threading
from typing import Callable

from .interactions import InteractionStore
from .invariant_guard import RuntimeExecutionDenied, RuntimeExecutionGate


@dataclass(frozen=True, slots=True)
class InteractionMaintenanceSnapshot:
    running: bool
    convergence_runs: int
    expired_interactions: int
    last_error_code: str | None


class InteractionMaintenanceSupervisor:
    """Converge expired interactions before and during Runtime service."""

    def __init__(
        self,
        interactions: InteractionStore,
        *,
        execution_gate: RuntimeExecutionGate | None = None,
        interval_seconds: float = 5.0,
        convergence_timeout_seconds: float = 5.0,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if not 0.01 <= interval_seconds <= 3600:
            raise ValueError("Interaction maintenance interval is invalid")
        if not 0.05 <= convergence_timeout_seconds <= 120:
            raise ValueError("Interaction convergence timeout is invalid")
        if not 0.05 <= shutdown_timeout_seconds <= 120:
            raise ValueError("Interaction maintenance shutdown timeout is invalid")
        self.interactions = interactions
        self.execution_gate = execution_gate
        self.interval_seconds = interval_seconds
        self.convergence_timeout_seconds = convergence_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._state_lock = threading.Lock()
        self._convergence_runs = 0
        self._expired_interactions = 0
        self._last_error_code: str | None = None
        self._start_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._stopping = False
        self._closed = False

    @property
    def running(self) -> bool:
        return (
            not self._stopping
            and not self._closed
            and self._monitor_task is not None
            and not self._monitor_task.done()
        )

    def snapshot(self) -> InteractionMaintenanceSnapshot:
        with self._state_lock:
            return InteractionMaintenanceSnapshot(
                running=self.running,
                convergence_runs=self._convergence_runs,
                expired_interactions=self._expired_interactions,
                last_error_code=self._last_error_code,
            )

    async def start(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise RuntimeError(
                    "Interaction maintenance supervisor has already been closed"
                )
            if self.running:
                return
            self._stopping = False
            self._stop_event = asyncio.Event()
            # Runtime composition opens the gate only after a successful
            # invariant audit. An unopened/critical gate leaves durable HITL
            # unchanged; it is never an implicit maintenance bypass.
            await self._converge(preflight=True)
            if self._stopping or self._closed:
                return
            self._monitor_task = asyncio.create_task(
                self._monitor_loop(),
                name="ecorex-interaction-maintenance",
            )

    async def stop(self) -> None:
        if self._closed:
            return
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(
                self._stop_owned_task(),
                name="ecorex-interaction-maintenance-stop",
            )
        await asyncio.shield(self._stop_task)

    async def _stop_owned_task(self) -> None:
        self._stopping = True
        self._stop_event.set()
        task = self._monitor_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self.shutdown_timeout_seconds,
                )
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._monitor_task = None
        self._closed = True

    async def _monitor_loop(self) -> None:
        while not self._stopping and not self._closed:
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_seconds,
                )
            except TimeoutError:
                await self._converge(preflight=False)
                continue
            return

    async def _converge(self, *, preflight: bool) -> None:
        try:
            await asyncio.wait_for(
                self._call_sync(
                    lambda: self._expire_due(preflight=preflight)
                ),
                timeout=self.convergence_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._record_error("interaction_maintenance_timeout")
        except BaseException as error:
            self._record_error(
                f"interaction_maintenance_failed:{type(error).__name__.casefold()}"
            )

    def _expire_due(self, *, preflight: bool) -> None:
        del preflight
        gate = self.execution_gate
        if gate is None:
            expired = self.interactions.expire_due()
        else:
            try:
                with gate.new_admission(
                    scope="hitl_maintenance",
                    subject="expire_due",
                ) as permit:
                    connection = self.interactions.database.connect()
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        expired = self.interactions.expire_due_in_transaction(
                            connection
                        )
                        gate.assert_permit(permit)
                        connection.commit()
                    except BaseException:
                        if connection.in_transaction:
                            connection.rollback()
                        raise
                    finally:
                        connection.close()
            except RuntimeExecutionDenied:
                expired = []
        with self._state_lock:
            self._convergence_runs += 1
            self._expired_interactions += len(expired)
            if gate is None or gate.snapshot().healthy:
                self._last_error_code = None

    def _record_error(self, code: str) -> None:
        safe = code if all(character.isalnum() or character in "_:" for character in code) else (
            "interaction_maintenance_failed:unknown"
        )
        with self._state_lock:
            self._convergence_runs += 1
            self._last_error_code = safe
        if self.execution_gate is not None:
            self.execution_gate.request_critical(error_code=safe)

    @staticmethod
    async def _call_sync(operation: Callable[[], None]) -> None:
        loop = asyncio.get_running_loop()
        completed: asyncio.Future[None] = loop.create_future()

        def settle(error: BaseException | None = None) -> None:
            if completed.done():
                return
            if error is None:
                completed.set_result(None)
            else:
                completed.set_exception(error)

        def invoke() -> None:
            try:
                operation()
            except BaseException as error:
                try:
                    loop.call_soon_threadsafe(settle, error)
                except RuntimeError:
                    return
            else:
                try:
                    loop.call_soon_threadsafe(settle, None)
                except RuntimeError:
                    return

        threading.Thread(
            target=invoke,
            name="ecorex-interaction-maintenance-converge",
            daemon=True,
        ).start()
        await completed


__all__ = [
    "InteractionMaintenanceSnapshot",
    "InteractionMaintenanceSupervisor",
]
