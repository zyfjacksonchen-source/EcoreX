"""Durable Runtime bridge and supervised executor for precise retouch."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import inspect
import sqlite3
import hashlib
import threading
from typing import Any, Callable, Protocol

from ecorex.artifacts import (
    ArtifactActionUnavailable,
    ArtifactProjection,
    ArtifactScope,
    ArtifactService,
    IdempotencyConflict,
    RetouchExecutionBinding,
    RetouchJob,
    RetouchJobProjection,
    RetouchJobStatus,
    RetouchRequest,
)
from ecorex.protocol import (
    JOB_TRANSITIONS,
    TERMINAL_TURN_STATUSES,
    TERMINAL_JOB_STATUSES,
    CreateTurnRequest,
    ItemKind,
    ItemStatus,
    JobStatus,
    TurnStatus,
)
from ecorex.runtime.errors import ConflictError, LeaseError
from ecorex.runtime.invariant_guard import (
    RuntimeExecutionDenied,
    RuntimeExecutionPermit,
)
from ecorex.runtime.jobs import _store_time
from ecorex.runtime.kernel import RuntimeKernel
from ecorex.runtime.snapshots import TurnSnapshotContext

from .retouch_adapter import (
    CloudImageRetouchAdapter,
    RetouchAdapterError,
    RetouchImageAsset,
    RetouchMaskAsset,
    StructuredRetouchAdapterRequest,
    StructuredRetouchAdapterResult,
    invoke_adapter,
)


RETOUCH_JOB_KIND = "artifact_retouch"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_reason(value: str, *, fallback: str) -> str:
    reason = str(value or "").strip() or fallback
    return reason[:512]


class RetouchWorkerOutcome(StrEnum):
    IDLE = "idle"
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RetouchWorkerResult:
    outcome: RetouchWorkerOutcome
    retouch_job_id: str | None = None
    durable_job_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RetouchSupervisorSnapshot:
    running: bool
    concurrency: int
    completed_runs: int
    retry_runs: int
    failed_runs: int
    last_outcome: RetouchWorkerOutcome | None
    last_error: str | None


class RetouchSnapshotContextProvider(Protocol):
    """Capture a current, backend-authoritative context for one retouch Turn."""

    def __call__(
        self,
        *,
        thread_id: str,
        request: RetouchRequest,
        turn_request: CreateTurnRequest,
    ) -> TurnSnapshotContext:
        ...


@dataclass(frozen=True, slots=True)
class RetouchTurnAdmission:
    request: CreateTurnRequest
    snapshot_context: TurnSnapshotContext


class RuntimeRetouchBridge:
    """Atomically joins Artifact metadata to the shared Runtime database."""

    def __init__(
        self,
        kernel: RuntimeKernel,
        *,
        snapshot_context_provider: RetouchSnapshotContextProvider,
        permission_mutation_lock: Any | None = None,
        clock: Callable[[], datetime] = _utc_now,
        max_attempts: int = 3,
        deadline_seconds: int = 900,
    ) -> None:
        if not 1 <= max_attempts <= 10:
            raise ValueError("retouch max_attempts must be between one and ten")
        if not 30 <= deadline_seconds <= 86_400:
            raise ValueError("retouch deadline must be between 30 seconds and one day")
        self.kernel = kernel
        self.snapshot_context_provider = snapshot_context_provider
        self.permission_mutation_lock = (
            permission_mutation_lock or threading.RLock()
        )
        if not all(
            callable(getattr(self.permission_mutation_lock, member, None))
            for member in ("acquire", "release")
        ):
            raise ValueError("retouch permission mutation lock is invalid")
        self.clock = clock
        self.max_attempts = max_attempts
        self.deadline_seconds = deadline_seconds

    def prepare_admission(
        self,
        artifact_id: str,
        scope: ArtifactScope,
        request: RetouchRequest,
    ) -> RetouchTurnAdmission:
        if scope.thread_id is None or scope.turn_id is None:
            raise ArtifactActionUnavailable(
                "precise retouch requires a Runtime Thread/Turn execution scope"
            )
        intent = request.global_instruction or "按标注精准修改图片"
        display_intent = f"精准修图：{intent}"
        message_digest = hashlib.sha256(
            "\0".join(
                (
                    scope.thread_id,
                    artifact_id,
                    request.client_request_id,
                )
            ).encode("utf-8")
        ).hexdigest()
        turn_request = CreateTurnRequest(
            input=display_intent,
            agent_model_id=(
                request.agent_model_id
                or self.kernel.get_turn(scope.turn_id).agent_model_id
            ),
            image_model_id=(
                request.image_model_id
                or self.kernel.get_turn(scope.turn_id).image_model_id
            ),
            client_message_id=f"retouch:{message_digest}",
            metadata={
                "operation": RETOUCH_JOB_KIND,
                "artifact_id": artifact_id,
                "base_revision_id": request.base_revision_id,
                "selected_artifact_ids": list(request.selected_artifact_ids),
                "annotations": [
                    annotation.to_dict() for annotation in request.annotations
                ],
                "reference_artifact_ids": list(request.reference_artifact_ids),
                "global_instruction": request.global_instruction,
                "source_turn_id": scope.turn_id,
                "client_request_id": request.client_request_id,
            },
        )
        snapshot_context = self.snapshot_context_provider(
            thread_id=scope.thread_id,
            request=request,
            turn_request=turn_request,
        )
        if not isinstance(snapshot_context, TurnSnapshotContext):
            raise TypeError(
                "retouch snapshot context provider returned an invalid value"
            )
        config = self.kernel.snapshots.get(snapshot_context.config_snapshot_id)
        canonical_agent_model_id = config.payload.get("agent_model_id")
        canonical_image_model_id = config.payload.get("image_model_id")
        if (
            not isinstance(canonical_agent_model_id, str)
            or not canonical_agent_model_id
            or not isinstance(canonical_image_model_id, str)
            or not canonical_image_model_id
        ):
            raise ArtifactActionUnavailable(
                "precise retouch requires available chat and image models"
            )
        return RetouchTurnAdmission(
            request=turn_request.model_copy(
                update={
                    "agent_model_id": canonical_agent_model_id,
                    "image_model_id": canonical_image_model_id,
                }
            ),
            snapshot_context=snapshot_context,
        )

    def assert_shared_database(self, service: ArtifactService) -> None:
        artifact_path = service.repository.database_path.resolve()
        runtime_path = self.kernel.database.path.resolve()
        if artifact_path != runtime_path:
            raise ValueError(
                "retouch coordinator requires Artifact and Runtime to share one database"
            )

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        job: RetouchJob,
        scope: ArtifactScope,
        *,
        admission: RetouchTurnAdmission,
    ) -> RetouchExecutionBinding:
        if scope.thread_id is not None:
            thread = connection.execute(
                "SELECT thread_id FROM threads WHERE thread_id = ?",
                (scope.thread_id,),
            ).fetchone()
            if thread is None:
                raise ConflictError("retouch execution scope references an unknown Thread")
        if scope.turn_id is not None:
            turn = connection.execute(
                "SELECT thread_id FROM turns WHERE turn_id = ?",
                (scope.turn_id,),
            ).fetchone()
            if turn is None or turn["thread_id"] != scope.thread_id:
                raise ConflictError(
                    "retouch execution Turn does not belong to its Thread"
                )
        now = self.clock()
        assert scope.thread_id is not None
        execution_turn = self.kernel._create_operation_turn_in_transaction(
            connection,
            thread_id=scope.thread_id,
            request=admission.request,
            snapshot_context=admission.snapshot_context,
            operation_kind=RETOUCH_JOB_KIND,
            account_id=scope.account_id,
            causation_id=scope.turn_id,
            correlation_id=job.request.client_request_id,
            now=now,
        )
        durable = self.kernel.jobs.enqueue_in_transaction(
            connection,
            kind=RETOUCH_JOB_KIND,
            payload={
                "retouch_job_id": job.job_id,
                "artifact_id": job.artifact_id,
                "base_revision_id": job.base_revision_id,
                "source_turn_id": scope.turn_id,
            },
            idempotency_key=f"retouch-runtime:{job.job_id}",
            thread_id=scope.thread_id,
            turn_id=execution_turn.turn_id,
            max_attempts=self.max_attempts,
            deadline=now + timedelta(seconds=self.deadline_seconds),
            now=now,
            event_context=admission.snapshot_context.to_dict(),
        )
        return RetouchExecutionBinding(
            durable_job_id=durable.job_id,
            thread_id=scope.thread_id,
            turn_id=execution_turn.turn_id,
        )

    def running_hook(
        self,
        *,
        worker_id: str,
        lease_token: str,
    ) -> Callable[[sqlite3.Connection, RetouchJob], None]:
        def start(connection: sqlite3.Connection, job: RetouchJob) -> None:
            row = self._owned_runtime_job(
                connection, job, worker_id=worker_id, lease_token=lease_token
            )
            now = self.clock()
            self.kernel.jobs._assert_transition(
                JobStatus(row["status"]), JobStatus.RUNNING
            )
            self.kernel.jobs._append_job_event(
                connection,
                row_or_values=row,
                event_type="job.started",
                payload={"worker_id": worker_id, "attempt": row["attempt"]},
                created_at=now,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (JobStatus.RUNNING.value, _store_time(now), row["job_id"]),
            )
            turn = self._execution_turn(connection, job)
            current = TurnStatus(turn["status"])
            if current is TurnStatus.QUEUED:
                for target in (
                    TurnStatus.PREPARING,
                    TurnStatus.TOOL_PENDING,
                    TurnStatus.TOOL_RUNNING,
                ):
                    turn = self._transition_turn(
                        connection, turn, target=target, now=now
                    )
            elif current is TurnStatus.RETRY_WAIT:
                self._transition_turn(
                    connection, turn, target=TurnStatus.TOOL_RUNNING, now=now
                )
            elif current is not TurnStatus.TOOL_RUNNING:
                raise ConflictError(
                    f"retouch Turn cannot start from {current.value}"
                )

        return start

    def completed_hook(
        self,
        *,
        worker_id: str,
        lease_token: str,
    ) -> Callable[[sqlite3.Connection, RetouchJob, ArtifactProjection], None]:
        def complete(
            connection: sqlite3.Connection,
            job: RetouchJob,
            artifact: ArtifactProjection,
        ) -> None:
            row = self._owned_runtime_job(
                connection, job, worker_id=worker_id, lease_token=lease_token
            )
            now = self.clock()
            turn = self._execution_turn(connection, job)
            turn = self._transition_turn(
                connection,
                turn,
                target=TurnStatus.FINALIZING,
                now=now,
            )
            self.kernel.jobs._assert_transition(
                JobStatus(row["status"]), JobStatus.COMPLETED
            )
            self.kernel.jobs._append_job_event(
                connection,
                row_or_values=row,
                event_type="job.completed",
                payload={"attempt": row["attempt"], "retouch_job_id": job.job_id},
                created_at=now,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, updated_at = ? "
                "WHERE job_id = ?",
                (JobStatus.COMPLETED.value, _store_time(now), row["job_id"]),
            )
            public_payload = {
                "retouch_job_id": job.job_id,
                "artifact": artifact.to_dict(),
                "change_summary": job.change_summary,
                "inspection_regions": [
                    region.to_dict() for region in job.inspection_regions
                ],
                "preview": {
                    "artifact_id": artifact.artifact_id,
                    "revision_id": artifact.revision_id,
                    "mime_type": artifact.mime_type,
                },
            }
            if job.execution_turn_id and job.execution_thread_id:
                self.kernel._create_item_in_transaction(
                    connection,
                    thread_id=job.execution_thread_id,
                    turn_id=job.execution_turn_id,
                    kind=ItemKind.ARTIFACT,
                    content=public_payload,
                    status=ItemStatus.COMPLETED,
                    idempotency_key=f"{job.job_id}:artifact-item",
                    now=now,
                )
                self.kernel.events.append_in_transaction(
                    connection,
                    thread_id=job.execution_thread_id,
                    turn_id=job.execution_turn_id,
                    job_id=row["job_id"],
                    event_type="artifact.retouch.completed",
                    payload={
                        "artifact_id": artifact.artifact_id,
                        "revision_id": artifact.revision_id,
                        "retouch_job_id": job.job_id,
                        "change_summary": job.change_summary,
                        "inspection_regions": [
                            region.to_dict() for region in job.inspection_regions
                        ],
                    },
                    correlation_id=job.request.client_request_id,
                    idempotency_key=f"{job.job_id}:completed-event",
                    created_at=now,
                )
                self._transition_turn(
                    connection,
                    turn,
                    target=TurnStatus.COMPLETED,
                    now=now,
                )

        return complete

    def requeued_hook(
        self,
        *,
        worker_id: str,
        lease_token: str,
        reason: str,
        delay_seconds: int,
    ) -> Callable[[sqlite3.Connection, RetouchJob], None]:
        def requeue(connection: sqlite3.Connection, job: RetouchJob) -> None:
            row = self._owned_runtime_job(
                connection, job, worker_id=worker_id, lease_token=lease_token
            )
            now = self.clock()
            self.kernel.jobs._assert_transition(
                JobStatus(row["status"]), JobStatus.RETRY_SCHEDULED
            )
            self.kernel.jobs._append_job_event(
                connection,
                row_or_values=row,
                event_type="job.retry_scheduled",
                payload={
                    "attempt": row["attempt"],
                    "error": _safe_reason(reason, fallback="retouch_retry"),
                },
                created_at=now,
            )
            connection.execute(
                "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, heartbeat_at = NULL, available_at = ?, "
                "last_error = ?, updated_at = ? WHERE job_id = ?",
                (
                    JobStatus.RETRY_SCHEDULED.value,
                    _store_time(now + timedelta(seconds=max(0, delay_seconds))),
                    _safe_reason(reason, fallback="retouch_retry"),
                    _store_time(now),
                    row["job_id"],
                ),
            )
            turn = self._execution_turn(connection, job)
            if TurnStatus(turn["status"]) is TurnStatus.TOOL_RUNNING:
                self._transition_turn(
                    connection,
                    turn,
                    target=TurnStatus.RETRY_WAIT,
                    reason=_safe_reason(reason, fallback="retouch_retry"),
                    now=now,
                )

        return requeue

    def terminal_hook(
        self,
        *,
        target: JobStatus,
        reason: str,
        worker_id: str | None = None,
        lease_token: str | None = None,
    ) -> Callable[[sqlite3.Connection, RetouchJob], None]:
        if target not in {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.DEAD_LETTER}:
            raise ValueError("retouch Runtime terminal target is invalid")

        def terminal(connection: sqlite3.Connection, job: RetouchJob) -> None:
            row = self._runtime_job(connection, job)
            current = JobStatus(row["status"])
            now = self.clock()
            if current in TERMINAL_JOB_STATUSES and current is not target:
                raise ConflictError(
                    "retouch Runtime job is terminal in a conflicting state"
                )
            if current not in TERMINAL_JOB_STATUSES:
                if worker_id is not None:
                    row = self._owned_runtime_job(
                        connection,
                        job,
                        worker_id=worker_id,
                        lease_token=lease_token or "",
                    )
                    current = JobStatus(row["status"])
                if target not in JOB_TRANSITIONS[current]:
                    raise ConflictError(
                        f"retouch Runtime job cannot transition from {current.value} "
                        f"to {target.value}"
                    )
                event_type = {
                    JobStatus.FAILED: "job.failed",
                    JobStatus.CANCELLED: "job.cancelled",
                    JobStatus.DEAD_LETTER: "job.dead_lettered",
                }[target]
                self.kernel.jobs._append_job_event(
                    connection,
                    row_or_values=row,
                    event_type=event_type,
                    payload={
                        "attempt": row["attempt"],
                        "reason": _safe_reason(reason, fallback=target.value),
                    },
                    created_at=now,
                )
                connection.execute(
                    "UPDATE jobs SET status = ?, lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = NULL, last_error = ?, "
                    "updated_at = ? WHERE job_id = ?",
                    (
                        target.value,
                        _safe_reason(reason, fallback=target.value),
                        _store_time(now),
                        row["job_id"],
                    ),
                )
            if job.execution_thread_id:
                event_type = (
                    "artifact.retouch.cancelled"
                    if job.status is RetouchJobStatus.CANCELLED
                    else "artifact.retouch.failed"
                )
                self.kernel.events.append_in_transaction(
                    connection,
                    thread_id=job.execution_thread_id,
                    turn_id=job.execution_turn_id,
                    job_id=row["job_id"],
                    event_type=event_type,
                    payload={
                        "artifact_id": job.artifact_id,
                        "retouch_job_id": job.job_id,
                        "reason": _safe_reason(reason, fallback=target.value),
                    },
                    correlation_id=job.request.client_request_id,
                    idempotency_key=f"{job.job_id}:{job.status.value}-event",
                    created_at=now,
                )
                self._finish_execution_turn(
                    connection,
                    job,
                    target=(
                        TurnStatus.CANCELLED
                        if job.status is RetouchJobStatus.CANCELLED
                        else TurnStatus.FAILED
                    ),
                    reason=_safe_reason(reason, fallback=target.value),
                    now=now,
                )

        return terminal

    def reconcile_hook(
        self, *, reason: str
    ) -> Callable[[sqlite3.Connection, RetouchJob], None]:
        def reconcile(connection: sqlite3.Connection, job: RetouchJob) -> None:
            if not job.execution_thread_id:
                return
            row = self._runtime_job(connection, job)
            now = self.clock()
            event_type = (
                "artifact.retouch.cancelled"
                if job.status is RetouchJobStatus.CANCELLED
                else "artifact.retouch.failed"
            )
            self.kernel.events.append_in_transaction(
                connection,
                thread_id=job.execution_thread_id,
                turn_id=job.execution_turn_id,
                job_id=row["job_id"],
                event_type=event_type,
                payload={
                    "artifact_id": job.artifact_id,
                    "retouch_job_id": job.job_id,
                    "reason": _safe_reason(reason, fallback="runtime_terminal"),
                },
                correlation_id=job.request.client_request_id,
                idempotency_key=f"{job.job_id}:{job.status.value}-event",
                created_at=now,
            )
            self._finish_execution_turn(
                connection,
                job,
                target=(
                    TurnStatus.CANCELLED
                    if job.status is RetouchJobStatus.CANCELLED
                    else TurnStatus.FAILED
                ),
                reason=_safe_reason(reason, fallback="runtime_terminal"),
                now=now,
            )

        return reconcile

    def missing_runtime_hook(
        self, *, reason: str
    ) -> Callable[[sqlite3.Connection, RetouchJob], None]:
        def missing(connection: sqlite3.Connection, job: RetouchJob) -> None:
            if not job.execution_thread_id or not job.execution_turn_id:
                return
            now = self.clock()
            self.kernel.events.append_in_transaction(
                connection,
                thread_id=job.execution_thread_id,
                turn_id=job.execution_turn_id,
                event_type="artifact.retouch.failed",
                payload={
                    "artifact_id": job.artifact_id,
                    "retouch_job_id": job.job_id,
                    "reason": _safe_reason(reason, fallback="durable_job_missing"),
                },
                correlation_id=job.request.client_request_id,
                idempotency_key=f"{job.job_id}:missing-runtime-event",
                created_at=now,
            )
            self._finish_execution_turn(
                connection,
                job,
                target=TurnStatus.FAILED,
                reason=_safe_reason(reason, fallback="durable_job_missing"),
                now=now,
            )

        return missing

    def _runtime_job(
        self, connection: sqlite3.Connection, job: RetouchJob
    ) -> sqlite3.Row:
        if not job.durable_job_id:
            raise ConflictError("retouch job is not bound to a durable Runtime job")
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job.durable_job_id,)
        ).fetchone()
        if row is None or row["kind"] != RETOUCH_JOB_KIND:
            raise ConflictError("retouch durable Runtime job binding is invalid")
        payload = self.kernel.jobs._from_row(row).payload
        if payload.get("retouch_job_id") != job.job_id:
            raise ConflictError("retouch durable Runtime job payload is invalid")
        return row

    def _execution_turn(
        self, connection: sqlite3.Connection, job: RetouchJob
    ) -> sqlite3.Row:
        if not job.execution_turn_id or not job.execution_thread_id:
            raise ConflictError("retouch job is not bound to an execution Turn")
        turn = self.kernel._require_turn(connection, job.execution_turn_id)
        if turn["thread_id"] != job.execution_thread_id:
            raise ConflictError("retouch execution Turn binding is invalid")
        metadata = self.kernel._turn_from_row(turn).metadata
        if (
            metadata.get("operation") != RETOUCH_JOB_KIND
            or metadata.get("artifact_id") != job.artifact_id
            or metadata.get("client_request_id")
            != job.request.client_request_id
        ):
            raise ConflictError("retouch execution Turn metadata is invalid")
        return turn

    def _transition_turn(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        target: TurnStatus,
        now: datetime,
        reason: str | None = None,
    ) -> sqlite3.Row:
        current = TurnStatus(row["status"])
        if current is target:
            return row
        self.kernel._transition_turn_in_transaction(
            connection,
            row=row,
            target=target,
            reason=reason,
            now=now,
        )
        return self.kernel._require_turn(connection, row["turn_id"])

    def _finish_execution_turn(
        self,
        connection: sqlite3.Connection,
        job: RetouchJob,
        *,
        target: TurnStatus,
        reason: str,
        now: datetime,
    ) -> None:
        turn = self._execution_turn(connection, job)
        current = TurnStatus(turn["status"])
        if current in TERMINAL_TURN_STATUSES:
            if current is not target:
                raise ConflictError(
                    "retouch execution Turn is terminal in a conflicting state"
                )
            return
        if target is TurnStatus.FAILED and current in {
            TurnStatus.ACCEPTED,
            TurnStatus.QUEUED,
        }:
            if current is TurnStatus.ACCEPTED:
                turn = self._transition_turn(
                    connection, turn, target=TurnStatus.QUEUED, now=now
                )
            turn = self._transition_turn(
                connection, turn, target=TurnStatus.PREPARING, now=now
            )
        self._transition_turn(
            connection,
            turn,
            target=target,
            reason=reason,
            now=now,
        )

    def _owned_runtime_job(
        self,
        connection: sqlite3.Connection,
        job: RetouchJob,
        *,
        worker_id: str,
        lease_token: str,
    ) -> sqlite3.Row:
        if not job.durable_job_id:
            raise ConflictError("retouch job has no durable Runtime binding")
        return self.kernel.jobs._owned_row(
            connection,
            job.durable_job_id,
            worker_id,
            lease_token,
            self.clock(),
        )


class RetouchCoordinator:
    def __init__(
        self,
        service: ArtifactService,
        bridge: RuntimeRetouchBridge,
        *,
        notify: Callable[[], None] | None = None,
    ) -> None:
        bridge.assert_shared_database(service)
        self.service = service
        self.bridge = bridge
        self.notify = notify

    def request(
        self,
        artifact_id: str,
        request: RetouchRequest,
        *,
        account_id: str = "local-user",
        execution_scope: ArtifactScope | None = None,
        on_persisted: Callable[
            [sqlite3.Connection, RetouchJob, ArtifactScope], None
        ]
        | None = None,
    ) -> RetouchJobProjection:
        scope = execution_scope or self.service.get_artifact_scope(artifact_id)
        if scope.thread_id is None or scope.turn_id is None:
            raise ArtifactActionUnavailable(
                "precise retouch requires a Runtime Thread/Turn execution scope"
            )
        # A replay of an already-bound request must not recapture policy or
        # create another Turn. The repository remains the final concurrency and
        # payload-conflict authority for races between this read and its write.
        self.service.get_user_artifact(artifact_id, account_id=account_id)
        existing = self.service.repository.find_retouch_job(
            artifact_id, request.client_request_id
        )
        if existing is not None and existing.durable_job_id is not None:
            if existing.request.to_dict() != request.to_dict():
                raise IdempotencyConflict(
                    "client_request_id was already used with a different retouch payload"
                )
            if on_persisted is None:
                return existing.public_projection()
            return self.service.request_retouch(
                artifact_id,
                request,
                account_id=account_id,
                execution_scope=scope,
                on_persisted=on_persisted,
            )

        # Snapshot capture and the Artifact/Turn/Job product transaction share
        # the same synchronous admission as permission mutation. No adapter or
        # other external provider is called inside this lock.
        with self.bridge.permission_mutation_lock:
            admission = self.bridge.prepare_admission(artifact_id, scope, request)

            def bind(
                connection: sqlite3.Connection,
                job: RetouchJob,
                stored_scope: ArtifactScope,
            ) -> RetouchExecutionBinding:
                return self.bridge.enqueue_in_transaction(
                    connection,
                    job,
                    stored_scope,
                    admission=admission,
                )

            projection = self.service.request_retouch(
                artifact_id,
                request,
                account_id=account_id,
                execution_scope=scope,
                on_created=bind,
                on_persisted=on_persisted,
            )
        internal = self.service.get_internal_retouch_job(projection.job_id)
        if not internal.durable_job_id:
            raise RuntimeError("retouch request did not acquire a durable Runtime job")
        if self.notify is not None:
            self.notify()
        return projection

    def adapter_request(self, retouch_job_id: str) -> StructuredRetouchAdapterRequest:
        job = self.service.get_internal_retouch_job(retouch_job_id)
        if not job.external_idempotency_key:
            raise RuntimeError("retouch job has no external idempotency key")
        scope = self.service.get_artifact_scope(job.artifact_id)
        account_id = scope.account_id
        base_projection = self.service.repository.get_revision_projection(
            job.artifact_id,
            job.base_revision_id,
            account_id=account_id,
        )
        base = self._asset(base_projection, account_id=account_id)
        selected: list[RetouchImageAsset] = []
        for artifact_id in job.request.selected_artifact_ids:
            revision_id = job.input_revision_ids.get(artifact_id)
            if not revision_id:
                raise RuntimeError("retouch selected revision snapshot is incomplete")
            projection = self.service.repository.get_revision_projection(
                artifact_id,
                revision_id,
                account_id=account_id,
            )
            selected.append(self._asset(projection, account_id=account_id))
        references: list[RetouchImageAsset] = []
        for artifact_id in job.request.reference_artifact_ids:
            revision_id = job.input_revision_ids.get(artifact_id)
            if not revision_id:
                raise RuntimeError("retouch reference revision snapshot is incomplete")
            references.append(
                self._asset(
                    self.service.repository.get_revision_projection(
                        artifact_id,
                        revision_id,
                        account_id=account_id,
                    ),
                    account_id=account_id,
                )
            )
        mask_asset: RetouchMaskAsset | None = None
        if job.request.mask is not None:
            mask_metadata = dict(job.request.mask)
            mask_content = self.service.blobs.read_bytes(mask_metadata["sha256"])
            if len(mask_content) != int(mask_metadata["size_bytes"]):
                raise RuntimeError("retouch mask size commitment changed")
            mask_asset = RetouchMaskAsset(
                sha256=str(mask_metadata["sha256"]),
                width_px=int(mask_metadata["width_px"]),
                height_px=int(mask_metadata["height_px"]),
                covered_fraction=float(mask_metadata["covered_fraction"]),
                pixel_regions=tuple(mask_metadata["pixel_regions"]),
                content=mask_content,
            )
        return StructuredRetouchAdapterRequest(
            job_id=job.job_id,
            idempotency_key=job.external_idempotency_key,
            model_id=self.bridge.kernel.get_turn(job.execution_turn_id).image_model_id,
            base=base,
            selected=tuple(selected),
            references=tuple(references),
            annotations=job.request.annotations,
            global_instruction=job.request.global_instruction,
            edit_surface=job.request.edit_surface,
            mask=mask_asset,
        )

    def _asset(
        self, projection: ArtifactProjection, *, account_id: str
    ) -> RetouchImageAsset:
        content = self.service.read_user_content(
            projection.artifact_id,
            projection.revision_id,
            account_id=account_id,
        )
        return RetouchImageAsset(
            artifact_id=projection.artifact_id,
            revision_id=projection.revision_id,
            mime_type=projection.mime_type,
            sha256=projection.sha256,
            content=content,
        )

    def cancel(self, retouch_job_id: str, *, reason: str = "user_cancelled") -> RetouchJobProjection:
        return self.service.fail_retouch(
            retouch_job_id,
            _safe_reason(reason, fallback="user_cancelled"),
            cancelled=True,
            on_terminal=self.bridge.terminal_hook(
                target=JobStatus.CANCELLED,
                reason=reason,
            ),
        )

    def reconcile(self) -> int:
        reconciled = 0
        for job in self.service.repository.list_active_retouch_jobs():
            if not job.durable_job_id:
                continue
            try:
                durable = self.bridge.kernel.jobs.get(job.durable_job_id)
            except Exception:
                try:
                    before_commit = self._reconcile_before_commit(job.job_id)
                    self.service.fail_retouch(
                        job.job_id,
                        "durable_job_missing",
                        on_terminal=self.bridge.missing_runtime_hook(
                            reason="durable_job_missing"
                        ),
                        before_commit=before_commit,
                    )
                except LeaseError:
                    break
                reconciled += 1
                continue
            if durable.status not in TERMINAL_JOB_STATUSES:
                continue
            cancelled = durable.status is JobStatus.CANCELLED
            reason = durable.last_error or (
                "runtime_job_cancelled" if cancelled else "runtime_job_terminal"
            )
            try:
                before_commit = self._reconcile_before_commit(job.job_id)
                self.service.fail_retouch(
                    job.job_id,
                    reason,
                    cancelled=cancelled,
                    on_terminal=self.bridge.reconcile_hook(reason=reason),
                    before_commit=before_commit,
                )
            except LeaseError:
                break
            reconciled += 1
        return reconciled

    def _reconcile_before_commit(
        self, subject: str
    ) -> Callable[[], None] | None:
        """Capture a short control permit and recheck it at transaction commit."""

        jobs = self.bridge.kernel.jobs
        with jobs.control_admission(
            scope="retouch_reconcile",
            subject=subject,
        ) as permit:
            pass
        if permit is None:
            return None
        gate = jobs.execution_gate
        if gate is None:
            raise LeaseError("runtime execution epoch is closed")

        def validate() -> None:
            try:
                gate.assert_permit(permit)
            except RuntimeExecutionDenied as error:
                raise LeaseError("runtime execution epoch is closed") from error

        return validate


class RetouchWorker:
    def __init__(
        self,
        coordinator: RetouchCoordinator,
        adapter: CloudImageRetouchAdapter,
        *,
        lease_seconds: int = 30,
        retry_delay_seconds: int = 2,
    ) -> None:
        if not 5 <= lease_seconds <= 300:
            raise ValueError("retouch worker lease must be between 5 and 300 seconds")
        if not 0 <= retry_delay_seconds <= 300:
            raise ValueError("retouch retry delay is invalid")
        self.coordinator = coordinator
        self.adapter = adapter
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds

    async def run_once(self, worker_id: str) -> RetouchWorkerResult:
        self.coordinator.reconcile()
        jobs = self.coordinator.bridge.kernel.jobs
        durable = jobs.lease_next(
            worker_id,
            lease_seconds=self.lease_seconds,
            kinds=[RETOUCH_JOB_KIND],
        )
        if durable is None:
            self.coordinator.reconcile()
            return RetouchWorkerResult(RetouchWorkerOutcome.IDLE)
        if durable.lease_token is None:
            raise RuntimeError("leased retouch job has no fencing token")
        retouch_job_id = str(durable.payload.get("retouch_job_id") or "")
        if not retouch_job_id:
            self.coordinator.bridge.kernel.jobs.cancel(
                durable.job_id, reason="invalid_retouch_payload"
            )
            return RetouchWorkerResult(
                RetouchWorkerOutcome.FAILED,
                durable_job_id=durable.job_id,
                reason="invalid_retouch_payload",
            )
        lease_token = durable.lease_token
        execution_permit: RuntimeExecutionPermit | None = None
        try:
            execution_permit = jobs.capture_execution_permit(
                durable.job_id,
                lease_token,
            )
            self.coordinator.service.mark_retouch_running(
                retouch_job_id,
                on_running=self.coordinator.bridge.running_hook(
                    worker_id=worker_id,
                    lease_token=lease_token,
                ),
                before_commit=self._execution_before_commit(
                    durable.job_id,
                    lease_token,
                    execution_permit,
                ),
            )
            internal = self.coordinator.service.get_internal_retouch_job(
                retouch_job_id
            )
            if internal.staged_result is None:
                request = self.coordinator.adapter_request(retouch_job_id)
                self._heartbeat(
                    durable.job_id,
                    worker_id,
                    lease_token,
                    phase="external_requested",
                    external_idempotency_key=request.idempotency_key,
                )
                heartbeat = asyncio.create_task(
                    self._heartbeat_loop(
                        durable.job_id,
                        worker_id,
                        lease_token,
                        request.idempotency_key,
                    )
                )
                try:
                    recovery = durable.attempt > 1 or bool(durable.checkpoint)
                    result, execution_permit = await self._invoke_with_permit(
                        durable_job_id=durable.job_id,
                        lease_token=lease_token,
                        request=request,
                        recovery_only=recovery,
                    )
                    if result is None:
                        result, execution_permit = await self._invoke_with_permit(
                            durable_job_id=durable.job_id,
                            lease_token=lease_token,
                            request=request,
                            recovery_only=False,
                        )
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                assert isinstance(result, StructuredRetouchAdapterResult)
                self.coordinator.service.stage_retouch_result(
                    retouch_job_id,
                    result.content,
                    mime_type=result.mime_type,
                    requested_name=result.requested_name,
                    change_summary=result.change_summary,
                    inspection_regions=result.inspection_regions,
                    quality_evidence=result.quality_evidence,
                    adapter_result_id=result.result_id,
                    before_commit=self._execution_before_commit(
                        durable.job_id,
                        lease_token,
                        execution_permit,
                    ),
                )
                self._heartbeat(
                    durable.job_id,
                    worker_id,
                    lease_token,
                    phase="result_staged",
                    external_idempotency_key=request.idempotency_key,
                )
            execution_permit = jobs.capture_execution_permit(
                durable.job_id,
                lease_token,
            )
            completed = self.coordinator.service.complete_staged_retouch(
                retouch_job_id,
                on_completed=self.coordinator.bridge.completed_hook(
                    worker_id=worker_id,
                    lease_token=lease_token,
                ),
                before_commit=self._execution_before_commit(
                    durable.job_id,
                    lease_token,
                    execution_permit,
                ),
            )
            self.coordinator.service.ensure_image_renditions(
                completed.artifact.artifact_id,
                revision_id=completed.artifact.revision_id,
            )
            jobs.retire_execution_permit(durable.job_id, lease_token)
            return RetouchWorkerResult(
                RetouchWorkerOutcome.COMPLETED,
                retouch_job_id=retouch_job_id,
                durable_job_id=durable.job_id,
            )
        except asyncio.CancelledError:
            raise
        except LeaseError:
            return RetouchWorkerResult(
                RetouchWorkerOutcome.FAILED,
                retouch_job_id=retouch_job_id,
                durable_job_id=durable.job_id,
                reason="execution_epoch_closed",
            )
        except Exception as error:
            return self._handle_failure(
                retouch_job_id=retouch_job_id,
                durable_job_id=durable.job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                attempt=durable.attempt,
                max_attempts=durable.max_attempts,
                error=error,
                execution_permit=execution_permit,
            )

    async def _invoke_with_permit(
        self,
        *,
        durable_job_id: str,
        lease_token: str,
        request: StructuredRetouchAdapterRequest,
        recovery_only: bool,
    ) -> tuple[StructuredRetouchAdapterResult | None, RuntimeExecutionPermit | None]:
        """Fence one async adapter attempt without retaining a threading lock."""

        jobs = self.coordinator.bridge.kernel.jobs
        permit = jobs.capture_execution_permit(durable_job_id, lease_token)
        try:
            result = await invoke_adapter(
                self.adapter,
                request,
                recovery_only=recovery_only,
            )
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
        jobs = self.coordinator.bridge.kernel.jobs

        def validate() -> None:
            jobs.assert_execution_permit(
                durable_job_id,
                lease_token,
                permit,
            )

        return validate

    def _handle_failure(
        self,
        *,
        retouch_job_id: str,
        durable_job_id: str,
        worker_id: str,
        lease_token: str,
        attempt: int,
        max_attempts: int,
        error: Exception,
        execution_permit: RuntimeExecutionPermit | None,
    ) -> RetouchWorkerResult:
        reason = (
            error.code
            if isinstance(error, RetouchAdapterError)
            else error.__class__.__name__.casefold()
        )
        retryable = (
            error.retryable
            if isinstance(error, RetouchAdapterError)
            else isinstance(error, (ConnectionError, TimeoutError, OSError))
        )
        if retryable and attempt < max_attempts:
            try:
                self.coordinator.service.repository.requeue_retouch_job(
                    retouch_job_id,
                    now=self.coordinator.bridge.clock(),
                    on_requeued=self.coordinator.bridge.requeued_hook(
                        worker_id=worker_id,
                        lease_token=lease_token,
                        reason=reason,
                        delay_seconds=self.retry_delay_seconds,
                    ),
                    before_commit=self._execution_before_commit(
                        durable_job_id,
                        lease_token,
                        execution_permit,
                    ),
                )
            except LeaseError:
                return RetouchWorkerResult(
                    RetouchWorkerOutcome.FAILED,
                    retouch_job_id=retouch_job_id,
                    durable_job_id=durable_job_id,
                    reason="execution_epoch_closed",
                )
            self.coordinator.bridge.kernel.jobs.retire_execution_permit(
                durable_job_id,
                lease_token,
            )
            return RetouchWorkerResult(
                RetouchWorkerOutcome.RETRY_SCHEDULED,
                retouch_job_id=retouch_job_id,
                durable_job_id=durable_job_id,
                reason=reason,
            )
        target = JobStatus.DEAD_LETTER if retryable else JobStatus.FAILED
        try:
            self.coordinator.service.fail_retouch(
                retouch_job_id,
                _safe_reason(reason, fallback="retouch_failed"),
                on_terminal=self.coordinator.bridge.terminal_hook(
                    target=target,
                    reason=reason,
                    worker_id=worker_id,
                    lease_token=lease_token,
                ),
                before_commit=self._execution_before_commit(
                    durable_job_id,
                    lease_token,
                    execution_permit,
                ),
            )
            self.coordinator.bridge.kernel.jobs.retire_execution_permit(
                durable_job_id,
                lease_token,
            )
        except LeaseError:
            return RetouchWorkerResult(
                RetouchWorkerOutcome.FAILED,
                retouch_job_id=retouch_job_id,
                durable_job_id=durable_job_id,
                reason="execution_epoch_closed",
            )
        except Exception:
            # A concurrent cancellation or expired fencing token is reconciled
            # from the durable Runtime terminal state on the next worker pass.
            self.coordinator.reconcile()
        return RetouchWorkerResult(
            RetouchWorkerOutcome.FAILED,
            retouch_job_id=retouch_job_id,
            durable_job_id=durable_job_id,
            reason=reason,
        )

    def _heartbeat(
        self,
        durable_job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        phase: str,
        external_idempotency_key: str,
    ) -> None:
        self.coordinator.bridge.kernel.jobs.heartbeat(
            durable_job_id,
            worker_id,
            lease_token,
            lease_seconds=self.lease_seconds,
            checkpoint={
                "schema_version": 1,
                "phase": phase,
                "external_idempotency_key": external_idempotency_key,
            },
        )

    async def _heartbeat_loop(
        self,
        durable_job_id: str,
        worker_id: str,
        lease_token: str,
        external_idempotency_key: str,
    ) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await asyncio.to_thread(
                self._heartbeat,
                durable_job_id,
                worker_id,
                lease_token,
                phase="external_running",
                external_idempotency_key=external_idempotency_key,
            )


class RetouchWorkerSupervisor:
    def __init__(
        self,
        worker: RetouchWorker,
        *,
        concurrency: int = 1,
        idle_poll_seconds: float = 0.25,
        shutdown_timeout_seconds: float = 5.0,
        close_adapter_on_stop: bool = True,
    ) -> None:
        if not 1 <= concurrency <= 4:
            raise ValueError("retouch concurrency must be between one and four")
        if not 0.01 <= idle_poll_seconds <= 60:
            raise ValueError("retouch poll interval is invalid")
        if not 0.1 <= shutdown_timeout_seconds <= 120:
            raise ValueError("retouch shutdown timeout is invalid")
        self.worker = worker
        self.concurrency = concurrency
        self.idle_poll_seconds = idle_poll_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.close_adapter_on_stop = close_adapter_on_stop
        self._tasks: list[asyncio.Task[None]] = []
        self._wake = asyncio.Event()
        self._stopping = False
        self._closed = False
        self._completed_runs = 0
        self._retry_runs = 0
        self._failed_runs = 0
        self._last_outcome: RetouchWorkerOutcome | None = None
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self._tasks) and any(not task.done() for task in self._tasks)

    def snapshot(self) -> RetouchSupervisorSnapshot:
        return RetouchSupervisorSnapshot(
            running=self.running,
            concurrency=self.concurrency,
            completed_runs=self._completed_runs,
            retry_runs=self._retry_runs,
            failed_runs=self._failed_runs,
            last_outcome=self._last_outcome,
            last_error=self._last_error,
        )

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("retouch supervisor has already been closed")
        if self.running:
            return
        self._stopping = False
        self._tasks = [
            asyncio.create_task(
                self._worker_loop(index), name=f"ecorex-retouch-worker-{index}"
            )
            for index in range(self.concurrency)
        ]

    def notify(self) -> None:
        self._wake.set()

    async def stop(self) -> None:
        if self._closed:
            return
        self._stopping = True
        self._wake.set()
        tasks = list(self._tasks)
        if tasks:
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
        if self.close_adapter_on_stop:
            close = getattr(self.worker.adapter, "aclose", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
        self._closed = True

    async def _worker_loop(self, index: int) -> None:
        worker_id = f"retouch-{id(self):x}-{index}"
        while not self._stopping:
            try:
                result = await self.worker.run_once(worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._failed_runs += 1
                self._last_error = error.__class__.__name__.casefold()
                await self._wait()
                continue
            self._last_outcome = result.outcome
            self._last_error = result.reason
            if result.outcome is RetouchWorkerOutcome.COMPLETED:
                self._completed_runs += 1
            elif result.outcome is RetouchWorkerOutcome.RETRY_SCHEDULED:
                self._retry_runs += 1
            elif result.outcome is RetouchWorkerOutcome.FAILED:
                self._failed_runs += 1
            if result.outcome is RetouchWorkerOutcome.IDLE:
                await self._wait()

    async def _wait(self) -> None:
        if self._stopping:
            return
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.idle_poll_seconds)
        except TimeoutError:
            pass


__all__ = [
    "RETOUCH_JOB_KIND",
    "RetouchCoordinator",
    "RetouchSnapshotContextProvider",
    "RetouchSupervisorSnapshot",
    "RetouchTurnAdmission",
    "RetouchWorker",
    "RetouchWorkerOutcome",
    "RetouchWorkerResult",
    "RetouchWorkerSupervisor",
    "RuntimeRetouchBridge",
]
