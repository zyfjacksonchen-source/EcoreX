"""Lease-fenced Durable Job executor for ShareSnapshot cloud effects."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
from typing import Awaitable, Callable, TypeVar
from urllib.parse import urlsplit

from ecorex.protocol import JobStatus
from ecorex.runtime.errors import LeaseError
from ecorex.runtime.invariant_guard import (
    RuntimeExecutionDenied,
    RuntimeExecutionGate,
    RuntimeExecutionPermit,
)

from .errors import ShareConflict, ShareMediaContractError
from .media_contract import shared_media_declarations
from .models import PublishedShare, ShareStatus, SharedMediaRendition
from .repository import (
    SHARE_PUBLISH_JOB_KIND,
    SHARE_REVOKE_JOB_KIND,
    ShareOperation,
    ShareRepository,
)
from .service import SharePublisher
from .transport import ShareTransportError


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_T = TypeVar("_T")


class ShareWorkerOutcome(StrEnum):
    IDLE = "idle"
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ShareWorkerResult:
    outcome: ShareWorkerOutcome
    share_id: str | None = None
    durable_job_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ShareSupervisorSnapshot:
    running: bool
    concurrency: int
    completed_runs: int
    retry_runs: int
    failed_runs: int
    last_outcome: ShareWorkerOutcome | None
    last_error: str | None


class ShareOperationWorker:
    """Executes only persisted share jobs and never receives HTTP request data."""

    def __init__(
        self,
        repository: ShareRepository,
        publisher: SharePublisher,
        *,
        allowed_public_hosts: frozenset[str],
        clock: Callable[[], datetime] = _utcnow,
        lease_seconds: int = 30,
        retry_delay_seconds: int = 2,
        media_loader: Callable[[str], bytes] | None = None,
        execution_gate: RuntimeExecutionGate | None = None,
    ) -> None:
        if not 5 <= lease_seconds <= 300:
            raise ValueError("share worker lease must be between 5 and 300 seconds")
        if not 0 <= retry_delay_seconds <= 300:
            raise ValueError("share worker retry delay is invalid")
        hosts = frozenset(host.casefold() for host in allowed_public_hosts if host)
        if not hosts:
            raise ValueError("share worker public host allowlist is required")
        self.repository = repository
        self.publisher = publisher
        self.allowed_public_hosts = hosts
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.media_loader = media_loader
        if execution_gate is not None:
            self.repository.jobs.bind_execution_gate(execution_gate)

    @property
    def execution_gate(self) -> RuntimeExecutionGate | None:
        return self.repository.jobs.execution_gate

    def maintenance_once(self, *, subject: str) -> bool:
        jobs = self.repository.jobs
        try:
            with jobs.control_admission(
                scope="share_maintenance",
                subject=subject,
            ) as permit:
                pass
            gate = jobs.execution_gate

            def validate() -> None:
                if gate is not None:
                    assert permit is not None
                    gate.assert_permit(permit)

            self.repository.expire_due(
                now=self.clock(),
                before_commit=validate,
            )
            self.repository.reconcile_terminal_jobs(
                now=self.clock(),
                before_commit=validate,
            )
            return True
        except (LeaseError, RuntimeExecutionDenied):
            return False

    async def run_once(self, worker_id: str) -> ShareWorkerResult:
        if not self.maintenance_once(subject=f"run:{worker_id}"):
            return ShareWorkerResult(
                ShareWorkerOutcome.IDLE,
                reason="execution_epoch_closed",
            )
        durable = self.repository.jobs.lease_next(
            worker_id,
            lease_seconds=self.lease_seconds,
            kinds=[SHARE_PUBLISH_JOB_KIND, SHARE_REVOKE_JOB_KIND],
            now=self.clock(),
        )
        if durable is None:
            self.maintenance_once(subject=f"idle:{worker_id}")
            return ShareWorkerResult(ShareWorkerOutcome.IDLE)
        if durable.lease_token is None:
            raise RuntimeError("leased share Job has no fencing token")
        lease_token = durable.lease_token
        operation: ShareOperation | None = None
        execution_permit: RuntimeExecutionPermit | None = None
        try:
            self.repository.jobs.start(
                durable.job_id,
                worker_id,
                lease_token,
                now=self.clock(),
            )
            operation = self.repository.get_operation(
                durable.job_id, now=self.clock()
            )
            execution_permit = self.repository.jobs.capture_execution_permit(
                durable.job_id,
                lease_token,
            )
            if operation.action == "publish":
                return await self._publish(
                    operation,
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
            if operation.action == "revoke":
                return await self._revoke(
                    operation,
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
            raise ShareConflict("share Durable Job action is invalid")
        except asyncio.CancelledError:
            # The running lease is intentionally left intact.  A replacement
            # process reclaims it and repeats the same cloud idempotency key.
            raise
        except LeaseError:
            return ShareWorkerResult(
                ShareWorkerOutcome.FAILED,
                share_id=(operation.share_id if operation is not None else None),
                durable_job_id=durable.job_id,
                reason="execution_epoch_closed",
            )
        except Exception as error:
            return self._handle_failure(
                durable_job_id=durable.job_id,
                share_id=(operation.share_id if operation is not None else None),
                worker_id=worker_id,
                lease_token=lease_token,
                error=error,
                execution_permit=execution_permit,
            )

    async def _invoke_with_permit(
        self,
        *,
        durable_job_id: str,
        lease_token: str,
        operation: Callable[[], Awaitable[_T]],
    ) -> tuple[_T, RuntimeExecutionPermit | None]:
        jobs = self.repository.jobs
        permit = jobs.capture_execution_permit(durable_job_id, lease_token)
        try:
            result = await operation()
        except asyncio.CancelledError:
            raise
        except BaseException:
            jobs.assert_execution_permit(durable_job_id, lease_token, permit)
            raise
        jobs.assert_execution_permit(durable_job_id, lease_token, permit)
        return result, permit

    def _execution_before_commit(
        self,
        durable_job_id: str,
        lease_token: str,
        permit: RuntimeExecutionPermit | None,
    ) -> Callable[[], None]:
        def validate() -> None:
            self.repository.jobs.assert_execution_permit(
                durable_job_id,
                lease_token,
                permit,
            )

        return validate

    async def _publish(
        self,
        operation: ShareOperation,
        *,
        worker_id: str,
        lease_token: str,
    ) -> ShareWorkerResult:
        if operation.projection.status is not ShareStatus.PUBLISHING:
            permit = self.repository.jobs.capture_execution_permit(
                operation.job_id,
                lease_token,
            )
            self.repository.skip_publish(
                operation.job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                reason=f"publish_fenced_{operation.projection.status.value}",
                now=self.clock(),
                before_commit=self._execution_before_commit(
                    operation.job_id,
                    lease_token,
                    permit,
                ),
            )
            self.repository.jobs.retire_execution_permit(
                operation.job_id,
                lease_token,
            )
            return ShareWorkerResult(
                ShareWorkerOutcome.CANCELLED,
                share_id=operation.share_id,
                durable_job_id=operation.job_id,
                reason="publication_fenced",
            )
        # Recheck the frozen durable payload immediately before any external
        # upload. This detects storage/schema drift without publishing a
        # snapshot that would later render an image as a file row.
        shared_media_declarations(
            operation.payload, require_publishable_schema=True
        )
        media_items = operation.payload.media_renditions()
        self._heartbeat(
            operation,
            worker_id=worker_id,
            lease_token=lease_token,
            phase="media_uploading" if media_items else "external_requested",
        )
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(
                operation,
                worker_id=worker_id,
                lease_token=lease_token,
            )
        )
        try:
            if media_items:
                upload_media = getattr(self.publisher, "upload_media", None)
                if self.media_loader is None or not callable(upload_media):
                    raise ShareTransportError(
                        "shared media publication is unavailable",
                        code="share_media_unavailable",
                        retryable=False,
                    )
                for media in media_items:
                    content = await asyncio.to_thread(self.media_loader, media.sha256)
                    self._validate_media_content(media, content)
                    await self._invoke_with_permit(
                        durable_job_id=operation.job_id,
                        lease_token=lease_token,
                        operation=lambda media=media, content=content: upload_media(
                            operation.share_id,
                            media,
                            content,
                            idempotency_key=f"{operation.share_id}:{media.media_id}",
                        ),
                    )
                self._heartbeat(
                    operation,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    phase="external_requested",
                )
            published, _permit = await self._invoke_with_permit(
                durable_job_id=operation.job_id,
                lease_token=lease_token,
                operation=lambda: self.publisher.publish(
                    operation.payload,
                    idempotency_key=operation.external_idempotency_key,
                ),
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        self._validate_published(operation, published)
        permit = self.repository.jobs.capture_execution_permit(
            operation.job_id,
            lease_token,
        )
        self.repository.complete_publish(
            operation.job_id,
            published,
            worker_id=worker_id,
            lease_token=lease_token,
            now=self.clock(),
            before_commit=self._execution_before_commit(
                operation.job_id,
                lease_token,
                permit,
            ),
        )
        self.repository.jobs.retire_execution_permit(
            operation.job_id,
            lease_token,
        )
        return ShareWorkerResult(
            ShareWorkerOutcome.COMPLETED,
            share_id=operation.share_id,
            durable_job_id=operation.job_id,
        )

    @staticmethod
    def _validate_media_content(media: SharedMediaRendition, content: object) -> None:
        if not isinstance(content, bytes):
            raise ShareTransportError(
                "shared media storage returned invalid content",
                code="share_media_storage_invalid",
                retryable=False,
            )
        if len(content) != media.size_bytes or hashlib.sha256(content).hexdigest() != media.sha256:
            raise ShareTransportError(
                "shared media storage content does not match its descriptor",
                code="share_media_integrity_invalid",
                retryable=False,
            )
        valid = {
            "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": content.startswith(b"\xff\xd8\xff"),
            "image/gif": content.startswith((b"GIF87a", b"GIF89a")),
            "image/webp": len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP",
            "image/avif": len(content) >= 16
            and content[4:8] == b"ftyp"
            and any(brand in content[8:32] for brand in (b"avif", b"avis")),
        }.get(media.mime_type, False)
        if not valid:
            raise ShareTransportError(
                "shared media bytes do not match their declared image type",
                code="share_media_type_invalid",
                retryable=False,
            )

    async def _revoke(
        self,
        operation: ShareOperation,
        *,
        worker_id: str,
        lease_token: str,
    ) -> ShareWorkerResult:
        if operation.projection.status is ShareStatus.REVOKED:
            permit = self.repository.jobs.capture_execution_permit(
                operation.job_id,
                lease_token,
            )
            self.repository.complete_revoke(
                operation.job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                now=self.clock(),
                before_commit=self._execution_before_commit(
                    operation.job_id,
                    lease_token,
                    permit,
                ),
            )
            self.repository.jobs.retire_execution_permit(
                operation.job_id,
                lease_token,
            )
            return ShareWorkerResult(
                ShareWorkerOutcome.COMPLETED,
                share_id=operation.share_id,
                durable_job_id=operation.job_id,
            )
        if operation.projection.status not in {
            ShareStatus.REVOKING,
            ShareStatus.EXPIRED,
            ShareStatus.FAILED,
        }:
            raise ShareConflict("share is not awaiting durable revocation")
        if operation.remote_snapshot_id:
            self._heartbeat(
                operation,
                worker_id=worker_id,
                lease_token=lease_token,
                phase="external_requested",
            )
            heartbeat = asyncio.create_task(
                self._heartbeat_loop(
                    operation,
                    worker_id=worker_id,
                    lease_token=lease_token,
                )
            )
            try:
                await self._invoke_with_permit(
                    durable_job_id=operation.job_id,
                    lease_token=lease_token,
                    operation=lambda: self.publisher.revoke(
                        operation.remote_snapshot_id,
                        idempotency_key=operation.external_idempotency_key,
                    ),
                )
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
        # No remote identity means publication was fenced or failed before a
        # cloud object existed.  Revocation is still a durable, terminal fact.
        permit = self.repository.jobs.capture_execution_permit(
            operation.job_id,
            lease_token,
        )
        self.repository.complete_revoke(
            operation.job_id,
            worker_id=worker_id,
            lease_token=lease_token,
            now=self.clock(),
            before_commit=self._execution_before_commit(
                operation.job_id,
                lease_token,
                permit,
            ),
        )
        self.repository.jobs.retire_execution_permit(
            operation.job_id,
            lease_token,
        )
        return ShareWorkerResult(
            ShareWorkerOutcome.COMPLETED,
            share_id=operation.share_id,
            durable_job_id=operation.job_id,
        )

    def _validate_published(
        self, operation: ShareOperation, published: PublishedShare
    ) -> None:
        if not isinstance(published, PublishedShare):
            raise ShareConflict("publisher returned an invalid share result")
        host = (urlsplit(published.public_url).hostname or "").casefold()
        if host not in self.allowed_public_hosts:
            raise ShareConflict("publisher returned a non-allowlisted share host")
        # A provider may use an opaque URL token, but it may never collapse the
        # remote identity to the Thread identity (the original duplicate-link
        # failure mode).
        if published.remote_snapshot_id == operation.thread_id:
            raise ShareConflict("publisher returned a thread-global share identity")

    def _heartbeat(
        self,
        operation: ShareOperation,
        *,
        worker_id: str,
        lease_token: str,
        phase: str,
    ) -> None:
        self.repository.jobs.heartbeat(
            operation.job_id,
            worker_id,
            lease_token,
            lease_seconds=self.lease_seconds,
            checkpoint={
                "schema_version": 1,
                "phase": phase,
                "action": operation.action,
                "share_id": operation.share_id,
                "external_idempotency_key": operation.external_idempotency_key,
            },
            now=self.clock(),
        )

    async def _heartbeat_loop(
        self,
        operation: ShareOperation,
        *,
        worker_id: str,
        lease_token: str,
    ) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(
                self._heartbeat,
                operation,
                worker_id=worker_id,
                lease_token=lease_token,
                phase="external_running",
            )

    def _handle_failure(
        self,
        *,
        durable_job_id: str,
        share_id: str | None,
        worker_id: str,
        lease_token: str,
        error: Exception,
        execution_permit: RuntimeExecutionPermit | None,
    ) -> ShareWorkerResult:
        if isinstance(error, ShareTransportError):
            reason = error.code
            retryable = error.retryable
        elif isinstance(error, ShareMediaContractError):
            reason = error.code
            # The payload attached to this Durable Job is immutable. The user
            # may recreate the share after the stated action, but replaying the
            # same invalid payload cannot repair it.
            retryable = False
        else:
            reason = error.__class__.__name__.casefold()
            retryable = isinstance(error, (ConnectionError, TimeoutError, OSError))
        try:
            target = self.repository.fail_operation(
                durable_job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                error_code=reason,
                retryable=retryable,
                retry_delay_seconds=self.retry_delay_seconds,
                now=self.clock(),
                before_commit=self._execution_before_commit(
                    durable_job_id,
                    lease_token,
                    execution_permit,
                ),
            )
            self.repository.jobs.retire_execution_permit(
                durable_job_id,
                lease_token,
            )
        except LeaseError:
            return ShareWorkerResult(
                ShareWorkerOutcome.FAILED,
                share_id=share_id,
                durable_job_id=durable_job_id,
                reason="execution_epoch_closed",
            )
        except ShareConflict:
            self.maintenance_once(subject=f"failure:{durable_job_id}")
            target = JobStatus.FAILED
        return ShareWorkerResult(
            (
                ShareWorkerOutcome.RETRY_SCHEDULED
                if target is JobStatus.RETRY_SCHEDULED
                else ShareWorkerOutcome.FAILED
            ),
            share_id=share_id,
            durable_job_id=durable_job_id,
            reason=reason,
        )


class ShareWorkerSupervisor:
    def __init__(
        self,
        worker: ShareOperationWorker,
        *,
        concurrency: int = 1,
        idle_poll_seconds: float = 0.25,
        shutdown_timeout_seconds: float = 5.0,
        execution_allowed: Callable[[], bool] | None = None,
        execution_gate: RuntimeExecutionGate | None = None,
    ) -> None:
        if not 1 <= concurrency <= 4:
            raise ValueError("share worker concurrency must be between one and four")
        if not 0.01 <= idle_poll_seconds <= 60:
            raise ValueError("share worker poll interval is invalid")
        if not 0.1 <= shutdown_timeout_seconds <= 60:
            raise ValueError("share worker shutdown timeout is invalid")
        self.worker = worker
        self.concurrency = concurrency
        self.idle_poll_seconds = idle_poll_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.execution_allowed = execution_allowed or (lambda: True)
        if execution_gate is not None:
            self.worker.repository.jobs.bind_execution_gate(execution_gate)
        self.execution_gate = (
            execution_gate or self.worker.repository.jobs.execution_gate
        )
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self._completed_runs = 0
        self._retry_runs = 0
        self._failed_runs = 0
        self._last_outcome: ShareWorkerOutcome | None = None
        self._last_error: str | None = None

    @property
    def snapshot(self) -> ShareSupervisorSnapshot:
        return ShareSupervisorSnapshot(
            running=bool(self._tasks) and not self._stopping,
            concurrency=self.concurrency,
            completed_runs=self._completed_runs,
            retry_runs=self._retry_runs,
            failed_runs=self._failed_runs,
            last_outcome=self._last_outcome,
            last_error=self._last_error,
        )

    def notify(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        if await asyncio.to_thread(self.execution_allowed) and self._gate_open():
            try:
                self.worker.repository.jobs.reclaim_expired(now=self.worker.clock())
            except LeaseError:
                pass
            self.worker.maintenance_once(subject="supervisor_start")
        self._tasks = [
            asyncio.create_task(self._worker_loop(index), name=f"share-worker-{index}")
            for index in range(self.concurrency)
        ]
        self.notify()

    async def stop(self) -> None:
        self._stopping = True
        self.notify()
        tasks = list(self._tasks)
        if tasks:
            for task in tasks:
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=self.shutdown_timeout_seconds,
                )
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _worker_loop(self, index: int) -> None:
        worker_id = f"share-{id(self):x}-{index}"
        while not self._stopping:
            try:
                if (
                    not await asyncio.to_thread(self.execution_allowed)
                    or not self._gate_open()
                ):
                    await self._wait()
                    continue
                result = await self.worker.run_once(worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # pragma: no cover - defensive supervisor fence
                self._failed_runs += 1
                self._last_error = error.__class__.__name__.casefold()
                await self._wait()
                continue
            self._last_outcome = result.outcome
            self._last_error = result.reason
            if result.outcome is ShareWorkerOutcome.COMPLETED:
                self._completed_runs += 1
            elif result.outcome is ShareWorkerOutcome.RETRY_SCHEDULED:
                self._retry_runs += 1
            elif result.outcome is ShareWorkerOutcome.FAILED:
                self._failed_runs += 1
            if result.outcome is ShareWorkerOutcome.IDLE:
                await self._wait()

    async def _wait(self) -> None:
        if self._stopping:
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.idle_poll_seconds)
        except TimeoutError:
            pass

    def _gate_open(self) -> bool:
        return self.execution_gate is None or self.execution_gate.snapshot().healthy


__all__ = [
    "ShareOperationWorker",
    "ShareSupervisorSnapshot",
    "ShareWorkerOutcome",
    "ShareWorkerResult",
    "ShareWorkerSupervisor",
]
