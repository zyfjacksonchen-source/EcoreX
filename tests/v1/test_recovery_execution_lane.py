from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import threading
import uuid

from fastapi.testclient import TestClient
import pytest

from ecorex.protocol import ActivateUpdateResponse, UpdateSnapshot
from ecorex.runtime import RuntimeSettings, create_app
from ecorex.runtime.database import SQLiteDatabase, json_dumps
from ecorex.runtime.recovery_gate import (
    RecoveryExecutionDenied,
    RecoveryExecutionGate,
)
from ecorex.update import ReleaseChannel, RuntimeUpdateService
from tests.v1.test_managed_session_runtime import (
    CompletingGateway,
    MutableClock,
    Vault,
    _headers,
    _install,
    _keys,
    _lease,
    _service,
    _settings,
)
from tests.v1.test_runtime_update_service import (
    Feed,
    _coordinator,
    _manifest,
    _package,
)


RUNTIME_TOKEN = "r" * 43
CSRF_TOKEN = "c" * 43
MUTATION_HEADERS = {
    "Authorization": f"Bearer {RUNTIME_TOKEN}",
    "Origin": "http://testserver",
    "X-EcoreX-CSRF": CSRF_TOKEN,
}
TRANSACTION_ID = "a" * 32
BUILD_DIGEST = hashlib.sha256(b"verified-local-build").hexdigest()


class DurableLocalUpdateService:
    def __init__(
        self,
        database,
        *,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.database = SQLiteDatabase(database)
        self.entered = entered
        self.release = release
        self.checks = 0
        self.started = 0
        self.stopped = 0
        self.value = UpdateSnapshot(
            current_version="1.0.0",
            state="awaiting_user",
            target_version="1.0.1",
            release_id="release-1.0.1-stable",
            build_digest=BUILD_DIGEST,
            transaction_id=TRANSACTION_ID,
            can_activate=True,
        )

    def snapshot(self) -> UpdateSnapshot:
        return self.value

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def check_now(self) -> UpdateSnapshot:
        self.checks += 1
        return self.value

    async def activate_verified_local(
        self,
        *,
        transaction_id: str,
        client_request_id: str,
        execution_guard,
    ) -> ActivateUpdateResponse:
        execution_guard()
        if transaction_id != TRANSACTION_ID or self.value.state != "awaiting_user":
            raise RuntimeError("local staged transaction mismatch")

        def commit_local_activation() -> None:
            with self.database.transaction() as connection:
                now = "2026-07-12T12:00:00.000000Z"
                connection.execute(
                    "INSERT INTO runtime_update_state("
                    "singleton,state,target_version,release_id,build_digest,"
                    "transaction_id,requires_refresh,error_code,updated_at"
                    ") VALUES(1,'activating','1.0.1','release-1.0.1-stable',"
                    "?,?,1,NULL,?) ON CONFLICT(singleton) DO UPDATE SET "
                    "state='activating',transaction_id=excluded.transaction_id,"
                    "requires_refresh=1,updated_at=excluded.updated_at",
                    (BUILD_DIGEST, TRANSACTION_ID, now),
                )
                connection.execute(
                    "INSERT INTO runtime_update_events("
                    "event_id,event_type,payload_json,created_at"
                    ") VALUES(?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        "update.local_activation_confirmed",
                        json_dumps(
                            {
                                "transaction_id": transaction_id,
                                "client_request_id_sha256": hashlib.sha256(
                                    client_request_id.encode()
                                ).hexdigest(),
                            }
                        ),
                        now,
                    ),
                )
                if self.entered is not None:
                    self.entered.set()
                if self.release is not None:
                    assert self.release.wait(timeout=5)

        await asyncio.to_thread(commit_local_activation)
        execution_guard()
        self.value = self.value.model_copy(
            update={
                "state": "activating",
                "can_activate": False,
                "requires_refresh": True,
            }
        )
        return ActivateUpdateResponse(update=self.value, restart_scheduled=True)


def _table_snapshot(database) -> dict[str, tuple[tuple[object, ...], ...]]:
    with database.reader() as connection:
        names = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        result = {}
        for name in names:
            quoted = name.replace('"', '""')
            rows = [tuple(row) for row in connection.execute(f'SELECT * FROM "{quoted}"')]
            result[name] = tuple(sorted(rows, key=repr))
        return result


def _changed_tables(
    before: dict[str, tuple[tuple[object, ...], ...]],
    after: dict[str, tuple[tuple[object, ...], ...]],
) -> set[str]:
    return {
        name
        for name in before.keys() | after.keys()
        if before.get(name) != after.get(name)
    }


def test_recovery_gate_accepts_only_fixed_scopes_and_invalidates_inflight_permits() -> None:
    gate = RecoveryExecutionGate()
    permit = gate.issue_permit(scope="session_logout", subject="logout:test")
    gate.assert_permit(permit)
    with pytest.raises(ValueError, match="scope"):
        gate.issue_permit(scope="thread_write", subject="forbidden")  # type: ignore[arg-type]

    other = RecoveryExecutionGate()
    with pytest.raises(RecoveryExecutionDenied, match="stale"):
        other.assert_permit(permit)

    gate.request_close(error_code="test_recovery_close")
    with pytest.raises(RecoveryExecutionDenied, match="closed"):
        gate.assert_permit(permit)
    assert gate.snapshot().status == "closed"


def test_critical_update_check_is_blocked_but_verified_local_activation_is_allowed(
    tmp_path,
) -> None:
    database = tmp_path / "runtime.db"
    updates = DurableLocalUpdateService(database)
    app = create_app(
        settings=RuntimeSettings(
            database_path=database,
            runtime_bearer_token=RUNTIME_TOKEN,
            csrf_token=CSRF_TOKEN,
            webui_origins=("http://testserver",),
            update_service=updates,
        )
    )
    client = TestClient(app)
    app.state.runtime_execution_gate.mark_critical(
        error_code="test_business_runtime_critical"
    )
    before = _table_snapshot(app.state.runtime.database)

    checked = client.post("/api/v1/update/check", headers=MUTATION_HEADERS)
    assert checked.status_code == 503
    assert checked.json()["code"] == "RUNTIME_READ_ONLY"
    assert updates.checks == 0

    activated = client.post(
        "/api/v1/update/activate",
        json={
            "transaction_id": TRANSACTION_ID,
            "confirmed": True,
            "client_request_id": "verified-local-api-activation",
        },
        headers=MUTATION_HEADERS,
    )
    assert activated.status_code == 200
    after = _table_snapshot(app.state.runtime.database)
    changed = _changed_tables(before, after)
    assert changed
    assert changed <= {"runtime_update_state", "runtime_update_events"}


def test_recovery_gate_close_during_database_commit_rolls_back(tmp_path) -> None:
    database = tmp_path / "runtime.db"
    entered = threading.Event()
    release = threading.Event()
    updates = DurableLocalUpdateService(
        database,
        entered=entered,
        release=release,
    )
    app = create_app(
        settings=RuntimeSettings(
            database_path=database,
            runtime_bearer_token=RUNTIME_TOKEN,
            csrf_token=CSRF_TOKEN,
            webui_origins=("http://testserver",),
            update_service=updates,
        )
    )
    client = TestClient(app, raise_server_exceptions=False)
    before = _table_snapshot(app.state.runtime.database)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            client.post,
            "/api/v1/update/activate",
            json={
                "transaction_id": TRANSACTION_ID,
                "confirmed": True,
                "client_request_id": "close-local-api-activation",
            },
            headers=MUTATION_HEADERS,
        )
        assert entered.wait(timeout=5)
        app.state.recovery_execution_gate.request_close(
            error_code="test_close_during_recovery_commit"
        )
        release.set()
        response = future.result(timeout=5)

    assert response.status_code == 503
    assert response.json()["code"] == "RECOVERY_LANE_CLOSED"
    assert _table_snapshot(app.state.runtime.database) == before


def test_logout_then_local_activation_contract_and_full_table_diff(tmp_path) -> None:
    private, public = _keys()
    now = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
    database = tmp_path / "runtime.db"
    session = _service(database, public, MutableClock(now), Vault())
    managed = _install(session, _lease(private, now=now))
    payload = _package("1.0.1")
    feed = Feed(_manifest(payload))
    restarts: list[str] = []
    updates = RuntimeUpdateService(
        database,
        coordinator=_coordinator(tmp_path / "update-fixture", payload),
        feed=feed,
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
        restart_requester=restarts.append,
    )
    prepared = asyncio.run(updates.check_now())
    assert prepared.transaction_id is not None
    app = create_app(
        settings=_settings(
            database,
            session,
            CompletingGateway(),
            update_service=updates,
        )
    )
    client = TestClient(app)
    app.state.runtime_execution_gate.mark_critical(
        error_code="test_logout_recovery_critical"
    )
    before = _table_snapshot(app.state.runtime.database)

    logged_out = client.post(
        "/api/v1/session/logout",
        json={
            "lease_digest": managed.lease_digest,
            "client_request_id": "critical-logout-request",
            "confirmed": True,
        },
        headers=_headers(mutation=True),
    )
    assert logged_out.status_code == 200

    # A cloud check cannot authenticate after revocation, while the exact
    # staged local transaction remains recoverable through host credentials.
    checked = client.post("/api/v1/update/check", headers=_headers(mutation=True))
    assert checked.status_code == 401
    feed_calls = feed.calls
    activated = client.post(
        "/api/v1/update/activate",
        json={
            "transaction_id": prepared.transaction_id,
            "confirmed": True,
            "client_request_id": "post-logout-local-activation",
        },
        headers=_headers(mutation=True),
    )
    assert activated.status_code == 200
    assert feed.calls == feed_calls
    assert restarts == [prepared.transaction_id]

    after = _table_snapshot(app.state.runtime.database)
    changed = _changed_tables(before, after)
    allowed = {
        name
        for name in after
        if name.startswith("managed_session") or name.startswith("runtime_update")
    }
    assert changed
    assert changed <= allowed
    assert not changed & {
        "threads",
        "turns",
        "items",
        "artifacts",
        "artifact_entities",
        "connector_instances",
        "connector_invocations",
    }
