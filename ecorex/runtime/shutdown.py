"""Bounded, failure-isolated shutdown for ASGI-owned Runtime services.

Shutdown is a fan-out operation: one faulty adapter must never prevent the
remaining workers, dispatchers, or brokers from receiving their stop signal.
Only stable error codes are returned so diagnostics cannot accidentally copy
provider exception text, credentials, or local paths.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import re
from typing import Any, Iterable


_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ShutdownFailure:
    service: str
    reason: str
    error_code: str


async def stop_services_isolated(
    services: Iterable[tuple[str, Any]],
    *,
    timeout_seconds: float,
) -> tuple[ShutdownFailure, ...]:
    """Stop every supplied service concurrently within one hard deadline.

    Async stop methods run on the event loop. Synchronous stop methods run in
    the loop executor so a legacy adapter cannot block unrelated async
    cleanup. A timed-out task is cancelled and observed in the background;
    the collector itself never waits indefinitely for cancellation-hostile
    third-party code.
    """

    if not isinstance(timeout_seconds, (int, float)) or isinstance(
        timeout_seconds, bool
    ):
        raise ValueError("shutdown timeout must be numeric")
    timeout = float(timeout_seconds)
    if not 0.01 <= timeout <= 300:
        raise ValueError("shutdown timeout is invalid")

    normalized: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for name, service in services:
        if not isinstance(name, str) or _SERVICE_NAME.fullmatch(name) is None:
            raise ValueError("shutdown service name is invalid")
        if name in seen:
            raise ValueError("shutdown service name is duplicated")
        seen.add(name)
        normalized.append((name, service))
    if not normalized:
        return ()

    tasks = {
        asyncio.create_task(
            _stop_one(service),
            name=f"ecorex-shutdown:{name}",
        ): name
        for name, service in normalized
    }
    done, pending = await asyncio.wait(tuple(tasks), timeout=timeout)
    failures: list[ShutdownFailure] = []
    for task in done:
        name = tasks[task]
        try:
            task.result()
        except asyncio.CancelledError:
            failures.append(
                ShutdownFailure(name, "cancelled", "shutdown_cancelled")
            )
        except Exception as error:
            failures.append(
                ShutdownFailure(
                    name,
                    "error",
                    _safe_error_code(error),
                )
            )
    for task in pending:
        name = tasks[task]
        failures.append(ShutdownFailure(name, "timeout", "shutdown_timeout"))
        task.cancel()
        task.add_done_callback(_consume_background_result)
    return tuple(sorted(failures, key=lambda failure: failure.service))


async def stop_service_phases_isolated(
    services: Iterable[tuple[int, str, Any]],
    *,
    timeout_seconds: float,
) -> tuple[ShutdownFailure, ...]:
    """Stop dependency phases in order and peers within each phase together.

    A dispatcher/worker belongs to an earlier phase than its publisher,
    transport, or adapter. A timeout in one peer is recorded but cannot stop
    the remaining peers or the next resource-close phase.
    """

    grouped: dict[int, list[tuple[str, Any]]] = {}
    names: set[str] = set()
    for phase, name, service in services:
        if not isinstance(phase, int) or isinstance(phase, bool) or phase < 1:
            raise ValueError("shutdown phase is invalid")
        if name in names:
            raise ValueError("shutdown service name is duplicated")
        names.add(name)
        grouped.setdefault(phase, []).append((name, service))
    failures: list[ShutdownFailure] = []
    for phase in sorted(grouped):
        failures.extend(
            await stop_services_isolated(
                grouped[phase],
                timeout_seconds=timeout_seconds,
            )
        )
    return tuple(failures)


async def _stop_one(service: Any) -> None:
    stop = getattr(service, "stop", None)
    if not callable(stop):
        raise TypeError("service has no callable stop method")
    if inspect.iscoroutinefunction(stop):
        await stop()
        return
    result = await asyncio.to_thread(stop)
    if inspect.isawaitable(result):
        await result


def _safe_error_code(error: BaseException) -> str:
    name = type(error).__name__
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name)[:96]
    return safe or "shutdown_error"


def _consume_background_result(task: asyncio.Task[None]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        # Cancellation-hostile third-party cleanup must not surface as an
        # unhandled task exception after the Runtime has already moved on.
        pass


__all__ = [
    "ShutdownFailure",
    "stop_service_phases_isolated",
    "stop_services_isolated",
]
