"""Runtime-side request for a bootstrap-owned, post-activation restart."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from .errors import BootstrapConfigurationError, RuntimeLaunchError


# This is a private child-to-parent protocol value, not a public process result.
# Keep it inside the portable 8-bit exit-code range and away from sysexits(3).
RUNTIME_RESTART_EXIT_CODE = 85
# Session/account/policy changes need to reload the same signed slot.  Keep a
# distinct code so an update restart can never weaken its pointer-change fence.
RUNTIME_RELOAD_EXIT_CODE = 86


def _thread_scheduler(callback: Callable[[], None]) -> None:
    threading.Thread(
        target=callback,
        name="ecorex-bootstrap-restart",
        daemon=True,
    ).start()


class DelayedRestartRequester:
    """Request one delayed process exit after an update was activated.

    The update service can receive ``request`` as an injected callback.  The
    delay lets its HTTP response flush before the process exits.  ``os._exit``
    is intentional: ``sys.exit`` raised from a helper thread would only stop
    that thread and leave the old Runtime serving traffic.
    """

    def __init__(
        self,
        *,
        delay_seconds: float = 0.35,
        exit_process: Callable[[int], object] = os._exit,
        sleeper: Callable[[float], object] = time.sleep,
        scheduler: Callable[[Callable[[], None]], object] = _thread_scheduler,
        exit_code: int = RUNTIME_RESTART_EXIT_CODE,
    ) -> None:
        if (
            isinstance(delay_seconds, bool)
            or not isinstance(delay_seconds, (int, float))
            or not 0.1 <= float(delay_seconds) <= 30.0
        ):
            raise BootstrapConfigurationError(
                "restart delay must be between 0.1 and 30 seconds"
            )
        if not callable(exit_process) or not callable(sleeper) or not callable(scheduler):
            raise BootstrapConfigurationError("restart hooks must be callable")
        if exit_code not in {RUNTIME_RESTART_EXIT_CODE, RUNTIME_RELOAD_EXIT_CODE}:
            raise BootstrapConfigurationError("Runtime restart exit code is unsupported")
        self._delay_seconds = float(delay_seconds)
        self._exit_process = exit_process
        self._sleeper = sleeper
        self._scheduler = scheduler
        self._exit_code = exit_code
        self._lock = threading.Lock()
        self._requested = False

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._requested

    def request(self, transaction_id: str | None = None) -> bool:
        """Schedule the dedicated restart exit exactly once.

        ``True`` means this call scheduled the exit.  Repeated requests return
        ``False`` and cannot create a restart storm. ``transaction_id`` makes
        this method directly compatible with ``RuntimeUpdateService``'s
        injected restart callback; it is deliberately neither stored nor
        logged by the process boundary.
        """

        if transaction_id is not None and not isinstance(transaction_id, str):
            raise BootstrapConfigurationError("restart transaction id must be a string")
        with self._lock:
            if self._requested:
                return False
            self._requested = True
        try:
            self._scheduler(self._exit_after_delay)
        except Exception as exc:
            with self._lock:
                self._requested = False
            raise RuntimeLaunchError("The delayed Runtime restart could not be scheduled") from exc
        return True

    def _exit_after_delay(self) -> None:
        self._sleeper(self._delay_seconds)
        self._exit_process(self._exit_code)
