from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ecorex.observability import (
    ManagedOTLPHTTPTraceExporter,
    PermanentTraceExportError,
    RetryableTraceExportError,
    TraceExportBatch,
    TraceOutbox,
)
from ecorex.protocol import ItemKind, ItemStatus, TurnStatus
from ecorex.runtime import RuntimeSettings, create_app


TOKEN = "managed-trace-token-12345678901234567890"
RUNTIME_TOKEN = "r" * 43
CSRF_TOKEN = "c" * 43
MUTATION = {
    "Authorization": f"Bearer {RUNTIME_TOKEN}",
    "Origin": "http://testserver",
    "X-ECoreX-CSRF": CSRF_TOKEN,
}


@dataclass(frozen=True)
class _Snapshot:
    account_id: str = "account-1"
    lease_digest: str = "b" * 64
    generation: int = 3


class _Session:
    def __init__(self, *, account_id: str = "account-1") -> None:
        self.value = _Snapshot(account_id=account_id)

    def snapshot(self):
        return self.value

    def bearer_token(self):
        return TOKEN


def _payload(*, marker: str = "safe") -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "ecorex-runtime"},
                        }
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "ecorex.runtime.event-store"},
                        "spans": [
                            {
                                "traceId": "1" * 32,
                                "spanId": "2" * 16,
                                "name": "ecorex.turn",
                                "kind": 1,
                                "startTimeUnixNano": "1783670400000000000",
                                "endTimeUnixNano": "1783670401000000000",
                                "attributes": [
                                    {
                                        "key": "test.marker",
                                        "value": {"stringValue": marker},
                                    }
                                ],
                                "status": {"code": 1},
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _batch(*, payload: dict | None = None, account_id: str = "account-1"):
    body = json.dumps(
        payload or _payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return TraceExportBatch(
        batch_id="tracebatch_" + "a" * 64,
        account_id=account_id,
        thread_id="thread_01J00000000000000000000000",
        segment_kind="turn",
        segment_id="turn_01J00000000000000000000000",
        through_seq=12,
        event_digest="d" * 64,
        chunk_index=0,
        chunk_count=1,
        span_count=1,
        payload=payload or _payload(),
        payload_sha256=hashlib.sha256(body).hexdigest(),
        attempts=0,
        created_at=datetime(2026, 7, 10, 12, tzinfo=UTC),
    )


def test_managed_otlp_json_export_is_fixed_bounded_and_proto3_compatible() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )

    exporter = ManagedOTLPHTTPTraceExporter(
        endpoint="https://otel.example/v1/traces",
        allowed_hosts=frozenset({"OTEL.EXAMPLE"}),
        session=_Session(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    batch = _batch()
    exporter.publish(batch)

    assert captured["url"] == "https://otel.example/v1/traces"
    headers = captured["headers"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "application/json"
    assert headers["accept-encoding"] == "identity"
    assert headers["idempotency-key"] == batch.batch_id
    sent = json.loads(captured["body"])
    span = sent["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert len(span["traceId"]) == 32
    assert len(span["spanId"]) == 16
    assert span["kind"] == 1
    assert isinstance(span["startTimeUnixNano"], str)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://otel.example/v1/traces",
        "https://other.example/v1/traces",
        "https://otel.example:4318/v1/traces",
        "https://otel.example/v1/metrics",
        "https://user:secret@otel.example/v1/traces",
        "https://otel.example/v1/traces?token=secret",
    ],
)
def test_otlp_endpoint_fails_closed(endpoint: str) -> None:
    with pytest.raises(ValueError, match="allowlisted HTTPS"):
        ManagedOTLPHTTPTraceExporter(
            endpoint=endpoint,
            allowed_hosts=frozenset({"otel.example"}),
            session=_Session(),
        )


def test_otlp_partial_success_is_not_misreported_as_success() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"partialSuccess": {"rejectedSpans": "1", "errorMessage": "bad"}},
        )

    exporter = ManagedOTLPHTTPTraceExporter(
        endpoint="https://otel.example/v1/traces",
        allowed_hosts=frozenset({"otel.example"}),
        session=_Session(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(PermanentTraceExportError) as error:
        exporter.publish(_batch())
    assert error.value.code == "partial_success"
    assert error.value.retryable is False


def test_otlp_retryable_status_preserves_bounded_retry_after() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"Retry-After": "999"})

    exporter = ManagedOTLPHTTPTraceExporter(
        endpoint="https://otel.example/v1/traces",
        allowed_hosts=frozenset({"otel.example"}),
        session=_Session(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RetryableTraceExportError) as error:
        exporter.publish(_batch())
    assert error.value.code == "remote_retryable"
    assert error.value.retry_after_seconds == 300


def test_otlp_retry_after_http_date_is_honored_and_bounded() -> None:
    retry_at = format_datetime(datetime.now(UTC) + timedelta(minutes=10), usegmt=True)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": retry_at})

    exporter = ManagedOTLPHTTPTraceExporter(
        endpoint="https://otel.example/v1/traces",
        allowed_hosts=frozenset({"otel.example"}),
        session=_Session(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RetryableTraceExportError) as error:
        exporter.publish(_batch())
    assert error.value.retry_after_seconds == 300


def test_otlp_rejects_invalid_identifiers_and_unredacted_paths_before_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, headers={"Content-Type": "application/json"}, content=b"{}"
        )

    exporter = ManagedOTLPHTTPTraceExporter(
        endpoint="https://otel.example/v1/traces",
        allowed_hosts=frozenset({"otel.example"}),
        session=_Session(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    invalid = _payload()
    invalid["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = "z" * 32
    with pytest.raises(PermanentTraceExportError) as identifier_error:
        exporter.publish(_batch(payload=invalid))
    assert identifier_error.value.code == "invalid_payload"

    unsafe = _payload(marker="C:\\Users\\secret\\trace.txt")
    with pytest.raises(PermanentTraceExportError) as unsafe_error:
        exporter.publish(_batch(payload=unsafe))
    assert unsafe_error.value.code == "unsafe_payload"
    assert calls == 0


class _RecordingPublisher:
    def __init__(self) -> None:
        self.batches: list[TraceExportBatch] = []

    def publish(self, batch: TraceExportBatch) -> None:
        self.batches.append(batch)


class _RetryingPublisher:
    def publish(self, _batch: TraceExportBatch) -> None:
        raise RetryableTraceExportError(
            "collector_busy",
            "collector busy",
            retry_after_seconds=300,
        )


def _complete_turn(app, turn_id: str) -> None:
    kernel = app.state.runtime
    kernel.transition_turn(turn_id, TurnStatus.PREPARING)
    kernel.transition_turn(turn_id, TurnStatus.MODEL_REQUESTED)
    kernel.transition_turn(turn_id, TurnStatus.STREAMING)
    item = kernel.create_item(
        turn_id=turn_id,
        kind=ItemKind.MESSAGE,
        status=ItemStatus.IN_PROGRESS,
        content={"role": "assistant", "text": ""},
    )
    kernel.append_message_delta(
        item.item_id,
        "C:\\Users\\secret\\report.docx bearer should-not-leak",
        idempotency_key=f"{turn_id}:trace-sensitive-delta",
    )
    kernel.transition_item(item.item_id, ItemStatus.COMPLETED)
    kernel.transition_turn(turn_id, TurnStatus.FINALIZING)
    kernel.transition_turn(turn_id, TurnStatus.COMPLETED)


def test_terminal_turn_is_transactionally_materialized_redacted_and_recoverable(
    tmp_path,
) -> None:
    publisher = _RecordingPublisher()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            account_id="account-1",
            runtime_bearer_token=RUNTIME_TOKEN,
            csrf_token=CSRF_TOKEN,
            webui_origins=("http://testserver",),
            trace_exporter=publisher,
            trace_max_spans_per_batch=2,
            trace_max_request_bytes=16 * 1024,
        )
    )
    client = TestClient(app)
    thread_id = client.post(
        "/api/v1/threads", json={"title": "OTLP"}, headers=MUTATION
    ).json()["thread_id"]
    turn_id = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"input": "write report", "client_message_id": "trace-message"},
        headers=MUTATION,
    ).json()["turn"]["turn_id"]
    _complete_turn(app, turn_id)

    outbox = app.state.trace_outbox
    with app.state.runtime.database.reader() as connection:
        segment = connection.execute(
            "SELECT * FROM observability_trace_segments WHERE segment_id = ?",
            (turn_id,),
        ).fetchone()
        assert segment is not None
        terminal_seq = int(segment["target_seq"])
    result = outbox.materialize()
    assert result.materialized_segments == 1
    assert result.created_batches >= 1
    assert outbox.count(pending_only=True) == result.created_batches

    outbox.publisher = _RetryingPublisher()
    retry = asyncio.run(outbox.drain(limit=1))
    assert retry.retry_scheduled == 1
    assert retry.published == 0
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "UPDATE observability_trace_outbox SET next_attempt_at = ? "
            "WHERE published_at IS NULL AND rejected_at IS NULL",
            ("2020-01-01T00:00:00+00:00",),
        )
    claimed = outbox._claim_next()
    assert claimed is not None
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "UPDATE observability_trace_outbox SET lease_expires_at = ? "
            "WHERE batch_id = ?",
            ("2020-01-01T00:00:00+00:00", claimed[0].batch_id),
        )
    recovered = TraceOutbox(
        app.state.runtime.database,
        account_id="account-1",
        cipher=app.state.audit_outbox.cipher,
        projector=app.state.trace_projector,
        publisher=publisher,
        max_spans_per_batch=2,
        max_request_bytes=16 * 1024,
    )
    drained = asyncio.run(recovered.drain(limit=32))
    assert drained.published == result.created_batches
    assert recovered.count(pending_only=True) == 0
    assert sum(batch.span_count for batch in publisher.batches) >= 1
    assert {batch.through_seq for batch in publisher.batches} == {terminal_seq}
    payload_text = json.dumps(
        [batch.payload for batch in publisher.batches], ensure_ascii=False
    )
    assert "C:\\Users\\secret" not in payload_text
    assert "should-not-leak" not in payload_text
    assert all(
        len(batch.payload["resourceSpans"][0]["scopeSpans"][0]["spans"]) <= 2
        for batch in publisher.batches
    )
    with app.state.runtime.database.reader() as connection:
        stored = connection.execute(
            "SELECT payload_json, payload_format FROM observability_trace_outbox LIMIT 1"
        ).fetchone()
    assert stored["payload_format"] == "aesgcm-v1"
    assert "resourceSpans" not in stored["payload_json"]


def test_nonterminal_events_do_not_export_unfinished_spans(tmp_path) -> None:
    publisher = _RecordingPublisher()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            account_id="account-1",
            runtime_bearer_token=RUNTIME_TOKEN,
            csrf_token=CSRF_TOKEN,
            webui_origins=("http://testserver",),
            trace_exporter=publisher,
        )
    )
    client = TestClient(app)
    thread_id = client.post(
        "/api/v1/threads", json={"title": "Pending"}, headers=MUTATION
    ).json()["thread_id"]
    client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"input": "pending", "client_message_id": "pending-message"},
        headers=MUTATION,
    )
    result = app.state.trace_outbox.materialize()
    assert result.attempted_segments == 0
    assert app.state.trace_outbox.count() == 0


def test_corrupt_encrypted_batch_is_quarantined_without_starving_next_trace(
    tmp_path,
) -> None:
    publisher = _RecordingPublisher()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            account_id="account-1",
            runtime_bearer_token=RUNTIME_TOKEN,
            csrf_token=CSRF_TOKEN,
            webui_origins=("http://testserver",),
            trace_exporter=publisher,
        )
    )
    client = TestClient(app)
    for index in range(2):
        thread_id = client.post(
            "/api/v1/threads",
            json={"title": f"Trace {index}"},
            headers=MUTATION,
        ).json()["thread_id"]
        turn_id = client.post(
            f"/api/v1/threads/{thread_id}/turns",
            json={
                "input": "complete",
                "client_message_id": f"trace-message-{index}",
            },
            headers=MUTATION,
        ).json()["turn"]["turn_id"]
        _complete_turn(app, turn_id)
    outbox = app.state.trace_outbox
    result = outbox.materialize()
    assert result.created_batches == 2
    with app.state.runtime.database.transaction() as connection:
        first = connection.execute(
            "SELECT batch_id FROM observability_trace_outbox "
            "ORDER BY created_at, batch_id LIMIT 1"
        ).fetchone()
        connection.execute(
            "UPDATE observability_trace_outbox SET payload_json = ? WHERE batch_id = ?",
            ("tampered-ciphertext", first["batch_id"]),
        )

    drained = asyncio.run(outbox.drain(limit=8))
    assert drained.attempted == 2
    assert drained.rejected == 1
    assert drained.published == 1
    assert drained.pending == 0
    assert len(publisher.batches) == 1
    with app.state.runtime.database.reader() as connection:
        rejected = connection.execute(
            "SELECT rejected_at, last_error_code FROM observability_trace_outbox "
            "WHERE batch_id = ?",
            (first["batch_id"],),
        ).fetchone()
    assert rejected["rejected_at"] is not None
    assert rejected["last_error_code"] == "payload_integrity"


def test_restart_backfill_reads_event_history_in_bounded_pages(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            account_id="account-1",
            runtime_bearer_token=RUNTIME_TOKEN,
            csrf_token=CSRF_TOKEN,
            webui_origins=("http://testserver",),
        )
    )
    client = TestClient(app)
    thread_id = client.post(
        "/api/v1/threads", json={"title": "Backfill"}, headers=MUTATION
    ).json()["thread_id"]
    turn_id = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"input": "complete", "client_message_id": "backfill-message"},
        headers=MUTATION,
    ).json()["turn"]["turn_id"]
    _complete_turn(app, turn_id)
    watermark = app.state.runtime.events.watermark(thread_id)

    recovered = TraceOutbox(
        app.state.runtime.database,
        account_id="account-1",
        cipher=app.state.audit_outbox.cipher,
        projector=app.state.trace_projector,
        publisher=_RecordingPublisher(),
    )
    assert recovered.backfill_events(batch_size=1) == watermark
    assert recovered.backfill_events(batch_size=1) == 0
    result = recovered.materialize()
    assert result.materialized_segments == 1
    assert result.created_batches == 1
