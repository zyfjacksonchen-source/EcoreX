from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from ecorex.protocol import CreateThreadRequest, JobStatus
from ecorex.runtime import RuntimeKernel
from ecorex.runtime.invariant_guard import RuntimeExecutionGate
from ecorex.sharing import (
    PublishedShare,
    ShareOperationWorker,
    ShareRepository,
    ShareSnapshotService,
    ShareStatus,
    ShareWorkerOutcome,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 10, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class Publisher:
    def __init__(self) -> None:
        self.publish_keys: list[str] = []
        self.revoke_keys: list[str] = []
        self.failures = 0
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.advance_clock: Clock | None = None
        self.advance_by = timedelta(hours=2)

    async def publish(self, payload, *, idempotency_key):
        self.publish_keys.append(idempotency_key)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.failures:
            self.failures -= 1
            raise TimeoutError("SECRET provider response must never be persisted")
        if self.advance_clock is not None:
            self.advance_clock.value += self.advance_by
        return PublishedShare(
            remote_snapshot_id="remote_" + payload.share_id,
            public_url=f"https://share.ecorex.test/s/{payload.share_id}",
        )

    async def revoke(self, remote_snapshot_id, *, idempotency_key):
        self.revoke_keys.append(idempotency_key)


def stack(tmp_path, *, max_attempts: int = 3):
    clock = Clock()
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    repository = ShareRepository(kernel.database, jobs=kernel.jobs)
    publisher = Publisher()
    service = ShareSnapshotService(
        kernel,
        repository=repository,
        publisher=publisher,
        account_id="account-1",
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=clock,
        max_attempts=max_attempts,
    )
    worker = ShareOperationWorker(
        repository,
        publisher,
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=clock,
        retry_delay_seconds=0,
        lease_seconds=5,
    )
    thread = kernel.create_thread(
        CreateThreadRequest(title="分享恢复", client_request_id="thread-create")
    )
    return clock, kernel, repository, publisher, service, worker, thread


def rows(path, sql: str):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(sql).fetchall()


def test_share_and_one_sanitized_job_are_committed_atomically(tmp_path) -> None:
    _clock, _kernel, _repo, publisher, service, _worker, thread = stack(tmp_path)

    async def create_twice():
        return await asyncio.gather(
            service.create(
                thread.thread_id,
                expires_in_hours=24,
                client_request_id="same-request",
            ),
            service.create(
                thread.thread_id,
                expires_in_hours=24,
                client_request_id="same-request",
            ),
        )

    first, second = asyncio.run(create_twice())
    assert first == second
    assert first.status is ShareStatus.PUBLISHING
    assert publisher.publish_keys == []
    jobs = rows(tmp_path / "runtime.db", "SELECT * FROM jobs WHERE kind='share_publish'")
    bindings = rows(tmp_path / "runtime.db", "SELECT * FROM share_job_bindings")
    assert len(jobs) == len(bindings) == 1
    assert json.loads(jobs[0]["payload_json"]) == {
        "action": "publish",
        "schema_version": 1,
        "share_id": first.share_id,
    }
    encoded = jobs[0]["payload_json"] + (jobs[0]["checkpoint_json"] or "")
    assert "account-1" not in encoded
    assert "public_url" not in encoded
    assert "messages" not in encoded
    assert "artifacts" not in encoded


def test_job_insert_failure_rolls_back_share_and_revoke_state(tmp_path) -> None:
    _clock, _kernel, _repo, _publisher, service, worker, thread = stack(tmp_path)
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        connection.execute(
            "CREATE TRIGGER reject_share_publish BEFORE INSERT ON jobs "
            "WHEN NEW.kind='share_publish' BEGIN "
            "SELECT RAISE(ABORT, 'injected publish failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected publish failure"):
        asyncio.run(
            service.create(
                thread.thread_id,
                expires_in_hours=24,
                client_request_id="atomic-publish",
            )
        )
    assert rows(tmp_path / "runtime.db", "SELECT * FROM share_snapshots") == []

    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        connection.execute("DROP TRIGGER reject_share_publish")
    queued = asyncio.run(
        service.create(
            thread.thread_id,
            expires_in_hours=24,
            client_request_id="publish-ok",
        )
    )
    asyncio.run(worker.run_once("publisher"))
    published = service.get(queued.share_id)
    assert published.status is ShareStatus.PUBLISHED
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        connection.execute(
            "CREATE TRIGGER reject_share_revoke BEFORE INSERT ON jobs "
            "WHEN NEW.kind='share_revoke' BEGIN "
            "SELECT RAISE(ABORT, 'injected revoke failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected revoke failure"):
        asyncio.run(
            service.revoke(published.share_id, client_request_id="atomic-revoke")
        )
    unchanged = service.get(published.share_id)
    assert unchanged.status is ShareStatus.PUBLISHED
    assert unchanged.public_url == published.public_url
    assert rows(
        tmp_path / "runtime.db",
        "SELECT * FROM share_operations WHERE client_request_id='atomic-revoke'",
    ) == []


def test_concurrent_workers_execute_same_request_once(tmp_path) -> None:
    _clock, _kernel, _repo, publisher, service, worker, thread = stack(tmp_path)
    created = asyncio.run(
        service.create(
            thread.thread_id, expires_in_hours=24, client_request_id="once"
        )
    )
    async def run_both():
        return await asyncio.gather(worker.run_once("worker-a"), worker.run_once("worker-b"))

    outcomes = asyncio.run(run_both())
    assert {item.outcome for item in outcomes} == {
        ShareWorkerOutcome.COMPLETED,
        ShareWorkerOutcome.IDLE,
    }
    assert publisher.publish_keys == [created.share_id]
    assert service.get(created.share_id).status is ShareStatus.PUBLISHED


def test_concurrent_same_revoke_request_has_one_job_and_one_cloud_call(tmp_path) -> None:
    clock, _kernel, _repo, publisher, service, worker, thread = stack(tmp_path)
    created = asyncio.run(
        service.create(
            thread.thread_id, expires_in_hours=24, client_request_id="publish"
        )
    )
    clock.value += timedelta(minutes=1)
    duplicate_create = asyncio.run(
        service.create(
            thread.thread_id, expires_in_hours=24, client_request_id="publish"
        )
    )
    assert duplicate_create == created
    asyncio.run(worker.run_once("publish-worker"))

    async def revoke_twice():
        return await asyncio.gather(
            service.revoke(created.share_id, client_request_id="same-revoke"),
            service.revoke(created.share_id, client_request_id="same-revoke"),
        )

    first, second = asyncio.run(revoke_twice())
    assert first == second
    assert first.status is ShareStatus.REVOKING
    clock.value += timedelta(minutes=1)
    assert asyncio.run(
        service.revoke(created.share_id, client_request_id="same-revoke")
    ) == first
    revoke_jobs = rows(
        tmp_path / "runtime.db", "SELECT * FROM jobs WHERE kind='share_revoke'"
    )
    assert len(revoke_jobs) == 1
    asyncio.run(worker.run_once("revoke-worker"))
    assert publisher.revoke_keys == [f"{created.share_id}:revoke"]
    assert service.get(created.share_id).status is ShareStatus.REVOKED


def test_transient_failure_retries_same_external_key_and_dead_letters_safely(tmp_path) -> None:
    clock, _kernel, repository, publisher, service, worker, thread = stack(
        tmp_path, max_attempts=2
    )
    publisher.failures = 1
    created = asyncio.run(
        service.create(
            thread.thread_id, expires_in_hours=24, client_request_id="retry"
        )
    )
    first = asyncio.run(worker.run_once("worker"))
    assert first.outcome is ShareWorkerOutcome.RETRY_SCHEDULED
    assert service.get(created.share_id).status is ShareStatus.PUBLISHING
    second = asyncio.run(worker.run_once("worker"))
    assert second.outcome is ShareWorkerOutcome.COMPLETED
    assert publisher.publish_keys == [created.share_id, created.share_id]
    assert service.get(created.share_id).status is ShareStatus.PUBLISHED

    publisher.failures = 2
    failed = asyncio.run(
        service.create(
            thread.thread_id, expires_in_hours=24, client_request_id="dead-letter"
        )
    )
    assert asyncio.run(worker.run_once("worker")).outcome is ShareWorkerOutcome.RETRY_SCHEDULED
    assert asyncio.run(worker.run_once("worker")).outcome is ShareWorkerOutcome.FAILED
    projection = service.get(failed.share_id)
    assert projection.status is ShareStatus.FAILED
    assert projection.error_code == "timeouterror"
    raw = (tmp_path / "runtime.db").read_bytes()
    assert b"SECRET provider response" not in raw
    repository.reconcile_terminal_jobs(now=clock())
    repository.reconcile_terminal_jobs(now=clock())
    terminal = rows(
        tmp_path / "runtime.db",
        "SELECT status FROM jobs WHERE kind='share_publish' "
        "ORDER BY rowid DESC LIMIT 1",
    )[0]
    assert terminal["status"] == JobStatus.DEAD_LETTER.value
    repository_events = rows(
        tmp_path / "runtime.db",
        "SELECT event_type, payload_json FROM events "
        "WHERE event_type='share.failed'",
    )
    assert len(repository_events) == 1
    assert "SECRET" not in repository_events[0]["payload_json"]


def test_expired_lease_is_reclaimed_after_restart_with_stable_idempotency(tmp_path) -> None:
    clock, _kernel, repository, publisher, service, _worker, thread = stack(tmp_path)
    created = asyncio.run(
        service.create(
            thread.thread_id, expires_in_hours=24, client_request_id="restart"
        )
    )
    leased = repository.jobs.lease_next(
        "crashed", kinds=["share_publish"], lease_seconds=5, now=clock()
    )
    assert leased is not None and leased.lease_token
    repository.jobs.start(
        leased.job_id, "crashed", leased.lease_token, now=clock()
    )
    repository.jobs.heartbeat(
        leased.job_id,
        "crashed",
        leased.lease_token,
        lease_seconds=5,
        checkpoint={
            "schema_version": 1,
            "phase": "external_requested",
            "action": "publish",
            "share_id": created.share_id,
            "external_idempotency_key": created.share_id,
        },
        now=clock(),
    )
    clock.value += timedelta(seconds=6)

    restarted_repository = ShareRepository(repository.database)
    restarted = ShareOperationWorker(
        restarted_repository,
        publisher,
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=clock,
        retry_delay_seconds=0,
        lease_seconds=5,
    )
    result = asyncio.run(restarted.run_once("replacement"))
    assert result.outcome is ShareWorkerOutcome.COMPLETED
    durable = restarted_repository.jobs.get(leased.job_id)
    assert durable.status is JobStatus.COMPLETED
    assert durable.attempt == 2
    assert publisher.publish_keys == [created.share_id]


def test_expiry_and_revoke_fence_never_expose_late_public_url(tmp_path) -> None:
    clock, _kernel, _repo, publisher, service, worker, thread = stack(tmp_path)
    publisher.advance_clock = clock
    created = asyncio.run(
        service.create(
            thread.thread_id, expires_in_hours=1, client_request_id="late"
        )
    )
    clock.value += timedelta(minutes=59, seconds=58)
    publisher.advance_by = timedelta(seconds=3)
    assert asyncio.run(worker.run_once("worker")).outcome is ShareWorkerOutcome.COMPLETED
    fenced = service.get(created.share_id)
    assert fenced.status is ShareStatus.EXPIRED
    assert fenced.public_url is None
    assert asyncio.run(worker.run_once("worker")).outcome is ShareWorkerOutcome.COMPLETED
    assert service.get(created.share_id).status is ShareStatus.REVOKED
    assert publisher.revoke_keys == [f"{created.share_id}:revoke"]


def test_revoke_while_publish_in_flight_is_durable_and_fenced(tmp_path) -> None:
    _clock, _kernel, _repo, publisher, service, worker, thread = stack(tmp_path)
    publisher.release = asyncio.Event()

    async def scenario():
        created = await service.create(
            thread.thread_id, expires_in_hours=24, client_request_id="race-create"
        )
        publishing = asyncio.create_task(worker.run_once("publish-worker"))
        await publisher.started.wait()
        revoking = await service.revoke(
            created.share_id, client_request_id="race-revoke"
        )
        assert revoking.status is ShareStatus.REVOKING
        assert revoking.public_url is None
        publisher.release.set()
        assert (await publishing).outcome is ShareWorkerOutcome.COMPLETED
        fenced = service.get(created.share_id)
        assert fenced.status is ShareStatus.REVOKING
        assert fenced.public_url is None
        assert (await worker.run_once("revoke-worker")).outcome is ShareWorkerOutcome.COMPLETED
        return created

    created = asyncio.run(scenario())
    assert service.get(created.share_id).status is ShareStatus.REVOKED
    assert publisher.publish_keys == [created.share_id]
    assert publisher.revoke_keys == [f"{created.share_id}:revoke"]
    events = rows(
        tmp_path / "runtime.db",
        "SELECT event_type FROM events WHERE event_type LIKE 'share.%' ORDER BY seq",
    )
    assert [row["event_type"] for row in events] == [
        "share.publish_fenced",
        "share.revoked",
    ]


def test_share_epoch_close_rejects_late_publish_and_maintenance_commit(
    tmp_path, monkeypatch
) -> None:
    clock, kernel, repository, publisher, service, worker, thread = stack(tmp_path)
    clock.value = datetime.now(timezone.utc)
    created = asyncio.run(
        service.create(
            thread.thread_id,
            expires_in_hours=1,
            client_request_id="epoch-share",
        )
    )
    gate = RuntimeExecutionGate()
    repository.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    assert gate.snapshot().healthy
    publisher.release = asyncio.Event()

    async def close_during_publish():
        loop_progressed = asyncio.Event()
        publishing = asyncio.create_task(worker.run_once("epoch-publisher"))
        await asyncio.wait_for(publisher.started.wait(), timeout=5)
        await asyncio.sleep(0)
        loop_progressed.set()
        assert loop_progressed.is_set()
        gate.mark_critical(error_code="share_publish_epoch_closed")
        publisher.release.set()
        return await publishing

    result = asyncio.run(close_during_publish())
    assert result.outcome is ShareWorkerOutcome.FAILED
    assert result.reason == "execution_epoch_closed"
    projection = service.get(created.share_id)
    assert projection.status is ShareStatus.PUBLISHING
    assert projection.public_url is None
    with repository.database.reader() as connection:
        durable = connection.execute(
            "SELECT status FROM jobs WHERE kind='share_publish'"
        ).fetchone()
    assert durable["status"] == JobStatus.RUNNING.value
    assert asyncio.run(worker.run_once("critical-worker")).outcome is ShareWorkerOutcome.IDLE
    assert publisher.publish_keys == [created.share_id]

    maintenance_root = tmp_path / "maintenance"
    maintenance_root.mkdir()
    (
        second_clock,
        second_kernel,
        second_repository,
        _publisher,
        second_service,
        second_worker,
        second_thread,
    ) = stack(maintenance_root)
    second_clock.value = datetime.now(timezone.utc)
    expiring = asyncio.run(
        second_service.create(
            second_thread.thread_id,
            expires_in_hours=1,
            client_request_id="maintenance-expiry",
        )
    )
    second_gate = RuntimeExecutionGate()
    second_repository.jobs.bind_execution_gate(second_gate)
    second_gate.record_report(second_kernel.invariants.audit())
    second_clock.value += timedelta(hours=2)
    original_assert = second_gate.assert_permit
    closed = False

    def close_before_commit(permit) -> None:
        nonlocal closed
        closed = True
        second_gate.request_critical(error_code="share_maintenance_precommit")
        original_assert(permit)

    monkeypatch.setattr(second_gate, "assert_permit", close_before_commit)
    assert second_worker.maintenance_once(subject="precommit") is False
    assert closed
    with second_repository.database.reader() as connection:
        raw = connection.execute(
            "SELECT status FROM share_snapshots WHERE share_id=?",
            (expiring.share_id,),
        ).fetchone()
    assert raw["status"] == ShareStatus.PUBLISHING.value
