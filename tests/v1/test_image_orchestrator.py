from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import random
import sqlite3
from threading import Lock

from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
import pytest

from ecorex.image_orchestrator.api import create_image_orchestration_router
from ecorex.image_orchestrator.cas import ImageContentStore
from ecorex.image_orchestrator.models import (
    ImageBackpressure,
    ImageIdempotencyConflict,
    ImageInvalidTransition,
    ImageJobStatus,
    ImageLeaseLost,
    ImageLimits,
    ImageOperation,
    ImageResult,
    ImageResultRejected,
    ImageSubmitRequest,
    ImageUsage,
)
from ecorex.image_orchestrator.postgres_store import LEASE_SQL, PostgresImageJobStore
from ecorex.image_orchestrator.postgres_schema import (
    CURRENT_IMAGE_SCHEMA_VERSION,
    ImageSchemaError,
)
from ecorex.image_orchestrator.provider import (
    ProviderResult,
    ProviderState,
    ProviderUncertain,
    ProviderUnavailable,
)
from ecorex.image_orchestrator.service import ImageOrchestrationService
from ecorex.image_orchestrator.sqlite_schema import SQLiteImageSchemaManager
from ecorex.image_orchestrator.sqlite_store import SQLiteImageJobStore
from ecorex.image_orchestrator.store import ImageJobStore
from ecorex.image_orchestrator.worker import (
    ImageJobWorker,
    ImageWorkerOutcome,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"ecorex-image-result"


def _sqlite_store(path: Path, **kwargs) -> SQLiteImageJobStore:
    SQLiteImageSchemaManager(path).migrate()
    return SQLiteImageJobStore(path, **kwargs)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
        self._lock = Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += timedelta(seconds=seconds)


def request(
    key: str,
    *,
    prompt: str = "draw a clean office diagram",
    max_attempts: int = 4,
    operation: ImageOperation = ImageOperation.GENERATE,
) -> ImageSubmitRequest:
    kwargs = {}
    if operation is ImageOperation.RETOUCH:
        kwargs = {
            "input_sha256": ("a" * 64,),
            "instruction": "make the title clearer",
        }
    return ImageSubmitRequest(
        operation=operation,
        model_id="image-2",
        client_request_id=key,
        prompt=prompt,
        max_attempts=max_attempts,
        **kwargs,
    )


def worker(
    store: SQLiteImageJobStore,
    provider: object,
    root: Path,
    clock: MutableClock,
    *,
    threshold: int = 5,
) -> ImageJobWorker:
    return ImageJobWorker(
        store,
        provider,  # type: ignore[arg-type]
        ImageContentStore(root),
        clock=clock,
        lease_seconds=5,
        heartbeat_seconds=0.1,
        base_retry_seconds=0.01,
        max_retry_seconds=1,
        random_source=random.Random(7),
        breaker_threshold=threshold,
        breaker_cooldown_seconds=30,
    )


def completed(provider_id: str, model_id: str, provider_request_id: str) -> ProviderResult:
    return ProviderResult(
        state=ProviderState.COMPLETED,
        provider_request_id=provider_request_id,
        payload=PNG,
        mime_type="image/png",
        sha256=hashlib.sha256(PNG).hexdigest(),
        usage=ImageUsage(provider_id, model_id, output_units=1, billed_units=7),
    )


def test_128_concurrent_replays_create_one_job_and_one_acceptance(tmp_path: Path) -> None:
    store = _sqlite_store(tmp_path / "jobs.db")
    body = request("request-concurrent-0001")

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda _: store.submit("tenant-001", body), range(128)))

    assert len({job.job_id for job, _created in results}) == 1
    assert sum(created for _job, created in results) == 1
    events = store.events(results[0][0].job_id)
    assert [event["event_type"] for event in events] == ["image.accepted", "image.queued"]

    with pytest.raises(ImageIdempotencyConflict):
        store.submit(
            "tenant-001",
            request("request-concurrent-0001", prompt="a conflicting request"),
        )


def test_weighted_fair_queue_does_not_starve_late_tenant(tmp_path: Path) -> None:
    store = _sqlite_store(tmp_path / "jobs.db")
    for index in range(20):
        store.submit("tenant-heavy", request(f"heavy-request-{index:04d}"))
    store.submit("tenant-late", request("late-request-0001"))

    leased_accounts = []
    for index in range(4):
        leased = store.lease_next(f"worker-{index:04d}")
        assert leased is not None
        leased_accounts.append(leased.account_id)
        store.cancel(leased.job_id, account_id=leased.account_id)

    assert "tenant-late" in leased_accounts[:3]


def test_backpressure_is_durable_across_restart(tmp_path: Path) -> None:
    limits = ImageLimits(
        max_queued_jobs=2,
        max_queued_weight=2,
        max_account_queued_jobs=2,
        max_account_queued_weight=2,
        max_running_jobs=1,
        max_account_running=1,
        max_model_running=1,
        max_operation_running=1,
    )
    path = tmp_path / "jobs.db"
    first = _sqlite_store(path, limits=limits)
    first.submit("tenant-001", request("request-pressure-0001"))
    first.submit("tenant-001", request("request-pressure-0002"))
    with pytest.raises(ImageBackpressure):
        first.submit("tenant-001", request("request-pressure-0003"))

    restarted = _sqlite_store(path, limits=limits)
    with pytest.raises(ImageBackpressure):
        restarted.submit("tenant-001", request("request-pressure-0003"))


def test_expired_lease_recovery_fences_late_worker(tmp_path: Path) -> None:
    clock = MutableClock()
    store = _sqlite_store(tmp_path / "jobs.db", clock=clock)
    accepted, _ = store.submit("tenant-001", request("request-fence-0001"))
    first = store.lease_next("worker-first", lease_seconds=5)
    assert first is not None and first.lease_token is not None
    running = store.transition(
        first.job_id,
        first.lease_token,
        expected=("leased",),
        target="running",
        checkpoint={"provider_started": True, "phase": "provider"},
    )
    clock.advance(6)

    assert store.reclaim_expired() == 1
    reclaimed = store.get(accepted.job_id)
    assert reclaimed.status is ImageJobStatus.RETRY_WAIT
    assert reclaimed.checkpoint["provider_uncertain"] is True
    with pytest.raises(ImageLeaseLost):
        store.heartbeat(running.job_id, first.lease_token, lease_seconds=5)

    second = store.lease_next("worker-second", lease_seconds=5)
    assert second is not None
    assert second.lease_generation == 2
    assert second.lease_token != first.lease_token


class LostResponseProvider:
    provider_id = "provider-lost-response"

    def __init__(self) -> None:
        self.submit_calls = 0
        self.recover_calls = 0
        self.results: dict[str, ProviderResult] = {}

    async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
        self.submit_calls += 1
        self.results[idempotency_key] = completed(
            self.provider_id, job.request.model_id, "provider-request-0001"
        )
        raise ProviderUncertain("provider accepted but response was lost")

    async def recover(self, job, *, idempotency_key: str, provider_request_id: str | None) -> ProviderResult:
        self.recover_calls += 1
        return self.results.get(idempotency_key, ProviderResult(ProviderState.NOT_FOUND))

    async def cancel(self, job, *, idempotency_key: str, provider_request_id: str | None) -> None:
        return None


def test_provider_uncertainty_recovers_without_duplicate_submit_or_usage(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        store = _sqlite_store(tmp_path / "jobs.db", clock=clock)
        provider = LostResponseProvider()
        accepted, _ = store.submit("tenant-001", request("request-uncertain-0001"))
        image_worker = worker(store, provider, tmp_path / "cas", clock)

        first = await image_worker.run_once("worker-first")
        assert first.outcome is ImageWorkerOutcome.RETRY_SCHEDULED
        clock.advance(2)
        second = await image_worker.run_once("worker-second")
        assert second.outcome is ImageWorkerOutcome.COMPLETED

        final = store.get(accepted.job_id)
        assert final.status is ImageJobStatus.COMPLETED
        assert final.result is not None
        assert final.usage is not None and final.usage.billed_units == 7
        assert provider.submit_calls == 1
        assert provider.recover_calls == 1
        assert sum(
            event["event_type"] == "image.completed" for event in store.events(final.job_id)
        ) == 1
        assert store.metrics().usage_billed_units == 7

    asyncio.run(scenario())


class PendingProvider:
    provider_id = "provider-pending"

    def __init__(self) -> None:
        self.submit_calls = 0
        self.recovered_ids: list[str | None] = []

    async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
        self.submit_calls += 1
        return ProviderResult(ProviderState.PENDING, provider_request_id="pending-request-0001")

    async def recover(self, job, *, idempotency_key: str, provider_request_id: str | None) -> ProviderResult:
        self.recovered_ids.append(provider_request_id)
        return completed(self.provider_id, job.request.model_id, "pending-request-0001")

    async def cancel(self, job, *, idempotency_key: str, provider_request_id: str | None) -> None:
        return None


def test_pending_provider_identity_survives_retry_and_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        path = tmp_path / "jobs.db"
        store = _sqlite_store(path, clock=clock)
        provider = PendingProvider()
        accepted, _ = store.submit("tenant-001", request("request-pending-0001"))
        first_worker = worker(store, provider, tmp_path / "cas", clock)
        assert (await first_worker.run_once("worker-first")).outcome is ImageWorkerOutcome.RETRY_SCHEDULED

        clock.advance(2)
        restarted = _sqlite_store(path, clock=clock)
        second_worker = worker(restarted, provider, tmp_path / "cas", clock)
        assert (await second_worker.run_once("worker-second")).outcome is ImageWorkerOutcome.COMPLETED
        assert provider.submit_calls == 1
        assert provider.recovered_ids == ["pending-request-0001"]
        assert restarted.get(accepted.job_id).provider_request_id == "pending-request-0001"

    asyncio.run(scenario())


class UnavailableProvider:
    provider_id = "provider-unavailable"

    def __init__(self) -> None:
        self.calls = 0

    async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
        self.calls += 1
        raise ProviderUnavailable("upstream is unavailable")

    async def recover(self, job, *, idempotency_key: str, provider_request_id: str | None) -> ProviderResult:
        self.calls += 1
        raise ProviderUnavailable("upstream is unavailable")

    async def cancel(self, job, *, idempotency_key: str, provider_request_id: str | None) -> None:
        return None


def test_retry_circuit_breaker_dead_letter_and_idempotent_requeue(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = MutableClock()
        store = _sqlite_store(tmp_path / "jobs.db", clock=clock)
        provider = UnavailableProvider()
        first, _ = store.submit(
            "tenant-001", request("request-dlq-0001", max_attempts=1)
        )
        image_worker = worker(store, provider, tmp_path / "cas", clock, threshold=1)
        outcome = await image_worker.run_once("worker-first")
        assert outcome.outcome is ImageWorkerOutcome.FAILED
        assert store.get(first.job_id).status is ImageJobStatus.DEAD_LETTER

        requeued = store.requeue_dead_letter(
            first.job_id,
            account_id="tenant-001",
            recovery_request_id="recovery-request-0001",
        )
        assert requeued.status is ImageJobStatus.QUEUED
        replay = store.requeue_dead_letter(
            first.job_id,
            account_id="tenant-001",
            recovery_request_id="recovery-request-0001",
        )
        assert replay.job_id == first.job_id
        store.cancel(first.job_id, account_id="tenant-001")

        second, _ = store.submit("tenant-001", request("request-circuit-0002"))
        circuit = await image_worker.run_once("worker-second")
        assert circuit.outcome is ImageWorkerOutcome.RETRY_SCHEDULED
        assert store.get(second.job_id).last_error_code == "provider_circuit_open"
        assert provider.calls == 1

    asyncio.run(scenario())


def test_atomic_result_usage_and_event_rollback_on_fault(tmp_path: Path) -> None:
    armed = {"value": True}

    def fault(phase: str, _job_id: str) -> None:
        if phase == "before_commit" and armed["value"]:
            raise RuntimeError("simulated process death before commit")

    store = _sqlite_store(tmp_path / "jobs.db", fault_hook=fault)
    accepted, _ = store.submit("tenant-001", request("request-atomic-0001"))
    leased = store.lease_next("worker-first")
    assert leased is not None and leased.lease_token
    store.transition(
        leased.job_id,
        leased.lease_token,
        expected=("leased",),
        target="running",
        checkpoint={"provider_started": True},
    )
    store.transition(
        leased.job_id,
        leased.lease_token,
        expected=("running",),
        target="verifying",
        checkpoint={"phase": "verifying"},
        provider_request_id="provider-request-0001",
    )
    store.transition(
        leased.job_id,
        leased.lease_token,
        expected=("verifying",),
        target="committing",
        checkpoint={"phase": "committing"},
    )
    result = ImageResult(hashlib.sha256(PNG).hexdigest(), len(PNG), "image/png")
    usage = ImageUsage("provider-1", "image-2", billed_units=7)
    with pytest.raises(RuntimeError):
        store.complete(accepted.job_id, leased.lease_token, result=result, usage=usage)

    after_fault = store.get(accepted.job_id)
    assert after_fault.status is ImageJobStatus.COMMITTING
    assert after_fault.result is None and after_fault.usage is None
    assert all(event["event_type"] != "image.completed" for event in store.events(accepted.job_id))

    armed["value"] = False
    completed_job = store.complete(
        accepted.job_id, leased.lease_token, result=result, usage=usage
    )
    assert completed_job.status is ImageJobStatus.COMPLETED


def test_cancelled_job_rejects_late_result_fence(tmp_path: Path) -> None:
    store = _sqlite_store(tmp_path / "jobs.db")
    accepted, _ = store.submit("tenant-001", request("request-cancel-0001"))
    leased = store.lease_next("worker-first")
    assert leased is not None and leased.lease_token
    store.cancel(accepted.job_id, account_id="tenant-001")

    with pytest.raises(ImageLeaseLost):
        store.transition(
            accepted.job_id,
            leased.lease_token,
            expected=("leased",),
            target="running",
            checkpoint={},
        )
    assert store.get(accepted.job_id).status is ImageJobStatus.CANCELLED


def test_cas_rejects_mime_spoof_and_verifies_digest(tmp_path: Path) -> None:
    cas = ImageContentStore(tmp_path / "cas")
    with pytest.raises(ImageResultRejected):
        cas.put(b"not-a-png", mime_type="image/png")
    stored = cas.put(PNG, mime_type="image/png", expected_sha256=hashlib.sha256(PNG).hexdigest())
    assert cas.read(stored.sha256) == PNG


@dataclass(frozen=True)
class Principal:
    account_id: str


def api_client(tmp_path: Path) -> TestClient:
    store = _sqlite_store(tmp_path / "api.db")
    service = ImageOrchestrationService(store)
    app = FastAPI()

    def principal(x_account: str = Header(...)) -> Principal:
        return Principal(x_account)

    app.include_router(
        create_image_orchestration_router(service, principal_dependency=principal)
    )
    return TestClient(app)


def test_api_is_strict_idempotent_and_tenant_isolated(tmp_path: Path) -> None:
    client = api_client(tmp_path)
    body = {
        "operation": "generate",
        "model_id": "image-2",
        "client_request_id": "request-api-0001",
        "prompt": "sensitive board plan",
    }
    first = client.post("/api/v1/images/jobs", headers={"x-account": "tenant-001"}, json=body)
    assert first.status_code == 202
    duplicate = client.post("/api/v1/images/jobs", headers={"x-account": "tenant-001"}, json=body)
    assert duplicate.status_code == 200
    assert duplicate.json()["created"] is False
    job_id = first.json()["job"]["job_id"]

    hidden = client.get(
        f"/api/v1/images/jobs/{job_id}", headers={"x-account": "tenant-002"}
    )
    assert hidden.status_code == 404
    visible = client.get(
        f"/api/v1/images/jobs/{job_id}", headers={"x-account": "tenant-001"}
    )
    assert visible.status_code == 200
    assert "prompt" not in visible.text

    injected = client.post(
        "/api/v1/images/jobs",
        headers={"x-account": "tenant-001"},
        json={**body, "client_request_id": "request-api-0002", "account_id": "tenant-002"},
    )
    assert injected.status_code == 422
    assert "sensitive board plan" not in injected.text
    assert "tenant-002" not in injected.text


def test_private_input_and_result_contract_is_tenant_scoped_and_verified(
    tmp_path: Path,
) -> None:
    store = _sqlite_store(tmp_path / "cloud.db")
    service = ImageOrchestrationService(store)
    cas = ImageContentStore(tmp_path / "private-cas")
    app = FastAPI()

    def principal(x_account: str = Header(...)) -> Principal:
        return Principal(x_account)

    app.include_router(
        create_image_orchestration_router(
            service,
            principal_dependency=principal,
            content_store=cas,
        )
    )
    client = TestClient(app)
    digest = hashlib.sha256(PNG).hexdigest()
    uploaded = client.put(
        f"/api/v1/images/inputs/{digest}",
        headers={"x-account": "tenant-001", "content-type": "image/png"},
        content=PNG,
    )
    assert uploaded.status_code == 200
    assert uploaded.json() == {
        "sha256": digest,
        "size_bytes": len(PNG),
        "mime_type": "image/png",
    }
    maximum_account = "a" * 256
    long_account_upload = client.put(
        f"/api/v1/images/inputs/{digest}",
        headers={"x-account": maximum_account, "content-type": "image/png"},
        content=PNG,
    )
    assert long_account_upload.status_code == 200
    references = cas.describe(digest).references
    assert len(references) == 2
    assert all(len(reference.reference_id) == 64 for reference in references)
    assert maximum_account not in repr(references)
    retouch = client.post(
        "/api/v1/images/jobs",
        headers={"x-account": "tenant-001"},
        json={
            "operation": "retouch",
            "model_id": "gpt-image-2",
            "client_request_id": "request-private-input-0001",
            "prompt": "structured retouch",
            "input_sha256": [digest],
            "instruction": "make the title clearer",
        },
    )
    assert retouch.status_code == 202
    fenced = client.post(
        "/api/v1/images/jobs",
        headers={"x-account": "tenant-002"},
        json={
            "operation": "retouch",
            "model_id": "gpt-image-2",
            "client_request_id": "request-private-input-0002",
            "prompt": "structured retouch",
            "input_sha256": [digest],
            "instruction": "make the title clearer",
        },
    )
    assert fenced.status_code == 409

    generated = client.post(
        "/api/v1/images/jobs",
        headers={"x-account": "tenant-001"},
        json={
            "operation": "generate",
            "model_id": "gpt-image-2",
            "client_request_id": "request-private-result-0001",
            "prompt": "generate a diagram",
        },
    ).json()["job"]
    leased = store.lease_next("worker-result")
    # The retouch job is first; cancel it so the generated job can be leased.
    assert leased is not None
    if leased.job_id != generated["job_id"]:
        store.cancel(leased.job_id, account_id=leased.account_id)
        leased = store.lease_next("worker-result")
    assert leased is not None and leased.job_id == generated["job_id"] and leased.lease_token
    store.transition(
        leased.job_id,
        leased.lease_token,
        expected=("leased",),
        target="running",
        checkpoint={"provider_started": True},
    )
    store.transition(
        leased.job_id,
        leased.lease_token,
        expected=("running",),
        target="verifying",
        checkpoint={"phase": "verifying"},
        provider_request_id="provider-result-0001",
    )
    store.transition(
        leased.job_id,
        leased.lease_token,
        expected=("verifying",),
        target="committing",
        checkpoint={"phase": "committing", "result_sha256": digest},
    )
    stored = cas.put(PNG, mime_type="image/png", expected_sha256=digest)
    store.complete(
        leased.job_id,
        leased.lease_token,
        result=stored,
        usage=ImageUsage("provider-1", "gpt-image-2", billed_units=1),
    )

    hidden = client.get(
        f"/api/v1/images/jobs/{leased.job_id}/result",
        headers={"x-account": "tenant-002"},
    )
    assert hidden.status_code == 404
    result = client.get(
        f"/api/v1/images/jobs/{leased.job_id}/result",
        headers={"x-account": "tenant-001"},
    )
    assert result.status_code == 200
    assert result.content == PNG
    assert result.headers["etag"] == f'"{digest}"'
    assert result.headers["x-content-sha256"] == digest
    assert result.headers["content-length"] == str(len(PNG))
    assert str(tmp_path) not in result.text


def test_chunked_image_input_is_rejected_at_the_cas_bound(tmp_path: Path) -> None:
    store = _sqlite_store(tmp_path / "bounded-api.db")
    service = ImageOrchestrationService(store)
    cas = ImageContentStore(tmp_path / "bounded-cas", max_bytes=16)
    app = FastAPI()

    def principal(x_account: str = Header(...)) -> Principal:
        return Principal(x_account)

    app.include_router(
        create_image_orchestration_router(
            service,
            principal_dependency=principal,
            content_store=cas,
        )
    )
    client = TestClient(app)
    digest = hashlib.sha256(PNG).hexdigest()

    def chunks():
        yield PNG[:12]
        yield PNG[12:24]
        yield PNG[24:]

    response = client.put(
        f"/api/v1/images/inputs/{digest}",
        headers={"x-account": "tenant-001", "content-type": "image/png"},
        content=chunks(),
    )
    assert response.status_code == 413
    with pytest.raises(ImageResultRejected):
        cas.read(digest)


def test_events_never_include_prompt_binary_paths_or_credentials(tmp_path: Path) -> None:
    store = _sqlite_store(tmp_path / "jobs.db")
    job, _ = store.submit(
        "tenant-001",
        request("request-event-0001", prompt="confidential acquisition plan"),
    )
    serialized = repr(store.events(job.job_id))
    assert "confidential acquisition plan" not in serialized
    assert "api_key" not in serialized
    assert "C:\\" not in serialized
    assert PNG.hex() not in serialized


def test_postgres_adapter_fails_closed_and_uses_skip_locked() -> None:
    def unavailable_connection() -> object:
        raise ConnectionError("unavailable")

    with pytest.raises(ImageSchemaError, match="unavailable"):
        PostgresImageJobStore(
            "postgresql://unused",
            connection_factory=unavailable_connection,
        )
    # Protocol compatibility can be checked without constructing an unsafe,
    # unvalidated production store.
    store = object.__new__(PostgresImageJobStore)
    assert isinstance(store, ImageJobStore)
    assert "FOR UPDATE SKIP LOCKED" in LEASE_SQL
    assert store.schema_version == CURRENT_IMAGE_SCHEMA_VERSION
    assert not hasattr(store, "initialize")
