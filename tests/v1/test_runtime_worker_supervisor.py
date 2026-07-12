from __future__ import annotations

import asyncio
import threading
import time

from fastapi.testclient import TestClient

from ecorex.gateway import GatewayEvent
from ecorex.runtime import (
    AgentWorkerSupervisor,
    RuntimeSettings,
    WorkerOutcome,
    WorkerRunResult,
    create_app,
)


RUNTIME_TOKEN = "r" * 43
CSRF_TOKEN = "c" * 43


class CompletingGateway:
    def __init__(self) -> None:
        self.requests = []
        self.closed = False
        self.close_count = 0

    async def stream(self, request):
        self.requests.append(request)
        yield GatewayEvent.model_validate(
            {
                "seq": 1,
                "event_type": "response.completed",
                "response_id": f"response_{len(self.requests)}",
            }
        )

    async def aclose(self) -> None:
        self.close_count += 1
        self.closed = True


class IdleWorker:
    def __init__(self, gateway) -> None:
        self.gateway = gateway

    async def run_once(self, _worker_id: str) -> WorkerRunResult:
        return WorkerRunResult(WorkerOutcome.IDLE)


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("supervisor condition was not reached")
        await asyncio.sleep(0.005)


def _owned_asyncio_tasks() -> list[asyncio.Task]:
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and task.get_name().startswith("ecorex-agent-worker")
    ]


def _settings(tmp_path, **updates) -> RuntimeSettings:
    values = {
        "database_path": tmp_path / "runtime.db",
        "runtime_bearer_token": RUNTIME_TOKEN,
        "csrf_token": CSRF_TOKEN,
        "webui_origins": ("http://testserver",),
        "model_worker_concurrency": 1,
        "model_worker_poll_seconds": 0.01,
        "model_worker_shutdown_seconds": 1,
    }
    values.update(updates)
    return RuntimeSettings(**values)


def _headers() -> tuple[dict[str, str], dict[str, str]]:
    auth = {"Authorization": f"Bearer {RUNTIME_TOKEN}"}
    mutation = {
        **auth,
        "Origin": "http://testserver",
        "X-EcoreX-CSRF": CSRF_TOKEN,
    }
    return auth, mutation


def test_bootstrap_separates_catalog_from_unconfigured_model_service(tmp_path) -> None:
    app = create_app(settings=_settings(tmp_path))
    auth, _mutation = _headers()

    response = TestClient(app).get("/api/v1/bootstrap", headers=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["models"]["chat"]
    assert body["models"]["image"]
    assert body["model_service"] == {
        "state": "unavailable",
        "reason": "managed_gateway_not_configured",
    }
    assert app.state.model_worker_supervisor is None


def test_asgi_lifespan_runs_durable_worker_and_closes_gateway(tmp_path) -> None:
    gateway = CompletingGateway()
    app = create_app(
        settings=_settings(
            tmp_path,
            model_gateway=gateway,
            allow_unmanaged_model_gateway_for_testing=True,
        )
    )
    auth, mutation = _headers()

    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/bootstrap", headers=auth).json()
        assert bootstrap["model_service"] == {"state": "ready", "reason": None}
        assert app.state.model_worker_supervisor.running is True

        thread = client.post(
            "/api/v1/threads", json={"title": "supervised"}, headers=mutation
        ).json()
        created = client.post(
            f"/api/v1/threads/{thread['thread_id']}/turns",
            json={"input": "complete in background", "client_message_id": "m-1"},
            headers=mutation,
        )
        assert created.status_code == 202
        turn_id = created.json()["turn"]["turn_id"]

        deadline = time.monotonic() + 2
        status = "queued"
        while time.monotonic() < deadline:
            projection = client.get(
                f"/api/v1/threads/{thread['thread_id']}/projection", headers=auth
            ).json()
            status = next(
                turn["status"] for turn in projection["turns"] if turn["turn_id"] == turn_id
            )
            if status == "completed":
                break
            time.sleep(0.01)

        assert status == "completed"
        assert len(gateway.requests) == 1
        assert app.state.model_worker_supervisor.snapshot().completed_runs == 1

    assert gateway.closed is True
    assert gateway.close_count == 1
    assert app.state.model_worker_supervisor.running is False
    assert app.state.model_gateway_lifecycle is None


def test_unexpected_slot_exit_restores_full_desired_concurrency() -> None:
    async def scenario() -> None:
        supervisor = AgentWorkerSupervisor(
            IdleWorker(CompletingGateway()),  # type: ignore[arg-type]
            concurrency=2,
            idle_poll_seconds=0.01,
            shutdown_timeout_seconds=0.2,
            close_gateway_on_stop=False,
            restart_backoff_initial_seconds=0.01,
            restart_backoff_max_seconds=0.02,
        )
        original_loop = supervisor._worker_loop
        exited = False

        async def exit_once(index: int) -> None:
            nonlocal exited
            if index == 0 and not exited:
                exited = True
                return
            await original_loop(index)

        supervisor._worker_loop = exit_once  # type: ignore[method-assign]
        await supervisor.start()
        await _wait_until(
            lambda: supervisor.running
            and supervisor.snapshot().restarted_slots == 1
        )

        snapshot = supervisor.snapshot()
        assert snapshot.running is True
        assert snapshot.concurrency == 2
        assert snapshot.desired_workers == 2
        assert snapshot.live_workers == 2
        assert snapshot.restarted_slots == 1
        assert snapshot.failed_runs == 1

        await supervisor.stop()
        assert supervisor.snapshot().live_workers == 0
        assert _owned_asyncio_tasks() == []

    asyncio.run(scenario())


def test_continuous_slot_failure_uses_bounded_backoff_without_restart_storm() -> None:
    async def scenario() -> None:
        supervisor = AgentWorkerSupervisor(
            IdleWorker(CompletingGateway()),  # type: ignore[arg-type]
            concurrency=1,
            idle_poll_seconds=0.01,
            shutdown_timeout_seconds=0.2,
            close_gateway_on_stop=False,
            restart_backoff_initial_seconds=0.02,
            restart_backoff_max_seconds=0.04,
        )
        attempts: list[float] = []
        third_attempt = asyncio.Event()

        async def always_fail(_index: int) -> None:
            attempts.append(asyncio.get_running_loop().time())
            if len(attempts) >= 3:
                third_attempt.set()
            raise RuntimeError("sensitive failure detail")

        supervisor._worker_loop = always_fail  # type: ignore[method-assign]
        await supervisor.start()
        await asyncio.wait_for(third_attempt.wait(), timeout=1)
        await _wait_until(lambda: supervisor.snapshot().failed_runs >= 3)
        assert attempts[1] - attempts[0] >= 0.015
        assert attempts[2] - attempts[1] >= 0.035
        assert supervisor.snapshot().restarted_slots == 2
        assert supervisor.snapshot().last_error == "worker_slot_failed:runtimeerror"
        assert "sensitive" not in (supervisor.snapshot().last_error or "")

        await supervisor.stop()
        stopped_attempts = len(attempts)
        await asyncio.sleep(0.08)
        assert len(attempts) == stopped_attempts
        assert _owned_asyncio_tasks() == []

    asyncio.run(scenario())


def test_restart_backoff_remains_bounded_after_extreme_failure_streak() -> None:
    supervisor = AgentWorkerSupervisor(
        IdleWorker(CompletingGateway()),  # type: ignore[arg-type]
        concurrency=1,
        close_gateway_on_stop=False,
        restart_backoff_initial_seconds=0.01,
        restart_backoff_max_seconds=5.0,
    )

    assert supervisor._restart_delay(1) == 0.01
    assert supervisor._restart_delay(10_000) == 5.0


def test_stop_during_restart_backoff_never_resurrects_slot() -> None:
    async def scenario() -> None:
        supervisor = AgentWorkerSupervisor(
            IdleWorker(CompletingGateway()),  # type: ignore[arg-type]
            concurrency=1,
            idle_poll_seconds=0.01,
            shutdown_timeout_seconds=0.2,
            close_gateway_on_stop=False,
            restart_backoff_initial_seconds=0.2,
            restart_backoff_max_seconds=0.2,
        )
        attempts = 0
        first_exit = asyncio.Event()

        async def exit_immediately(_index: int) -> None:
            nonlocal attempts
            attempts += 1
            first_exit.set()

        supervisor._worker_loop = exit_immediately  # type: ignore[method-assign]
        await supervisor.start()
        await first_exit.wait()
        await _wait_until(
            lambda: supervisor.snapshot().failed_runs == 1
            and supervisor.snapshot().live_workers == 0
        )
        await supervisor.stop()
        await supervisor.stop()
        await asyncio.sleep(0.25)

        assert attempts == 1
        assert supervisor.running is False
        assert supervisor.snapshot().restarted_slots == 0
        assert _owned_asyncio_tasks() == []

    asyncio.run(scenario())


def test_gateway_close_supports_sync_and_sync_returned_awaitable() -> None:
    class SyncGateway:
        def __init__(self) -> None:
            self.close_count = 0

        def aclose(self) -> None:
            self.close_count += 1

    class AwaitableGateway:
        def __init__(self) -> None:
            self.close_count = 0
            self.closed = False

        def aclose(self):
            self.close_count += 1

            async def complete() -> None:
                await asyncio.sleep(0)
                self.closed = True

            return complete()

    async def scenario() -> None:
        sync_gateway = SyncGateway()
        sync_supervisor = AgentWorkerSupervisor(
            IdleWorker(sync_gateway),  # type: ignore[arg-type]
            concurrency=1,
            idle_poll_seconds=0.01,
            shutdown_timeout_seconds=0.2,
        )
        await sync_supervisor.start()
        await sync_supervisor.stop()
        await sync_supervisor.stop()
        assert sync_gateway.close_count == 1
        assert sync_supervisor.snapshot().last_error is None

        awaitable_gateway = AwaitableGateway()
        awaitable_supervisor = AgentWorkerSupervisor(
            IdleWorker(awaitable_gateway),  # type: ignore[arg-type]
            concurrency=1,
            idle_poll_seconds=0.01,
            shutdown_timeout_seconds=0.2,
        )
        await awaitable_supervisor.start()
        await awaitable_supervisor.stop()
        assert awaitable_gateway.close_count == 1
        assert awaitable_gateway.closed is True
        assert awaitable_supervisor.snapshot().last_error is None
        assert _owned_asyncio_tasks() == []

    asyncio.run(scenario())


def test_hung_and_throwing_gateway_close_are_bounded_and_redacted() -> None:
    class HungGateway:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def aclose(self) -> None:
            self.entered.set()
            self.release.wait(timeout=5)
            self.finished.set()

    class ThrowingGateway:
        def __init__(self) -> None:
            self.close_count = 0

        def aclose(self) -> None:
            self.close_count += 1
            raise RuntimeError("credential-shaped detail must not escape")

    async def scenario() -> None:
        hung_gateway = HungGateway()
        hung_supervisor = AgentWorkerSupervisor(
            IdleWorker(hung_gateway),  # type: ignore[arg-type]
            concurrency=1,
            idle_poll_seconds=0.01,
            shutdown_timeout_seconds=0.1,
        )
        await hung_supervisor.start()
        started = asyncio.get_running_loop().time()
        await hung_supervisor.stop()
        elapsed = asyncio.get_running_loop().time() - started
        assert hung_gateway.entered.is_set()
        assert elapsed < 0.4
        assert hung_supervisor.snapshot().last_error == "gateway_close_timeout"
        assert hung_supervisor.running is False
        hung_gateway.release.set()
        await _wait_until(hung_gateway.finished.is_set)

        throwing_gateway = ThrowingGateway()
        throwing_supervisor = AgentWorkerSupervisor(
            IdleWorker(throwing_gateway),  # type: ignore[arg-type]
            concurrency=1,
            idle_poll_seconds=0.01,
            shutdown_timeout_seconds=0.1,
        )
        await throwing_supervisor.start()
        await throwing_supervisor.stop()
        assert throwing_gateway.close_count == 1
        assert (
            throwing_supervisor.snapshot().last_error
            == "gateway_close_failed:runtimeerror"
        )
        assert "credential" not in (
            throwing_supervisor.snapshot().last_error or ""
        )
        assert _owned_asyncio_tasks() == []

    asyncio.run(scenario())
