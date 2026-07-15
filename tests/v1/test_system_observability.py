from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json

from fastapi.testclient import TestClient
import pytest

from ecorex.memory import MemoryService
from ecorex.observability import (
    RuntimeSignalRegistry,
    SystemObservabilityService,
    SystemObservabilitySupervisor,
)
from ecorex.protocol import CreateThreadRequest, CreateTurnRequest
from ecorex.runtime import (
    RuntimeExecutionDenied,
    RuntimeExecutionGate,
    RuntimeKernel,
    RuntimeSettings,
    create_app,
)
from ecorex.runtime.commit_guard import transaction_commit_guard
from ecorex.runtime.sqlite_connection import TransactionSafeConnection


def test_live_signal_registry_is_thread_safe_bounded_and_monotonic() -> None:
    registry = RuntimeSignalRegistry()
    registry.sse_connected()
    registry.sse_connected()
    registry.sse_events_sent(7)
    registry.observe_event_loop_lag(123.4567)
    registry.sse_disconnected()

    snapshot = registry.snapshot()
    assert snapshot.sse_connections == 1
    assert snapshot.sse_peak_connections == 2
    assert snapshot.sse_events_sent == 7
    assert snapshot.sse_disconnects == 1
    assert snapshot.event_loop_lag_ms == 123.4567


def test_system_sample_covers_runtime_storage_memory_and_redacted_services(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread(CreateThreadRequest(title="health"))
    kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="排队任务", client_message_id="health-message"),
    )
    memory = MemoryService(kernel.database)
    with memory.database.transaction() as connection:
        connection.execute(
            "INSERT INTO memory_canonical_records("
            "record_id,legacy_chunk_id,scope,source,path,start_line,end_line,text,legacy_hash"
            ") VALUES('memory-1','legacy-memory-1','user','learned','memory/user.md',1,1,'偏好','h')"
        )

    registry = RuntimeSignalRegistry()
    registry.sse_connected()
    registry.sse_events_sent(5)
    service = SystemObservabilityService(
        kernel.database,
        registry=registry,
        providers={
            "connector": lambda: {
                "state": "ready",
                "access_token": "secret-token-value",
                "path": str(tmp_path / "private"),
            }
        },
    )
    sample = service.collect()
    payload = sample.to_dict(technical=True)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert sample.overall == "healthy"
    assert payload["metrics"]["storage"]["jobs"]["queued"] == 1
    assert payload["metrics"]["storage"]["memory_active"] == 1
    assert payload["metrics"]["runtime"]["sse_connections"] == 1
    assert "secret-token-value" not in encoded
    assert str(tmp_path) not in encoded
    assert "[REDACTED:SECRET]" in encoded
    assert "[REDACTED:PATH:" in encoded
    assert "metrics" not in sample.to_dict(technical=False)


def test_health_transition_is_audited_and_survives_service_restart(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    registry = RuntimeSignalRegistry()
    service = SystemObservabilityService(kernel.database, registry=registry)
    healthy = service.collect()
    registry.observe_event_loop_lag(1500)
    attention = service.collect()
    assert healthy.overall == "healthy"
    assert attention.overall == "attention"

    restarted = SystemObservabilityService(kernel.database, registry=registry)
    latest = restarted.latest(collect_if_missing=False)
    assert latest is not None
    assert latest.sample_id == attention.sample_id
    assert latest.overall == "attention"
    assert len(restarted.history(limit=2)) == 2
    with kernel.database.reader() as connection:
        rows = connection.execute(
            "SELECT from_status,to_status FROM system_health_events ORDER BY created_at,event_id"
        ).fetchall()
        assert [(row["from_status"], row["to_status"]) for row in rows] == [
            (None, "healthy"),
            ("healthy", "attention"),
        ]


def test_observability_supervisor_samples_without_blocking_shutdown(tmp_path) -> None:
    async def scenario() -> None:
        kernel = RuntimeKernel(tmp_path / "runtime.db")
        service = SystemObservabilityService(kernel.database)
        supervisor = SystemObservabilitySupervisor(
            service,
            collect_interval_seconds=1,
            lag_interval_seconds=0.05,
        )
        await supervisor.start()
        await asyncio.sleep(0.14)
        await supervisor.stop()
        assert service.latest(collect_if_missing=False) is not None

    asyncio.run(scenario())


def test_system_sample_rolls_back_when_runtime_epoch_closes_at_commit(
    tmp_path, monkeypatch
) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    gate = RuntimeExecutionGate()
    gate.record_report(kernel.invariants.audit())

    @contextmanager
    def persistence_scope():
        with gate.new_admission(
            scope="system_observability",
            subject="health_sample",
        ) as permit, transaction_commit_guard(lambda: gate.assert_permit(permit)):
            yield

    service = SystemObservabilityService(
        kernel.database,
        persistence_allowed=lambda: gate.snapshot().healthy,
        persistence_scope=persistence_scope,
    )
    original_commit = TransactionSafeConnection.commit
    closed = False

    def close_epoch_at_dirty_commit(connection) -> None:
        nonlocal closed
        if (
            not closed
            and connection.in_transaction
            and connection.total_changes
            != connection._ecorex_last_finished_changes
        ):
            closed = True
            gate.mark_critical(error_code="test_system_sample_commit_race")
        original_commit(connection)

    monkeypatch.setattr(TransactionSafeConnection, "commit", close_epoch_at_dirty_commit)

    with pytest.raises(RuntimeExecutionDenied):
        service.collect()

    assert closed is True
    with kernel.database.reader() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM system_metric_samples"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM system_health_state"
        ).fetchone()[0] == 0


def test_latest_health_projection_does_not_create_a_sample_on_get(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    service = SystemObservabilityService(kernel.database)
    with kernel.database.reader() as connection:
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM system_metric_samples"
            ).fetchone()[0]
        ) == 0
    projected = service.latest()
    assert projected is not None
    with kernel.database.reader() as connection:
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM system_metric_samples"
            ).fetchone()[0]
        ) == 0
    persisted = service.collect()
    assert persisted is not None
    with kernel.database.reader() as connection:
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM system_metric_samples"
            ).fetchone()[0]
        ) == 1


def test_runtime_health_api_counts_sse_without_exposing_metrics_by_default(tmp_path) -> None:
    token = "r" * 32
    csrf = "c" * 32
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=token,
            csrf_token=csrf,
            webui_origins=("http://testserver",),
        )
    )
    auth = {"Authorization": f"Bearer {token}"}
    mutation = {
        **auth,
        "Origin": "http://testserver",
        "X-EcoreX-CSRF": csrf,
    }
    with TestClient(app) as client:
        thread_id = client.post(
            "/api/v1/threads", json={"title": "system health"}, headers=mutation
        ).json()["thread_id"]
        response = client.get(
            f"/api/v1/threads/{thread_id}/events",
            params={"follow": "false"},
            headers={**auth, "Accept": "text/event-stream"},
        )
        assert response.status_code == 200

        public_health = client.get("/api/v1/system/health", headers=auth)
        assert public_health.status_code == 200
        assert public_health.json()["summary"].startswith("EcoreX")
        assert "metrics" not in public_health.json()

        technical = client.get(
            "/api/v1/system/health", params={"technical": "true"}, headers=auth
        ).json()
        runtime = technical["metrics"]["runtime"]
        assert runtime["sse_connections"] == 0
        assert runtime["sse_peak_connections"] == 1
        assert runtime["sse_events_sent"] >= 1
        assert runtime["sse_disconnects"] == 1
        providers = technical["metrics"]["services"]
        assert {
            "audit",
            "traces",
            "shares",
            "retouch",
            "device_authorization",
            "images",
            "artifact_events",
        } <= set(providers)
        assert isinstance(providers["audit"]["pending"], int)
        assert isinstance(providers["images"]["publications"], dict)
