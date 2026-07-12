from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ecorex.connectors import (
    AuthChallenge,
    AuthGrant,
    ConnectorAuthKind,
    ConnectorHealth,
    ConnectorHealthResult,
    ConnectorMaintenanceSupervisor,
    ConnectorService,
    ConnectorUnavailable,
    InMemoryCredentialVault,
    SQLiteConnectorRepository,
    build_connector_composition,
    builtin_connector_registry,
    builtin_connector_definitions,
)
from ecorex.protocol import CreateThreadRequest
from ecorex.runtime import RuntimeKernel
from ecorex.runtime.invariant_guard import RuntimeExecutionGate
from ecorex.runtime.invariants import RuntimeInvariantAuditor


CALLBACK = "http://127.0.0.1:8765/api/v1/connectors/oauth/callback"


class RuntimeAdapter:
    def __init__(self) -> None:
        self.invocation_count = 0
        self.revoked = False
        self.begin_auth_count = 0
        self.health_check_count = 0

    async def begin_auth(
        self,
        *,
        flow_id: str,
        auth_kind: ConnectorAuthKind,
        return_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> AuthChallenge:
        self.begin_auth_count += 1
        assert return_uri == CALLBACK
        return AuthChallenge(
            flow_id=flow_id,
            connector_id="feishu",
            auth_kind=auth_kind,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            authorization_url=(
                "https://auth.example/authorize"
                f"?state={state}&code_challenge={code_challenge}"
                f"&code_challenge_method={code_challenge_method}"
            ),
        )

    async def complete_auth(
        self,
        *,
        flow_id: str,
        response: Mapping[str, str],
        private_state: Mapping[str, str],
    ) -> AuthGrant:
        del flow_id
        assert response["state"] == private_state["state"]
        assert private_state["pkce_verifier"]
        return AuthGrant(
            account_subject="runtime-account",
            account_display_name="运行时团队",
            granted_scopes=frozenset(
                {
                    "docx:document:readonly",
                    "docx:document",
                    "drive:drive:readonly",
                    "im:message",
                }
            ),
            credential_material={"access_token": "RUNTIME-SECRET-CREDENTIAL"},
        )

    async def check_health(
        self, credentials: Mapping[str, str]
    ) -> ConnectorHealthResult:
        self.health_check_count += 1
        assert credentials["access_token"] == "RUNTIME-SECRET-CREDENTIAL"
        return ConnectorHealthResult(ConnectorHealth.CONNECTED)

    async def invoke(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any],
        credentials: Mapping[str, str],
        idempotency_key: str | None,
    ) -> Any:
        del idempotency_key
        assert credentials["access_token"] == "RUNTIME-SECRET-CREDENTIAL"
        self.invocation_count += 1
        if action_id == "documents.read":
            document_id = str(inputs["document_id"])
            return {
                "ok": True,
                "document_id": document_id,
                "revision_id": "rev_01JABCDEFGH1234567890",
                "title": "产品方案",
                "content": "公开办公文档正文",
                "url": f"https://docs.example.test/document/{document_id}",
                "updated_at": "2026-07-10T15:34:00+08:00",
            }
        return {"ok": True, "action_id": action_id, "title": inputs.get("title")}

    async def revoke(
        self,
        *,
        credentials: Mapping[str, str],
        idempotency_key: str,
    ) -> bool:
        assert credentials["access_token"] == "RUNTIME-SECRET-CREDENTIAL"
        assert idempotency_key.startswith("ecorex-disconnect:")
        self.revoked = True
        return True


class RecordingEventSink:
    def __init__(self) -> None:
        self.event_ids: list[str] = []
        self.event_types: list[str] = []

    def publish(self, event) -> None:
        if event.event_id not in self.event_ids:
            self.event_ids.append(event.event_id)
            self.event_types.append(event.event_type)


def _enqueue_test_outbox(
    service: ConnectorService,
    aggregate_id: str,
    *,
    event_type: str = "connector.test.emitted",
) -> str:
    with service.control_admission(
        operation="test_enqueue_outbox",
        subject=aggregate_id,
    ):
        with service.repository._write() as connection:
            service.repository._append_outbox(
                connection,
                event_type=event_type,
                aggregate_id=aggregate_id,
                payload={"aggregate_id": aggregate_id, "status": "pending"},
            )
            row = connection.execute(
                "SELECT event_id FROM connector_outbox WHERE aggregate_id=? "
                "ORDER BY aggregate_seq DESC LIMIT 1",
                (aggregate_id,),
            ).fetchone()
    assert row is not None
    return str(row[0])


def _composition(tmp_path: Path, *, deny: bool = False):
    adapter = RuntimeAdapter()
    events = RecordingEventSink()
    composition = build_connector_composition(
        database_path=tmp_path / "runtime.sqlite3",
        oauth_return_uri=CALLBACK,
        adapters={"feishu": adapter},
        vault=InMemoryCredentialVault(),
        event_sink=events,
        hard_deny_provider=(
            (lambda _instance, action: frozenset({action}))
            if deny
            else None
        ),
        maintenance_interval_seconds=0.01,
    )
    app = FastAPI()
    app.include_router(composition.router, prefix="/api/v1")
    return composition, adapter, events, TestClient(app)


def _connect(client: TestClient) -> str:
    begun = client.post(
        "/api/v1/connectors/feishu/auth/begin",
        json={"auth_kind": "oauth2"},
    )
    assert begun.status_code == 200
    body = begun.json()
    state = parse_qs(urlsplit(body["authorization_url"]).query)["state"][0]
    completed = client.get(
        "/api/v1/connectors/oauth/callback",
        params={"state": state, "code": "provider-code"},
    )
    assert completed.status_code == 200
    projection = completed.json()
    encoded = str(projection)
    assert "RUNTIME-SECRET-CREDENTIAL" not in encoded
    assert "credential_ref" not in encoded
    assert "runtime-account" not in encoded
    return str(projection["instance_id"])


def test_mountable_router_exposes_dynamic_safe_connector_lifecycle(tmp_path: Path) -> None:
    composition, adapter, events, client = _composition(tmp_path)
    initial = client.get("/api/v1/connectors")
    assert initial.status_code == 200
    feishu = next(
        item
        for item in initial.json()["items"]
        if item["definition"]["connector_id"] == "feishu"
    )
    assert feishu["instances"] == []

    instance_id = _connect(client)
    refreshed = client.post(
        f"/api/v1/connectors/instances/{instance_id}/health"
    )
    assert refreshed.status_code == 200
    document_id = "550e8400-e29b-41d4-a716-446655440000"
    invoked = client.post(
        f"/api/v1/connectors/instances/{instance_id}/actions/documents.read",
        json={"inputs": {"document_id": document_id}},
    )
    assert invoked.status_code == 200
    assert invoked.json()["document_id"] == document_id
    assert invoked.json()["url"].endswith(document_id)

    write = client.post(
        f"/api/v1/connectors/instances/{instance_id}/actions/documents.write",
        json={"inputs": {"title": "正式方案"}, "idempotency_key": "runtime-write"},
    )
    replay = client.post(
        f"/api/v1/connectors/instances/{instance_id}/actions/documents.write",
        json={"inputs": {"title": "正式方案"}, "idempotency_key": "runtime-write"},
    )
    assert write.status_code == replay.status_code == 200
    assert write.json() == replay.json()
    assert adapter.invocation_count == 2  # one read and one write
    assert "connector.instance.connected" in events.event_types

    disconnected = client.delete(
        f"/api/v1/connectors/instances/{instance_id}"
    )
    assert disconnected.status_code == 204
    assert adapter.revoked is True
    raw = (tmp_path / "runtime.sqlite3").read_bytes()
    assert b"RUNTIME-SECRET-CREDENTIAL" not in raw


def test_router_policy_errors_are_stable_and_never_echo_internal_detail(
    tmp_path: Path,
) -> None:
    _composition_value, _adapter, _events, client = _composition(tmp_path, deny=True)
    instance_id = _connect(client)
    denied = client.post(
        f"/api/v1/connectors/instances/{instance_id}/actions/documents.write",
        json={"inputs": {"title": "blocked"}, "idempotency_key": "deny-key"},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "connector_permission_denied"
    assert "documents.write" not in str(denied.json())
    missing_key = client.post(
        f"/api/v1/connectors/instances/{instance_id}/actions/documents.write",
        json={"inputs": {"title": "missing-key"}},
    )
    # Policy runs first and remains backend-authoritative.
    assert missing_key.status_code == 403

    injected_callback = client.post(
        "/api/v1/connectors/feishu/auth/begin",
        json={
            "auth_kind": "oauth2",
            "return_uri": "https://attacker.example/callback",
        },
    )
    assert injected_callback.status_code == 422


def test_action_input_contract_is_enforced_before_the_adapter_boundary(
    tmp_path: Path,
) -> None:
    _composition_value, adapter, _events, client = _composition(tmp_path)
    instance_id = _connect(client)

    missing_identity = client.post(
        f"/api/v1/connectors/instances/{instance_id}/actions/documents.read",
        json={"inputs": {}},
    )
    unknown_field = client.post(
        f"/api/v1/connectors/instances/{instance_id}/actions/documents.read",
        json={"inputs": {"document_id": "doc-a", "provider_secret": "no"}},
    )

    assert missing_identity.status_code == 422
    assert unknown_field.status_code == 422
    assert missing_identity.json()["detail"] == {
        "code": "connector_input_invalid",
        "message": "连接器操作参数不符合要求",
    }
    assert "provider_secret" not in str(unknown_field.json())
    assert adapter.invocation_count == 0


def test_router_lifecycle_headers_replay_and_fingerprint_conflict(tmp_path: Path) -> None:
    composition, adapter, _events, client = _composition(tmp_path)
    request_headers = {"X-EcoreX-Client-Request-ID": "api-auth-stable"}
    first = client.post(
        "/api/v1/connectors/feishu/auth/begin",
        json={"auth_kind": "oauth2"},
        headers=request_headers,
    )
    replay = client.post(
        "/api/v1/connectors/feishu/auth/begin",
        json={"auth_kind": "oauth2"},
        headers=request_headers,
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert adapter.begin_auth_count == 1
    assert composition.repository.lifecycle_request_state("api-auth-stable")[
        "status"
    ] == "completed"

    conflict = client.post(
        "/api/v1/connectors/feishu/auth/begin",
        json={"auth_kind": "device_code"},
        headers=request_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "connector_idempotency_conflict"


def test_oauth_callback_html_is_no_store_exact_origin_and_secret_free(
    tmp_path: Path,
) -> None:
    _composition_value, _adapter, _events, client = _composition(tmp_path)
    instance_id = _connect(client)
    begun = client.post(
        f"/api/v1/connectors/instances/{instance_id}/reauthorize",
        json={"auth_kind": "oauth2"},
        headers={"X-EcoreX-Client-Request-ID": "api-reauthorize-stable"},
    )
    assert begun.status_code == 200
    challenge = begun.json()
    state_value = parse_qs(urlsplit(challenge["authorization_url"]).query)["state"][0]
    provider_code = "PROVIDER-CODE-MUST-NOT-BE-REFLECTED"
    completed = client.get(
        "/api/v1/connectors/oauth/callback",
        params={"state": state_value, "code": provider_code},
        headers={"Accept": "text/html"},
    )
    assert completed.status_code == 200
    assert completed.headers["cache-control"].startswith("no-store")
    assert completed.headers["referrer-policy"] == "no-referrer"
    assert "default-src 'none'" in completed.headers["content-security-policy"]
    assert completed.headers["x-ecorex-connector-result"] == "completed"
    assert completed.headers["x-ecorex-connector-code"] == "ok"
    assert '"http://127.0.0.1:8765"' in completed.text
    assert "postMessage" in completed.text
    assert "window.close" in completed.text
    assert state_value not in completed.text
    assert provider_code not in completed.text
    assert "RUNTIME-SECRET-CREDENTIAL" not in completed.text

    catalog = client.get("/api/v1/connectors").json()
    feishu = next(
        item
        for item in catalog["items"]
        if item["definition"]["connector_id"] == "feishu"
    )
    assert [item["instance_id"] for item in feishu["instances"]] == [instance_id]
    assert feishu["instances"][0]["health"] == "connected"

    invalid_state = "INVALID-STATE-MUST-NOT-BE-REFLECTED"
    invalid_code = "INVALID-CODE-MUST-NOT-BE-REFLECTED"
    failed = client.get(
        "/api/v1/connectors/oauth/callback",
        params={"state": invalid_state, "code": invalid_code},
        headers={"Accept": "text/html"},
    )
    assert failed.status_code == 400
    assert failed.headers["x-ecorex-connector-result"] == "failed"
    assert failed.headers["x-ecorex-connector-code"] == "connector_auth_error"
    assert invalid_state not in failed.text
    assert invalid_code not in failed.text


def test_uncertain_operation_resolution_and_maintenance_supervisor(tmp_path: Path) -> None:
    composition, _adapter, _events, client = _composition(tmp_path)
    instance_id = _connect(client)
    acquired = composition.repository.acquire_instance_operation(
        instance_id,
        operation_kind="simulated_crash",
        lease_seconds=1,
    )
    assert acquired is not None
    _instance, lease = acquired
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        connection.execute(
            "UPDATE connector_operation_leases SET expires_at=? WHERE operation_id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                lease.operation_id,
            ),
        )
    uncertain = client.get(
        f"/api/v1/connectors/instances/{instance_id}/uncertain-operations"
    )
    assert uncertain.status_code == 200
    assert uncertain.json()["operation_ids"] == []
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        assert connection.execute(
            "SELECT status FROM connector_operation_leases WHERE operation_id=?",
            (lease.operation_id,),
        ).fetchone()[0] == "active"
    composition.repository.recover_expired_operation_leases()
    uncertain = client.get(
        f"/api/v1/connectors/instances/{instance_id}/uncertain-operations"
    )
    assert uncertain.json()["operation_ids"] == [lease.operation_id]
    resolved = client.post(
        f"/api/v1/connectors/instances/{instance_id}/uncertain-operations/"
        f"{lease.operation_id}/resolve",
        json={"resolution": "manually_reconciled"},
    )
    assert resolved.status_code == 204

    calls = 0

    async def fake_maintenance() -> None:
        nonlocal calls
        calls += 1

    composition.service.maintenance_once = fake_maintenance  # type: ignore[method-assign]

    async def run_supervisor() -> None:
        supervisor = ConnectorMaintenanceSupervisor(
            composition.service,
            interval_seconds=0.01,
        )
        await supervisor.start()
        await asyncio.sleep(0.04)
        await supervisor.stop()

    asyncio.run(run_supervisor())
    assert calls >= 2


def test_connector_maintenance_serializes_stuck_sync_publisher_work(
    tmp_path: Path, monkeypatch
) -> None:
    composition, _adapter, _events, _client = _composition(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    active = 0
    peak = 0

    def blocked_drain(*, limit: int) -> int:
        nonlocal active, peak
        assert limit == 100
        active += 1
        peak = max(peak, active)
        entered.set()
        release.wait(2)
        active -= 1
        return 0

    monkeypatch.setattr(
        composition.service,
        "_drain_outbox_locked",
        blocked_drain,
    )

    async def scenario() -> None:
        first = asyncio.create_task(composition.service.maintenance_once())
        assert await asyncio.to_thread(entered.wait, 1)
        # The second pass observes the non-blocking drain lock and returns; it
        # cannot start another publisher thread behind the stuck first pass.
        await asyncio.wait_for(
            composition.service.maintenance_once(),
            timeout=0.5,
        )
        release.set()
        await first

    asyncio.run(scenario())
    assert peak == 1


def test_outbox_nudge_while_owner_busy_is_not_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition, _adapter, events, _client = _composition(tmp_path)
    service = composition.service
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    original = service._drain_outbox_locked

    def first_scan_is_blocked(*, limit: int) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(2)
            return 0
        return original(limit=limit)

    monkeypatch.setattr(service, "_drain_outbox_locked", first_scan_is_blocked)
    failures: list[BaseException] = []

    def own_drain() -> None:
        try:
            service.drain_outbox()
        except BaseException as error:
            failures.append(error)

    owner = threading.Thread(target=own_drain)
    owner.start()
    assert entered.wait(1)
    event_id = _enqueue_test_outbox(service, "connector-test-busy-generation")

    # Busy registration returns quickly, but advances the generation that the
    # current owner must consume before it can transition to idle.
    assert service.drain_outbox() == 0
    release.set()
    owner.join(timeout=3)

    assert not owner.is_alive()
    assert failures == []
    assert calls >= 2
    assert composition.repository.pending_outbox_count() == 0
    assert events.event_ids.count(event_id) == 1
    health = service.outbox_delivery_health()
    assert health.status == "idle"
    assert health.pending == 0
    assert health.completed_generation >= health.requested_generation


def test_outbox_health_seqlock_never_combines_stale_pending_with_active_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition, _adapter, _events, _client = _composition(tmp_path)
    service = composition.service
    repository = composition.repository
    original_pending = repository.pending_outbox_count
    original_drain = service._drain_outbox_locked

    for round_index in range(10):
        pending_sampled = threading.Event()
        release_pending_sample = threading.Event()
        drain_entered = threading.Event()
        release_drain = threading.Event()
        first_sample = True
        sample_lock = threading.Lock()

        def sampled_pending() -> int:
            nonlocal first_sample
            value = original_pending()
            with sample_lock:
                block = first_sample
                first_sample = False
            if block:
                pending_sampled.set()
                assert release_pending_sample.wait(2)
            return value

        def blocked_drain(*, limit: int) -> int:
            drain_entered.set()
            assert release_drain.wait(2)
            return original_drain(limit=limit)

        monkeypatch.setattr(repository, "pending_outbox_count", sampled_pending)
        monkeypatch.setattr(service, "_drain_outbox_locked", blocked_drain)
        observed = []
        health_failures: list[BaseException] = []

        def read_health() -> None:
            try:
                observed.append(service.outbox_delivery_health())
            except BaseException as error:
                health_failures.append(error)

        health_reader = threading.Thread(target=read_health)
        health_reader.start()
        assert pending_sampled.wait(1)
        _enqueue_test_outbox(
            service,
            f"connector-test-health-seqlock-{round_index}",
        )
        drain_failures: list[BaseException] = []

        def drain() -> None:
            try:
                service.drain_outbox()
            except BaseException as error:
                drain_failures.append(error)

        owner = threading.Thread(target=drain)
        owner.start()
        assert drain_entered.wait(1)

        # The first durable sample is stale (zero), while generation/active has
        # advanced and one event is durable. The seqlock must reject that mixed
        # view and resample to pending=1 + active=true.
        release_pending_sample.set()
        health_reader.join(timeout=2)
        assert not health_reader.is_alive()
        assert health_failures == []
        assert len(observed) == 1
        snapshot = observed[0]
        assert snapshot.pending == 1
        assert snapshot.active is True
        assert snapshot.status == "draining"

        release_drain.set()
        owner.join(timeout=3)
        assert not owner.is_alive()
        assert drain_failures == []
        monkeypatch.setattr(repository, "pending_outbox_count", original_pending)
        monkeypatch.setattr(service, "_drain_outbox_locked", original_drain)
        assert original_pending() == 0


@pytest.mark.parametrize("close_gate", [False, True])
def test_outbox_publish_and_ack_share_runtime_epoch_fence(
    tmp_path: Path,
    close_gate: bool,
) -> None:
    database = tmp_path / f"outbox-epoch-{close_gate}.db"
    kernel = RuntimeKernel(database)
    repository = SQLiteConnectorRepository(database)
    gate = RuntimeExecutionGate()
    gate.record_report(kernel.invariants.audit())
    thread = kernel.create_thread(
        CreateThreadRequest(
            title="connector outbox epoch",
            client_request_id=f"connector-outbox-epoch-{close_gate}",
        )
    )
    entered = threading.Event()
    release = threading.Event()
    seen_permits = []

    def publish(event) -> None:
        # The attempt thread must carry the Connector permit through the actual
        # EventStore commit, not merely validate before calling this sink.
        seen_permits.append(service._execution_permit_context.get())
        with kernel.database.transaction() as connection:
            kernel.events.append_in_transaction(
                connection,
                thread_id=thread.thread_id,
                event_type="system.test.connector_delivered",
                payload={"source_event_id": event.event_id},
                idempotency_key=f"test-connector:{event.event_id}",
            )
            entered.set()
            assert release.wait(2)

    service = ConnectorService(
        builtin_connector_registry({"feishu": RuntimeAdapter()}),
        allowed_return_uris=frozenset({CALLBACK}),
        vault=InMemoryCredentialVault(),
        repository=repository,
        outbox_publisher=publish,
        outbox_publish_timeout_seconds=1,
        execution_gate=gate,
    )
    event_id = _enqueue_test_outbox(
        service,
        f"connector-test-epoch-{close_gate}",
    )
    failures: list[BaseException] = []

    def drain() -> None:
        try:
            service.drain_outbox()
        except BaseException as error:
            failures.append(error)

    owner = threading.Thread(target=drain)
    owner.start()
    assert entered.wait(1)
    if close_gate:
        gate.mark_critical(error_code="connector_outbox_epoch_test")
    release.set()
    owner.join(timeout=3)

    assert not owner.is_alive()
    assert len(seen_permits) == 1 and seen_permits[0] is not None
    with kernel.database.reader() as connection:
        delivered = int(
            connection.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE event_type='system.test.connector_delivered'"
            ).fetchone()[0]
        )
        outbox = connection.execute(
            "SELECT published_at FROM connector_outbox WHERE event_id=?",
            (event_id,),
        ).fetchone()
    assert outbox is not None
    if close_gate:
        assert delivered == 0
        assert outbox[0] is None
        assert failures and isinstance(failures[0], ConnectorUnavailable)
        health = service.outbox_delivery_health()
        assert health.pending == 1
        assert health.status == "degraded"
    else:
        assert delivered == 1
        assert outbox[0] is not None
        assert failures == []
        assert service.outbox_delivery_health().status == "idle"


def test_stuck_outbox_publisher_is_bounded_observable_and_restart_recoverable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    composition, _adapter, _events, _client = _composition(tmp_path)
    service = composition.service
    service.outbox_publish_timeout_seconds = 0.05
    entered = threading.Event()
    release = threading.Event()
    original_finished = threading.Event()
    delivery_lock = threading.Lock()
    delivered: set[str] = set()
    deliveries: list[str] = []

    def deliver_once(event) -> None:
        with delivery_lock:
            if event.event_id not in delivered:
                delivered.add(event.event_id)
                deliveries.append(event.event_id)

    def stuck_publish(event) -> None:
        entered.set()
        assert release.wait(5)
        deliver_once(event)
        original_finished.set()

    service.outbox_publisher = stuck_publish
    first_event = _enqueue_test_outbox(service, "connector-test-stuck-first")
    started = time.monotonic()
    service.drain_outbox()
    assert time.monotonic() - started < 0.75
    assert entered.is_set()
    health = service.outbox_delivery_health()
    assert health.status == "stuck"
    assert health.stuck_event_id == first_event
    assert health.pending == 1

    second_event = _enqueue_test_outbox(service, "connector-test-stuck-second")
    assert service.drain_outbox() == 0
    assert delivered == set()
    assert service.outbox_delivery_health().pending == 2

    async def bounded_stop() -> float:
        supervisor = ConnectorMaintenanceSupervisor(
            service,
            interval_seconds=3600,
            stop_timeout_seconds=0.2,
        )
        await supervisor.start()
        before = time.monotonic()
        with pytest.raises(ConnectorUnavailable, match="publisher is stuck"):
            await supervisor.stop()
        return time.monotonic() - before

    assert asyncio.run(bounded_stop()) < 0.75

    # Simulate process loss: the isolated thread cannot keep its lease alive.
    # A restarted owner may reclaim only after expiry and the sink deduplicates
    # the immutable event_id if the old process later reports success.
    with sqlite3.connect(database) as connection:
        old_lease = str(
            connection.execute(
                "SELECT lease_token FROM connector_outbox WHERE event_id=?",
                (first_event,),
            ).fetchone()[0]
        )
        connection.execute(
            "UPDATE connector_outbox SET lease_expires_at=? WHERE event_id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), first_event),
        )
    with pytest.raises(RuntimeError, match="publish lease was lost"):
        service.repository.mark_outbox_published(first_event, old_lease)
    restarted = ConnectorService(
        builtin_connector_registry({"feishu": RuntimeAdapter()}),
        allowed_return_uris=frozenset({CALLBACK}),
        vault=InMemoryCredentialVault(),
        repository=SQLiteConnectorRepository(database),
        outbox_publisher=deliver_once,
        outbox_publish_timeout_seconds=1,
    )
    assert restarted.flush_pending_outbox(timeout_seconds=2) == 2
    assert restarted.repository.pending_outbox_count() == 0

    release.set()
    assert original_finished.wait(2)
    deadline = time.monotonic() + 2
    while service.outbox_delivery_health().status == "stuck":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert deliveries.count(first_event) == 1
    assert deliveries.count(second_event) == 1


def test_connector_epoch_close_rejects_late_oauth_and_new_maintenance(
    tmp_path: Path, monkeypatch
) -> None:
    composition, adapter, _events, _client = _composition(tmp_path)
    gate = RuntimeExecutionGate()
    composition.service.bind_execution_gate(gate)
    gate.record_report(RuntimeInvariantAuditor(composition.repository._runtime_database).audit())
    assert gate.snapshot().healthy
    entered = asyncio.Event()
    release = asyncio.Event()

    original_begin = adapter.begin_auth

    async def blocked_begin(**values):
        entered.set()
        await release.wait()
        return await original_begin(**values)

    monkeypatch.setattr(adapter, "begin_auth", blocked_begin)

    async def close_during_oauth():
        request = asyncio.create_task(
            composition.service.begin_connect(
                "feishu",
                auth_kind=ConnectorAuthKind.OAUTH2,
                return_uri=CALLBACK,
                client_request_id="epoch-oauth-begin",
            )
        )
        await entered.wait()
        loop_progressed = asyncio.Event()
        await asyncio.sleep(0)
        loop_progressed.set()
        assert loop_progressed.is_set()
        gate.mark_critical(error_code="connector_oauth_epoch_closed")
        release.set()
        with pytest.raises(ConnectorUnavailable):
            await request

        calls = 0

        async def counted_maintenance() -> None:
            nonlocal calls
            calls += 1

        monkeypatch.setattr(
            composition.service,
            "maintenance_once",
            counted_maintenance,
        )
        supervisor = ConnectorMaintenanceSupervisor(
            composition.service,
            interval_seconds=0.01,
            execution_gate=gate,
        )
        await supervisor.start()
        await asyncio.sleep(0.04)
        await supervisor.stop()
        assert calls == 0

    asyncio.run(close_during_oauth())
    assert adapter.begin_auth_count == 1
    assert composition.service.catalog()
    with composition.repository._connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM connector_auth_flows"
        ).fetchone()[0] == 0
        lifecycle = connection.execute(
            "SELECT status FROM connector_lifecycle_requests "
            "WHERE client_request_id='epoch-oauth-begin'"
        ).fetchone()
    assert lifecycle["status"] == "running"


def test_connector_late_oauth_grant_never_activates_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    composition, adapter, _events, _client = _composition(tmp_path)
    gate = RuntimeExecutionGate()
    composition.service.bind_execution_gate(gate)
    gate.record_report(
        RuntimeInvariantAuditor(composition.repository._runtime_database).audit()
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original_complete = adapter.complete_auth

    async def blocked_complete(**values):
        entered.set()
        await release.wait()
        return await original_complete(**values)

    monkeypatch.setattr(adapter, "complete_auth", blocked_complete)

    async def scenario():
        challenge = await composition.service.begin_connect(
            "feishu",
            auth_kind=ConnectorAuthKind.OAUTH2,
            return_uri=CALLBACK,
            client_request_id="epoch-oauth-grant",
        )
        state = parse_qs(urlsplit(challenge.authorization_url).query)["state"][0]
        completing = asyncio.create_task(
            composition.service.complete_connect(
                challenge.flow_id,
                {"state": state, "code": "one-shot-code"},
            )
        )
        await entered.wait()
        gate.mark_critical(error_code="connector_grant_epoch_closed")
        release.set()
        with pytest.raises(ConnectorUnavailable):
            await completing

    asyncio.run(scenario())
    assert composition.repository.list_instances() == ()
    with composition.repository._connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM connector_runtime_instances"
        ).fetchone()[0] == 0
    vault_values = getattr(composition.service.vault, "_values", {})
    assert all(
        "RUNTIME-SECRET-CREDENTIAL" not in repr(material)
        for material in vault_values.values()
    )


def test_connector_maintenance_precommit_epoch_close_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    composition, _adapter, _events, client = _composition(tmp_path)
    instance_id = _connect(client)
    acquired = composition.repository.acquire_instance_operation(
        instance_id,
        operation_kind="precommit-maintenance",
        lease_seconds=1,
    )
    assert acquired is not None
    _instance, lease = acquired
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        connection.execute(
            "UPDATE connector_operation_leases SET expires_at=? "
            "WHERE operation_id=?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                lease.operation_id,
            ),
        )
    gate = RuntimeExecutionGate()
    composition.service.bind_execution_gate(gate)
    gate.record_report(RuntimeInvariantAuditor(composition.repository._runtime_database).audit())
    original_assert = gate.assert_permit
    closed = False

    def close_before_commit(permit) -> None:
        nonlocal closed
        closed = True
        gate.request_critical(error_code="connector_maintenance_precommit")
        original_assert(permit)

    monkeypatch.setattr(gate, "assert_permit", close_before_commit)
    with pytest.raises(ConnectorUnavailable):
        asyncio.run(composition.service.maintenance_once())
    assert closed
    with sqlite3.connect(tmp_path / "runtime.sqlite3") as connection:
        status = connection.execute(
            "SELECT status FROM connector_operation_leases WHERE operation_id=?",
            (lease.operation_id,),
        ).fetchone()[0]
    assert status == "active"


def test_builtin_actions_have_distinct_closed_public_output_contracts() -> None:
    definitions = {item.connector_id: item for item in builtin_connector_definitions()}
    feishu = definitions["feishu"]
    read_schema = feishu.action("documents.read").output_schema
    search_schema = feishu.action("drive.search").output_schema
    send_schema = feishu.action("messages.send").output_schema
    assert read_schema is not search_schema
    assert search_schema is not send_schema
    assert read_schema["additionalProperties"] is False
    assert "content" in read_schema["properties"]
    assert "items" in search_schema["properties"]
    assert "message_id" in send_schema["properties"]
    assert read_schema["properties"]["document_id"]["x-ecorex-public-kind"] == "public_id"
    assert read_schema["properties"]["url"]["x-ecorex-public-kind"] == "public_uri"
