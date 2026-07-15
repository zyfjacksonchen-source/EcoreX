"""SQLite persistence for public shares and isolated diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
import sqlite3
from typing import Callable

from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.jobs import DurableJobStore, _store_time
from ecorex.runtime.schema_catalog import validate_product_schema
from ecorex.protocol import JOB_TRANSITIONS, TERMINAL_JOB_STATUSES, JobStatus

from .errors import ShareConflict, ShareNotFound
from .models import (
    DiagnosticPayload,
    DiagnosticSnapshotProjection,
    PublishedShare,
    SharePayload,
    ShareSnapshotProjection,
    ShareStatus,
)


_OPAQUE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
SHARE_PUBLISH_JOB_KIND = "share_publish"
SHARE_REVOKE_JOB_KIND = "share_revoke"


@dataclass(frozen=True, slots=True)
class ShareOperation:
    job_id: str
    account_id: str
    share_id: str
    thread_id: str
    action: str
    client_request_id: str
    external_idempotency_key: str
    projection: ShareSnapshotProjection
    payload: SharePayload = field(repr=False)
    remote_snapshot_id: str | None = None


def _require_key(value: str, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_KEY.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("snapshot timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _read_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ShareConflict("snapshot timestamp is invalid")
    return parsed.astimezone(timezone.utc)


class ShareRepository:
    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        jobs: DurableJobStore | None = None,
    ) -> None:
        self.database = database
        self.jobs = jobs or DurableJobStore(database)
        if self.jobs.database.path.resolve() != self.database.path.resolve():
            raise ValueError("share state and Durable Jobs must use one database")
        self._validate_schema()

    def _validate_schema(self) -> None:
        """Fail closed on schema drift without repairing product storage."""

        with self.database.reader() as connection:
            validate_product_schema(connection)

    @staticmethod
    def _create_fingerprint(payload: SharePayload) -> str:
        return hashlib.sha256(
            b"ecorex-share-create-v1\n"
            + payload.thread_id.encode("utf-8")
            + b"\0"
            + str(payload.source_watermark).encode("ascii")
            + b"\0"
            + payload.expires_at.isoformat().encode("ascii")
        ).hexdigest()

    def begin_create(
        self,
        *,
        account_id: str,
        client_request_id: str,
        payload: SharePayload,
        now: datetime,
        max_attempts: int = 3,
        deadline_seconds: int = 3600,
    ) -> tuple[ShareSnapshotProjection, SharePayload]:
        _require_key(account_id, "share account identity")
        _require_key(client_request_id, "share client_request_id")
        fingerprint = self._create_fingerprint(payload)
        encoded = payload.canonical_bytes().decode("utf-8")
        if not 1 <= max_attempts <= 10:
            raise ValueError("share publish max_attempts is invalid")
        if not 30 <= deadline_seconds <= 86_400:
            raise ValueError("share publish deadline is invalid")
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM share_snapshots WHERE account_id=? AND client_request_id=?",
                (account_id, client_request_id),
            ).fetchone()
            if existing is not None:
                stored_payload = self._validated_payload(existing)
                stored_duration = stored_payload.expires_at - stored_payload.created_at
                requested_duration = payload.expires_at - payload.created_at
                if (
                    existing["request_fingerprint"]
                    != self._create_fingerprint(stored_payload)
                    or stored_payload.thread_id != payload.thread_id
                    or stored_duration != requested_duration
                ):
                    raise ShareConflict(
                        "share client_request_id was reused with different input"
                    )
                existing = self._expire_if_needed(connection, existing, now=now)
                if existing["status"] == ShareStatus.PUBLISHING.value:
                    self._enqueue_share_job(
                        connection,
                        account_id=account_id,
                        share_id=existing["share_id"],
                        thread_id=existing["thread_id"],
                        action="publish",
                        client_request_id=client_request_id,
                        max_attempts=max_attempts,
                        deadline=min(
                            _read_time(existing["expires_at"]),
                            now + timedelta(seconds=deadline_seconds),
                        ),
                        now=now,
                    )
                return self._projection(existing), stored_payload
            connection.execute(
                "INSERT INTO share_snapshots("
                "share_id, account_id, thread_id, source_watermark, payload_json, "
                "payload_sha256, client_request_id, request_fingerprint, status, "
                "expires_at, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payload.share_id,
                    account_id,
                    payload.thread_id,
                    payload.source_watermark,
                    encoded,
                    payload.sha256,
                    client_request_id,
                    fingerprint,
                    ShareStatus.PUBLISHING.value,
                    _iso(payload.expires_at),
                    _iso(payload.created_at),
                    _iso(payload.created_at),
                ),
            )
            row = self._require(connection, payload.share_id, account_id=account_id)
            self._enqueue_share_job(
                connection,
                account_id=account_id,
                share_id=payload.share_id,
                thread_id=payload.thread_id,
                action="publish",
                client_request_id=client_request_id,
                max_attempts=max_attempts,
                deadline=min(
                    payload.expires_at,
                    now + timedelta(seconds=deadline_seconds),
                ),
                now=now,
            )
            return self._projection(row), payload

    def mark_published(
        self,
        share_id: str,
        published: PublishedShare,
        *,
        account_id: str,
        now: datetime,
    ) -> ShareSnapshotProjection:
        _require_key(account_id, "share account identity")
        with self.database.transaction() as connection:
            row = self._require(connection, share_id, account_id=account_id)
            self._validated_payload(row)
            row = self._expire_if_needed(connection, row, now=now)
            if row["status"] == ShareStatus.PUBLISHED.value:
                if (
                    row["remote_snapshot_id"] != published.remote_snapshot_id
                    or row["public_url"] != published.public_url
                ):
                    raise ShareConflict("publisher changed an existing share identity")
                return self._projection(row)
            if row["status"] != ShareStatus.PUBLISHING.value:
                raise ShareConflict("share cannot publish from its current status")
            connection.execute(
                "UPDATE share_snapshots SET status=?, remote_snapshot_id=?, public_url=?, "
                "error_code=NULL, updated_at=? WHERE share_id=? AND account_id=?",
                (
                    ShareStatus.PUBLISHED.value,
                    published.remote_snapshot_id,
                    published.public_url,
                    _iso(now),
                    share_id,
                    account_id,
                ),
            )
            return self._projection(
                self._require(connection, share_id, account_id=account_id)
            )

    def mark_failed(
        self,
        share_id: str,
        error_code: str,
        *,
        account_id: str,
        now: datetime,
    ) -> ShareSnapshotProjection:
        _require_key(account_id, "share account identity")
        with self.database.transaction() as connection:
            row = self._require(connection, share_id, account_id=account_id)
            self._validated_payload(row)
            row = self._expire_if_needed(connection, row, now=now)
            if row["status"] not in {
                ShareStatus.PUBLISHING.value,
                ShareStatus.FAILED.value,
            }:
                return self._projection(row)
            connection.execute(
                "UPDATE share_snapshots SET status=?, error_code=?, public_url=NULL, "
                "updated_at=? WHERE share_id=? AND account_id=?",
                (
                    ShareStatus.FAILED.value,
                    error_code[:128],
                    _iso(now),
                    share_id,
                    account_id,
                ),
            )
            return self._projection(
                self._require(connection, share_id, account_id=account_id)
            )

    def begin_revoke(
        self,
        share_id: str,
        *,
        account_id: str,
        client_request_id: str,
        now: datetime,
        max_attempts: int = 3,
        deadline_seconds: int = 3600,
    ) -> tuple[ShareSnapshotProjection, str | None]:
        _require_key(account_id, "share account identity")
        _require_key(client_request_id, "share client_request_id")
        fingerprint = hashlib.sha256(
            f"ecorex-share-revoke-v1\n{account_id}\0{share_id}".encode("utf-8")
        ).hexdigest()
        if not 1 <= max_attempts <= 10:
            raise ValueError("share revoke max_attempts is invalid")
        if not 30 <= deadline_seconds <= 86_400:
            raise ValueError("share revoke deadline is invalid")
        with self.database.transaction() as connection:
            row = self._require(connection, share_id, account_id=account_id)
            self._validated_payload(row)
            row = self._expire_if_needed(connection, row, now=now)
            operation = connection.execute(
                "SELECT * FROM share_operations WHERE account_id=? AND client_request_id=?",
                (account_id, client_request_id),
            ).fetchone()
            if operation is not None:
                if operation["request_fingerprint"] != fingerprint:
                    raise ShareConflict(
                        "share operation client_request_id was reused with different input"
                    )
            else:
                connection.execute(
                    "INSERT INTO share_operations(operation_id, account_id, share_id, action, "
                    "client_request_id, request_fingerprint, created_at) VALUES (?, ?, ?, 'revoke', ?, ?, ?)",
                    (
                        "shop_" + secrets.token_hex(16),
                        account_id,
                        share_id,
                        client_request_id,
                        fingerprint,
                        _iso(now),
                    ),
                )
            if row["status"] in {
                ShareStatus.REVOKED.value,
                ShareStatus.EXPIRED.value,
            }:
                return self._projection(row), row["remote_snapshot_id"]
            if row["status"] not in {
                ShareStatus.PUBLISHING.value,
                ShareStatus.PUBLISHED.value,
                ShareStatus.REVOKING.value,
                ShareStatus.FAILED.value,
            }:
                raise ShareConflict("share cannot be revoked from its current status")
            if (
                row["status"] == ShareStatus.FAILED.value
                and not row["remote_snapshot_id"]
            ):
                connection.execute(
                    "UPDATE share_snapshots SET status=?, error_code=NULL, public_url=NULL, "
                    "revoked_at=?, updated_at=? WHERE share_id=? AND account_id=?",
                    (
                        ShareStatus.REVOKED.value,
                        _iso(now),
                        _iso(now),
                        share_id,
                        account_id,
                    ),
                )
                row = self._require(connection, share_id, account_id=account_id)
                return self._projection(row), None
            if row["status"] != ShareStatus.REVOKING.value:
                connection.execute(
                    "UPDATE share_snapshots SET status=?, public_url=NULL, error_code=NULL, "
                    "updated_at=? "
                    "WHERE share_id=? AND account_id=?",
                    (
                        ShareStatus.REVOKING.value,
                        _iso(now),
                        share_id,
                        account_id,
                    ),
                )
                row = self._require(connection, share_id, account_id=account_id)
            self._enqueue_share_job(
                connection,
                account_id=account_id,
                share_id=share_id,
                thread_id=row["thread_id"],
                action="revoke",
                client_request_id=client_request_id,
                max_attempts=max_attempts,
                deadline=now + timedelta(seconds=deadline_seconds),
                now=now,
            )
            return self._projection(row), row["remote_snapshot_id"]

    def get_operation(self, job_id: str, *, now: datetime) -> ShareOperation:
        """Load only the durable identity needed by a share worker.

        The Job payload intentionally carries no conversation text, artifact
        path, cloud URL, account identity, or remote snapshot identifier.
        Sensitive snapshot content remains in the integrity-checked share row.
        """

        with self.database.transaction() as connection:
            job, binding, share = self._operation_rows(connection, job_id)
            share = self._expire_if_needed(connection, share, now=now)
            payload = self._validated_payload(share)
            expected = {
                "schema_version": 1,
                "share_id": binding["share_id"],
                "action": binding["action"],
            }
            try:
                stored_job_payload = json.loads(job["payload_json"])
            except (TypeError, ValueError):
                raise ShareConflict("share Durable Job payload is invalid") from None
            if stored_job_payload != expected:
                raise ShareConflict("share Durable Job payload integrity is invalid")
            return ShareOperation(
                job_id=job_id,
                account_id=binding["account_id"],
                share_id=binding["share_id"],
                thread_id=share["thread_id"],
                action=binding["action"],
                client_request_id=binding["client_request_id"],
                external_idempotency_key=binding["external_idempotency_key"],
                projection=self._projection(share),
                payload=payload,
                remote_snapshot_id=share["remote_snapshot_id"],
            )

    def skip_publish(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        reason: str,
        now: datetime,
        before_commit: Callable[[], None] | None = None,
    ) -> ShareSnapshotProjection:
        with self.database.transaction() as connection:
            job, binding, share = self._owned_operation_rows(
                connection,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                now=now,
                action="publish",
            )
            share = self._expire_if_needed(connection, share, now=now)
            if share["status"] == ShareStatus.PUBLISHING.value:
                raise ShareConflict("active publication cannot be skipped")
            self._finish_owned_job(
                connection,
                job,
                target=JobStatus.CANCELLED,
                event_type="job.cancelled",
                error=reason,
                now=now,
            )
            self._append_share_event(
                connection,
                share,
                binding,
                event_type="share.publish_skipped",
                payload={"share_id": share["share_id"], "reason": reason[:128]},
                idempotency_key=f"share:{job_id}:publish-skipped",
                now=now,
            )
            if before_commit is not None:
                before_commit()
            return self._projection(
                self._require(
                    connection, share["share_id"], account_id=share["account_id"]
                )
            )

    def complete_publish(
        self,
        job_id: str,
        published: PublishedShare,
        *,
        worker_id: str,
        lease_token: str,
        now: datetime,
        before_commit: Callable[[], None] | None = None,
    ) -> ShareSnapshotProjection:
        with self.database.transaction() as connection:
            job, binding, share = self._owned_operation_rows(
                connection,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                now=now,
                action="publish",
            )
            self._validated_payload(share)
            share = self._expire_if_needed(connection, share, now=now)
            current_remote = share["remote_snapshot_id"]
            if current_remote is not None and current_remote != published.remote_snapshot_id:
                raise ShareConflict("publisher changed an existing share identity")
            status = ShareStatus(share["status"])
            if status is ShareStatus.PUBLISHING:
                connection.execute(
                    "UPDATE share_snapshots SET status=?, remote_snapshot_id=?, "
                    "public_url=?, error_code=NULL, updated_at=? "
                    "WHERE share_id=? AND account_id=?",
                    (
                        ShareStatus.PUBLISHED.value,
                        published.remote_snapshot_id,
                        published.public_url,
                        _iso(now),
                        share["share_id"],
                        share["account_id"],
                    ),
                )
                terminal_event = "share.created"
                terminal_payload = {
                    "share_id": share["share_id"],
                    "source_watermark": share["source_watermark"],
                    "expires_at": share["expires_at"],
                }
            elif status in {
                ShareStatus.REVOKING,
                ShareStatus.EXPIRED,
                ShareStatus.REVOKED,
            }:
                # A local revoke/expiry fence won the race while the remote
                # idempotent request was in flight.  Persist only the opaque
                # remote identity so a durable cleanup can revoke it; never
                # re-expose the returned URL.
                connection.execute(
                    "UPDATE share_snapshots SET remote_snapshot_id=?, public_url=NULL, "
                    "updated_at=? WHERE share_id=? AND account_id=?",
                    (
                        published.remote_snapshot_id,
                        _iso(now),
                        share["share_id"],
                        share["account_id"],
                    ),
                )
                terminal_event = "share.publish_fenced"
                terminal_payload = {
                    "share_id": share["share_id"],
                    "reason": status.value,
                }
                if status is ShareStatus.EXPIRED:
                    self._enqueue_share_job(
                        connection,
                        account_id=share["account_id"],
                        share_id=share["share_id"],
                        thread_id=share["thread_id"],
                        action="revoke",
                        client_request_id=f"expiry:{share['share_id']}",
                        max_attempts=3,
                        deadline=now + timedelta(hours=1),
                        now=now,
                    )
            elif status is ShareStatus.PUBLISHED:
                if share["public_url"] != published.public_url:
                    raise ShareConflict("publisher changed an existing share URL")
                terminal_event = "share.created"
                terminal_payload = {
                    "share_id": share["share_id"],
                    "source_watermark": share["source_watermark"],
                    "expires_at": share["expires_at"],
                }
            else:
                raise ShareConflict("share cannot publish from its current status")
            self._finish_owned_job(
                connection,
                job,
                target=JobStatus.COMPLETED,
                event_type="job.completed",
                error=None,
                now=now,
            )
            refreshed = self._require(
                connection, share["share_id"], account_id=share["account_id"]
            )
            self._append_share_event(
                connection,
                refreshed,
                binding,
                event_type=terminal_event,
                payload=terminal_payload,
                idempotency_key=f"share:{share['share_id']}:{terminal_event}",
                now=now,
            )
            if before_commit is not None:
                before_commit()
            return self._projection(refreshed)

    def complete_revoke(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        now: datetime,
        before_commit: Callable[[], None] | None = None,
    ) -> ShareSnapshotProjection:
        with self.database.transaction() as connection:
            job, binding, share = self._owned_operation_rows(
                connection,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                now=now,
                action="revoke",
            )
            self._validated_payload(share)
            share = self._expire_if_needed(connection, share, now=now)
            status = ShareStatus(share["status"])
            if status is not ShareStatus.REVOKED:
                if status not in {
                    ShareStatus.REVOKING,
                    ShareStatus.EXPIRED,
                    ShareStatus.FAILED,
                }:
                    raise ShareConflict("share is not awaiting revocation")
                connection.execute(
                    "UPDATE share_snapshots SET status=?, public_url=NULL, "
                    "error_code=NULL, revoked_at=?, updated_at=? "
                    "WHERE share_id=? AND account_id=?",
                    (
                        ShareStatus.REVOKED.value,
                        _iso(now),
                        _iso(now),
                        share["share_id"],
                        share["account_id"],
                    ),
                )
            self._finish_owned_job(
                connection,
                job,
                target=JobStatus.COMPLETED,
                event_type="job.completed",
                error=None,
                now=now,
            )
            refreshed = self._require(
                connection, share["share_id"], account_id=share["account_id"]
            )
            self._append_share_event(
                connection,
                refreshed,
                binding,
                event_type="share.revoked",
                payload={"share_id": share["share_id"]},
                idempotency_key=f"share:{share['share_id']}:revoked",
                now=now,
            )
            if before_commit is not None:
                before_commit()
            return self._projection(refreshed)

    def fail_operation(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        retry_delay_seconds: int,
        now: datetime,
        before_commit: Callable[[], None] | None = None,
    ) -> JobStatus:
        safe_error = re.sub(r"[^a-z0-9_.:-]+", "_", error_code.casefold())[:128]
        safe_error = safe_error or "share_operation_failed"
        with self.database.transaction() as connection:
            job, binding, share = self._owned_operation_rows(
                connection,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                now=now,
            )
            attempt = int(job["attempt"])
            if retryable and attempt < int(job["max_attempts"]):
                target = JobStatus.RETRY_SCHEDULED
                event_type = "job.retry_scheduled"
                available_at = now + timedelta(seconds=max(0, retry_delay_seconds))
            elif retryable:
                target = JobStatus.DEAD_LETTER
                event_type = "job.dead_lettered"
                available_at = now
            else:
                target = JobStatus.FAILED
                event_type = "job.failed"
                available_at = now
            self._finish_owned_job(
                connection,
                job,
                target=target,
                event_type=event_type,
                error=safe_error,
                available_at=available_at,
                now=now,
            )
            if target in TERMINAL_JOB_STATUSES:
                self._converge_terminal_failure(
                    connection,
                    share=share,
                    binding=binding,
                    job_id=job_id,
                    error_code=safe_error,
                    now=now,
                )
            if before_commit is not None:
                before_commit()
            return target

    def reconcile_terminal_jobs(
        self,
        *,
        now: datetime,
        before_commit: Callable[[], None] | None = None,
    ) -> int:
        """Repair state after deadline expiry or a process crash at a fence."""

        repaired = 0
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT b.*, j.status AS job_status, j.last_error, "
                "s.thread_id, s.status AS share_status, s.remote_snapshot_id "
                "FROM share_job_bindings AS b "
                "JOIN jobs AS j ON j.job_id=b.job_id "
                "JOIN share_snapshots AS s ON s.share_id=b.share_id "
                "AND s.account_id=b.account_id "
                "WHERE j.status IN (?, ?, ?, ?) "
                "AND s.status IN (?, ?, ?)",
                (
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                    JobStatus.DEAD_LETTER.value,
                    ShareStatus.PUBLISHING.value,
                    ShareStatus.REVOKING.value,
                    ShareStatus.FAILED.value,
                ),
            ).fetchall()
            for binding in rows:
                if binding["job_status"] == JobStatus.COMPLETED.value:
                    # Atomic completion normally makes this impossible.  A
                    # completed revoke with no remote identity is still safe
                    # to converge to revoked after an older schema migration.
                    if (
                        binding["action"] == "revoke"
                        and binding["share_status"] == ShareStatus.REVOKING.value
                        and not binding["remote_snapshot_id"]
                    ):
                        connection.execute(
                            "UPDATE share_snapshots SET status=?, revoked_at=?, "
                            "updated_at=? WHERE share_id=? AND account_id=?",
                            (
                                ShareStatus.REVOKED.value,
                                _iso(now),
                                _iso(now),
                                binding["share_id"],
                                binding["account_id"],
                            ),
                        )
                        repaired += 1
                    continue
                share = self._require(
                    connection,
                    binding["share_id"],
                    account_id=binding["account_id"],
                )
                before = share["status"]
                self._converge_terminal_failure(
                    connection,
                    share=share,
                    binding=binding,
                    job_id=binding["job_id"],
                    error_code=str(binding["last_error"] or "share_job_terminal"),
                    now=now,
                )
                after = self._require(
                    connection,
                    binding["share_id"],
                    account_id=binding["account_id"],
                )["status"]
                repaired += int(before != after)
            if before_commit is not None:
                before_commit()
        return repaired

    def mark_revoked(
        self, share_id: str, *, account_id: str, now: datetime
    ) -> ShareSnapshotProjection:
        _require_key(account_id, "share account identity")
        with self.database.transaction() as connection:
            row = self._require(connection, share_id, account_id=account_id)
            self._validated_payload(row)
            if row["status"] == ShareStatus.REVOKED.value:
                return self._projection(row)
            if row["status"] not in {
                ShareStatus.REVOKING.value,
                ShareStatus.EXPIRED.value,
                ShareStatus.FAILED.value,
            } or not row["remote_snapshot_id"]:
                raise ShareConflict("share is not awaiting revocation")
            connection.execute(
                "UPDATE share_snapshots SET status=?, public_url=NULL, revoked_at=?, "
                "updated_at=? WHERE share_id=? AND account_id=?",
                (
                    ShareStatus.REVOKED.value,
                    _iso(now),
                    _iso(now),
                    share_id,
                    account_id,
                ),
            )
            return self._projection(
                self._require(connection, share_id, account_id=account_id)
            )

    def get(
        self,
        share_id: str,
        *,
        account_id: str,
        now: datetime,
    ) -> ShareSnapshotProjection:
        _require_key(account_id, "share account identity")
        with self.database.reader() as connection:
            row = self._require(connection, share_id, account_id=account_id)
            self._validated_payload(row)
            row = self._effective_expiry(row, now=now)
            return self._projection(row)

    def list_for_thread(
        self,
        thread_id: str,
        *,
        account_id: str,
        now: datetime,
        limit: int = 100,
    ) -> tuple[list[ShareSnapshotProjection], int]:
        _require_key(account_id, "share account identity")
        if not 1 <= limit <= 200:
            raise ValueError("share list limit must be between 1 and 200")
        with self.database.reader() as connection:
            rows = connection.execute(
                "SELECT * FROM share_snapshots "
                "WHERE account_id=? AND thread_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (account_id, thread_id, limit),
            ).fetchall()
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM share_snapshots "
                    "WHERE account_id=? AND thread_id=?",
                    (account_id, thread_id),
                ).fetchone()[0]
            )
            projections: list[ShareSnapshotProjection] = []
            for row in rows:
                payload = self._validated_payload(row)
                if payload.thread_id != thread_id:
                    raise ShareConflict("share payload thread identity is invalid")
                projections.append(
                    self._projection(self._effective_expiry(row, now=now))
                )
            return projections, total

    def expire_due(
        self,
        *,
        now: datetime,
        before_commit: Callable[[], None] | None = None,
    ) -> int:
        """Converge expiry from an explicit maintenance/mutation boundary."""

        with self.database.transaction() as connection:
            changed = connection.execute(
                "UPDATE share_snapshots SET status=?, public_url=NULL, error_code=NULL, "
                "updated_at=? WHERE expires_at<=? AND status NOT IN (?, ?)",
                (
                    ShareStatus.EXPIRED.value,
                    _iso(now),
                    _iso(now),
                    ShareStatus.REVOKED.value,
                    ShareStatus.EXPIRED.value,
                ),
            ).rowcount
            if before_commit is not None:
                before_commit()
        return max(0, changed)

    def create_diagnostic(
        self,
        *,
        account_id: str,
        client_request_id: str,
        payload: DiagnosticPayload,
    ) -> DiagnosticSnapshotProjection:
        _require_key(account_id, "diagnostic account identity")
        _require_key(client_request_id, "diagnostic client_request_id")
        fingerprint = hashlib.sha256(
            b"ecorex-diagnostic-create-v1\n" + payload.canonical_bytes()
        ).hexdigest()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM diagnostic_snapshots WHERE account_id=? AND client_request_id=?",
                (account_id, client_request_id),
            ).fetchone()
            if existing is not None:
                stored_payload = self._validate_diagnostic(existing)
                if (
                    existing["request_fingerprint"]
                    != hashlib.sha256(
                        b"ecorex-diagnostic-create-v1\n"
                        + stored_payload.canonical_bytes()
                    ).hexdigest()
                    or stored_payload.thread_id != payload.thread_id
                    or stored_payload.reason_code != payload.reason_code
                ):
                    raise ShareConflict(
                        "diagnostic client_request_id was reused with different input"
                    )
                return self._diagnostic_projection(existing)
            encoded = payload.canonical_bytes().decode("utf-8")
            connection.execute(
                "INSERT INTO diagnostic_snapshots("
                "diagnostic_id, account_id, thread_id, source_watermark, reason_code, "
                "payload_json, payload_sha256, client_request_id, request_fingerprint, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payload.diagnostic_id,
                    account_id,
                    payload.thread_id,
                    payload.source_watermark,
                    payload.reason_code,
                    encoded,
                    payload.sha256,
                    client_request_id,
                    fingerprint,
                    _iso(payload.created_at),
                ),
            )
            row = connection.execute(
                "SELECT * FROM diagnostic_snapshots WHERE diagnostic_id=?",
                (payload.diagnostic_id,),
            ).fetchone()
            return self._diagnostic_projection(row)

    def read_payload(self, share_id: str, *, account_id: str) -> SharePayload:
        _require_key(account_id, "share account identity")
        with self.database.reader() as connection:
            return self._validated_payload(
                self._require(connection, share_id, account_id=account_id)
            )

    def read_diagnostic(
        self, diagnostic_id: str, *, account_id: str
    ) -> DiagnosticPayload:
        _require_key(account_id, "diagnostic account identity")
        with self.database.reader() as connection:
            row = connection.execute(
                "SELECT * FROM diagnostic_snapshots WHERE diagnostic_id=? AND account_id=?",
                (diagnostic_id, account_id),
            ).fetchone()
            if row is None:
                raise ShareNotFound("diagnostic snapshot was not found")
            return self._validate_diagnostic(row)

    def _enqueue_share_job(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        share_id: str,
        thread_id: str,
        action: str,
        client_request_id: str,
        max_attempts: int,
        deadline: datetime,
        now: datetime,
    ) -> str:
        if action not in {"publish", "revoke"}:
            raise ValueError("share action is invalid")
        external_idempotency_key = (
            share_id if action == "publish" else f"{share_id}:revoke"
        )
        existing = connection.execute(
            "SELECT * FROM share_job_bindings WHERE account_id=? AND share_id=? "
            "AND action=? AND client_request_id=?",
            (account_id, share_id, action, client_request_id),
        ).fetchone()
        if existing is not None:
            job = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (existing["job_id"],)
            ).fetchone()
            expected_kind = (
                SHARE_PUBLISH_JOB_KIND
                if action == "publish"
                else SHARE_REVOKE_JOB_KIND
            )
            try:
                stored_payload = json.loads(job["payload_json"]) if job else None
            except (TypeError, ValueError):
                stored_payload = None
            if (
                job is None
                or job["kind"] != expected_kind
                or existing["external_idempotency_key"]
                != external_idempotency_key
                or stored_payload
                != {
                    "schema_version": 1,
                    "share_id": share_id,
                    "action": action,
                }
            ):
                raise ShareConflict("share Durable Job binding is invalid")
            return str(existing["job_id"])
        identity = hashlib.sha256(
            (
                "ecorex-share-job-v1\n"
                + account_id
                + "\0"
                + share_id
                + "\0"
                + action
                + "\0"
                + client_request_id
            ).encode("utf-8")
        ).hexdigest()
        durable = self.jobs.enqueue_in_transaction(
            connection,
            kind=(
                SHARE_PUBLISH_JOB_KIND
                if action == "publish"
                else SHARE_REVOKE_JOB_KIND
            ),
            payload={
                "schema_version": 1,
                "share_id": share_id,
                "action": action,
            },
            idempotency_key=f"share-runtime:{identity}",
            thread_id=thread_id,
            turn_id=None,
            max_attempts=max_attempts,
            deadline=deadline,
            now=now,
        )
        connection.execute(
            "INSERT OR IGNORE INTO share_job_bindings("
            "job_id, account_id, share_id, action, client_request_id, "
            "external_idempotency_key, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                durable.job_id,
                account_id,
                share_id,
                action,
                client_request_id,
                external_idempotency_key,
                _iso(now),
            ),
        )
        binding = connection.execute(
            "SELECT * FROM share_job_bindings WHERE job_id=?",
            (durable.job_id,),
        ).fetchone()
        if binding is None or any(
            binding[key] != expected
            for key, expected in {
                "account_id": account_id,
                "share_id": share_id,
                "action": action,
                "client_request_id": client_request_id,
                "external_idempotency_key": external_idempotency_key,
            }.items()
        ):
            raise ShareConflict("share Durable Job binding is invalid")
        return durable.job_id

    @staticmethod
    def _operation_rows(
        connection: sqlite3.Connection, job_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        job = connection.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        binding = connection.execute(
            "SELECT * FROM share_job_bindings WHERE job_id=?", (job_id,)
        ).fetchone()
        if job is None or binding is None:
            raise ShareNotFound("share operation was not found")
        expected_kind = (
            SHARE_PUBLISH_JOB_KIND
            if binding["action"] == "publish"
            else SHARE_REVOKE_JOB_KIND
        )
        if job["kind"] != expected_kind:
            raise ShareConflict("share Durable Job kind is invalid")
        share = ShareRepository._require(
            connection,
            binding["share_id"],
            account_id=binding["account_id"],
        )
        if job["thread_id"] != share["thread_id"] or job["turn_id"] is not None:
            raise ShareConflict("share Durable Job scope is invalid")
        return job, binding, share

    def _owned_operation_rows(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        worker_id: str,
        lease_token: str,
        now: datetime,
        action: str | None = None,
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        job = self.jobs._owned_row(
            connection, job_id, worker_id, lease_token, now
        )
        _job, binding, share = self._operation_rows(connection, job_id)
        if action is not None and binding["action"] != action:
            raise ShareConflict("share Durable Job action is invalid")
        return job, binding, share

    def _finish_owned_job(
        self,
        connection: sqlite3.Connection,
        job: sqlite3.Row,
        *,
        target: JobStatus,
        event_type: str,
        error: str | None,
        now: datetime,
        available_at: datetime | None = None,
    ) -> None:
        current = JobStatus(job["status"])
        if target not in JOB_TRANSITIONS[current]:
            raise ShareConflict(
                f"share Job cannot transition from {current.value} to {target.value}"
            )
        self.jobs._append_job_event(
            connection,
            row_or_values=job,
            event_type=event_type,
            payload={"attempt": job["attempt"], "error": error},
            created_at=now,
        )
        connection.execute(
            "UPDATE jobs SET status=?, lease_owner=NULL, lease_token=NULL, "
            "lease_expires_at=NULL, heartbeat_at=NULL, available_at=?, "
            "last_error=?, updated_at=? WHERE job_id=?",
            (
                target.value,
                _store_time(available_at or now),
                error,
                _store_time(now),
                job["job_id"],
            ),
        )

    def _append_share_event(
        self,
        connection: sqlite3.Connection,
        share: sqlite3.Row,
        binding: sqlite3.Row,
        *,
        event_type: str,
        payload: dict[str, object],
        idempotency_key: str,
        now: datetime,
    ) -> None:
        self.jobs.events.append_in_transaction(
            connection,
            thread_id=share["thread_id"],
            job_id=binding["job_id"],
            event_type=event_type,
            payload=payload,
            correlation_id=binding["client_request_id"],
            idempotency_key=idempotency_key,
            created_at=now,
        )

    def _converge_terminal_failure(
        self,
        connection: sqlite3.Connection,
        *,
        share: sqlite3.Row,
        binding: sqlite3.Row,
        job_id: str,
        error_code: str,
        now: datetime,
    ) -> None:
        status = ShareStatus(share["status"])
        action = binding["action"]
        should_fail = action == "publish" and status is ShareStatus.PUBLISHING
        if action == "revoke" and status is ShareStatus.REVOKING:
            newer_active = connection.execute(
                "SELECT 1 FROM share_job_bindings AS b "
                "JOIN jobs AS j ON j.job_id=b.job_id "
                "WHERE b.account_id=? AND b.share_id=? AND b.action='revoke' "
                "AND b.job_id<>? AND j.status IN (?, ?, ?, ?) LIMIT 1",
                (
                    share["account_id"],
                    share["share_id"],
                    job_id,
                    JobStatus.QUEUED.value,
                    JobStatus.LEASED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.RETRY_SCHEDULED.value,
                ),
            ).fetchone()
            should_fail = newer_active is None
        if not should_fail:
            return
        safe_error = re.sub(r"[^a-z0-9_.:-]+", "_", error_code.casefold())[:128]
        safe_error = safe_error or "share_operation_failed"
        connection.execute(
            "UPDATE share_snapshots SET status=?, public_url=NULL, error_code=?, "
            "updated_at=? WHERE share_id=? AND account_id=?",
            (
                ShareStatus.FAILED.value,
                safe_error,
                _iso(now),
                share["share_id"],
                share["account_id"],
            ),
        )
        refreshed = self._require(
            connection, share["share_id"], account_id=share["account_id"]
        )
        self._append_share_event(
            connection,
            refreshed,
            binding,
            event_type="share.failed",
            payload={
                "share_id": share["share_id"],
                "operation": action,
                "error_code": safe_error,
            },
            idempotency_key=f"share:{job_id}:failed",
            now=now,
        )

    @staticmethod
    def _require(
        connection: sqlite3.Connection,
        share_id: str,
        *,
        account_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM share_snapshots WHERE share_id=? AND account_id=?",
            (share_id, account_id),
        ).fetchone()
        if row is None:
            raise ShareNotFound("share snapshot was not found")
        return row

    @staticmethod
    def _validated_payload(row: sqlite3.Row) -> SharePayload:
        encoded = row["payload_json"].encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != row["payload_sha256"]:
            raise ShareConflict("share payload integrity check failed")
        try:
            payload = SharePayload.model_validate_json(encoded)
        except ValueError:
            raise ShareConflict("share payload is invalid") from None
        if payload.share_id != row["share_id"]:
            raise ShareConflict("share payload identity is invalid")
        if (
            payload.thread_id != row["thread_id"]
            or payload.source_watermark != row["source_watermark"]
            or _iso(payload.created_at) != row["created_at"]
            or _iso(payload.expires_at) != row["expires_at"]
        ):
            raise ShareConflict("share payload metadata is invalid")
        return payload

    @staticmethod
    def _validate_diagnostic(row: sqlite3.Row) -> DiagnosticPayload:
        encoded = row["payload_json"].encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != row["payload_sha256"]:
            raise ShareConflict("diagnostic payload integrity check failed")
        try:
            payload = DiagnosticPayload.model_validate_json(encoded)
        except ValueError:
            raise ShareConflict("diagnostic payload is invalid") from None
        if payload.diagnostic_id != row["diagnostic_id"]:
            raise ShareConflict("diagnostic payload identity is invalid")
        if (
            payload.thread_id != row["thread_id"]
            or payload.source_watermark != row["source_watermark"]
            or payload.reason_code != row["reason_code"]
            or _iso(payload.created_at) != row["created_at"]
        ):
            raise ShareConflict("diagnostic payload metadata is invalid")
        return payload

    @staticmethod
    def _effective_expiry(
        row: sqlite3.Row,
        *,
        now: datetime,
    ) -> sqlite3.Row | dict[str, object]:
        expires_at = _read_time(row["expires_at"])
        if (
            expires_at is None
            or expires_at > now
            or row["status"]
            in {ShareStatus.REVOKED.value, ShareStatus.EXPIRED.value}
        ):
            return row
        projected = dict(row)
        projected.update(
            {
                "status": ShareStatus.EXPIRED.value,
                "public_url": None,
                "error_code": None,
                "updated_at": _iso(expires_at),
            }
        )
        return projected

    @staticmethod
    def _expire_if_needed(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: datetime,
    ) -> sqlite3.Row:
        expires_at = _read_time(row["expires_at"])
        if (
            expires_at is not None
            and expires_at <= now
            and row["status"]
            not in {ShareStatus.REVOKED.value, ShareStatus.EXPIRED.value}
        ):
            connection.execute(
                "UPDATE share_snapshots SET status=?, public_url=NULL, error_code=NULL, "
                "updated_at=? WHERE share_id=? AND account_id=?",
                (
                    ShareStatus.EXPIRED.value,
                    _iso(now),
                    row["share_id"],
                    row["account_id"],
                ),
            )
            refreshed = connection.execute(
                "SELECT * FROM share_snapshots WHERE share_id=? AND account_id=?",
                (row["share_id"], row["account_id"]),
            ).fetchone()
            if refreshed is None:  # pragma: no cover - protected by transaction
                raise ShareNotFound("share snapshot was not found")
            return refreshed
        return row

    @staticmethod
    def _projection(
        row: sqlite3.Row | dict[str, object],
    ) -> ShareSnapshotProjection:
        try:
            status = ShareStatus(row["status"])
            stored_public_url = row["public_url"]
            public_url = stored_public_url if status is ShareStatus.PUBLISHED else None
            remote_snapshot_id = row["remote_snapshot_id"]
            if status is ShareStatus.PUBLISHED:
                if not remote_snapshot_id:
                    raise ValueError("published share has no remote identity")
                PublishedShare(
                    remote_snapshot_id=remote_snapshot_id,
                    public_url=public_url,
                )
            elif stored_public_url is not None:
                raise ValueError("inactive share exposes a public URL")
            if status is ShareStatus.PUBLISHING and remote_snapshot_id is not None:
                raise ValueError("unpublished share exposes a remote identity")
            return ShareSnapshotProjection(
                share_id=row["share_id"],
                thread_id=row["thread_id"],
                source_watermark=row["source_watermark"],
                status=status,
                public_url=public_url,
                expires_at=_read_time(row["expires_at"]),
                created_at=_read_time(row["created_at"]),
                updated_at=_read_time(row["updated_at"]),
                revoked_at=_read_time(row["revoked_at"]),
                error_code=row["error_code"],
            )
        except (TypeError, ValueError):
            raise ShareConflict("share projection integrity check failed") from None

    @staticmethod
    def _diagnostic_projection(row: sqlite3.Row) -> DiagnosticSnapshotProjection:
        return DiagnosticSnapshotProjection(
            diagnostic_id=row["diagnostic_id"],
            thread_id=row["thread_id"],
            source_watermark=row["source_watermark"],
            reason_code=row["reason_code"],
            created_at=_read_time(row["created_at"]),
        )
