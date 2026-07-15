"""PostgreSQL image-job store for horizontally scaled cloud workers.

``psycopg`` is intentionally imported only when a connection is opened.  The
local runtime and SQLite correctness suite therefore do not acquire a cloud
database dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
import secrets
from typing import Any
import uuid

from .models import (
    ACTIVE_STATUSES,
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
    ImageResult,
    ImageSubmitRequest,
    ImageUsage,
    canonical_json,
    default_deadline,
    require_utc,
    utc_now,
)
from .postgres_schema import (
    CURRENT_IMAGE_SCHEMA_VERSION,
    PostgresImageSchemaManager,
    PostgresImageSchemaReceipt,
)
from .store import ProviderCircuitDecision


ConnectionFactory = Callable[[], Any]
Clock = Callable[[], datetime]
_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{2,255}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,255}$")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$")
_CODE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_FORBIDDEN_EVENT_KEY = re.compile(
    r"(?:^|_)(?:binary|bytes|content|path|token|secret|password|private|api_key)(?:$|_)",
    re.IGNORECASE,
)


LEASE_SQL = """
SELECT candidate.*
FROM image_jobs AS candidate
WHERE candidate.status IN ('queued','retry_wait')
  AND candidate.available_at <= %(now)s
  AND candidate.deadline > %(now)s
  AND (SELECT COUNT(*) FROM image_jobs
       WHERE status IN ('leased','running','verifying','committing')) < %(global_cap)s
  AND (SELECT COUNT(*) FROM image_jobs
       WHERE account_id=candidate.account_id
         AND status IN ('leased','running','verifying','committing')) < %(account_cap)s
  AND (SELECT COUNT(*) FROM image_jobs
       WHERE model_id=candidate.model_id
         AND status IN ('leased','running','verifying','committing')) < %(model_cap)s
  AND (SELECT COUNT(*) FROM image_jobs
       WHERE operation=candidate.operation
         AND status IN ('leased','running','verifying','committing')) < %(operation_cap)s
ORDER BY candidate.fair_finish ASC,candidate.priority DESC,
         candidate.created_at ASC,candidate.job_id ASC
FOR UPDATE SKIP LOCKED
LIMIT 1
"""


def _json(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise RuntimeError("stored image JSON is invalid")
    return dict(value)


def _datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("stored image timestamp is invalid")
    return value.astimezone(UTC)


def _request_dict(request: ImageSubmitRequest) -> dict[str, Any]:
    return {
        **request.provider_payload(),
        "client_request_id": request.client_request_id,
        "priority": request.priority,
        "max_attempts": request.max_attempts,
        "deadline_seconds": request.deadline_seconds,
        "model_config_id": request.model_config_id,
        "model_config_revision": request.model_config_revision,
        "provider_model_id": request.provider_model_id,
    }


def _request(value: Any) -> ImageSubmitRequest:
    raw = _json(value)
    return ImageSubmitRequest(
        operation=raw["operation"],
        model_id=raw["model_id"],
        client_request_id=raw["client_request_id"],
        prompt=raw["prompt"],
        width=raw["width"],
        height=raw["height"],
        count=raw["count"],
        input_sha256=tuple(raw["input_sha256"]),
        instruction=raw.get("instruction"),
        priority=raw["priority"],
        max_attempts=raw["max_attempts"],
        deadline_seconds=raw["deadline_seconds"],
        metadata=raw.get("metadata", {}),
        model_config_id=raw.get("model_config_id"),
        model_config_revision=raw.get("model_config_revision"),
        provider_model_id=raw.get("provider_model_id"),
    )


def _checkpoint(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result = dict(value or {})
    if len(result) > 32 or len(canonical_json(result).encode("utf-8")) > 64 * 1024:
        raise ValueError("checkpoint is too large")
    for key in result:
        if not isinstance(key, str) or _FORBIDDEN_EVENT_KEY.search(key):
            raise ValueError("checkpoint contains a forbidden field")
    _safe_payload(result)
    return result


def _safe_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    def inspect(value: Any, key: str = "") -> None:
        if key and key != "size_bytes" and _FORBIDDEN_EVENT_KEY.search(key):
            raise ValueError("event contains a forbidden field")
        if isinstance(value, Mapping):
            for nested_key, nested in value.items():
                if not isinstance(nested_key, str):
                    raise ValueError("event key is invalid")
                inspect(nested, nested_key)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                inspect(nested)
        elif isinstance(value, bytes):
            raise ValueError("event cannot contain bytes")
        elif isinstance(value, str):
            if len(value) > 1024 or "\x00" in value:
                raise ValueError("event string is invalid")
            if re.match(r"^(?:[A-Za-z]:[\\/]|/|\\\\)", value):
                raise ValueError("event cannot contain a filesystem path")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ValueError("event value is invalid")

    safe = dict(payload)
    inspect(safe)
    if len(canonical_json(safe).encode("utf-8")) > 16 * 1024:
        raise ValueError("event is too large")
    return safe


class _PooledConnectionLease:
    """Return a psycopg connection to its pool when store code calls close."""

    def __init__(self, pool: Any, connection: Any) -> None:
        self._pool = pool
        self._connection = connection

    def __getattr__(self, name: str) -> Any:
        connection = self._connection
        if connection is None:
            raise RuntimeError("PostgreSQL connection lease is closed")
        return getattr(connection, name)

    def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        try:
            self._pool.putconn(connection)
        except Exception:
            try:
                connection.close()
            except Exception:
                pass
            raise RuntimeError(
                "PostgreSQL image connection could not return to its pool"
            ) from None


class PostgresImageConnectionPool:
    """Bounded default pool for API and worker processes.

    A pool instance is callable and therefore satisfies ``ConnectionFactory``.
    It owns credentials only inside psycopg; this wrapper never renders the DSN
    or native exception text.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 16,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        if not 0 <= min_size <= max_size <= 128 or max_size < 1:
            raise ValueError("PostgreSQL image pool size is invalid")
        if not 0.1 <= timeout_seconds <= 120:
            raise ValueError("PostgreSQL image pool timeout is invalid")
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError:
            raise RuntimeError(
                "PostgreSQL image orchestration requires the image-cloud extra"
            ) from None
        try:
            self._pool = ConnectionPool(
                conninfo=dsn,
                min_size=min_size,
                max_size=max_size,
                timeout=timeout_seconds,
                kwargs={"row_factory": dict_row},
                open=True,
            )
        except Exception:
            raise RuntimeError("PostgreSQL image connection pool could not start") from None
        self.timeout_seconds = timeout_seconds
        self._closed = False

    def __call__(self) -> Any:
        if self._closed:
            raise RuntimeError("PostgreSQL image connection pool is closed")
        try:
            connection = self._pool.getconn(timeout=self.timeout_seconds)
        except Exception:
            raise RuntimeError("PostgreSQL image connection is unavailable") from None
        return _PooledConnectionLease(self._pool, connection)

    def close(self, *, timeout_seconds: float = 5.0) -> None:
        if not 0 <= timeout_seconds <= 120:
            raise ValueError("PostgreSQL image pool close timeout is invalid")
        if self._closed:
            return
        self._closed = True
        try:
            self._pool.close(timeout=timeout_seconds)
        except Exception:
            # Closing happens during process drain.  Mark the wrapper closed
            # regardless so no caller can lease a half-shut-down connection.
            return


class PostgresImageJobStore:
    """Cloud store with database-authoritative admission and lease fencing."""

    deployment_scope = "shared"
    schema_version = CURRENT_IMAGE_SCHEMA_VERSION
    lease_sql = LEASE_SQL

    def __init__(
        self,
        dsn: str,
        *,
        limits: ImageLimits = ImageLimits(),
        model_weights: Mapping[str, float] | None = None,
        clock: Clock = utc_now,
        connection_factory: ConnectionFactory | None = None,
        pool_min_size: int = 1,
        pool_max_size: int = 16,
        pool_timeout_seconds: float = 10.0,
    ) -> None:
        if not isinstance(dsn, str) or (not dsn.strip() and connection_factory is None):
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn
        self.limits = limits
        self.model_weights = dict(model_weights or {})
        self.clock = clock
        self._owned_pool: PostgresImageConnectionPool | None = None
        if connection_factory is None:
            self._owned_pool = PostgresImageConnectionPool(
                dsn,
                min_size=pool_min_size,
                max_size=pool_max_size,
                timeout_seconds=pool_timeout_seconds,
            )
            connection_factory = self._owned_pool
        elif (
            pool_min_size != 1
            or pool_max_size != 16
            or pool_timeout_seconds != 10.0
        ):
            raise ValueError("pool settings cannot accompany a connection factory")
        self._connection_factory = connection_factory
        # Runtime processes never create or opportunistically repair cloud
        # tables.  They also cannot opt out of validation: an absent, older,
        # newer or drifted schema must stop this worker/API process before it
        # can lease or mutate a job.
        try:
            self.schema_receipt: PostgresImageSchemaReceipt = self.validate_schema()
        except BaseException:
            if self._owned_pool is not None:
                self._owned_pool.close()
            raise

    def close(self) -> None:
        if self._owned_pool is not None:
            self._owned_pool.close()

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise RuntimeError(
                "PostgreSQL image orchestration requires the optional psycopg package"
            ) from None
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            with connection.transaction():
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[Any]:
        connection = self._connect()
        try:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                yield connection
        finally:
            connection.close()

    def validate_schema(self) -> PostgresImageSchemaReceipt:
        """Read-only startup check; this store never executes schema DDL."""

        return PostgresImageSchemaManager(
            self.dsn,
            connection_factory=self._connection_factory,
        ).validate()

    def ping(self) -> None:
        """Cheap read-only readiness probe bound to the installed receipt.

        Full physical-catalog validation is intentionally performed when the
        process composes.  Readiness may run every few seconds, so it checks
        connectivity and the immutable migration receipt without repeatedly
        scanning every PostgreSQL catalog relation.
        """

        try:
            with self._read() as connection:
                row = connection.execute(
                    "SELECT version,migration_checksum,target_schema_sha256 "
                    "FROM ecorex_image_schema_migrations "
                    "ORDER BY version DESC LIMIT 1"
                ).fetchone()
        except Exception:
            raise RuntimeError("PostgreSQL image readiness is unavailable") from None
        if isinstance(row, Mapping):
            values = (
                row.get("version"),
                row.get("migration_checksum"),
                row.get("target_schema_sha256"),
            )
        elif isinstance(row, (tuple, list)) and len(row) == 3:
            values = tuple(row)
        else:
            raise RuntimeError("PostgreSQL image readiness receipt is unavailable")
        if values != (
            self.schema_receipt.migration_version,
            self.schema_receipt.migration_checksum,
            self.schema_receipt.target_schema_sha256,
        ):
            raise RuntimeError("PostgreSQL image readiness receipt is incompatible")

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
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (account_id,)
            )
            existing = connection.execute(
                "SELECT sha256,size_bytes,mime_type FROM image_inputs "
                "WHERE account_id=%s AND sha256=%s",
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
                "VALUES(%s,%s,%s,%s,%s)",
                (
                    account_id,
                    receipt.sha256,
                    receipt.size_bytes,
                    receipt.mime_type,
                    self._now(),
                ),
            )
            return receipt

    def get_input(self, account_id: str, sha256: str) -> ImageInputReceipt:
        self._validate_account(account_id)
        with self._read() as connection:
            row = connection.execute(
                "SELECT sha256,size_bytes,mime_type FROM image_inputs "
                "WHERE account_id=%s AND sha256=%s",
                (account_id, sha256),
            ).fetchone()
        if row is None:
            raise ImageInputNotFound("image input was not found")
        return ImageInputReceipt(
            row["sha256"], int(row["size_bytes"]), row["mime_type"]
        )

    @staticmethod
    def _global_lock(connection: Any) -> None:
        connection.execute(
            "SELECT singleton FROM image_scheduler_control WHERE singleton=TRUE FOR UPDATE"
        ).fetchone()

    def submit(self, account_id: str, request: ImageSubmitRequest) -> tuple[ImageJob, bool]:
        self._validate_account(account_id)
        if not isinstance(request, ImageSubmitRequest):
            raise TypeError("request must be an ImageSubmitRequest")
        now = self._now()
        fingerprint = request.fingerprint()
        weight = request.scheduling_weight(self.model_weights)
        with self._transaction() as connection:
            # Serializes the tenant idempotency namespace without creating a
            # placeholder row, then serializes bounded global admission.
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (account_id,)
            )
            self._global_lock(connection)
            self._expire_schedulable_in(connection, now, account_id=None)
            duplicate = connection.execute(
                "SELECT * FROM image_jobs WHERE account_id=%s AND client_request_id=%s",
                (account_id, request.client_request_id),
            ).fetchone()
            if duplicate is not None:
                if duplicate["request_fingerprint"] != fingerprint:
                    raise ImageIdempotencyConflict(
                        "client_request_id was reused with a different image request"
                    )
                return self._from_row(connection, duplicate), False
            self._assert_capacity(connection, account_id, weight)
            global_floor = connection.execute(
                "SELECT COALESCE(MIN(fair_finish),0) AS value FROM image_jobs "
                "WHERE status IN ('queued','retry_wait','leased','running','verifying','committing')"
            ).fetchone()["value"]
            account = connection.execute(
                "SELECT last_finish FROM image_scheduler_accounts WHERE account_id=%s FOR UPDATE",
                (account_id,),
            ).fetchone()
            finish = max(float(global_floor), float(account["last_finish"]) if account else float(global_floor)) + weight
            connection.execute(
                "INSERT INTO image_scheduler_accounts(account_id,last_finish) VALUES(%s,%s) "
                "ON CONFLICT(account_id) DO UPDATE SET last_finish=EXCLUDED.last_finish",
                (account_id, finish),
            )
            job_id = "imgjob_" + uuid.uuid4().hex
            provider_key = "imgprov_" + hashlib.sha256(
                b"ecorex-image-provider-v1\0" + job_id.encode("ascii")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO image_jobs(
                    job_id,account_id,operation,model_id,size_class,weight,priority,
                    client_request_id,request_fingerprint,request_json,status,attempt,
                    max_attempts,fair_finish,available_at,deadline,provider_idempotency_key,
                    checkpoint_json,created_at,updated_at
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'accepted',0,%s,%s,%s,%s,%s,'{}'::jsonb,%s,%s)
                """,
                (
                    job_id, account_id, request.operation.value, request.model_id,
                    request.size_class, weight, request.priority, request.client_request_id,
                    fingerprint, canonical_json(_request_dict(request)), request.max_attempts,
                    finish, now, default_deadline(request, now), provider_key, now, now,
                ),
            )
            self._event(connection, job_id, account_id, "image.accepted", {
                "operation": request.operation.value,
                "model_id": request.model_id,
                "size_class": request.size_class,
                "weight": weight,
            }, now)
            connection.execute(
                "UPDATE image_jobs SET status='queued',updated_at=%s WHERE job_id=%s",
                (now, job_id),
            )
            self._event(connection, job_id, account_id, "image.queued", {"attempt": 0}, now)
            row = connection.execute("SELECT * FROM image_jobs WHERE job_id=%s", (job_id,)).fetchone()
            return self._from_row(connection, row), True

    def _assert_capacity(self, connection: Any, account_id: str, weight: int) -> None:
        global_row = connection.execute(
            "SELECT COUNT(*) AS count,COALESCE(SUM(weight),0) AS weight FROM image_jobs "
            "WHERE status IN ('queued','retry_wait','leased','running','verifying','committing')"
        ).fetchone()
        account_row = connection.execute(
            "SELECT COUNT(*) AS count,COALESCE(SUM(weight),0) AS weight FROM image_jobs "
            "WHERE account_id=%s AND status IN ('queued','retry_wait','leased','running','verifying','committing')",
            (account_id,),
        ).fetchone()
        if (
            int(global_row["count"]) >= self.limits.max_queued_jobs
            or int(global_row["weight"]) + weight > self.limits.max_queued_weight
            or int(account_row["count"]) >= self.limits.max_account_queued_jobs
            or int(account_row["weight"]) + weight > self.limits.max_account_queued_weight
        ):
            raise ImageBackpressure("image queue capacity is exhausted")

    def get(self, job_id: str, *, account_id: str | None = None) -> ImageJob:
        with self._read() as connection:
            row = connection.execute("SELECT * FROM image_jobs WHERE job_id=%s", (job_id,)).fetchone()
            if row is None or (account_id is not None and row["account_id"] != account_id):
                raise ImageJobNotFound("image job was not found")
            return self._from_row(connection, row)

    def lease_next(self, worker_id: str, *, lease_seconds: int = 30) -> ImageJob | None:
        if not _ACCOUNT.fullmatch(worker_id) or not 5 <= lease_seconds <= 300:
            raise ValueError("worker identity or lease duration is invalid")
        now = self._now()
        with self._transaction() as connection:
            self._global_lock(connection)
            self._expire_schedulable_in(connection, now, account_id=None)
            self._reclaim_in(connection, now, account_id=None)
            row = connection.execute(
                LEASE_SQL,
                {
                    "now": now,
                    "global_cap": self.limits.max_running_jobs,
                    "account_cap": self.limits.max_account_running,
                    "model_cap": self.limits.max_model_running,
                    "operation_cap": self.limits.max_operation_running,
                },
            ).fetchone()
            if row is None:
                return None
            generation = int(row["lease_generation"]) + 1
            token = f"{generation}:" + secrets.token_urlsafe(32)
            expiry = min(now + timedelta(seconds=lease_seconds), _datetime(row["deadline"]))
            leased = connection.execute(
                "UPDATE image_jobs SET status='leased',attempt=attempt+1,lease_owner=%s,"
                "lease_token=%s,lease_generation=%s,lease_expires_at=%s,heartbeat_at=%s,updated_at=%s "
                "WHERE job_id=%s AND status IN ('queued','retry_wait') RETURNING *",
                (worker_id, token, generation, expiry, now, now, row["job_id"]),
            ).fetchone()
            if leased is None:
                return None
            self._event(connection, row["job_id"], row["account_id"], "image.leased", {
                "attempt": int(row["attempt"]) + 1,
                "lease_generation": generation,
            }, now)
            return self._from_row(connection, leased)

    def _owned(self, connection: Any, job_id: str, lease_token: str) -> Mapping[str, Any]:
        row = connection.execute(
            "SELECT * FROM image_jobs WHERE job_id=%s FOR UPDATE", (job_id,)
        ).fetchone()
        if row is None:
            raise ImageJobNotFound("image job was not found")
        stored = row["lease_token"]
        if stored is None or not secrets.compare_digest(stored, lease_token):
            raise ImageLeaseLost("image job fencing token is stale")
        if ImageJobStatus(row["status"]) not in ACTIVE_STATUSES:
            raise ImageLeaseLost("image job is no longer owned by this lease")
        now = self._now()
        if _datetime(row["lease_expires_at"]) <= now or _datetime(row["deadline"]) <= now:
            raise ImageLeaseLost("image job lease or deadline expired")
        return row

    def heartbeat(self, job_id: str, lease_token: str, *, lease_seconds: int = 30) -> ImageJob:
        if not 5 <= lease_seconds <= 300:
            raise ValueError("lease duration is invalid")
        now = self._now()
        with self._transaction() as connection:
            row = self._owned(connection, job_id, lease_token)
            expiry = min(now + timedelta(seconds=lease_seconds), _datetime(row["deadline"]))
            updated = connection.execute(
                "UPDATE image_jobs SET lease_expires_at=%s,heartbeat_at=%s,updated_at=%s "
                "WHERE job_id=%s RETURNING *", (expiry, now, now, job_id)
            ).fetchone()
            self._event(connection, job_id, row["account_id"], "image.heartbeat", {
                "lease_generation": row["lease_generation"]
            }, now)
            return self._from_row(connection, updated)

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
        if provider_request_id is not None and not _PROVIDER_ID.fullmatch(provider_request_id):
            raise ValueError("provider request identity is invalid")
        now = self._now()
        with self._transaction() as connection:
            row = self._owned(connection, job_id, lease_token)
            if ImageJobStatus(row["status"]) not in expected_statuses:
                raise ImageInvalidTransition("image job is in an unexpected state")
            updated = connection.execute(
                "UPDATE image_jobs SET status=%s,checkpoint_json=%s::jsonb,provider_request_id=%s,updated_at=%s "
                "WHERE job_id=%s RETURNING *",
                (
                    target_status.value, canonical_json(checkpoint_value),
                    provider_request_id or row["provider_request_id"], now, job_id,
                ),
            ).fetchone()
            self._event(connection, job_id, row["account_id"], f"image.{target_status.value}", {
                "attempt": row["attempt"], "lease_generation": row["lease_generation"]
            }, now)
            return self._from_row(connection, updated)

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
            exhausted = int(row["attempt"]) >= int(row["max_attempts"]) or _datetime(row["deadline"]) <= available_at
            target = ImageJobStatus.DEAD_LETTER if exhausted else ImageJobStatus.RETRY_WAIT
            updated = connection.execute(
                "UPDATE image_jobs SET status=%s,available_at=%s,checkpoint_json=%s::jsonb,last_error_code=%s,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=%s "
                "WHERE job_id=%s RETURNING *",
                (target.value, available_at, canonical_json(checkpoint_value), error_code, now, job_id),
            ).fetchone()
            self._event(connection, job_id, row["account_id"],
                        "image.dead_lettered" if exhausted else "image.retry_wait",
                        {"attempt": row["attempt"], "error_code": error_code}, now)
            return self._from_row(connection, updated)

    def fail(self, job_id: str, lease_token: str, *, error_code: str) -> ImageJob:
        self._validate_code(error_code)
        now = self._now()
        with self._transaction() as connection:
            row = self._owned(connection, job_id, lease_token)
            updated = connection.execute(
                "UPDATE image_jobs SET status='failed',last_error_code=%s,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=%s "
                "WHERE job_id=%s RETURNING *", (error_code, now, job_id)
            ).fetchone()
            self._event(connection, job_id, row["account_id"], "image.failed", {"error_code": error_code}, now)
            return self._from_row(connection, updated)

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
            connection.execute(
                "INSERT INTO image_results(job_id,sha256,size_bytes,mime_type,committed_at) "
                "VALUES(%s,%s,%s,%s,%s)",
                (job_id, result.sha256, result.size_bytes, result.mime_type, now),
            )
            connection.execute(
                "INSERT INTO image_usage(job_id,usage_json,committed_at) VALUES(%s,%s::jsonb,%s)",
                (job_id, canonical_json(usage.to_dict()), now),
            )
            updated = connection.execute(
                "UPDATE image_jobs SET status='completed',lease_owner=NULL,lease_token=NULL,"
                "lease_expires_at=NULL,heartbeat_at=NULL,checkpoint_json='{}'::jsonb,updated_at=%s "
                "WHERE job_id=%s RETURNING *", (now, job_id)
            ).fetchone()
            self._event(connection, job_id, row["account_id"], "image.completed", {
                "sha256": result.sha256,
                "size_bytes": result.size_bytes,
                "mime_type": result.mime_type,
                "billed_units": usage.billed_units,
            }, now)
            return self._from_row(connection, updated)

    def cancel(self, job_id: str, *, account_id: str) -> ImageJob:
        self._validate_account(account_id)
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id=%s AND account_id=%s FOR UPDATE",
                (job_id, account_id),
            ).fetchone()
            if row is None:
                raise ImageJobNotFound("image job was not found")
            if ImageJobStatus(row["status"]) in TERMINAL_STATUSES:
                return self._from_row(connection, row)
            updated = connection.execute(
                "UPDATE image_jobs SET status='cancelled',cancellation_requested=TRUE,lease_owner=NULL,"
                "lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=%s "
                "WHERE job_id=%s RETURNING *", (now, job_id)
            ).fetchone()
            self._event(connection, job_id, account_id, "image.cancelled", {}, now)
            return self._from_row(connection, updated)

    def reclaim_expired(self, *, account_id: str | None = None) -> int:
        if account_id is not None:
            self._validate_account(account_id)
        with self._transaction() as connection:
            self._global_lock(connection)
            now = self._now()
            return self._expire_schedulable_in(
                connection, now, account_id=account_id
            ) + self._reclaim_in(connection, now, account_id=account_id)

    def _expire_schedulable_in(
        self,
        connection: Any,
        now: datetime,
        *,
        account_id: str | None,
    ) -> int:
        query = (
            "SELECT * FROM image_jobs WHERE status IN ('queued','retry_wait') "
            "AND deadline<=%s"
        )
        params: list[Any] = [now]
        if account_id is not None:
            query += " AND account_id=%s"
            params.append(account_id)
        query += " FOR UPDATE SKIP LOCKED"
        rows = connection.execute(query, tuple(params)).fetchall()
        expired = 0
        for row in rows:
            updated = connection.execute(
                "UPDATE image_jobs SET status='failed',last_error_code='deadline_exceeded',"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=%s "
                "WHERE job_id=%s AND status IN ('queued','retry_wait') AND deadline<=%s "
                "RETURNING job_id",
                (now, row["job_id"], now),
            ).fetchone()
            if updated is None:
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

    def _reclaim_in(self, connection: Any, now: datetime, *, account_id: str | None) -> int:
        query = (
            "SELECT * FROM image_jobs WHERE status IN ('leased','running','verifying','committing') "
            "AND (lease_expires_at<=%s OR deadline<=%s)"
        )
        params: list[Any] = [now, now]
        if account_id is not None:
            query += " AND account_id=%s"
            params.append(account_id)
        query += " FOR UPDATE SKIP LOCKED"
        rows = connection.execute(query, tuple(params)).fetchall()
        for row in rows:
            checkpoint = _json(row["checkpoint_json"])
            if _datetime(row["deadline"]) <= now:
                target, error = ImageJobStatus.FAILED, "deadline_exceeded"
            elif int(row["attempt"]) >= int(row["max_attempts"]):
                target, error = ImageJobStatus.DEAD_LETTER, "lease_exhausted"
            else:
                target, error = ImageJobStatus.RETRY_WAIT, "lease_reclaimed"
                if checkpoint.get("provider_started"):
                    checkpoint["provider_uncertain"] = True
            connection.execute(
                "UPDATE image_jobs SET status=%s,available_at=%s,checkpoint_json=%s::jsonb,last_error_code=%s,"
                "lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,updated_at=%s "
                "WHERE job_id=%s",
                (target.value, now, canonical_json(checkpoint), error, now, row["job_id"]),
            )
            self._event(connection, row["job_id"], row["account_id"],
                        "image.reclaimed" if target is ImageJobStatus.RETRY_WAIT else f"image.{target.value}",
                        {"attempt": row["attempt"], "error_code": error}, now)
        return len(rows)

    def requeue_dead_letter(
        self,
        job_id: str,
        *,
        account_id: str,
        recovery_request_id: str,
    ) -> ImageJob:
        self._validate_account(account_id)
        if not isinstance(recovery_request_id, str) or not _REQUEST_ID.fullmatch(recovery_request_id):
            raise ValueError("recovery_request_id is invalid")
        now = self._now()
        with self._transaction() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (account_id,)
            )
            existing = connection.execute(
                "SELECT job_id FROM image_recovery_requests WHERE account_id=%s AND recovery_request_id=%s",
                (account_id, recovery_request_id),
            ).fetchone()
            if existing is not None:
                if existing["job_id"] != job_id:
                    raise ImageIdempotencyConflict(
                        "recovery_request_id was reused for another image job"
                    )
                row = connection.execute("SELECT * FROM image_jobs WHERE job_id=%s", (job_id,)).fetchone()
                if row is None or row["account_id"] != account_id:
                    raise ImageJobNotFound("image job was not found")
                return self._from_row(connection, row)
            row = connection.execute(
                "SELECT * FROM image_jobs WHERE job_id=%s AND account_id=%s FOR UPDATE",
                (job_id, account_id),
            ).fetchone()
            if row is None:
                raise ImageJobNotFound("image job was not found")
            if ImageJobStatus(row["status"]) is not ImageJobStatus.DEAD_LETTER:
                raise ImageInvalidTransition("only a dead-letter image job can be requeued")
            connection.execute(
                "INSERT INTO image_recovery_requests(account_id,recovery_request_id,job_id,created_at) "
                "VALUES(%s,%s,%s,%s)", (account_id, recovery_request_id, job_id, now)
            )
            updated = connection.execute(
                "UPDATE image_jobs SET status='queued',attempt=0,available_at=%s,last_error_code=NULL,updated_at=%s "
                "WHERE job_id=%s RETURNING *", (now, now, job_id)
            ).fetchone()
            self._event(connection, job_id, account_id, "image.requeued", {"attempt": 0}, now)
            return self._from_row(connection, updated)

    def metrics(self, *, account_id: str | None = None) -> ImageMetrics:
        if account_id is not None:
            self._validate_account(account_id)
        where = " WHERE account_id=%s" if account_id is not None else ""
        params: tuple[Any, ...] = (account_id,) if account_id is not None else ()
        with self._read() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count,COALESCE(SUM(weight),0) AS weight,MIN(created_at) AS oldest "
                "FROM image_jobs" + where + " GROUP BY status", params
            ).fetchall()
            usage_join = (
                " JOIN image_jobs j ON j.job_id=u.job_id WHERE j.account_id=%s"
                if account_id is not None else ""
            )
            usage_rows = connection.execute(
                "SELECT u.usage_json FROM image_usage u" + usage_join, params
            ).fetchall()
        counts = {row["status"]: int(row["count"]) for row in rows}
        queued_weight = sum(int(row["weight"]) for row in rows if row["status"] in {"queued", "retry_wait"})
        now = self._now()
        oldest_seconds = max([0.0, *((now - _datetime(row["oldest"])).total_seconds()
                                      for row in rows if row["status"] in {"queued", "retry_wait"} and row["oldest"] is not None)])
        billed = sum(int(_json(row["usage_json"])["billed_units"]) for row in usage_rows)
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
        with self._read() as connection:
            rows = connection.execute(
                "SELECT seq,event_id,event_type,payload_json,created_at FROM image_events "
                "WHERE job_id=%s ORDER BY seq", (job_id,)
            ).fetchall()
        return tuple({
            "seq": int(row["seq"]),
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "payload": _json(row["payload_json"]),
            "created_at": _datetime(row["created_at"]).isoformat(),
        } for row in rows)

    def breaker_open_until(self, scope: str) -> datetime | None:
        self._validate_scope(scope)
        with self._read() as connection:
            row = connection.execute(
                "SELECT open_until FROM image_breakers WHERE scope=%s", (scope,)
            ).fetchone()
        value = _datetime(row["open_until"]) if row and row["open_until"] is not None else None
        return value if value is not None and value > self._now() else None

    def admit_provider_call(
        self,
        scope: str,
        *,
        probe_seconds: int,
    ) -> ProviderCircuitDecision:
        """Atomically lease one half-open probe across all worker replicas."""

        self._validate_scope(scope)
        if isinstance(probe_seconds, bool) or not 1 <= probe_seconds <= 3600:
            raise ValueError("provider circuit probe duration is invalid")
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT failure_count,open_until FROM image_breakers "
                "WHERE scope=%s FOR UPDATE",
                (scope,),
            ).fetchone()
            if (
                row is None
                or int(row["failure_count"]) <= 0
                or row["open_until"] is None
            ):
                return ProviderCircuitDecision(admitted=True)
            open_until = _datetime(row["open_until"])
            if open_until > now:
                return ProviderCircuitDecision(
                    admitted=False,
                    retry_at=open_until,
                )
            probe_until = now + timedelta(seconds=probe_seconds)
            connection.execute(
                "UPDATE image_breakers SET open_until=%s,updated_at=%s WHERE scope=%s",
                (probe_until, now, scope),
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
                "SELECT failure_count FROM image_breakers WHERE scope=%s FOR UPDATE", (scope,)
            ).fetchone()
            failures = (int(row["failure_count"]) if row else 0) + 1
            open_until = now + timedelta(seconds=cooldown_seconds) if failures >= threshold else None
            connection.execute(
                "INSERT INTO image_breakers(scope,failure_count,open_until,updated_at) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(scope) DO UPDATE SET failure_count=EXCLUDED.failure_count,"
                "open_until=EXCLUDED.open_until,updated_at=EXCLUDED.updated_at",
                (scope, failures, open_until, now),
            )
            return open_until

    def record_provider_rate_limit(
        self,
        scope: str,
        *,
        retry_at: datetime,
        cooldown_seconds: int,
    ) -> datetime:
        """Persist one scope-wide rate-limit fence for every replica."""

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
                "SELECT failure_count,open_until FROM image_breakers "
                "WHERE scope=%s FOR UPDATE",
                (scope,),
            ).fetchone()
            existing = (
                _datetime(row["open_until"])
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
                "INSERT INTO image_breakers(scope,failure_count,open_until,updated_at) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(scope) DO UPDATE SET failure_count=EXCLUDED.failure_count,"
                "open_until=EXCLUDED.open_until,updated_at=EXCLUDED.updated_at",
                (scope, failures, open_until, now),
            )
            return open_until

    def record_provider_success(self, scope: str) -> None:
        self._validate_scope(scope)
        now = self._now()
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO image_breakers(scope,failure_count,open_until,updated_at) VALUES(%s,0,NULL,%s) "
                "ON CONFLICT(scope) DO UPDATE SET failure_count=0,open_until=NULL,updated_at=EXCLUDED.updated_at",
                (scope, now),
            )

    @staticmethod
    def _validate_code(code: str) -> None:
        if not isinstance(code, str) or not _CODE.fullmatch(code):
            raise ValueError("error code is invalid")

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if not isinstance(scope, str) or not _SCOPE.fullmatch(scope):
            raise ValueError("provider breaker scope is invalid")

    @staticmethod
    def _event(
        connection: Any,
        job_id: str,
        account_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO image_events(event_id,job_id,account_id,event_type,payload_json,created_at) "
            "VALUES(%s,%s,%s,%s,%s::jsonb,%s)",
            (
                "imgevt_" + uuid.uuid4().hex,
                job_id,
                account_id,
                event_type,
                canonical_json(_safe_payload(payload)),
                now,
            ),
        )

    @staticmethod
    def _from_row(connection: Any, row: Mapping[str, Any]) -> ImageJob:
        result_row = connection.execute(
            "SELECT sha256,size_bytes,mime_type FROM image_results WHERE job_id=%s",
            (row["job_id"],),
        ).fetchone()
        usage_row = connection.execute(
            "SELECT usage_json FROM image_usage WHERE job_id=%s", (row["job_id"],)
        ).fetchone()
        result = ImageResult(
            result_row["sha256"], int(result_row["size_bytes"]), result_row["mime_type"]
        ) if result_row is not None else None
        usage = ImageUsage(**_json(usage_row["usage_json"])) if usage_row is not None else None
        return ImageJob(
            job_id=row["job_id"],
            account_id=row["account_id"],
            request=_request(row["request_json"]),
            status=ImageJobStatus(row["status"]),
            weight=int(row["weight"]),
            attempt=int(row["attempt"]),
            fair_finish=float(row["fair_finish"]),
            available_at=_datetime(row["available_at"]),
            deadline=_datetime(row["deadline"]),
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
            provider_idempotency_key=row["provider_idempotency_key"],
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_generation=int(row["lease_generation"]),
            lease_expires_at=_datetime(row["lease_expires_at"]),
            heartbeat_at=_datetime(row["heartbeat_at"]),
            provider_request_id=row["provider_request_id"],
            checkpoint=_json(row["checkpoint_json"]),
            cancellation_requested=bool(row["cancellation_requested"]),
            last_error_code=row["last_error_code"],
            result=result,
            usage=usage,
        )


__all__ = [
    "LEASE_SQL",
    "PostgresImageConnectionPool",
    "PostgresImageJobStore",
]
