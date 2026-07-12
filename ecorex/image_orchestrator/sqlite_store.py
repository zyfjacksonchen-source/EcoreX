"""SQLite WAL reference store with lease fencing and fault-injection hooks."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from typing import Any, Iterator
import uuid

from .models import (
    ACTIVE_STATUSES,
    SCHEDULABLE_STATUSES,
    TERMINAL_STATUSES,
    ImageBackpressure,
    ImageIdempotencyConflict,
    ImageInputNotFound,
    ImageInputReceipt,
    ImageInvalidTransition,
    ImageJob,
    ImageJobNotFound,
    ImageJobStatus,
    ImageLeaseLost,
    ImageLimits,
    ImageMetrics,
    ImageOperation,
    ImageResult,
    ImageSubmitRequest,
    ImageUsage,
    canonical_json,
    default_deadline,
    require_utc,
    utc_now,
)
from .sqlite_schema import SQLiteImageSchemaManager, SQLiteImageSchemaReceipt
from .store import ProviderCircuitDecision


Clock = Callable[[], datetime]
FaultHook = Callable[[str, str], None]
_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{2,255}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SAFE_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$")
_ACTIVE_VALUES = tuple(status.value for status in ACTIVE_STATUSES)
_SCHEDULABLE_VALUES = tuple(status.value for status in SCHEDULABLE_STATUSES)
_TERMINAL_VALUES = tuple(status.value for status in TERMINAL_STATUSES)
_FORBIDDEN_EVENT_KEY = re.compile(
    r"(?:^|_)(?:binary|bytes|content|path|token|secret|password|private|api_key)(?:$|_)",
    re.IGNORECASE,
)


def _time(value: datetime) -> str:
    return require_utc(value, "timestamp").isoformat(timespec="microseconds")


def _read_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise RuntimeError("stored image timestamp is naive")
    return parsed.astimezone(UTC)


def _request_payload(request: ImageSubmitRequest) -> dict[str, Any]:
    return {
        **request.provider_payload(),
        "client_request_id": request.client_request_id,
        "priority": request.priority,
        "max_attempts": request.max_attempts,
        "deadline_seconds": request.deadline_seconds,
    }


def _request_from_json(value: str) -> ImageSubmitRequest:
    raw = json.loads(value)
    return ImageSubmitRequest(
        operation=raw["operation"],
        model_id=raw["model_id"],
        client_request_id=raw["client_request_id"],
        prompt=raw["prompt"],
        width=raw["width"],
        height=raw["height"],
        count=raw["count"],
        input_sha256=tuple(raw["input_sha256"]),
        instruction=raw["instruction"],
        priority=raw["priority"],
        max_attempts=raw["max_attempts"],
        deadline_seconds=raw["deadline_seconds"],
        metadata=raw["metadata"],
    )


def _checkpoint(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(value or {})
    if len(result) > 32:
        raise ValueError("checkpoint is too large")
    encoded = canonical_json(result).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("checkpoint is too large")
    for key in result:
        if not isinstance(key, str) or _FORBIDDEN_EVENT_KEY.search(key):
            raise ValueError("checkpoint contains a forbidden field")
    _safe_event(result)
    return result


def _safe_event(payload: Mapping[str, Any]) -> str:
    def inspect_value(value: Any, key: str = "") -> None:
        if key and key != "size_bytes" and _FORBIDDEN_EVENT_KEY.search(key):
            raise ValueError("event payload contains a forbidden field")
        if isinstance(value, Mapping):
            for nested_key, nested in value.items():
                if not isinstance(nested_key, str):
                    raise ValueError("event payload key is invalid")
                inspect_value(nested, nested_key)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                inspect_value(nested)
        elif isinstance(value, bytes):
            raise ValueError("event payload cannot contain binary data")
        elif isinstance(value, str):
            if len(value) > 1024 or "\x00" in value:
                raise ValueError("event string is invalid")
            if re.match(r"^(?:[A-Za-z]:[\\/]|/|\\\\)", value):
                raise ValueError("event payload cannot contain filesystem paths")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ValueError("event payload type is invalid")

    inspect_value(payload)
    encoded = canonical_json(dict(payload))
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise ValueError("event payload is too large")
    return encoded


class SQLiteImageJobStore:
    """Portable correctness reference; PostgreSQL is the cloud scale backend."""

    deployment_scope = "local"

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        limits: ImageLimits = ImageLimits(),
        model_weights: Mapping[str, float] | None = None,
        clock: Clock = utc_now,
        fault_hook: FaultHook | None = None,
    ) -> None:
        schema = SQLiteImageSchemaManager(database_path)
        self.path = schema.path
        self.schema_receipt: SQLiteImageSchemaReceipt = schema.validate()
        self.limits = limits
        self.model_weights = dict(model_weights or {})
        self.clock = clock
        self.fault_hook = fault_hook or (lambda _phase, _job_id: None)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=rw",
            uri=True,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        for attempt in range(8):
            try:
                connection.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).casefold() or attempt == 7:
                    connection.close()
                    raise
                time.sleep(0.002 * (2**attempt))
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _now(self) -> datetime:
        return require_utc(self.clock(), "image store clock")

    @staticmethod
    def _validate_account(account_id: str) -> None:
        if not isinstance(account_id, str) or not _ACCOUNT.fullmatch(account_id):
            raise ValueError("account_id is invalid")

    def register_input(
        self,
        account_id: str,
        receipt: ImageInputReceipt,
    ) -> ImageInputReceipt:
        self._validate_account(account_id)
        if not isinstance(receipt, ImageInputReceipt):
            raise TypeError("receipt must be an ImageInputReceipt")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT sha256,size_bytes,mime_type FROM image_inputs "
                "WHERE account_id=? AND sha256=?",
                (account_id, receipt.sha256),
            ).fetchone()
            if existing is not None:
                stored = ImageInputReceipt(
                    existing["sha256"],
                    int(existing["size_bytes"]),
                    existing["mime_type"],
                )
                if stored != receipt:
                    raise ImageIdempotencyConflict(
                        "image input digest was registered with different metadata"
                    )
                return stored
            connection.execute(
                "INSERT INTO image_inputs(account_id,sha256,size_bytes,mime_type,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    account_id,
                    receipt.sha256,
                    receipt.size_bytes,
                    receipt.mime_type,
                    _time(self._now()),
                ),
            )
            return receipt

    def get_input(self, account_id: str, sha256: str) -> ImageInputReceipt:
        self._validate_account(account_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT sha256,size_bytes,mime_type FROM image_inputs "
                "WHERE account_id=? AND sha256=?",
                (account_id, sha256),
            ).fetchone()
        if row is None:
            raise ImageInputNotFound("image input was not found")
        return ImageInputReceipt(
            row["sha256"], int(row["size_bytes"]), row["mime_type"]
        )

    def submit(self, account_id: str, request: ImageSubmitRequest) -> tuple[ImageJob, bool]:
        self._validate_account(account_id)
        if not isinstance(request, ImageSubmitRequest):
            raise TypeError("request must be an ImageSubmitRequest")
        fingerprint = request.fingerprint()
        now = self._now()
        weight = request.scheduling_weight(self.model_weights)
        with self._transaction() as connection:
            # Deadline is authoritative even while no worker is running.  A
            # schedulable row which has expired must not remain invisible to
            # leasing while permanently consuming durable queue capacity.
            self._expire_schedulable_in(connection, now, account_id=None)
            duplicate = connection.execute(
                "SELECT * FROM image_jobs WHERE account_id=? AND client_request_id=?",
                (account_id, request.client_request_id),
            ).fetchone()
            if duplicate is not None:
                if duplicate["request_fingerprint"] != fingerprint:
                    raise ImageIdempotencyConflict(
                        "client_request_id was reused with a different image request"
                    )
                return self._from_row(connection, duplicate), False
            self._assert_capacity(connection, account_id, weight)
            global_floor_row = connection.execute(
                "SELECT COALESCE(MIN(fair_finish),0) AS value FROM image_jobs "
                "WHERE status IN ('queued','retry_wait','leased','running','verifying','committing')"
            ).fetchone()
            account_row = connection.execute(
                "SELECT last_finish FROM image_scheduler_accounts WHERE account_id=?",
                (account_id,),
            ).fetchone()
            global_floor = float(global_floor_row["value"])
            account_finish = float(account_row["last_finish"]) if account_row else global_floor
            fair_finish = max(global_floor, account_finish) + float(weight)
            connection.execute(
                "INSERT INTO image_scheduler_accounts(account_id,last_finish) VALUES(?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET last_finish=excluded.last_finish",
                (account_id, fair_finish),
            )
            job_id = "imgjob_" + uuid.uuid4().hex
            provider_key = "imgprov_" + hashlib.sha256(
                b"ecorex-image-provider-v1\0" + job_id.encode("ascii")
            ).hexdigest()
            deadline = default_deadline(request, now)
            timestamp = _time(now)
            connection.execute(
                """
                INSERT INTO image_jobs(
                    job_id,account_id,operation,model_id,size_class,weight,priority,
                    client_request_id,request_fingerprint,request_json,status,attempt,
                    max_attempts,fair_finish,available_at,deadline,provider_idempotency_key,
                    checkpoint_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,'accepted',0,?,?,?,?,?,'{}',?,?)
                """,
                (
                    job_id,
                    account_id,
                    request.operation.value,
                    request.model_id,
                    request.size_class,
                    weight,
                    request.priority,
                    request.client_request_id,
                    fingerprint,
                    canonical_json(_request_payload(request)),
                    request.max_attempts,
                    fair_finish,
                    timestamp,
                    _time(deadline),
                    provider_key,
                    timestamp,
                    timestamp,
                ),
            )
            self._event(
                connection,
                job_id,
                account_id,
                "image.accepted",
                {
                    "operation": request.operation.value,
                    "model_id": request.model_id,
                    "size_class": request.size_class,
                    "weight": weight,
                },
                now,
            )
            self.fault_hook("accepted", job_id)
            connection.execute(
                "UPDATE image_jobs SET status='queued',updated_at=? WHERE job_id=?",
                (timestamp, job_id),
            )
            self._event(connection, job_id, account_id, "image.queued", {"attempt": 0}, now)
            row = connection.execute("SELECT * FROM image_jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._from_row(connection, row), True

    def _assert_capacity(self, connection: sqlite3.Connection, account_id: str, weight: int) -> None:
        placeholders = ",".join("?" for _ in (*_SCHEDULABLE_VALUES, *_ACTIVE_VALUES))
        states = (*_SCHEDULABLE_VALUES, *_ACTIVE_VALUES)
        global_row = connection.execute(
            f"SELECT COUNT(*) AS count,COALESCE(SUM(weight),0) AS weight FROM image_jobs "
            f"WHERE status IN ({placeholders})",
            states,
        ).fetchone()
        account_row = connection.execute(
            f"SELECT COUNT(*) AS count,COALESCE(SUM(weight),0) AS weight FROM image_jobs "
            f"WHERE account_id=? AND status IN ({placeholders})",
            (account_id, *states),
        ).fetchone()
        if (
            int(global_row["count"]) >= self.limits.max_queued_jobs
            or int(global_row["weight"]) + weight > self.limits.max_queued_weight
            or int(account_row["count"]) >= self.limits.max_account_queued_jobs
            or int(account_row["weight"]) + weight > self.limits.max_account_queued_weight
        ):
            raise ImageBackpressure("image queue capacity is exhausted")

    def get(self, job_id: str, *, account_id: str | None = None) -> ImageJob:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None or (account_id is not None and row["account_id"] != account_id):
                raise ImageJobNotFound("image job was not found")
            return self._from_row(connection, row)

    def lease_next(self, worker_id: str, *, lease_seconds: int = 30) -> ImageJob | None:
        if not _ACCOUNT.fullmatch(worker_id) or not 5 <= lease_seconds <= 300:
            raise ValueError("worker identity or lease duration is invalid")
        now = self._now()
        with self._transaction() as connection:
            self._expire_schedulable_in(connection, now, account_id=None)
            self._reclaim_in(connection, now, account_id=None)
            active = ",".join("?" for _ in _ACTIVE_VALUES)
            row = connection.execute(
                f"""
                SELECT candidate.* FROM image_jobs AS candidate
                WHERE candidate.status IN ('queued','retry_wait')
                  AND candidate.available_at<=? AND candidate.deadline>?
                  AND (SELECT COUNT(*) FROM image_jobs WHERE status IN ({active})) < ?
                  AND (SELECT COUNT(*) FROM image_jobs WHERE account_id=candidate.account_id
                       AND status IN ({active})) < ?
                  AND (SELECT COUNT(*) FROM image_jobs WHERE model_id=candidate.model_id
                       AND status IN ({active})) < ?
                  AND (SELECT COUNT(*) FROM image_jobs WHERE operation=candidate.operation
                       AND status IN ({active})) < ?
                ORDER BY candidate.fair_finish ASC,candidate.priority DESC,
                         candidate.created_at ASC,candidate.job_id ASC
                LIMIT 1
                """,
                (
                    _time(now),
                    _time(now),
                    *_ACTIVE_VALUES,
                    self.limits.max_running_jobs,
                    *_ACTIVE_VALUES,
                    self.limits.max_account_running,
                    *_ACTIVE_VALUES,
                    self.limits.max_model_running,
                    *_ACTIVE_VALUES,
                    self.limits.max_operation_running,
                ),
            ).fetchone()
            if row is None:
                return None
            generation = int(row["lease_generation"]) + 1
            token = f"{generation}:" + secrets.token_urlsafe(32)
            expiry = min(now + timedelta(seconds=lease_seconds), _read_time(row["deadline"]))
            updated = connection.execute(
                "UPDATE image_jobs SET status='leased',attempt=attempt+1,lease_owner=?,"
                "lease_token=?,lease_generation=?,lease_expires_at=?,heartbeat_at=?,updated_at=? "
                "WHERE job_id=? AND status IN ('queued','retry_wait')",
                (worker_id, token, generation, _time(expiry), _time(now), _time(now), row["job_id"]),
            )
            if updated.rowcount != 1:
                return None
            self._event(
                connection,
                row["job_id"],
                row["account_id"],
                "image.leased",
                {"attempt": int(row["attempt"]) + 1, "lease_generation": generation},
                now,
            )
            leased = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
            return self._from_row(connection, leased)

    def _owned(self, connection: sqlite3.Connection, job_id: str, token: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM image_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ImageJobNotFound("image job was not found")
        if row["lease_token"] is None or not secrets.compare_digest(row["lease_token"], token):
            raise ImageLeaseLost("image job fencing token is stale")
        if ImageJobStatus(row["status"]) not in ACTIVE_STATUSES:
            raise ImageLeaseLost("image job is no longer owned by this lease")
        now = self._now()
        if _read_time(row["lease_expires_at"]) <= now or _read_time(row["deadline"]) <= now:
            raise ImageLeaseLost("image job lease or deadline expired")
        return row

    def heartbeat(self, job_id: str, lease_token: str, *, lease_seconds: int = 30) -> ImageJob:
        if not 5 <= lease_seconds <= 300:
            raise ValueError("lease duration is invalid")
        now = self._now()
        with self._transaction() as connection:
            row = self._owned(connection, job_id, lease_token)
            expiry = min(now + timedelta(seconds=lease_seconds), _read_time(row["deadline"]))
            connection.execute(
                "UPDATE image_jobs SET lease_expires_at=?,heartbeat_at=?,updated_at=? WHERE job_id=?",
                (_time(expiry), _time(now), _time(now), job_id),
            )
            self._event(
                connection,
                job_id,
                row["account_id"],
                "image.heartbeat",
                {"lease_generation": row["lease_generation"]},
                now,
            )
            return self._from_row(
                connection,
                connection.execute("SELECT * FROM image_jobs WHERE job_id=?", (job_id,)).fetchone(),
            )

    def transition(
        self,
        job_id: str,
        lease_token: str,
        *,
        expected: tuple[str, ...],
        target: str,
        checkpoint: Mapping[str, Any] | None = None,
        provider_request_id: str | None = None,
    ) -> ImageJob:
        expected_statuses = tuple(ImageJobStatus(value) for value in expected)
        target_status = ImageJobStatus(target)
        allowed = {
            ImageJobStatus.LEASED: {ImageJobStatus.RUNNING},
            ImageJobStatus.RUNNING: {ImageJobStatus.VERIFYING},
            ImageJobStatus.VERIFYING: {ImageJobStatus.COMMITTING},
        }
        if any(target_status not in allowed.get(status, set()) for status in expected_statuses):
            raise ImageInvalidTransition("image job transition is invalid")
        checkpoint_value = _checkpoint(checkpoint)
        if provider_request_id is not None and (
            not isinstance(provider_request_id, str)
            or not _SAFE_PROVIDER_ID.fullmatch(provider_request_id)
        ):
            raise ValueError("provider request identity is invalid")
        now = self._now()
        with self._transaction() as connection:
            row = self._owned(connection, job_id, lease_token)
            if ImageJobStatus(row["status"]) not in expected_statuses:
                raise ImageInvalidTransition("image job is in an unexpected state")
            provider_id = provider_request_id or row["provider_request_id"]
            connection.execute(
                "UPDATE image_jobs SET status=?,checkpoint_json=?,provider_request_id=?,updated_at=? "
                "WHERE job_id=?",
                (target_status.value, canonical_json(checkpoint_value), provider_id, _time(now), job_id),
            )
            self._event(
                connection,
                job_id,
                row["account_id"],
                f"image.{target_status.value}",
                {"attempt": row["attempt"], "lease_generation": row["lease_generation"]},
                now,
            )
            self.fault_hook(target_status.value, job_id)
            return self._from_row(
                connection,
                connection.execute("SELECT * FROM image_jobs WHERE job_id=?", (job_id,)).fetchone(),
            )

    def schedule_retry(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_code: str,
        available_at: datetime,
        checkpoint: Mapping[str, Any],
    ) -> ImageJob:
        self._validate_code(error_code)
        available_at = require_utc(available_at, "retry availability")
        checkpoint_value = _checkpoint(checkpoint)
        now = self._now()
        with self._transaction() as connection:
            row = self._owned(connection, job_id, lease_token)
            exhausted = int(row["attempt"]) >= int(row["max_attempts"]) or _read_time(row["deadline"]) <= available_at
            target = ImageJobStatus.DEAD_LETTER if exhausted else ImageJobStatus.RETRY_WAIT
            connection.execute(
                "UPDATE image_jobs SET status=?,available_at=?,checkpoint_json=?,last_error_code=?,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=? "
                "WHERE job_id=?",
                (target.value, _time(available_at), canonical_json(checkpoint_value), error_code, _time(now), job_id),
            )
            self._event(
                connection,
                job_id,
                row["account_id"],
                "image.dead_lettered" if exhausted else "image.retry_wait",
                {"attempt": row["attempt"], "error_code": error_code},
                now,
            )
            return self._from_row(connection, connection.execute("SELECT * FROM image_jobs WHERE job_id=?", (job_id,)).fetchone())

    def fail(self, job_id: str, lease_token: str, *, error_code: str) -> ImageJob:
        self._validate_code(error_code)
        now = self._now()
        with self._transaction() as connection:
            row = self._owned(connection, job_id, lease_token)
            connection.execute(
                "UPDATE image_jobs SET status='failed',last_error_code=?,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=? WHERE job_id=?",
                (error_code, _time(now), job_id),
            )
            self._event(connection, job_id, row["account_id"], "image.failed", {"error_code": error_code}, now)
            return self._from_row(connection, connection.execute("SELECT * FROM image_jobs WHERE job_id=?", (job_id,)).fetchone())

    def complete(
        self,
        job_id: str,
        lease_token: str,
        *,
        result: ImageResult,
        usage: ImageUsage,
    ) -> ImageJob:
        if not isinstance(result, ImageResult) or not isinstance(usage, ImageUsage):
            raise TypeError("result and usage contracts are required")
        now = self._now()
        with self._transaction() as connection:
            row = self._owned(connection, job_id, lease_token)
            if ImageJobStatus(row["status"]) is not ImageJobStatus.COMMITTING:
                raise ImageInvalidTransition("only a committing image job can complete")
            self.fault_hook("before_commit", job_id)
            connection.execute(
                "INSERT INTO image_results(job_id,sha256,size_bytes,mime_type,committed_at) VALUES(?,?,?,?,?)",
                (job_id, result.sha256, result.size_bytes, result.mime_type, _time(now)),
            )
            connection.execute(
                "INSERT INTO image_usage(job_id,usage_json,committed_at) VALUES(?,?,?)",
                (job_id, canonical_json(usage.to_dict()), _time(now)),
            )
            connection.execute(
                "UPDATE image_jobs SET status='completed',lease_owner=NULL,lease_token=NULL,"
                "lease_expires_at=NULL,heartbeat_at=NULL,checkpoint_json='{}',updated_at=? WHERE job_id=?",
                (_time(now), job_id),
            )
            self._event(
                connection,
                job_id,
                row["account_id"],
                "image.completed",
                {
                    "sha256": result.sha256,
                    "size_bytes": result.size_bytes,
                    "mime_type": result.mime_type,
                    "billed_units": usage.billed_units,
                },
                now,
            )
            self.fault_hook("committed", job_id)
            return self._from_row(connection, connection.execute("SELECT * FROM image_jobs WHERE job_id=?", (job_id,)).fetchone())

    def cancel(self, job_id: str, *, account_id: str) -> ImageJob:
        self._validate_account(account_id)
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id=? AND account_id=?", (job_id, account_id)
            ).fetchone()
            if row is None:
                raise ImageJobNotFound("image job was not found")
            if ImageJobStatus(row["status"]) in TERMINAL_STATUSES:
                return self._from_row(connection, row)
            connection.execute(
                "UPDATE image_jobs SET status='cancelled',cancellation_requested=1,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=? WHERE job_id=?",
                (_time(now), job_id),
            )
            self._event(connection, job_id, account_id, "image.cancelled", {}, now)
            return self._from_row(connection, connection.execute("SELECT * FROM image_jobs WHERE job_id=?", (job_id,)).fetchone())

    def reclaim_expired(self, *, account_id: str | None = None) -> int:
        if account_id is not None:
            self._validate_account(account_id)
        now = self._now()
        with self._transaction() as connection:
            return self._expire_schedulable_in(
                connection, now, account_id=account_id
            ) + self._reclaim_in(connection, now, account_id=account_id)

    def _expire_schedulable_in(
        self,
        connection: sqlite3.Connection,
        now: datetime,
        *,
        account_id: str | None,
    ) -> int:
        parameters: list[Any] = [_time(now)]
        account_clause = ""
        if account_id is not None:
            account_clause = " AND account_id=?"
            parameters.append(account_id)
        rows = connection.execute(
            "SELECT * FROM image_jobs WHERE status IN ('queued','retry_wait') "
            "AND deadline<=?" + account_clause,
            parameters,
        ).fetchall()
        expired = 0
        for row in rows:
            updated = connection.execute(
                "UPDATE image_jobs SET status='failed',last_error_code='deadline_exceeded',"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=? "
                "WHERE job_id=? AND status IN ('queued','retry_wait') AND deadline<=?",
                (_time(now), row["job_id"], _time(now)),
            )
            if updated.rowcount != 1:
                continue
            expired += 1
            self._event(
                connection,
                row["job_id"],
                row["account_id"],
                "image.failed",
                {
                    "attempt": row["attempt"],
                    "error_code": "deadline_exceeded",
                },
                now,
            )
        return expired

    def requeue_dead_letter(
        self,
        job_id: str,
        *,
        account_id: str,
        recovery_request_id: str,
    ) -> ImageJob:
        self._validate_account(account_id)
        if not isinstance(recovery_request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:@-]{7,255}", recovery_request_id
        ):
            raise ValueError("recovery_request_id is invalid")
        now = self._now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT job_id FROM image_recovery_requests WHERE account_id=? AND recovery_request_id=?",
                (account_id, recovery_request_id),
            ).fetchone()
            if existing is not None:
                if existing["job_id"] != job_id:
                    raise ImageIdempotencyConflict(
                        "recovery_request_id was reused for another image job"
                    )
                row = connection.execute("SELECT * FROM image_jobs WHERE job_id=?", (job_id,)).fetchone()
                if row is None or row["account_id"] != account_id:
                    raise ImageJobNotFound("image job was not found")
                return self._from_row(connection, row)
            row = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id=? AND account_id=?", (job_id, account_id)
            ).fetchone()
            if row is None:
                raise ImageJobNotFound("image job was not found")
            if ImageJobStatus(row["status"]) is not ImageJobStatus.DEAD_LETTER:
                raise ImageInvalidTransition("only a dead-letter image job can be requeued")
            connection.execute(
                "INSERT INTO image_recovery_requests(account_id,recovery_request_id,job_id,created_at) "
                "VALUES(?,?,?,?)",
                (account_id, recovery_request_id, job_id, _time(now)),
            )
            connection.execute(
                "UPDATE image_jobs SET status='queued',attempt=0,available_at=?,last_error_code=NULL,"
                "updated_at=? WHERE job_id=?",
                (_time(now), _time(now), job_id),
            )
            self._event(connection, job_id, account_id, "image.requeued", {"attempt": 0}, now)
            return self._from_row(connection, connection.execute("SELECT * FROM image_jobs WHERE job_id=?", (job_id,)).fetchone())

    def _reclaim_in(self, connection: sqlite3.Connection, now: datetime, *, account_id: str | None) -> int:
        parameters: list[Any] = [_time(now), _time(now)]
        account_clause = ""
        if account_id is not None:
            account_clause = " AND account_id=?"
            parameters.append(account_id)
        rows = connection.execute(
            "SELECT * FROM image_jobs WHERE status IN ('leased','running','verifying','committing') "
            "AND (lease_expires_at<=? OR deadline<=?)" + account_clause,
            parameters,
        ).fetchall()
        for row in rows:
            deadline_expired = _read_time(row["deadline"]) <= now
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
            exhausted = int(row["attempt"]) >= int(row["max_attempts"])
            if deadline_expired:
                target = ImageJobStatus.FAILED
                error = "deadline_exceeded"
            elif exhausted:
                target = ImageJobStatus.DEAD_LETTER
                error = "lease_exhausted"
            else:
                target = ImageJobStatus.RETRY_WAIT
                error = "lease_reclaimed"
                if checkpoint.get("provider_started"):
                    checkpoint["provider_uncertain"] = True
            connection.execute(
                "UPDATE image_jobs SET status=?,available_at=?,checkpoint_json=?,last_error_code=?,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=? "
                "WHERE job_id=?",
                (target.value, _time(now), canonical_json(checkpoint), error, _time(now), row["job_id"]),
            )
            self._event(
                connection,
                row["job_id"],
                row["account_id"],
                "image.reclaimed" if target is ImageJobStatus.RETRY_WAIT else f"image.{target.value}",
                {"attempt": row["attempt"], "error_code": error},
                now,
            )
        return len(rows)

    def metrics(self, *, account_id: str | None = None) -> ImageMetrics:
        if account_id is not None:
            self._validate_account(account_id)
        clause = " WHERE account_id=?" if account_id is not None else ""
        params = (account_id,) if account_id is not None else ()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count,COALESCE(SUM(weight),0) AS weight,MIN(created_at) AS oldest "
                "FROM image_jobs" + clause + " GROUP BY status",
                params,
            ).fetchall()
            counts = {row["status"]: int(row["count"]) for row in rows}
            queued_weight = sum(
                int(row["weight"])
                for row in rows
                if row["status"] in {"queued", "retry_wait"}
            )
            oldest_values = [
                _read_time(row["oldest"])
                for row in rows
                if row["status"] in {"queued", "retry_wait"} and row["oldest"]
            ]
            usage_clause = (
                " JOIN image_jobs j ON j.job_id=u.job_id WHERE j.account_id=?"
                if account_id is not None
                else ""
            )
            usage_rows = connection.execute(
                "SELECT u.usage_json FROM image_usage u" + usage_clause, params
            ).fetchall()
        now = self._now()
        oldest_seconds = max(
            [0.0, *((now - value).total_seconds() for value in oldest_values if value is not None)]
        )
        billed = sum(int(json.loads(row["usage_json"])["billed_units"]) for row in usage_rows)
        return ImageMetrics(
            queued=counts.get("queued", 0),
            active=sum(counts.get(status.value, 0) for status in ACTIVE_STATUSES),
            retry_wait=counts.get("retry_wait", 0),
            completed=counts.get("completed", 0),
            failed=counts.get("failed", 0),
            cancelled=counts.get("cancelled", 0),
            dead_letter=counts.get("dead_letter", 0),
            queued_weight=queued_weight,
            oldest_queued_seconds=oldest_seconds,
            usage_billed_units=billed,
        )

    def events(self, job_id: str) -> tuple[Mapping[str, Any], ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT seq,event_id,event_type,payload_json,created_at FROM image_events "
                "WHERE job_id=? ORDER BY seq", (job_id,)
            ).fetchall()
        return tuple(
            {
                "seq": row["seq"],
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        )

    def breaker_open_until(self, scope: str) -> datetime | None:
        self._validate_scope(scope)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT open_until FROM image_breakers WHERE scope=?", (scope,)
            ).fetchone()
        if row is None or row["open_until"] is None:
            return None
        value = _read_time(row["open_until"])
        return value if value > self._now() else None

    def admit_provider_call(
        self,
        scope: str,
        *,
        probe_seconds: int,
    ) -> ProviderCircuitDecision:
        """Admit a normal call or atomically lease the single half-open probe."""

        self._validate_scope(scope)
        if isinstance(probe_seconds, bool) or not 1 <= probe_seconds <= 3600:
            raise ValueError("provider circuit probe duration is invalid")
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT failure_count,open_until FROM image_breakers WHERE scope=?",
                (scope,),
            ).fetchone()
            if (
                row is None
                or int(row["failure_count"]) <= 0
                or row["open_until"] is None
            ):
                return ProviderCircuitDecision(admitted=True)
            open_until = _read_time(row["open_until"])
            assert open_until is not None
            if open_until > now:
                return ProviderCircuitDecision(
                    admitted=False,
                    retry_at=open_until,
                )
            # BEGIN IMMEDIATE serializes this compare-and-lease operation across
            # every local worker/process.  A crashed probe becomes eligible
            # again only after this bounded durable lease expires.
            probe_until = now + timedelta(seconds=probe_seconds)
            connection.execute(
                "UPDATE image_breakers SET open_until=?,updated_at=? WHERE scope=?",
                (_time(probe_until), _time(now), scope),
            )
            return ProviderCircuitDecision(admitted=True, half_open=True)

    def record_provider_failure(
        self,
        scope: str,
        *,
        threshold: int,
        cooldown_seconds: int,
    ) -> datetime | None:
        self._validate_scope(scope)
        if not 1 <= threshold <= 100 or not 1 <= cooldown_seconds <= 3600:
            raise ValueError("breaker configuration is invalid")
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT failure_count FROM image_breakers WHERE scope=?", (scope,)
            ).fetchone()
            failures = (int(row["failure_count"]) if row else 0) + 1
            open_until = now + timedelta(seconds=cooldown_seconds) if failures >= threshold else None
            connection.execute(
                "INSERT INTO image_breakers(scope,failure_count,open_until,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET failure_count=excluded.failure_count,"
                "open_until=excluded.open_until,updated_at=excluded.updated_at",
                (scope, failures, None if open_until is None else _time(open_until), _time(now)),
            )
            return open_until

    def record_provider_rate_limit(
        self,
        scope: str,
        *,
        retry_at: datetime,
        cooldown_seconds: int,
    ) -> datetime:
        """Open the whole provider scope for at least the advertised window."""

        self._validate_scope(scope)
        retry_at = require_utc(retry_at, "provider rate-limit retry")
        if (
            isinstance(cooldown_seconds, bool)
            or not 1 <= cooldown_seconds <= 3600
        ):
            raise ValueError("breaker cooldown is invalid")
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT failure_count,open_until FROM image_breakers WHERE scope=?",
                (scope,),
            ).fetchone()
            existing = (
                _read_time(row["open_until"])
                if row is not None and row["open_until"] is not None
                else None
            )
            open_until = max(
                now + timedelta(seconds=cooldown_seconds),
                retry_at,
                existing or now,
            )
            failures = (int(row["failure_count"]) if row else 0) + 1
            connection.execute(
                "INSERT INTO image_breakers(scope,failure_count,open_until,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET failure_count=excluded.failure_count,"
                "open_until=excluded.open_until,updated_at=excluded.updated_at",
                (scope, failures, _time(open_until), _time(now)),
            )
            return open_until

    def record_provider_success(self, scope: str) -> None:
        self._validate_scope(scope)
        now = self._now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO image_breakers(scope,failure_count,open_until,updated_at) VALUES(?,0,NULL,?) "
                "ON CONFLICT(scope) DO UPDATE SET failure_count=0,open_until=NULL,updated_at=excluded.updated_at",
                (scope, _time(now)),
            )

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if not isinstance(scope, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}", scope):
            raise ValueError("provider breaker scope is invalid")

    @staticmethod
    def _validate_code(code: str) -> None:
        if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code):
            raise ValueError("error code is invalid")

    def _event(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        account_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO image_events(event_id,job_id,account_id,event_type,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                "imgevt_" + uuid.uuid4().hex,
                job_id,
                account_id,
                event_type,
                _safe_event(payload),
                _time(now),
            ),
        )

    @staticmethod
    def _from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> ImageJob:
        result_row = connection.execute(
            "SELECT sha256,size_bytes,mime_type FROM image_results WHERE job_id=?", (row["job_id"],)
        ).fetchone()
        usage_row = connection.execute(
            "SELECT usage_json FROM image_usage WHERE job_id=?", (row["job_id"],)
        ).fetchone()
        result = (
            ImageResult(result_row["sha256"], result_row["size_bytes"], result_row["mime_type"])
            if result_row is not None
            else None
        )
        usage = ImageUsage(**json.loads(usage_row["usage_json"])) if usage_row is not None else None
        return ImageJob(
            job_id=row["job_id"],
            account_id=row["account_id"],
            request=_request_from_json(row["request_json"]),
            status=ImageJobStatus(row["status"]),
            weight=int(row["weight"]),
            attempt=int(row["attempt"]),
            fair_finish=float(row["fair_finish"]),
            available_at=_read_time(row["available_at"]),
            deadline=_read_time(row["deadline"]),
            created_at=_read_time(row["created_at"]),
            updated_at=_read_time(row["updated_at"]),
            provider_idempotency_key=row["provider_idempotency_key"],
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_generation=int(row["lease_generation"]),
            lease_expires_at=_read_time(row["lease_expires_at"]),
            heartbeat_at=_read_time(row["heartbeat_at"]),
            provider_request_id=row["provider_request_id"],
            checkpoint=json.loads(row["checkpoint_json"] or "{}"),
            cancellation_requested=bool(row["cancellation_requested"]),
            last_error_code=row["last_error_code"],
            result=result,
            usage=usage,
        )


__all__ = ["SQLiteImageJobStore"]
