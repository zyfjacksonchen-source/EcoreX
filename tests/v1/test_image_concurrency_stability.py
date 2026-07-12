from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import random
import threading
import time

from ecorex.image_orchestrator.cas import ImageContentStore
from ecorex.image_orchestrator.managed_provider import _parse_retry_after
from ecorex.image_orchestrator.models import (
    ImageJobStatus,
    ImageLimits,
    ImageSubmitRequest,
    ImageUsage,
)
from ecorex.image_orchestrator.postgres_store import PostgresImageJobStore
from ecorex.image_orchestrator.provider import (
    ProviderRateLimited,
    ProviderResult,
    ProviderState,
)
from ecorex.image_orchestrator.sqlite_schema import SQLiteImageSchemaManager
from ecorex.image_orchestrator.sqlite_store import SQLiteImageJobStore
from ecorex.image_orchestrator.worker import ImageJobWorker, ImageWorkerOutcome


PNG = b"\x89PNG\r\n\x1a\n" + b"ecorex-concurrency-stability"
PNG_SHA256 = hashlib.sha256(PNG).hexdigest()


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += timedelta(seconds=seconds)


def _store(path: Path, clock: MutableClock, **kwargs) -> SQLiteImageJobStore:
    SQLiteImageSchemaManager(path).migrate()
    return SQLiteImageJobStore(path, clock=clock, **kwargs)


def _request(
    request_id: str,
    *,
    deadline_seconds: int = 900,
    max_attempts: int = 4,
) -> ImageSubmitRequest:
    return ImageSubmitRequest(
        operation="generate",
        model_id="image-2",
        client_request_id=request_id,
        prompt="draw a bounded office image",
        deadline_seconds=deadline_seconds,
        max_attempts=max_attempts,
    )


def _completed(provider_id: str, model_id: str, request_id: str) -> ProviderResult:
    return ProviderResult(
        ProviderState.COMPLETED,
        provider_request_id=request_id,
        payload=PNG,
        mime_type="image/png",
        sha256=PNG_SHA256,
        usage=ImageUsage(provider_id, model_id, output_units=1, billed_units=1),
    )


def _worker(
    store: SQLiteImageJobStore,
    provider: object,
    content_store: ImageContentStore,
    clock: MutableClock,
    **kwargs,
) -> ImageJobWorker:
    return ImageJobWorker(
        store,
        provider,  # type: ignore[arg-type]
        content_store,
        clock=clock,
        lease_seconds=5,
        heartbeat_seconds=0.1,
        base_retry_seconds=0.01,
        max_retry_seconds=300,
        random_source=random.Random(11),
        **kwargs,
    )


def test_schedulable_deadline_expiry_is_atomic_idempotent_and_releases_capacity(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    limits = ImageLimits(
        max_queued_jobs=1,
        max_queued_weight=1,
        max_account_queued_jobs=1,
        max_account_queued_weight=1,
        max_running_jobs=1,
        max_account_running=1,
        max_model_running=1,
        max_operation_running=1,
    )
    path = tmp_path / "deadline.db"
    first_store = _store(path, clock, limits=limits)
    expired, _ = first_store.submit(
        "tenant-deadline",
        _request("deadline-request-0001", deadline_seconds=30),
    )
    clock.advance(31)

    # Competing restart recovery calls may all observe the same database, but
    # exactly one durable transition/event is allowed.
    with ThreadPoolExecutor(max_workers=16) as pool:
        reclaimed = list(
            pool.map(
                lambda _index: _store(path, clock, limits=limits).reclaim_expired(),
                range(32),
            )
        )
    assert sum(reclaimed) == 1

    restarted = _store(path, clock, limits=limits)
    projection = restarted.get(expired.job_id)
    assert projection.status is ImageJobStatus.FAILED
    assert projection.last_error_code == "deadline_exceeded"
    assert [
        event["event_type"]
        for event in restarted.events(expired.job_id)
    ].count("image.failed") == 1

    admitted, created = restarted.submit(
        "tenant-deadline",
        _request("deadline-request-0002", deadline_seconds=30),
    )
    assert created and admitted.status is ImageJobStatus.QUEUED
    assert restarted.metrics().queued == 1


def test_postgres_expiry_adapter_uses_locked_idempotent_terminal_transition() -> None:
    now = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    row = {
        "job_id": "imgjob_" + "a" * 32,
        "account_id": "tenant-postgres",
        "attempt": 0,
    }

    class Cursor:
        def __init__(self, *, one=None, many=None) -> None:
            self.one = one
            self.many = many or []

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.many

    class Connection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement, _params=()):
            normalized = " ".join(str(statement).split())
            self.statements.append(normalized)
            if normalized.startswith("SELECT * FROM image_jobs"):
                return Cursor(many=[row])
            if normalized.startswith("UPDATE image_jobs SET status='failed'"):
                return Cursor(one={"job_id": row["job_id"]})
            raise AssertionError(normalized)

    connection = Connection()
    store = object.__new__(PostgresImageJobStore)
    events: list[tuple[str, str, dict]] = []
    store._event = (  # type: ignore[method-assign]
        lambda _connection, job_id, _account_id, event_type, payload, _now: events.append(
            (job_id, event_type, payload)
        )
    )
    expired = PostgresImageJobStore._expire_schedulable_in(
        store,
        connection,
        now,
        account_id=None,
    )
    assert expired == 1
    assert "FOR UPDATE SKIP LOCKED" in connection.statements[0]
    assert "status IN ('queued','retry_wait') AND deadline<=%s" in connection.statements[1]
    assert events == [
        (
            row["job_id"],
            "image.failed",
            {"attempt": 0, "error_code": "deadline_exceeded"},
        )
    ]


def test_postgres_rate_limit_fence_locks_scope_and_keeps_the_longest_window() -> None:
    now = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)

    class Cursor:
        def __init__(self, row=None) -> None:
            self.row = row

        def fetchone(self):
            return self.row

    class Connection:
        def __init__(self) -> None:
            self.statements: list[tuple[str, tuple]] = []

        def execute(self, statement, params=()):
            normalized = " ".join(str(statement).split())
            self.statements.append((normalized, tuple(params)))
            if normalized.startswith("SELECT failure_count,open_until"):
                return Cursor(
                    {"failure_count": 2, "open_until": now + timedelta(seconds=10)}
                )
            if normalized.startswith("INSERT INTO image_breakers"):
                return Cursor()
            raise AssertionError(normalized)

    connection = Connection()
    store = object.__new__(PostgresImageJobStore)
    store.clock = lambda: now

    @contextmanager
    def transaction():
        yield connection

    store._transaction = transaction  # type: ignore[method-assign]
    retry_at = now + timedelta(seconds=120)
    open_until = PostgresImageJobStore.record_provider_rate_limit(
        store,
        "provider-postgres/image-2/generate/small",
        retry_at=retry_at,
        cooldown_seconds=30,
    )
    assert open_until == retry_at
    assert "FOR UPDATE" in connection.statements[0][0]
    assert connection.statements[1][1][2] == retry_at


def test_durable_half_open_allows_exactly_one_probe_across_concurrent_workers(
    tmp_path: Path,
) -> None:
    class BlockingProvider:
        provider_id = "provider-half-open"

        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return _completed(self.provider_id, job.request.model_id, "half-open-result")

        async def recover(self, job, *, idempotency_key: str, provider_request_id):
            raise AssertionError("a never-submitted circuit wait must not recover")

        async def cancel(self, job, *, idempotency_key: str, provider_request_id):
            return None

    async def scenario() -> None:
        clock = MutableClock()
        path = tmp_path / "half-open.db"
        store = _store(
            path,
            clock,
            limits=ImageLimits(
                max_running_jobs=32,
                max_account_running=32,
                max_model_running=32,
                max_operation_running=32,
            ),
        )
        for index in range(16):
            store.submit("tenant-half-open", _request(f"half-open-request-{index:04d}"))
        scope = "provider-half-open/image-2/generate/small"
        store.record_provider_failure(scope, threshold=1, cooldown_seconds=10)
        clock.advance(11)

        provider = BlockingProvider()
        image_worker = _worker(
            store,
            provider,
            ImageContentStore(tmp_path / "half-open-cas"),
            clock,
            breaker_threshold=1,
            breaker_cooldown_seconds=10,
            breaker_probe_seconds=60,
        )
        tasks = [
            asyncio.create_task(image_worker.run_once(f"half-open-worker-{index:03d}"))
            for index in range(16)
        ]
        await asyncio.wait_for(provider.started.wait(), timeout=2)
        async def all_non_probe_calls_decided() -> None:
            while sum(task.done() for task in tasks) < 15:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(all_non_probe_calls_decided(), timeout=5)
        assert provider.calls == 1
        provider.release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        assert sum(item.outcome is ImageWorkerOutcome.COMPLETED for item in results) == 1
        assert sum(item.outcome is ImageWorkerOutcome.RETRY_SCHEDULED for item in results) == 15
        assert provider.calls == 1

    asyncio.run(scenario())


def test_retry_after_is_bounded_and_initial_429_retries_submit_not_recover(
    tmp_path: Path,
) -> None:
    fixed = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    assert _parse_retry_after("0", now=fixed) == 1.0
    assert _parse_retry_after("999999", now=fixed) == 3600.0
    assert _parse_retry_after("not-a-delay", now=fixed) is None
    assert _parse_retry_after("Sun, 12 Jul 2026 08:02:00 GMT", now=fixed) == 120.0

    class RateLimitedOnceProvider:
        provider_id = "provider-rate-limit"

        def __init__(self) -> None:
            self.submit_calls = 0
            self.recover_calls = 0

        async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
            self.submit_calls += 1
            if self.submit_calls == 1:
                raise ProviderRateLimited(
                    retry_after_seconds=120,
                    recovery_required=False,
                )
            return _completed(self.provider_id, job.request.model_id, "rate-result")

        async def recover(self, job, *, idempotency_key: str, provider_request_id):
            self.recover_calls += 1
            raise AssertionError("a known-unaccepted 429 must not recover")

        async def cancel(self, job, *, idempotency_key: str, provider_request_id):
            return None

    async def scenario() -> None:
        clock = MutableClock()
        store = _store(tmp_path / "rate.db", clock)
        accepted, _ = store.submit("tenant-rate", _request("rate-request-0001"))
        provider = RateLimitedOnceProvider()
        image_worker = _worker(
            store,
            provider,
            ImageContentStore(tmp_path / "rate-cas"),
            clock,
        )
        first = await image_worker.run_once("rate-worker-first")
        assert first.outcome is ImageWorkerOutcome.RETRY_SCHEDULED
        waiting = store.get(accepted.job_id)
        assert waiting.available_at >= clock() + timedelta(seconds=120)
        assert waiting.checkpoint["provider_started"] is False
        clock.advance(121)
        second = await image_worker.run_once("rate-worker-second")
        assert second.outcome is ImageWorkerOutcome.COMPLETED
        assert provider.submit_calls == 2
        assert provider.recover_calls == 0

        class MissingHintProvider(RateLimitedOnceProvider):
            provider_id = "provider-rate-limit-no-hint"

            async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
                self.submit_calls += 1
                raise ProviderRateLimited()

        fallback_clock = MutableClock()
        fallback_store = _store(tmp_path / "rate-no-hint.db", fallback_clock)
        fallback_store.submit(
            "tenant-rate-fallback",
            _request("rate-fallback-request-0001"),
        )
        fallback_provider = MissingHintProvider()
        fallback_worker = _worker(
            fallback_store,
            fallback_provider,
            ImageContentStore(tmp_path / "rate-no-hint-cas"),
            fallback_clock,
            breaker_cooldown_seconds=30,
        )
        fallback = await fallback_worker.run_once("rate-worker-fallback")
        assert fallback.outcome is ImageWorkerOutcome.RETRY_SCHEDULED
        assert fallback_store.breaker_open_until(
            "provider-rate-limit-no-hint/image-2/generate/small"
        ) == fallback_clock() + timedelta(seconds=30)

    asyncio.run(scenario())


def test_rate_limit_fences_the_scope_then_admits_one_half_open_probe(
    tmp_path: Path,
) -> None:
    class ScopeRateProvider:
        provider_id = "provider-scope-rate-limit"

        def __init__(self) -> None:
            self.submit_calls = 0
            self.probe_started = asyncio.Event()
            self.release_probe = asyncio.Event()

        async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
            self.submit_calls += 1
            if self.submit_calls == 1:
                raise ProviderRateLimited(retry_after_seconds=60)
            self.probe_started.set()
            await self.release_probe.wait()
            return _completed(self.provider_id, job.request.model_id, "scope-rate-probe")

        async def recover(self, job, *, idempotency_key: str, provider_request_id):
            raise AssertionError("known-unaccepted rate limits must not recover")

        async def cancel(self, job, *, idempotency_key: str, provider_request_id):
            return None

    async def scenario() -> None:
        clock = MutableClock()
        limits = ImageLimits(
            max_running_jobs=32,
            max_account_running=32,
            max_model_running=32,
            max_operation_running=32,
        )
        store = _store(tmp_path / "scope-rate.db", clock, limits=limits)
        for index in range(9):
            store.submit(
                "tenant-scope-rate",
                _request(f"scope-rate-request-{index:04d}"),
            )
        provider = ScopeRateProvider()
        image_worker = _worker(
            store,
            provider,
            ImageContentStore(tmp_path / "scope-rate-cas"),
            clock,
            breaker_threshold=5,
            breaker_cooldown_seconds=30,
            breaker_probe_seconds=120,
        )

        first = await image_worker.run_once("scope-rate-first")
        assert first.outcome is ImageWorkerOutcome.RETRY_SCHEDULED
        scope = "provider-scope-rate-limit/image-2/generate/small"
        assert store.breaker_open_until(scope) == clock() + timedelta(seconds=60)

        fenced = await asyncio.gather(
            *(
                image_worker.run_once(f"scope-rate-fenced-{index:03d}")
                for index in range(8)
            )
        )
        assert all(
            result.outcome is ImageWorkerOutcome.RETRY_SCHEDULED
            for result in fenced
        )
        assert provider.submit_calls == 1

        clock.advance(61)
        probes = [
            asyncio.create_task(
                image_worker.run_once(f"scope-rate-probe-{index:03d}")
            )
            for index in range(8)
        ]
        await asyncio.wait_for(provider.probe_started.wait(), timeout=2)

        async def other_probe_contenders_are_fenced() -> None:
            while sum(task.done() for task in probes) < 7:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(other_probe_contenders_are_fenced(), timeout=5)
        assert provider.submit_calls == 2
        provider.release_probe.set()
        results = await asyncio.wait_for(asyncio.gather(*probes), timeout=5)
        assert sum(
            result.outcome is ImageWorkerOutcome.COMPLETED for result in results
        ) == 1
        assert sum(
            result.outcome is ImageWorkerOutcome.RETRY_SCHEDULED
            for result in results
        ) == 7
        assert provider.submit_calls == 2

    asyncio.run(scenario())


def test_slow_cas_staging_renews_lease_until_atomic_completion(tmp_path: Path) -> None:
    class ImmediateProvider:
        provider_id = "provider-slow-cas"

        async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
            return _completed(self.provider_id, job.request.model_id, "slow-cas-result")

        async def recover(self, job, *, idempotency_key: str, provider_request_id):
            raise AssertionError("slow CAS staging must not force provider recovery")

        async def cancel(self, job, *, idempotency_key: str, provider_request_id):
            return None

    class SlowClockCAS(ImageContentStore):
        def __init__(self, root: Path, clock: MutableClock) -> None:
            super().__init__(root)
            self.clock = clock

        def put(self, payload, **kwargs):
            for _ in range(3):
                self.clock.advance(2)
                time.sleep(0.12)
            return super().put(payload, **kwargs)

    async def scenario() -> None:
        clock = MutableClock()
        store = _store(tmp_path / "slow-cas.db", clock)
        accepted, _ = store.submit("tenant-cas", _request("slow-cas-request-0001"))
        image_worker = _worker(
            store,
            ImmediateProvider(),
            SlowClockCAS(tmp_path / "slow-cas", clock),
            clock,
        )
        outcome = await image_worker.run_once("slow-cas-worker")
        assert outcome.outcome is ImageWorkerOutcome.COMPLETED
        assert store.get(accepted.job_id).status is ImageJobStatus.COMPLETED
        assert sum(
            event["event_type"] == "image.heartbeat"
            for event in store.events(accepted.job_id)
        ) >= 2

    asyncio.run(scenario())


def test_commit_fault_recovers_staged_cas_without_second_provider_effect(
    tmp_path: Path,
) -> None:
    class CountingProvider:
        provider_id = "provider-staged-recovery"

        def __init__(self) -> None:
            self.submit_calls = 0
            self.recover_calls = 0

        async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
            self.submit_calls += 1
            return _completed(self.provider_id, job.request.model_id, "staged-result")

        async def recover(self, job, *, idempotency_key: str, provider_request_id):
            self.recover_calls += 1
            raise AssertionError("durable staged CAS must complete without provider recovery")

        async def cancel(self, job, *, idempotency_key: str, provider_request_id):
            return None

    async def scenario() -> None:
        clock = MutableClock()
        path = tmp_path / "staged.db"
        armed = {"value": True}

        def fault(phase: str, _job_id: str) -> None:
            if phase == "before_commit" and armed["value"]:
                armed["value"] = False
                raise RuntimeError("controlled commit fault")

        store = _store(path, clock, fault_hook=fault)
        accepted, _ = store.submit("tenant-staged", _request("staged-request-0001"))
        provider = CountingProvider()
        content_store = ImageContentStore(tmp_path / "staged-cas")
        first_worker = _worker(store, provider, content_store, clock)
        first = await first_worker.run_once("staged-worker-first")
        assert first.outcome is ImageWorkerOutcome.RETRY_SCHEDULED
        waiting = store.get(accepted.job_id)
        assert waiting.status is ImageJobStatus.RETRY_WAIT
        assert waiting.checkpoint["staged_result"]["sha256"] == PNG_SHA256

        clock.advance(2)
        restarted = _store(path, clock)
        second_worker = _worker(restarted, provider, content_store, clock)
        second = await second_worker.run_once("staged-worker-second")
        assert second.outcome is ImageWorkerOutcome.COMPLETED
        assert restarted.get(accepted.job_id).status is ImageJobStatus.COMPLETED
        assert provider.submit_calls == 1
        assert provider.recover_calls == 0
        assert restarted.metrics().usage_billed_units == 1
        assert sum(
            event["event_type"] == "image.completed"
            for event in restarted.events(accepted.job_id)
        ) == 1

    asyncio.run(scenario())


def test_lost_commit_response_resolves_from_durable_terminal_fact(tmp_path: Path) -> None:
    class ImmediateProvider:
        provider_id = "provider-commit-response"

        def __init__(self) -> None:
            self.submit_calls = 0

        async def submit(self, job, *, idempotency_key: str) -> ProviderResult:
            self.submit_calls += 1
            return _completed(self.provider_id, job.request.model_id, "commit-response")

        async def recover(self, job, *, idempotency_key: str, provider_request_id):
            raise AssertionError("a committed terminal fact must not recover")

        async def cancel(self, job, *, idempotency_key: str, provider_request_id):
            return None

    class LostCommitResponseStore(SQLiteImageJobStore):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.lose_once = True

        def complete(self, *args, **kwargs):
            completed = super().complete(*args, **kwargs)
            if self.lose_once:
                self.lose_once = False
                raise ConnectionError("controlled response loss after commit")
            return completed

    async def scenario() -> None:
        clock = MutableClock()
        path = tmp_path / "commit-response.db"
        SQLiteImageSchemaManager(path).migrate()
        store = LostCommitResponseStore(path, clock=clock)
        accepted, _ = store.submit(
            "tenant-commit-response",
            _request("commit-response-request-0001"),
        )
        provider = ImmediateProvider()
        image_worker = _worker(
            store,
            provider,
            ImageContentStore(tmp_path / "commit-response-cas"),
            clock,
        )
        outcome = await image_worker.run_once("commit-response-worker")
        assert outcome.outcome is ImageWorkerOutcome.COMPLETED
        assert outcome.reason == "commit_observed"
        assert store.get(accepted.job_id).status is ImageJobStatus.COMPLETED
        assert provider.submit_calls == 1
        assert sum(
            event["event_type"] == "image.completed"
            for event in store.events(accepted.job_id)
        ) == 1

    asyncio.run(scenario())
