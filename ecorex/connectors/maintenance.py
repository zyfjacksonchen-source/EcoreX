"""Lifecycle-managed recovery and outbox supervisor for connector state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ecorex.runtime.invariant_guard import RuntimeExecutionGate

from .service import ConnectorService


MaintenanceErrorSink = Callable[[str], None]
MaintenanceAllowed = Callable[[], bool]


class ConnectorMaintenanceSupervisor:
    """Run connector recovery without owning the parent ASGI lifespan."""

    def __init__(
        self,
        service: ConnectorService,
        *,
        interval_seconds: float = 15.0,
        error_sink: MaintenanceErrorSink | None = None,
        maintenance_allowed: MaintenanceAllowed | None = None,
        maintenance_timeout_seconds: float = 30.0,
        stop_timeout_seconds: float = 5.0,
        execution_gate: RuntimeExecutionGate | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("connector maintenance interval must be positive")
        self.service = service
        self.interval_seconds = float(interval_seconds)
        if maintenance_timeout_seconds <= 0:
            raise ValueError("connector maintenance timeout must be positive")
        if stop_timeout_seconds <= 0:
            raise ValueError("connector maintenance stop timeout must be positive")
        self.maintenance_timeout_seconds = float(maintenance_timeout_seconds)
        self.stop_timeout_seconds = float(stop_timeout_seconds)
        self.error_sink = error_sink
        self.maintenance_allowed = maintenance_allowed or (lambda: True)
        self.execution_gate = execution_gate or service.execution_gate
        if execution_gate is not None:
            service.bind_execution_gate(execution_gate)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(),
            name="ecorex-connector-maintenance",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.stop_timeout_seconds
        self._stop.set()
        task = self._task
        self._task = None
        try:
            await asyncio.wait_for(
                task,
                timeout=max(0.01, deadline - loop.time()),
            )
        except TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass
        if self.service.outbox_publisher is None:
            return
        if (
            self.execution_gate is not None
            and not self.execution_gate.snapshot().healthy
        ):
            # A closed epoch cannot authorize EventStore/outbox commits. The
            # durable rows remain for recovery after the next healthy startup.
            return
        remaining = deadline - loop.time()
        if remaining < 0.05:
            raise TimeoutError("connector shutdown flush deadline expired")
        flush_budget = max(0.05, remaining - min(0.05, remaining * 0.1))
        await asyncio.wait_for(
            self.service.flush_pending_outbox_async(
                timeout_seconds=flush_budget,
            ),
            timeout=remaining,
        )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                allowed = await asyncio.to_thread(self.maintenance_allowed)
                gate_open = (
                    self.execution_gate is None
                    or self.execution_gate.snapshot().healthy
                )
                if allowed and gate_open:
                    await asyncio.wait_for(
                        self.service.maintenance_once(),
                        timeout=self.maintenance_timeout_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                if self.error_sink is not None:
                    try:
                        await asyncio.to_thread(
                            self.error_sink, "connector_maintenance_failed"
                        )
                    except Exception:
                        # Diagnostics must not become a second failure source.
                        pass
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.interval_seconds,
                )
            except TimeoutError:
                continue


__all__ = [
    "ConnectorMaintenanceSupervisor",
    "MaintenanceAllowed",
    "MaintenanceErrorSink",
]
