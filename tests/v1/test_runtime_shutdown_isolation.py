from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from ecorex.runtime import RuntimeSettings, create_app
from ecorex.runtime.shutdown import (
    ShutdownFailure,
    stop_service_phases_isolated,
    stop_services_isolated,
)


class _Stopped:
    def __init__(self) -> None:
        self.called = False

    async def stop(self) -> None:
        self.called = True


class _Failed:
    async def stop(self) -> None:
        raise RuntimeError("SECRET C:\\Users\\operator\\credential.txt")


class _Hung:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def stop(self) -> None:
        self.started.set()
        await asyncio.Event().wait()


class _Synchronous:
    def __init__(self) -> None:
        self.called = False

    def stop(self) -> None:
        self.called = True


def test_shutdown_isolates_failure_and_timeout_while_stopping_every_service() -> None:
    async def scenario():
        stopped = _Stopped()
        failed = _Failed()
        hung = _Hung()
        started = time.monotonic()
        failures = await stop_services_isolated(
            (
                ("worker", stopped),
                ("provider", failed),
                ("legacy_adapter", hung),
            ),
            timeout_seconds=0.05,
        )
        elapsed = time.monotonic() - started
        await asyncio.sleep(0)
        return stopped, hung, failures, elapsed

    stopped, hung, failures, elapsed = asyncio.run(scenario())

    assert stopped.called is True
    assert hung.started.is_set()
    assert elapsed < 0.5
    assert [(failure.service, failure.reason, failure.error_code) for failure in failures] == [
        ("legacy_adapter", "timeout", "shutdown_timeout"),
        ("provider", "error", "RuntimeError"),
    ]
    assert "SECRET" not in str(failures)
    assert "credential.txt" not in str(failures)


def test_shutdown_supports_sync_services_and_empty_collections() -> None:
    synchronous = _Synchronous()
    assert asyncio.run(
        stop_services_isolated(
            (("sync_adapter", synchronous),),
            timeout_seconds=1,
        )
    ) == ()
    assert synchronous.called is True
    assert asyncio.run(stop_services_isolated((), timeout_seconds=1)) == ()


def test_shutdown_orders_quiesce_before_resource_close() -> None:
    order: list[str] = []

    class Dispatcher:
        async def stop(self) -> None:
            await asyncio.sleep(0.01)
            order.append("dispatcher")

    class Publisher:
        async def stop(self) -> None:
            assert order == ["dispatcher"]
            order.append("publisher")

    failures = asyncio.run(
        stop_service_phases_isolated(
            (
                (1, "dispatcher", Dispatcher()),
                (2, "publisher", Publisher()),
            ),
            timeout_seconds=1,
        )
    )
    assert failures == ()
    assert order == ["dispatcher", "publisher"]


def test_shutdown_timeout_does_not_skip_later_resource_phase() -> None:
    closed = _Synchronous()
    failures = asyncio.run(
        stop_service_phases_isolated(
            (
                (1, "hung_worker", _Hung()),
                (2, "provider", closed),
            ),
            timeout_seconds=0.05,
        )
    )
    assert closed.called is True
    assert [(failure.service, failure.reason) for failure in failures] == [
        ("hung_worker", "timeout")
    ]


@pytest.mark.parametrize(
    ("services", "timeout"),
    [
        ((("Bad Name", _Stopped()),), 1),
        ((("worker", _Stopped()), ("worker", _Stopped())), 1),
        ((("worker", _Stopped()),), 0),
        ((("worker", _Stopped()),), True),
    ],
)
def test_shutdown_rejects_ambiguous_contracts(services, timeout) -> None:
    with pytest.raises(ValueError):
        asyncio.run(stop_services_isolated(services, timeout_seconds=timeout))


def test_runtime_lifespan_does_not_let_one_provider_block_other_cleanup(
    tmp_path,
) -> None:
    class HungImageClient:
        async def aclose(self) -> None:
            await asyncio.Event().wait()

    class ClosedAuditPublisher:
        def __init__(self) -> None:
            self.closed = False

        async def publish(self, _record) -> None:
            return None

        async def aclose(self) -> None:
            self.closed = True

    closed = ClosedAuditPublisher()
    settings = RuntimeSettings(
        database_path=tmp_path / "runtime.db",
        image_orchestration_client=HungImageClient(),
        audit_publisher=closed,
        allow_unmanaged_model_gateway_for_testing=True,
        lifecycle_shutdown_seconds=0.2,
    )
    app = create_app(settings=settings)

    started = time.monotonic()
    with TestClient(app) as client:
        app.state.logout_shutdown_failures = (
            ShutdownFailure("provider", "error", "RuntimeError"),
        )
        health = client.get(
            "/api/v1/system/health",
            params={"technical": "true"},
            headers={"Authorization": f"Bearer {settings.runtime_bearer_token}"},
        )
        assert health.status_code == 200
        lifecycle = health.json()["metrics"]["services"]["lifecycle"]
        assert lifecycle == {
            "state": "degraded",
            "failure_count": 1,
            "failures": [
                {
                    "service": "provider",
                    "reason": "error",
                    "error_code": "RuntimeError",
                }
            ],
        }
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert closed.closed is True
    failures = app.state.runtime_shutdown_failures
    assert any(
        failure.service == "image_gateway"
        and failure.reason == "timeout"
        and failure.error_code == "shutdown_timeout"
        for failure in failures
    )
    assert all(failure.service != "audit_publisher" for failure in failures)


def test_connector_first_shutdown_publish_stall_obeys_process_hard_deadline(
    tmp_path,
) -> None:
    child_source = r'''
import asyncio
import json
from pathlib import Path
import sys
import threading
import time

from ecorex.connectors import (
    ConnectorMaintenanceSupervisor,
    ConnectorService,
    InMemoryCredentialVault,
    SQLiteConnectorRepository,
    builtin_connector_registry,
)
from ecorex.runtime import RuntimeKernel
from ecorex.runtime.invariant_guard import RuntimeExecutionGate
from ecorex.runtime.shutdown import stop_service_phases_isolated

database = Path(sys.argv[1])
kernel = RuntimeKernel(database)
repository = SQLiteConnectorRepository(database)
gate = RuntimeExecutionGate()
gate.record_report(kernel.invariants.audit())
publisher_entered = threading.Event()
publisher_release = threading.Event()

def stuck_publish(_event):
    publisher_entered.set()
    publisher_release.wait(30)

service = ConnectorService(
    builtin_connector_registry({}),
    allowed_return_uris=frozenset(
        {"http://127.0.0.1:8765/api/v1/connectors/oauth/callback"}
    ),
    vault=InMemoryCredentialVault(),
    repository=repository,
    outbox_publisher=stuck_publish,
    outbox_publish_timeout_seconds=2,
    execution_gate=gate,
)
with service.control_admission(
    operation="child_enqueue_outbox",
    subject="connector-child-hard-deadline",
):
    with repository._write() as connection:
        repository._append_outbox(
            connection,
            event_type="connector.test.shutdown",
            aggregate_id="connector-child-hard-deadline",
            payload={"status": "pending"},
        )

supervisor = ConnectorMaintenanceSupervisor(
    service,
    interval_seconds=3600,
    maintenance_allowed=lambda: False,
    stop_timeout_seconds=0.2,
    execution_gate=gate,
)
order = []

class Producer:
    async def stop(self):
        order.append("producer_stopped")

class GateCloser:
    async def stop(self):
        gate.mark_critical(error_code="child_shutdown_gate_closed")
        order.append("gate_closed")

class AdapterCloser:
    async def stop(self):
        assert not gate.snapshot().healthy
        order.append("adapter_closed")

async def scenario():
    await supervisor.start()
    await asyncio.sleep(0)
    started = time.monotonic()
    failures = await stop_service_phases_isolated(
        (
            (1, "producer", Producer()),
            (2, "connector_maintenance", supervisor),
            (3, "runtime_invariant", GateCloser()),
            (4, "connector_adapter", AdapterCloser()),
        ),
        timeout_seconds=0.2,
    )
    return time.monotonic() - started, failures

process_started = time.monotonic()
shutdown_elapsed, failures = asyncio.run(scenario())
process_elapsed = time.monotonic() - process_started
health = service.outbox_delivery_health()
print(json.dumps({
    "shutdown_elapsed": shutdown_elapsed,
    "process_elapsed": process_elapsed,
    "publisher_entered": publisher_entered.is_set(),
    "order": order,
    "pending": repository.pending_outbox_count(),
    "health": health.status,
    "failures": [
        {
            "service": failure.service,
            "reason": failure.reason,
            "error_code": failure.error_code,
        }
        for failure in failures
    ],
}, sort_keys=True), flush=True)
'''
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-c", child_source, str(tmp_path / "child-runtime.db")],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )
    wall_elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["shutdown_elapsed"] < 0.8
    assert payload["process_elapsed"] < 0.8
    # Cold-import time varies by CI host; the child-reported interval brackets
    # asyncio.run (including default-executor shutdown) and is the hard-budget
    # assertion. The wall bound only detects a process that fails to exit.
    assert wall_elapsed < 3.5
    assert payload["publisher_entered"] is True
    assert payload["order"] == [
        "producer_stopped",
        "gate_closed",
        "adapter_closed",
    ]
    assert payload["pending"] == 1
    assert payload["health"] == "stuck"
    assert any(
        failure["service"] == "connector_maintenance"
        for failure in payload["failures"]
    )
