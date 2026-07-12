"""Fail-closed Runtime execution isolation driven by invariant audits."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import hmac
import re
import secrets
import threading
from typing import Any, Callable, Iterator, Literal

from .invariants import RuntimeInvariantAuditor, RuntimeInvariantReport


RuntimeExecutionStatus = Literal["healthy", "critical"]
_SAFE_CODE = re.compile(r"^[a-z0-9_:-]{1,160}$")
_SAFE_SCOPE = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_code(value: str, *, fallback: str) -> str:
    normalized = str(value or "").strip().casefold()
    return normalized if _SAFE_CODE.fullmatch(normalized) else fallback


@dataclass(frozen=True, slots=True)
class RuntimeExecutionGateSnapshot:
    status: RuntimeExecutionStatus
    checked_at: datetime | None
    violation_codes: tuple[str, ...]
    violation_count: int
    last_error_code: str | None
    epoch: int
    draining: bool = False

    @property
    def healthy(self) -> bool:
        return self.status == "healthy" and not self.draining

    def to_provider_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_at": (
                None if self.checked_at is None else self.checked_at.isoformat()
            ),
            "violation_codes": list(self.violation_codes),
            "violation_count": self.violation_count,
            "last_error_code": self.last_error_code,
            "epoch": self.epoch,
            "draining": self.draining,
        }


@dataclass(frozen=True, slots=True)
class RuntimeExecutionAdmission:
    allowed: bool
    status: RuntimeExecutionStatus
    checked_at: datetime | None
    epoch: int


@dataclass(frozen=True, slots=True)
class RuntimeExecutionPermit:
    """Process-local, signed authority for one execution subject and epoch."""

    gate_id: str
    epoch: int
    scope: str
    subject: str
    nonce: str
    signature: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeDrainPermit:
    """Process-local authority for one reversible update drain."""

    gate_id: str
    epoch: int
    subject: str
    nonce: str
    signature: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _CriticalRequest:
    """One immutable, first-writer-wins diagnostic for epoch closure."""

    error_code: str
    checked_at: datetime


class RuntimeExecutionDenied(RuntimeError):
    """The execution epoch is closed or the supplied permit is not authentic."""


class RuntimeExecutionGate:
    """Linearizes execution admission with a latched invariant boundary.

    A new gate begins closed until its first successful audit. Once an audit
    violation, exception, timeout, or supervisor shutdown latches ``critical``,
    later healthy reports may update the check timestamp but cannot reopen it.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._gate_id = secrets.token_hex(16)
        self._permit_secret = secrets.token_bytes(32)
        self._epoch = 0
        self._closure_requested = threading.Event()
        self._critical_request_lock = threading.Lock()
        self._critical_request: _CriticalRequest | None = None
        self._critical_completion = threading.Event()
        self._status: RuntimeExecutionStatus = "critical"
        self._checked_at: datetime | None = None
        self._violation_codes: tuple[str, ...] = ()
        self._violation_count = 0
        self._last_error_code: str | None = "invariant_audit_required"
        self._latched = False
        self._drain_permit: RuntimeDrainPermit | None = None
        self._drain_allowed_durable_subjects: frozenset[str] = frozenset()

    @contextmanager
    def admission(self) -> Iterator[RuntimeExecutionAdmission]:
        """Capture one admission without retaining a process lock.

        Callers that mutate state must issue a permit and validate it again at
        their commit boundary.  Retaining this lock while waiting on SQLite or
        a provider creates a gate/database lock inversion with guarded commits.
        """

        with self._lock:
            admission = RuntimeExecutionAdmission(
                allowed=(
                    self._status == "healthy"
                    and not self._latched
                    and not self._closure_requested.is_set()
                    and self._drain_permit is None
                ),
                status=(
                    "critical"
                    if self._closure_requested.is_set()
                    else self._status
                ),
                checked_at=self._checked_at,
                epoch=self._epoch,
            )
        yield admission

    def issue_permit(
        self,
        *,
        scope: str,
        subject: str,
        admission: RuntimeExecutionAdmission | None = None,
    ) -> RuntimeExecutionPermit:
        """Issue a process-local permit only while this epoch is healthy."""

        if not isinstance(scope, str) or not _SAFE_SCOPE.fullmatch(scope):
            raise ValueError("execution permit scope is invalid")
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise ValueError("execution permit subject is invalid")
        with self._lock:
            if admission is None:
                self._require_open_locked()
            elif (
                not isinstance(admission, RuntimeExecutionAdmission)
                or not admission.allowed
                or admission.status != "healthy"
                or admission.epoch != self._epoch
                or self._status != "healthy"
                or self._latched
                or self._closure_requested.is_set()
                or self._drain_permit is not None
            ):
                raise RuntimeExecutionDenied(
                    "runtime execution admission is stale"
                )
            nonce = secrets.token_hex(16)
            signature = self._sign_permit(
                epoch=self._epoch,
                scope=scope,
                subject=subject,
                nonce=nonce,
            )
            return RuntimeExecutionPermit(
                gate_id=self._gate_id,
                epoch=self._epoch,
                scope=scope,
                subject=subject,
                nonce=nonce,
                signature=signature,
            )

    @contextmanager
    def admit(
        self,
        permit: RuntimeExecutionPermit,
        *,
        check_on_exit: bool = True,
    ) -> Iterator[None]:
        """Validate around one operation without retaining a thread lock."""

        self.assert_permit(permit)
        try:
            yield
        except BaseException:
            raise
        else:
            if check_on_exit:
                self.assert_permit(permit)

    @contextmanager
    def new_admission(
        self, *, scope: str, subject: str
    ) -> Iterator[RuntimeExecutionPermit]:
        """Issue and revalidate a permit without retaining a thread lock."""

        permit = self.issue_permit(scope=scope, subject=subject)
        try:
            yield permit
        except BaseException:
            raise
        else:
            self.assert_permit(permit)

    def assert_permit(self, permit: RuntimeExecutionPermit) -> None:
        with self._lock:
            self._require_permit_locked(permit)

    def _require_open_locked(self) -> None:
        if (
            self._status != "healthy"
            or self._latched
            or self._closure_requested.is_set()
            or self._drain_permit is not None
        ):
            raise RuntimeExecutionDenied("runtime execution epoch is closed")

    def _require_permit_locked(self, permit: RuntimeExecutionPermit) -> None:
        if not isinstance(permit, RuntimeExecutionPermit):
            raise RuntimeExecutionDenied("runtime execution permit is invalid")
        expected = self._sign_permit(
            epoch=permit.epoch,
            scope=permit.scope,
            subject=permit.subject,
            nonce=permit.nonce,
        )
        if (
            permit.gate_id != self._gate_id
            or permit.epoch != self._epoch
            or not hmac.compare_digest(permit.signature, expected)
        ):
            raise RuntimeExecutionDenied("runtime execution permit is stale")
        if (
            self._status != "healthy"
            or self._latched
            or self._closure_requested.is_set()
        ):
            raise RuntimeExecutionDenied("runtime execution epoch is closed")
        if self._drain_permit is not None and (
            permit.scope != "durable_job"
            or permit.subject not in self._drain_allowed_durable_subjects
        ):
            raise RuntimeExecutionDenied("runtime execution is draining")

    def begin_drain(
        self,
        *,
        subject: str,
        allowed_durable_subjects: frozenset[str],
    ) -> RuntimeDrainPermit:
        """Close new admission while allowing only already-leased jobs.

        The caller captures ``allowed_durable_subjects`` while holding the
        Runtime database write lock. A job lease that has not committed at
        that boundary therefore cannot become executable after the drain.
        """

        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise ValueError("runtime drain subject is invalid")
        if not isinstance(allowed_durable_subjects, frozenset) or any(
            not isinstance(value, str) or not value or len(value) > 512
            for value in allowed_durable_subjects
        ):
            raise ValueError("runtime drain durable subjects are invalid")
        with self._lock:
            self._require_open_locked()
            nonce = secrets.token_hex(16)
            signature = self._sign_drain(
                epoch=self._epoch,
                subject=subject,
                nonce=nonce,
            )
            permit = RuntimeDrainPermit(
                gate_id=self._gate_id,
                epoch=self._epoch,
                subject=subject,
                nonce=nonce,
                signature=signature,
            )
            self._drain_allowed_durable_subjects = allowed_durable_subjects
            self._drain_permit = permit
            return permit

    def assert_drain(self, permit: RuntimeDrainPermit) -> None:
        with self._lock:
            self._require_drain_locked(permit)

    def cancel_drain(self, permit: RuntimeDrainPermit) -> None:
        """Reopen normal admission only for the exact active drain."""

        with self._lock:
            self._require_drain_locked(permit)
            self._drain_permit = None
            self._drain_allowed_durable_subjects = frozenset()

    def _require_drain_locked(self, permit: RuntimeDrainPermit) -> None:
        if not isinstance(permit, RuntimeDrainPermit):
            raise RuntimeExecutionDenied("runtime drain permit is invalid")
        expected = self._sign_drain(
            epoch=permit.epoch,
            subject=permit.subject,
            nonce=permit.nonce,
        )
        if (
            self._drain_permit != permit
            or permit.gate_id != self._gate_id
            or permit.epoch != self._epoch
            or not hmac.compare_digest(permit.signature, expected)
        ):
            raise RuntimeExecutionDenied("runtime drain permit is stale")

    def _sign_drain(self, *, epoch: int, subject: str, nonce: str) -> str:
        payload = "\0".join(
            (self._gate_id, str(epoch), "runtime_update_drain", subject, nonce)
        ).encode("utf-8")
        return hmac.new(self._permit_secret, payload, hashlib.sha256).hexdigest()

    def _sign_permit(
        self, *, epoch: int, scope: str, subject: str, nonce: str
    ) -> str:
        payload = "\0".join(
            (self._gate_id, str(epoch), scope, subject, nonce)
        ).encode("utf-8")
        return hmac.new(self._permit_secret, payload, hashlib.sha256).hexdigest()

    def record_report(self, report: RuntimeInvariantReport) -> None:
        if not isinstance(report, RuntimeInvariantReport):
            self.mark_critical(error_code="invariant_audit_invalid_report")
            return
        checked_at = report.checked_at
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        else:
            checked_at = checked_at.astimezone(UTC)
        codes = tuple(
            sorted(
                {
                    _safe_code(
                        violation.code,
                        fallback="invalid_invariant_violation_code",
                    )
                    for violation in report.violations
                }
            )
        )
        if report.violations:
            # Signal closure before waiting for an in-flight provider/commit
            # admission. External results then fail their exit check and a
            # transaction that has not reached its guarded commit rolls back.
            self._closure_requested.set()
        with self._lock:
            self._checked_at = checked_at
            if report.violations:
                if not self._latched:
                    self._epoch += 1
                self._status = "critical"
                self._latched = True
                self._violation_codes = codes
                self._violation_count = len(report.violations)
                self._last_error_code = None
            elif not self._latched and not self._closure_requested.is_set():
                if self._status != "healthy":
                    self._epoch += 1
                self._status = "healthy"
                self._violation_codes = ()
                self._violation_count = 0
                self._last_error_code = None

    def record_audit_exception(self, error: BaseException) -> None:
        error_type = _safe_code(
            type(error).__name__,
            fallback="unknown",
        )
        self.mark_critical(error_code=f"invariant_audit_failed:{error_type}")

    def mark_critical(
        self,
        *,
        error_code: str,
        checked_at: datetime | None = None,
    ) -> None:
        safe_error = _safe_code(
            error_code,
            fallback="invariant_audit_failed:invalid_error_code",
        )
        timestamp = checked_at or _utc_now()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        else:
            timestamp = timestamp.astimezone(UTC)
        request, published = self._publish_critical_request(
            _CriticalRequest(error_code=safe_error, checked_at=timestamp)
        )
        if not published:
            return
        try:
            with self._lock:
                self._apply_requested_critical(request)
        finally:
            self._critical_completion.set()

    def request_critical(
        self,
        *,
        error_code: str,
        checked_at: datetime | None = None,
    ) -> None:
        """Close admission immediately and finish diagnostics without blocking.

        This is used by bounded maintenance timeouts.  An already-admitted
        transaction may finish its linearized section, but the Event flag makes
        every subsequent admission fail before a potentially blocked holder
        releases the gate lock.
        """

        safe_error = _safe_code(
            error_code,
            fallback="invariant_audit_failed:invalid_error_code",
        )
        timestamp = checked_at or _utc_now()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        else:
            timestamp = timestamp.astimezone(UTC)
        request, published = self._publish_critical_request(
            _CriticalRequest(error_code=safe_error, checked_at=timestamp)
        )
        if not published:
            return
        if self._lock.acquire(blocking=False):
            try:
                self._apply_requested_critical(request)
            finally:
                self._lock.release()
                self._critical_completion.set()
            return
        threading.Thread(
            target=self._finish_requested_critical,
            name="ecorex-runtime-gate-close",
            daemon=True,
        ).start()

    def _publish_critical_request(
        self,
        request: _CriticalRequest,
    ) -> tuple[_CriticalRequest, bool]:
        """Publish one atomic diagnostic without waiting on the gate lock."""

        with self._critical_request_lock:
            published = self._critical_request
            if published is not None:
                return published, False
            self._critical_request = request
            # Publish the immutable diagnostic before closing admission. Any
            # observer that sees this Event can therefore project one exact
            # error/timestamp pair without consulting independently mutable
            # fields or waiting on the main gate lock.
            self._closure_requested.set()
            return request, True

    def _finish_requested_critical(self) -> None:
        request = self._critical_request
        if request is None:
            return
        try:
            with self._lock:
                self._apply_requested_critical(request)
        finally:
            self._critical_completion.set()

    def _apply_requested_critical(self, request: _CriticalRequest) -> None:
        if not self._latched:
            self._epoch += 1
        self._status = "critical"
        self._latched = True
        self._checked_at = request.checked_at
        self._last_error_code = request.error_code

    def snapshot(self) -> RuntimeExecutionGateSnapshot:
        closure_requested = self._closure_requested.is_set()
        request = self._critical_request
        acquired = self._lock.acquire(blocking=not closure_requested)
        if not acquired:
            # A bounded timeout may request closure while the timed-out
            # maintenance transaction still owns admission. Immutable tuple /
            # scalar references are safe to snapshot here; most importantly,
            # health reads and new lease admission observe critical instantly.
            return RuntimeExecutionGateSnapshot(
                status="critical",
                checked_at=(
                    request.checked_at if request is not None else self._checked_at
                ),
                violation_codes=self._violation_codes,
                violation_count=self._violation_count,
                last_error_code=(
                    request.error_code if request is not None else self._last_error_code
                ),
                epoch=(
                    self._epoch + 1
                    if not self._latched and self._closure_requested.is_set()
                    else self._epoch
                ),
                draining=self._drain_permit is not None,
            )
        try:
            closure_requested = self._closure_requested.is_set()
            return RuntimeExecutionGateSnapshot(
                status="critical" if closure_requested else self._status,
                checked_at=(
                    request.checked_at
                    if closure_requested and request is not None
                    else self._checked_at
                ),
                violation_codes=self._violation_codes,
                violation_count=self._violation_count,
                last_error_code=(
                    request.error_code
                    if closure_requested and request is not None
                    else self._last_error_code
                ),
                epoch=(
                    self._epoch + 1
                    if closure_requested and not self._latched
                    else self._epoch
                ),
                draining=self._drain_permit is not None,
            )
        finally:
            self._lock.release()

    def provider_dict(self) -> dict[str, Any]:
        return self.snapshot().to_provider_dict()


@dataclass(frozen=True, slots=True)
class RuntimeInvariantSupervisorSnapshot:
    running: bool
    audit_count: int
    audit_interval_seconds: float
    audit_timeout_seconds: float
    gate: RuntimeExecutionGateSnapshot

    def to_provider_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "audit_count": self.audit_count,
            "audit_interval_seconds": self.audit_interval_seconds,
            "audit_timeout_seconds": self.audit_timeout_seconds,
            **self.gate.to_provider_dict(),
        }


class RuntimeInvariantSupervisor:
    """Runs preflight and periodic audits without repairing durable state."""

    def __init__(
        self,
        auditor: RuntimeInvariantAuditor,
        gate: RuntimeExecutionGate,
        *,
        audit_interval_seconds: float = 60.0,
        audit_timeout_seconds: float = 30.0,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if not 0.01 <= audit_interval_seconds <= 3600:
            raise ValueError("Runtime invariant audit interval is invalid")
        if not 0.05 <= audit_timeout_seconds <= 300:
            raise ValueError("Runtime invariant audit timeout is invalid")
        if not 0.05 <= shutdown_timeout_seconds <= 120:
            raise ValueError("Runtime invariant shutdown timeout is invalid")
        self.auditor = auditor
        self.gate = gate
        self.audit_interval_seconds = audit_interval_seconds
        self.audit_timeout_seconds = audit_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._audit_count = 0
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

    def snapshot(self) -> RuntimeInvariantSupervisorSnapshot:
        return RuntimeInvariantSupervisorSnapshot(
            running=self.running,
            audit_count=self._audit_count,
            audit_interval_seconds=self.audit_interval_seconds,
            audit_timeout_seconds=self.audit_timeout_seconds,
            gate=self.gate.snapshot(),
        )

    def provider_dict(self) -> dict[str, Any]:
        return self.snapshot().to_provider_dict()

    async def start(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise RuntimeError(
                    "Runtime invariant supervisor has already been closed"
                )
            if self.running:
                return
            self._stopping = False
            self._stop_event = asyncio.Event()
            # This await is the preflight fence. Composition must call it before
            # starting any worker supervisor that can request a durable lease.
            await self._audit_once()
            if self._stopping or self._closed:
                return
            self._monitor_task = asyncio.create_task(
                self._monitor_loop(),
                name="ecorex-runtime-invariant-supervisor",
            )

    async def stop(self) -> None:
        if self._closed:
            return
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(
                self._stop_owned_task(),
                name="ecorex-runtime-invariant-stop",
            )
        await asyncio.shield(self._stop_task)

    async def _stop_owned_task(self) -> None:
        self._stopping = True
        self.gate.mark_critical(error_code="invariant_supervisor_stopped")
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
                    timeout=self.audit_interval_seconds,
                )
            except TimeoutError:
                await self._audit_once()
                continue
            return

    async def _audit_once(self) -> None:
        self._audit_count += 1
        try:
            await asyncio.wait_for(
                self._call_sync_audit(self._audit_and_record),
                timeout=self.audit_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.gate.mark_critical(error_code="invariant_audit_timeout")
        except BaseException as error:
            self.gate.record_audit_exception(error)

    def _audit_and_record(self) -> None:
        try:
            report = self.auditor.audit()
        except BaseException as error:
            self.gate.record_audit_exception(error)
            return
        self.gate.record_report(report)

    @staticmethod
    async def _call_sync_audit(audit: Callable[[], None]) -> None:
        """Run a potentially blocking audit behind a cancellable asyncio boundary."""

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
                audit()
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
            name="ecorex-runtime-invariant-audit",
            daemon=True,
        ).start()
        await completed


__all__ = [
    "RuntimeExecutionAdmission",
    "RuntimeExecutionDenied",
    "RuntimeExecutionGate",
    "RuntimeExecutionGateSnapshot",
    "RuntimeExecutionPermit",
    "RuntimeDrainPermit",
    "RuntimeExecutionStatus",
    "RuntimeInvariantSupervisor",
    "RuntimeInvariantSupervisorSnapshot",
]
