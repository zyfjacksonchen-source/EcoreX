from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading
import time

from fastapi.testclient import TestClient

from ecorex.protocol import CreateTurnRequest, InteractionKind, TurnStatus
from ecorex.runtime import (
    InteractionMaintenanceSupervisor,
    RuntimeExecutionGate,
    RuntimeKernel,
    RuntimeSettings,
    create_app,
)


RUNTIME_TOKEN = "r" * 43
CSRF_TOKEN = "c" * 43
AUTH = {"Authorization": f"Bearer {RUNTIME_TOKEN}"}
MUTATION = {
    **AUTH,
    "Origin": "http://testserver",
    "X-EcoreX-CSRF": CSRF_TOKEN,
}
FORBIDDEN_JOB_FIELDS = {
    "payload",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "heartbeat_at",
    "checkpoint",
    "idempotency_key",
    "last_error",
}


def _settings(tmp_path) -> RuntimeSettings:
    return RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        runtime_bearer_token=RUNTIME_TOKEN,
        csrf_token=CSRF_TOKEN,
        webui_origins=("http://testserver",),
    )


def _running_turn(kernel: RuntimeKernel):
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="需要确认的任务"),
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    leased = kernel.jobs.lease_next("projection-worker")
    assert leased is not None and leased.lease_token is not None
    kernel.jobs.start(leased.job_id, "projection-worker", leased.lease_token)
    return thread, created, leased


def test_projection_uses_safe_job_and_interaction_contracts_after_restart(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    kernel = app.state.runtime
    thread, _created, leased = _running_turn(kernel)
    sensitive_checkpoint = {
        "phase": "tool-call",
        "path": "C:/private/customer/report.docx",
        "arguments": {"secret": "must-not-enter-sse"},
    }
    kernel.jobs.heartbeat(
        leased.job_id,
        "projection-worker",
        leased.lease_token,
        checkpoint=sensitive_checkpoint,
    )
    assert kernel.jobs.get(leased.job_id).checkpoint == sensitive_checkpoint

    client = TestClient(app)
    active = client.get(
        f"/api/v1/threads/{thread.thread_id}/projection", headers=AUTH
    )
    assert active.status_code == 200
    active_job = active.json()["jobs"][0]
    assert FORBIDDEN_JOB_FIELDS.isdisjoint(active_job)
    assert active_job["status"] == "running"
    event_page = client.get(
        f"/api/v1/threads/{thread.thread_id}/events", headers=AUTH
    ).json()
    encoded_events = str(event_page).casefold()
    assert "projection-worker" not in encoded_events
    assert "must-not-enter-sse" not in encoded_events
    assert "c:/private" not in encoded_events
    heartbeat = next(
        event for event in event_page["events"] if event["event_type"] == "job.heartbeat"
    )
    assert set(heartbeat["payload"]) == {"attempt", "checkpoint_sha256"}

    interaction = kernel.request_interaction(
        job_id=leased.job_id,
        worker_id="projection-worker",
        lease_token=leased.lease_token,
        kind=InteractionKind.INFORMATION,
        prompt="请确认后继续",
        idempotency_key="safe-interaction",
    )
    restarted = create_app(settings=_settings(tmp_path))
    hydrated = TestClient(restarted).get(
        f"/api/v1/threads/{thread.thread_id}/projection", headers=AUTH
    )
    assert hydrated.status_code == 200
    body = hydrated.json()
    assert body["jobs"][0]["status"] == "waiting_human"
    assert FORBIDDEN_JOB_FIELDS.isdisjoint(body["jobs"][0])
    assert body["interactions"][0]["interaction_id"] == interaction.interaction_id
    assert body["interactions"][0]["status"] == "pending"
    assert "idempotency_key" not in body["interactions"][0]


def test_job_terminal_events_only_publish_bounded_reason_and_digest(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    secret = "provider secret at C:/private/customer/report.docx"
    failed = kernel.jobs.enqueue(
        kind="maintenance",
        payload={"internal": secret},
        idempotency_key="terminal-redaction-failed",
        thread_id=thread.thread_id,
    )
    leased = kernel.jobs.lease_next("internal-worker")
    assert leased is not None and leased.lease_token is not None
    kernel.jobs.start(leased.job_id, "internal-worker", leased.lease_token)
    kernel.jobs.fail(
        leased.job_id,
        "internal-worker",
        leased.lease_token,
        error=secret,
        retryable=False,
    )
    cancelled = kernel.jobs.enqueue(
        kind="maintenance",
        payload={},
        idempotency_key="terminal-redaction-cancelled",
        thread_id=thread.thread_id,
    )
    kernel.jobs.cancel(cancelled.job_id, reason=secret)

    page = kernel.events.page(thread.thread_id)
    encoded = str([event.model_dump(mode="json") for event in page.events]).casefold()
    assert "provider secret" not in encoded
    assert "c:/private" not in encoded
    failed_fact = next(event for event in page.events if event.event_type == "job.failed")
    cancelled_fact = next(
        event for event in page.events if event.event_type == "job.cancelled"
    )
    assert failed_fact.payload["reason_code"] == "execution_failed"
    assert cancelled_fact.payload["reason_code"] == "cancelled"
    assert len(str(failed_fact.payload["diagnostic_sha256"])) == 64
    assert len(str(cancelled_fact.payload["diagnostic_sha256"])) == 64
    assert kernel.jobs.get(failed.job_id).last_error == secret
    assert kernel.jobs.get(cancelled.job_id).last_error == secret


def test_maintenance_expires_hitl_without_projection_visits(tmp_path) -> None:
    async def scenario() -> None:
        kernel = RuntimeKernel(tmp_path / "runtime.db")
        thread, created, leased = _running_turn(kernel)
        interaction = kernel.request_interaction(
            job_id=leased.job_id,
            worker_id="projection-worker",
            lease_token=leased.lease_token,
            kind=InteractionKind.INFORMATION,
            prompt="即将过期",
            idempotency_key="expiring-interaction",
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        with kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE interactions SET expires_at=? WHERE interaction_id=?",
                (
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    interaction.interaction_id,
                ),
            )
        gate = RuntimeExecutionGate()
        gate.record_report(kernel.invariants.audit())
        kernel.jobs.bind_execution_gate(gate)
        supervisor = InteractionMaintenanceSupervisor(
            kernel.interactions,
            execution_gate=gate,
            interval_seconds=60,
            convergence_timeout_seconds=1,
            shutdown_timeout_seconds=0.2,
        )
        await supervisor.start()
        assert kernel.interactions.get(interaction.interaction_id).status.value == "expired"
        assert kernel.jobs.get(leased.job_id).status.value == "failed"
        assert kernel.get_turn(created.turn.turn_id).status is TurnStatus.FAILED
        assert supervisor.snapshot().expired_interactions == 1
        await supervisor.stop()

    asyncio.run(scenario())


def test_maintenance_timeout_closes_gate_before_any_later_lease(tmp_path) -> None:
    class BlockingInteractions:
        def __init__(self, delegate) -> None:
            self.delegate = delegate
            self.database = delegate.database
            self.entered = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def expire_due_in_transaction(self, connection, *, now=None):
            self.entered.set()
            self.release.wait(timeout=5)
            self.finished.set()
            return self.delegate.expire_due_in_transaction(connection, now=now)

    async def scenario() -> None:
        kernel = RuntimeKernel(tmp_path / "runtime.db")
        _thread = kernel.create_thread()
        kernel.create_turn(_thread.thread_id, CreateTurnRequest(input="queued"))
        gate = RuntimeExecutionGate()
        gate.record_report(kernel.invariants.audit())
        kernel.jobs.bind_execution_gate(gate)
        interactions = BlockingInteractions(kernel.interactions)
        supervisor = InteractionMaintenanceSupervisor(
            interactions,  # type: ignore[arg-type]
            execution_gate=gate,
            interval_seconds=60,
            convergence_timeout_seconds=0.05,
            shutdown_timeout_seconds=0.1,
        )
        started = time.monotonic()
        await supervisor.start()
        assert time.monotonic() - started < 0.5
        assert interactions.entered.is_set()
        assert gate.snapshot().status == "critical"
        assert gate.snapshot().last_error_code == "interaction_maintenance_timeout"

        lease_task = asyncio.create_task(
            asyncio.to_thread(kernel.jobs.lease_next, "late-worker")
        )
        await asyncio.sleep(0.02)
        assert lease_task.done()
        assert await lease_task is None
        interactions.release.set()
        while not interactions.finished.is_set():
            await asyncio.sleep(0.005)
        assert gate.snapshot().status == "critical"
        await supervisor.stop()

    asyncio.run(scenario())


def test_healthy_preflight_holds_gate_across_expiry_transaction(tmp_path) -> None:
    class BlockingInteractions:
        def __init__(self, delegate) -> None:
            self.delegate = delegate
            self.database = delegate.database
            self.entered = threading.Event()
            self.release = threading.Event()

        def expire_due_in_transaction(self, connection, *, now=None):
            self.entered.set()
            self.release.wait(timeout=5)
            return self.delegate.expire_due_in_transaction(connection, now=now)

    async def scenario() -> None:
        kernel = RuntimeKernel(tmp_path / "runtime.db")
        thread = kernel.create_thread()
        created = kernel.create_turn(thread.thread_id, CreateTurnRequest(input="queued"))
        gate = RuntimeExecutionGate()
        gate.record_report(kernel.invariants.audit())
        kernel.jobs.bind_execution_gate(gate)
        interactions = BlockingInteractions(kernel.interactions)
        supervisor = InteractionMaintenanceSupervisor(
            interactions,  # type: ignore[arg-type]
            execution_gate=gate,
            interval_seconds=60,
            convergence_timeout_seconds=1,
            shutdown_timeout_seconds=0.2,
        )
        start_task = asyncio.create_task(supervisor.start())
        await asyncio.to_thread(interactions.entered.wait, 1)
        lease_task = asyncio.create_task(
            asyncio.to_thread(kernel.jobs.lease_next, "linearized-worker")
        )
        await asyncio.sleep(0.02)
        assert not lease_task.done()
        interactions.release.set()
        await start_task
        leased = await lease_task
        assert leased is not None and leased.job_id == created.job.job_id
        await supervisor.stop()

    asyncio.run(scenario())


def test_critical_mode_blocks_semantic_get_mutation_but_keeps_reads(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)
    created = client.post("/api/v1/threads", json={}, headers=MUTATION)
    assert created.status_code == 201
    thread_id = created.json()["thread_id"]
    turn = client.post(
        f"/api/v1/threads/{thread_id}/turns",
        json={"input": "queued"},
        headers=MUTATION,
    ).json()["turn"]
    with app.state.runtime.database.transaction() as connection:
        connection.execute(
            "UPDATE turns SET status='completed' WHERE turn_id=?",
            (turn["turn_id"],),
        )
    app.state.runtime_execution_gate.record_report(
        app.state.runtime.invariants.audit()
    )
    assert app.state.runtime_execution_gate.snapshot().status == "critical"

    projection = client.get(
        f"/api/v1/threads/{thread_id}/projection", headers=AUTH
    )
    health = client.get("/api/v1/system/health?technical=true", headers=AUTH)
    assert projection.status_code == health.status_code == 200
    assert health.json()["overall"] == "critical"

    blocked = client.post("/api/v1/threads", json={}, headers=MUTATION)
    oauth_get_mutation = client.get(
        "/api/v1/connectors/oauth/callback?state=opaque&code=opaque"
    )
    for response in (blocked, oauth_get_mutation):
        assert response.status_code == 503
        assert response.json()["code"] == "RUNTIME_READ_ONLY"
        assert response.headers["cache-control"] == "no-store"


def test_http_epoch_close_rolls_back_domain_commit_after_request_admission(
    tmp_path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    entered = threading.Event()
    release = threading.Event()

    @app.post("/api/v1/test-epoch-commit")
    def guarded_commit() -> dict[str, bool]:
        with app.state.runtime.database.transaction() as connection:
            connection.execute(
                "INSERT INTO runtime_meta(key,value) VALUES "
                "('test_http_epoch_commit','must_rollback')"
            )
            entered.set()
            assert release.wait(timeout=5)
        return {"committed": True}

    client = TestClient(app, raise_server_exceptions=False)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.post,
            "/api/v1/test-epoch-commit",
            json={},
            headers=MUTATION,
        )
        assert entered.wait(timeout=5)
        app.state.runtime_execution_gate.mark_critical(
            error_code="test_http_epoch_closed"
        )
        release.set()
        response = future.result(timeout=5)

    assert response.status_code == 503
    assert response.json()["code"] == "RUNTIME_READ_ONLY"
    with app.state.runtime.database.reader() as connection:
        assert connection.execute(
            "SELECT 1 FROM runtime_meta WHERE key='test_http_epoch_commit'"
        ).fetchone() is None


def test_http_epoch_guard_fences_raw_shared_connection_commit(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    entered = threading.Event()
    release = threading.Event()

    @app.post("/api/v1/test-raw-epoch-commit")
    def guarded_raw_commit() -> dict[str, bool]:
        connection = app.state.runtime.database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO runtime_meta(key,value) VALUES "
                "('test_raw_http_epoch_commit','must_rollback')"
            )
            entered.set()
            assert release.wait(timeout=5)
            connection.commit()
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        return {"committed": True}

    client = TestClient(app, raise_server_exceptions=False)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.post,
            "/api/v1/test-raw-epoch-commit",
            json={},
            headers=MUTATION,
        )
        assert entered.wait(timeout=5)
        app.state.runtime_execution_gate.mark_critical(
            error_code="test_raw_http_epoch_closed"
        )
        release.set()
        response = future.result(timeout=5)

    assert response.status_code == 503
    assert response.json()["code"] == "RUNTIME_READ_ONLY"
    with app.state.runtime.database.reader() as connection:
        assert connection.execute(
            "SELECT 1 FROM runtime_meta WHERE key='test_raw_http_epoch_commit'"
        ).fetchone() is None


def test_http_epoch_guard_owns_executescript_implicit_commit(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    entered = threading.Event()
    release = threading.Event()

    @app.post("/api/v1/test-script-epoch-commit")
    def guarded_script_commit() -> dict[str, bool]:
        connection = app.state.runtime.database.connect()

        def block_until_epoch_closes() -> int:
            entered.set()
            assert release.wait(timeout=5)
            return 1

        try:
            connection.create_function(
                "block_until_epoch_closes",
                0,
                block_until_epoch_closes,
            )
            connection.executescript(
                "SELECT block_until_epoch_closes();"
                "INSERT INTO runtime_meta(key,value) VALUES "
                "('test_script_http_epoch_commit','must_rollback');"
            )
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        return {"committed": True}

    client = TestClient(app, raise_server_exceptions=False)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.post,
            "/api/v1/test-script-epoch-commit",
            json={},
            headers=MUTATION,
        )
        assert entered.wait(timeout=5)
        app.state.runtime_execution_gate.mark_critical(
            error_code="test_script_http_epoch_closed"
        )
        release.set()
        response = future.result(timeout=5)

    assert response.status_code == 503
    assert response.json()["code"] == "RUNTIME_READ_ONLY"
    with app.state.runtime.database.reader() as connection:
        assert connection.execute(
            "SELECT 1 FROM runtime_meta WHERE key='test_script_http_epoch_commit'"
        ).fetchone() is None
