"""Durable multi-instance fan-out for non-authoritative update hints."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .models import ControlUpdateSignal
from .repository import ControlPlaneRepository


class UpdateSignalBroadcaster(Protocol):
    async def broadcast_signal(
        self,
        repository: ControlPlaneRepository,
        signal: ControlUpdateSignal,
    ) -> int: ...

class DurableUpdateSignalPoller:
    """Consume the shared append-only signal log for one app instance.

    A cursor is acknowledged only after local fan-out.  A crash between fan-out
    and acknowledgement may repeat the same stable event ID; Runtime-side
    signal persistence makes that replay idempotent. Falling behind retention
    processes only the retained committed suffix; the normal signed-feed poll
    is the recovery path for facts that retention already removed.
    """

    def __init__(
        self,
        repository: ControlPlaneRepository,
        broadcaster: UpdateSignalBroadcaster,
        *,
        consumer_id: str,
        poll_interval_seconds: float = 0.25,
        batch_size: int = 128,
        retention_seconds: int = 7 * 24 * 60 * 60,
        retain_latest: int = 1024,
        retention_interval_seconds: float = 60 * 60,
    ) -> None:
        if not 0.01 <= poll_interval_seconds <= 60:
            raise ValueError("update signal poll interval is invalid")
        if not 1 <= batch_size <= 256:
            raise ValueError("update signal poll batch size is invalid")
        if not 60 <= retention_seconds <= 90 * 24 * 60 * 60:
            raise ValueError("update signal retention period is invalid")
        if not 1 <= retain_latest <= 100_000:
            raise ValueError("update signal retention floor is invalid")
        if not 1 <= retention_interval_seconds <= 24 * 60 * 60:
            raise ValueError("update signal retention interval is invalid")
        # Repository validation is the single identity grammar authority.
        repository.update_signal_consumer_cursor(consumer_id)
        self.repository = repository
        self.broadcaster = broadcaster
        self.consumer_id = consumer_id
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.batch_size = int(batch_size)
        self.retention_seconds = int(retention_seconds)
        self.retain_latest = int(retain_latest)
        self.retention_interval_seconds = float(retention_interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._poll_lock = asyncio.Lock()
        self._cursor = 0
        self._next_retention_at = 0.0
        self._closed = False
        self.last_error: str | None = None

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("update signal poller is closed")
        if self.running:
            return
        self._cursor = await asyncio.to_thread(
            self.repository.update_signal_consumer_cursor, self.consumer_id
        )
        self._stop.clear()
        self._next_retention_at = asyncio.get_running_loop().time()
        self._task = asyncio.create_task(
            self._run(), name=f"ecorex-control-signal-{self.consumer_id}"
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def poll_once(self) -> int:
        """Process at most one bounded batch and return the new cursor."""

        async with self._poll_lock:
            batch = await asyncio.to_thread(
                self.repository.read_update_signals,
                after_sequence=self._cursor,
                limit=self.batch_size,
            )
            for signal in batch.signals:
                await self.broadcaster.broadcast_signal(self.repository, signal)
                self._cursor = signal.sequence
            if batch.signals:
                self._cursor = await asyncio.to_thread(
                    self.repository.acknowledge_update_signals,
                    self.consumer_id,
                    self._cursor,
                )
            return self._cursor

    async def _run(self) -> None:
        delay = self.poll_interval_seconds
        while not self._stop.is_set():
            try:
                prior = self._cursor
                await self.poll_once()
                await self._apply_retention_if_due()
                self.last_error = None
                delay = self.poll_interval_seconds
                if self._cursor != prior:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as error:  # poll fallback remains available
                self.last_error = error.__class__.__name__
                delay = min(max(delay * 2, 0.1), 30.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def _apply_retention_if_due(self) -> None:
        loop = asyncio.get_running_loop()
        if loop.time() < self._next_retention_at:
            return
        cutoff = datetime.now(UTC) - timedelta(seconds=self.retention_seconds)
        await asyncio.to_thread(
            self.repository.prune_update_signals,
            before=cutoff,
            retain_latest=self.retain_latest,
        )
        self._next_retention_at = loop.time() + self.retention_interval_seconds
