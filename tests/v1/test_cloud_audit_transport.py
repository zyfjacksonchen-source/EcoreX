from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from ecorex.control_plane import (
    CloudAuditConflict,
    CloudAuditIntegrityError,
    CloudAuditRejected,
    CloudAuditRepository,
    ControlPlaneRepository,
    ControlPrincipal,
    create_control_plane_app,
    migrate_cloud_audit_database,
    migrate_control_plane_database,
)
from ecorex.observability import (
    AuditOutbox,
    AuditPayloadCipher,
    AuditRetentionPolicy,
    ManagedHTTPSAuditPublisher,
    PermanentAuditPublishError,
    RetryableAuditPublishError,
)
from ecorex.protocol import AuditRecordProjection
from ecorex.runtime.database import json_dumps
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.errors import SchemaVersionError


TOKEN = "managed-session-token-12345678901234567890"


def _record(
    *,
    audit_id: str = "audit_" + "a" * 64,
    account_id: str = "account-1",
    payload: dict | None = None,
    attempts: int = 1,
    created_at: datetime | None = None,
) -> AuditRecordProjection:
    safe_payload = payload or {
        "operation": "document.preview",
        "path": "[REDACTED:PATH:123456789abc]",
        "access_token": "[REDACTED:SECRET]",
    }
    digest = hashlib.sha256(json_dumps(safe_payload).encode("utf-8")).hexdigest()
    return AuditRecordProjection(
        audit_id=audit_id,
        source_event_id="evt_01J00000000000000000000000",
        category="artifact",
        event_type="artifact.previewed",
        account_id=account_id,
        thread_id="thread_01J00000000000000000000000",
        turn_id="turn_01J00000000000000000000000",
        trace_id="1" * 32,
        payload=safe_payload,
        payload_sha256=digest,
        binary_included=False,
        delivery_status="pending",
        attempts=attempts,
        created_at=created_at or datetime(2026, 7, 10, 8, 30, tzinfo=UTC),
    )


@dataclass(frozen=True)
class _Snapshot:
    account_id: str = "account-1"
    lease_digest: str = "b" * 64
    generation: int = 7


class _Session:
    def __init__(self, *, account_id: str = "account-1") -> None:
        self.value = _Snapshot(account_id=account_id)

    def snapshot(self):
        return self.value

    def bearer_token(self):
        return TOKEN


class _Verifier:
    def verify(self, _payload, _signature):
        return True


class _Authenticator:
    principals = {
        TOKEN: ControlPrincipal(
            subject="runtime-client",
            client_id="client-1",
            account_id="account-1",
            organization_id="org-1",
        ),
        "other-account-token-123456789012345": ControlPrincipal(
            subject="other-client",
            client_id="client-2",
            account_id="account-2",
        ),
        "audit-admin-token-1234567890123456": ControlPrincipal(
            subject="auditor-1",
            client_id="admin-1",
            account_id="admin-account",
            roles=frozenset({"audit_admin"}),
        ),
        "release-admin-token-12345678901234": ControlPrincipal(
            subject="release-admin",
            client_id="admin-2",
            account_id="admin-account",
            roles=frozenset({"release_admin"}),
        ),
    }

    def authenticate(self, bearer_token: str) -> ControlPrincipal:
        try:
            return self.principals[bearer_token]
        except KeyError as error:
            raise PermissionError("invalid token") from error


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _audit_repository(tmp_path, **updates) -> CloudAuditRepository:
    values = {
        "path": tmp_path / "control.db",
        "encryption_key": b"e" * 32,
        "integrity_key": b"h" * 32,
    }
    values.update(updates)
    migrate_cloud_audit_database(values["path"])
    return CloudAuditRepository(**values)


def _app(tmp_path):
    database = tmp_path / "control.db"
    migrate_control_plane_database(database)
    migrate_cloud_audit_database(database)
    audit = CloudAuditRepository(
        database,
        encryption_key=b"e" * 32,
        integrity_key=b"h" * 32,
    )
    release = ControlPlaneRepository(database, verifier=_Verifier())
    return create_control_plane_app(
        release,
        authenticator=_Authenticator(),
        audit_repository=audit,
    ), audit


def test_managed_https_publisher_uses_fixed_endpoint_identity_and_bounded_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(201, json={"created": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    record = _record()
    publisher = ManagedHTTPSAuditPublisher(
        base_url="https://audit.example",
        allowed_hosts=frozenset({"AUDIT.EXAMPLE"}),
        session=_Session(),
        client=client,
    )
    publisher.publish(record)

    assert captured["url"] == "https://audit.example/api/v1/audit/records"
    headers = captured["headers"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["idempotency-key"] == record.audit_id
    assert headers["accept-encoding"] == "identity"
    assert json.loads(captured["body"]) == record.model_dump(mode="json")


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    [
        (401, RetryableAuditPublishError, True),
        (429, RetryableAuditPublishError, True),
        (503, RetryableAuditPublishError, True),
        (400, PermanentAuditPublishError, False),
        (403, PermanentAuditPublishError, False),
        (409, PermanentAuditPublishError, False),
    ],
)
def test_publisher_classifies_remote_failures_without_echoing_response(
    status, error_type, retryable
) -> None:
    secret = "server-secret-never-echo"
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, text=secret)
        )
    )
    publisher = ManagedHTTPSAuditPublisher(
        base_url="https://audit.example",
        allowed_hosts=frozenset({"audit.example"}),
        session=_Session(),
        client=client,
    )
    with pytest.raises(error_type) as captured:
        publisher.publish(_record())
    assert captured.value.retryable is retryable
    assert secret not in str(captured.value)
    assert TOKEN not in str(captured.value)


def test_publisher_fails_closed_for_account_redirect_compression_and_origin() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(201)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    publisher = ManagedHTTPSAuditPublisher(
        base_url="https://audit.example/",
        allowed_hosts=frozenset({"audit.example"}),
        session=_Session(account_id="different-account"),
        client=client,
    )
    with pytest.raises(PermanentAuditPublishError, match="account"):
        publisher.publish(_record())
    assert calls == 0

    for response in (
        httpx.Response(307, headers={"Location": "https://evil.example"}),
        httpx.Response(201, headers={"Content-Encoding": "gzip"}, content=b"x"),
        httpx.Response(201, content=b"x" * (64 * 1024 + 1)),
    ):
        blocked = ManagedHTTPSAuditPublisher(
            base_url="https://audit.example",
            allowed_hosts=frozenset({"audit.example"}),
            session=_Session(),
            client=httpx.Client(
                transport=httpx.MockTransport(lambda _request, r=response: r)
            ),
        )
        with pytest.raises(PermanentAuditPublishError):
            blocked.publish(_record())

    with pytest.raises(ValueError, match="allowlisted HTTPS origin"):
        ManagedHTTPSAuditPublisher(
            base_url="https://evil.example",
            allowed_hosts=frozenset({"audit.example"}),
            session=_Session(),
            client=client,
        )


def test_repository_encrypts_payload_and_idempotently_ignores_local_attempts(tmp_path) -> None:
    repository = _audit_repository(tmp_path)
    principal = _Authenticator.principals[TOKEN]
    record = _record(attempts=1)
    created = repository.ingest(principal, record, idempotency_key=record.audit_id)
    replayed = repository.ingest(
        principal,
        record.model_copy(update={"attempts": 9}),
        idempotency_key=record.audit_id,
    )
    assert created.created is True
    assert replayed.created is False

    connection = sqlite3.connect(tmp_path / "control.db")
    try:
        envelope, payload_format = connection.execute(
            "SELECT payload_envelope, payload_format FROM cloud_audit_records"
        ).fetchone()
    finally:
        connection.close()
    assert payload_format == "aesgcm-v1"
    assert "document.preview" not in envelope
    assert TOKEN not in envelope

    changed = _record(payload={"operation": "different"})
    with pytest.raises(CloudAuditConflict):
        repository.ingest(principal, changed, idempotency_key=changed.audit_id)


def test_ingestion_rejects_account_digest_secret_path_and_binary_before_storage(tmp_path) -> None:
    repository = _audit_repository(tmp_path)
    principal = _Authenticator.principals[TOKEN]
    record = _record()
    with pytest.raises(CloudAuditRejected, match="account"):
        repository.ingest(
            _Authenticator.principals["other-account-token-123456789012345"],
            record,
            idempotency_key=record.audit_id,
        )
    with pytest.raises(CloudAuditRejected, match="idempotency"):
        repository.ingest(principal, record, idempotency_key="wrong")

    tampered_digest = record.model_copy(update={"payload_sha256": "0" * 64})
    with pytest.raises(CloudAuditRejected, match="digest"):
        repository.ingest(
            principal, tampered_digest, idempotency_key=tampered_digest.audit_id
        )
    for unsafe in (
        {"authorization": "Bearer raw-secret-123456"},
        {"local_path": "C:\\Users\\alice\\private.docx"},
        {"image_data": "A" * 1024},
    ):
        candidate = _record(payload=unsafe)
        with pytest.raises(CloudAuditRejected, match="safety"):
            repository.ingest(principal, candidate, idempotency_key=candidate.audit_id)


def test_control_plane_ingestion_rbac_query_and_access_audit(tmp_path) -> None:
    app, repository = _app(tmp_path)
    client = TestClient(app)
    record = _record()
    body = record.model_dump(mode="json")

    assert client.post("/api/v1/audit/records", json=body).status_code == 401
    wrong = client.post(
        "/api/v1/audit/records",
        json=body,
        headers={
            **_headers("other-account-token-123456789012345"),
            "Idempotency-Key": record.audit_id,
        },
    )
    assert wrong.status_code == 422
    assert "account-1" not in wrong.text
    created = client.post(
        "/api/v1/audit/records",
        json=body,
        headers={**_headers(TOKEN), "Idempotency-Key": record.audit_id},
    )
    assert created.status_code == 201
    replayed = client.post(
        "/api/v1/audit/records",
        json={**body, "attempts": 99},
        headers={**_headers(TOKEN), "Idempotency-Key": record.audit_id},
    )
    assert replayed.status_code == 200
    assert replayed.json()["created"] is False

    release_admin = _headers("release-admin-token-12345678901234")
    assert client.get(
        "/api/v1/admin/audit/records", headers=release_admin
    ).status_code == 403
    audit_admin = _headers("audit-admin-token-1234567890123456")
    listed = client.get("/api/v1/admin/audit/records", headers=audit_admin)
    assert listed.status_code == 200
    assert listed.json()["records"][0]["audit_id"] == record.audit_id
    assert "payload" not in listed.json()["records"][0]
    detail = client.get(
        f"/api/v1/admin/audit/records/{record.audit_id}", headers=audit_admin
    )
    assert detail.status_code == 200
    assert detail.json()["payload"] == record.payload
    actions = [entry.action for entry in repository.integrity_entries()]
    assert actions == [
        "audit.ingest.created",
        "audit.ingest.replayed",
        "audit.metadata.queried",
        "audit.payload.accessed",
    ]


def test_ingestion_validation_and_body_limit_never_echo_sensitive_input(tmp_path) -> None:
    app, _repository = _app(tmp_path)
    client = TestClient(app)
    secret = "raw-secret-must-not-return"
    invalid = client.post(
        "/api/v1/audit/records",
        json={"payload": {"access_token": secret}},
        headers={**_headers(TOKEN), "Idempotency-Key": "audit-invalid"},
    )
    assert invalid.status_code == 422
    assert secret not in invalid.text
    oversized = client.post(
        "/api/v1/audit/records",
        content=b"{" + b"x" * (1024 * 1024) + b"}",
        headers={
            **_headers(TOKEN),
            "Idempotency-Key": "audit-oversized",
            "Content-Type": "application/json",
        },
    )
    assert oversized.status_code == 413
    assert secret not in oversized.text


def test_cipher_and_hmac_tampering_fail_closed(tmp_path) -> None:
    repository = _audit_repository(tmp_path)
    principal = _Authenticator.principals[TOKEN]
    record = _record()
    repository.ingest(principal, record, idempotency_key=record.audit_id)

    connection = sqlite3.connect(tmp_path / "control.db")
    try:
        connection.execute(
            "UPDATE cloud_audit_records SET payload_envelope = ? WHERE audit_id = ?",
            ("{}", record.audit_id),
        )
    except sqlite3.IntegrityError:
        # Immutability trigger is the first line of defense.  Drop it only to
        # emulate out-of-process disk tampering and exercise AES-GCM verification.
        connection.rollback()
        connection.execute("DROP TRIGGER cloud_audit_records_no_update")
        connection.execute(
            "UPDATE cloud_audit_records SET payload_envelope = ? WHERE audit_id = ?",
            ("{}", record.audit_id),
        )
    connection.commit()
    connection.close()
    with pytest.raises(CloudAuditIntegrityError):
        repository.get_detail(
            _Authenticator.principals["audit-admin-token-1234567890123456"],
            record.audit_id,
        )

    second_path = tmp_path / "hmac.db"
    migrate_cloud_audit_database(second_path)
    second = CloudAuditRepository(
        second_path, encryption_key=b"e" * 32, integrity_key=b"h" * 32
    )
    second.ingest(principal, record, idempotency_key=record.audit_id)
    connection = sqlite3.connect(second_path)
    connection.execute("DROP TRIGGER cloud_audit_integrity_no_update")
    connection.execute(
        "UPDATE cloud_audit_integrity SET entry_mac = ? WHERE sequence = 1",
        ("0" * 64,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(CloudAuditIntegrityError):
        second.integrity_entries()


def test_configurable_retention_keeps_aggregate_longer_than_raw(tmp_path) -> None:
    repository = _audit_repository(
        tmp_path,
        retention=AuditRetentionPolicy(raw_days=30, aggregate_days=180),
    )
    principal = _Authenticator.principals[TOKEN]
    now = datetime(2026, 7, 10, tzinfo=UTC)
    old = _record(created_at=now - timedelta(days=45))
    repository.ingest(principal, old, idempotency_key=old.audit_id)
    result = repository.enforce_retention(
        _Authenticator.principals["audit-admin-token-1234567890123456"],
        now=now,
    )
    assert result.raw_deleted == 1
    assert result.aggregate_deleted == 0
    assert result.idempotency_deleted == 0
    replayed = repository.ingest(principal, old, idempotency_key=old.audit_id)
    assert replayed.created is False
    aggregates = repository.list_aggregates(
        _Authenticator.principals["audit-admin-token-1234567890123456"]
    )
    assert aggregates[0].record_count == 1
    assert result.raw_days == 30
    assert result.aggregate_days == 180


def test_local_outbox_terminals_permanent_publish_failure_without_retry_loop(tmp_path) -> None:
    class RejectingPublisher:
        def publish(self, _record):
            raise PermanentAuditPublishError(
                "remote_rejected", "cloud audit publication was permanently rejected"
            )

    outbox = AuditOutbox(
        tmp_path / "runtime.db",
        account_id="account-1",
        cipher=AuditPayloadCipher(b"e" * 32),
        publisher=RejectingPublisher(),
    )
    with outbox.database.transaction() as connection:
        outbox._persist_view_in_transaction(
            connection,
            source_event_id="event-permanent-rejection",
            category="task",
            event_type="job.failed",
            thread_id="thread-1",
            turn_id="turn-1",
            trace_id="1" * 32,
            payload={"reason": "bounded"},
            created_at=datetime.now(UTC),
        )

    first = __import__("asyncio").run(outbox.drain())
    second = __import__("asyncio").run(outbox.drain())
    assert first.attempted == 1
    assert first.rejected == 1
    assert first.retry_scheduled == 0
    assert first.pending == 0
    assert second.attempted == 0
    terminal = outbox.list(status="rejected")
    assert len(terminal) == 1
    assert terminal[0].last_error_code == "remote_rejected"
    assert terminal[0].rejected_at is not None


def test_local_outbox_rejects_pre_rejected_status_schema_without_repair(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "legacy-runtime.db")
    with database.transaction() as connection:
        connection.execute("DROP INDEX idx_observability_audit_pending_v2")
        connection.execute("DROP INDEX idx_observability_audit_thread")
        connection.execute("DROP INDEX idx_observability_audit_pending")
        connection.execute("DROP TABLE observability_audit_outbox")
        connection.executescript(
            """
            CREATE TABLE observability_audit_outbox (
                audit_id TEXT PRIMARY KEY,
                source_event_id TEXT NOT NULL,
                category TEXT NOT NULL,
                event_type TEXT NOT NULL,
                account_id TEXT NOT NULL,
                thread_id TEXT,
                turn_id TEXT,
                trace_id TEXT,
                payload_json TEXT NOT NULL,
                payload_format TEXT NOT NULL DEFAULT 'aesgcm-v1',
                payload_sha256 TEXT NOT NULL,
                binary_included INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                published_at TEXT,
                last_error_code TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(source_event_id, category, event_type)
            );
            """
        )
    with pytest.raises(SchemaVersionError, match="idx_observability_audit"):
        AuditOutbox(
            database,
            account_id="account-1",
            cipher=AuditPayloadCipher(b"e" * 32),
        )
    with database.reader() as connection:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(observability_audit_outbox)"
            ).fetchall()
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                "PRAGMA index_list(observability_audit_outbox)"
            ).fetchall()
        }
    assert "rejected_at" not in columns
    assert "idx_observability_audit_pending_v2" not in indexes
