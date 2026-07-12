from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest

from ecorex.protocol import (
    CreateTurnRequest,
    ItemKind,
    ItemStatus,
    PublicToolActivity,
    TurnStatus,
)
from ecorex.runtime import (
    RuntimeInvariantAuditor,
    RuntimeInvariantError,
    RuntimeKernel,
    SQLiteDatabase,
    AgentTurnWorker,
    TurnSnapshotContext,
    WorkerOutcome,
)
from ecorex.runtime.errors import ConflictError
from ecorex.update import ReleaseChannel, RuntimeUpdateService


class SimulatedCommitCrash(BaseException):
    pass


class CrashOnEvent:
    """Fault-injection sink that fails inside the caller's SQLite transaction."""

    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        self.armed = True

    def record_in_transaction(self, _connection, event) -> None:
        if self.armed and event.event_type == self.event_type:
            raise SimulatedCommitCrash(event.event_type)


def test_executescript_cannot_precommit_an_owned_runtime_transaction(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")

    with pytest.raises(SimulatedCommitCrash):
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO threads(thread_id, status, metadata_json, created_at, updated_at) "
                "VALUES ('must-rollback', 'active', '{}', 'now', 'now')"
            )
            connection.executescript(
                "CREATE TABLE transaction_fault_probe(value TEXT);"
            )
            raise SimulatedCommitCrash("after executescript")

    with database.reader() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM threads WHERE thread_id = 'must-rollback'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'transaction_fault_probe'"
        ).fetchone()[0] == 0


def test_worker_sqlite_wait_does_not_block_the_asgi_event_loop(
    tmp_path, monkeypatch
) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    entered = threading.Event()
    release = threading.Event()

    def contended_lease(*_args, **_kwargs):
        entered.set()
        release.wait(timeout=2)
        return None

    monkeypatch.setattr(kernel.jobs, "lease_next", contended_lease)
    worker = AgentTurnWorker(
        kernel,
        gateway=object(),  # unused because the simulated scheduler is idle
        capabilities=object(),
    )

    async def scenario():
        task = asyncio.create_task(worker.run_once("worker-responsive"))
        await asyncio.sleep(0.02)
        event_loop_remained_responsive = entered.is_set() and not task.done()
        release.set()
        return event_loop_remained_responsive, await task

    responsive, result = asyncio.run(scenario())
    assert responsive is True
    assert result.outcome is WorkerOutcome.IDLE


def test_update_repository_wait_does_not_block_the_asgi_event_loop(
    tmp_path, monkeypatch
) -> None:
    class NoUpdateFeed:
        def latest(self, **_kwargs):
            return None

    service = RuntimeUpdateService(
        tmp_path / "runtime.db",
        coordinator=object(),  # unused when the signed feed has no candidate
        feed=NoUpdateFeed(),
        artifact_id="core-windows-x64",
        current_version="1.0.0",
        channel=ReleaseChannel.STABLE,
        platform="windows",
        architecture="x64",
    )
    original_snapshot = service.snapshot
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def contended_snapshot():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            release.wait(timeout=2)
        return original_snapshot()

    monkeypatch.setattr(service, "snapshot", contended_snapshot)

    async def scenario():
        task = asyncio.create_task(service.check_now())
        await asyncio.sleep(0.02)
        event_loop_remained_responsive = entered.is_set() and not task.done()
        release.set()
        return event_loop_remained_responsive, await task

    responsive, snapshot = asyncio.run(scenario())
    assert responsive is True
    assert snapshot.state == "idle"


def _running_turn(kernel: RuntimeKernel, *, max_attempts: int = 3):
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="fault-injection", client_message_id="message-1"),
    )
    # Establish the simulated scheduler clock when the scenario runs, not when
    # pytest imports this module.  A full suite can spend longer than one lease
    # window between collection and execution.
    now = datetime.now(timezone.utc)
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET max_attempts = ?, available_at = ? WHERE job_id = ?",
            (max_attempts, now.isoformat(), created.job.job_id),
        )
    leased = kernel.jobs.lease_next("worker-1", lease_seconds=120, now=now)
    assert leased is not None and leased.lease_token
    kernel.jobs.start(
        leased.job_id,
        "worker-1",
        leased.lease_token,
        now=now,
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.MODEL_REQUESTED)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.STREAMING)
    return thread, created, leased


def _after_lease_expiry(leased) -> datetime:
    assert leased.lease_expires_at is not None
    return leased.lease_expires_at + timedelta(microseconds=1)


def test_commit_fault_rolls_back_event_projection_and_job_as_one_unit(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    thread = kernel.create_thread()
    before = kernel.events.watermark(thread.thread_id)
    crash = CrashOnEvent("turn.queued")
    kernel.events.event_sink = crash

    with pytest.raises(SimulatedCommitCrash):
        kernel.create_turn(
            thread.thread_id,
            CreateTurnRequest(input="must rollback", client_message_id="rollback-1"),
        )

    restarted = RuntimeKernel(path)
    projection = restarted.projection(thread.thread_id)
    assert projection.turns == []
    assert projection.items == []
    assert restarted.events.watermark(thread.thread_id) == before
    with restarted.database.reader() as connection:
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    RuntimeInvariantAuditor(path).audit().raise_if_invalid()


def test_turn_event_cannot_replace_its_accepted_snapshot_context(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    thread = kernel.create_thread()
    created = kernel.create_turn(thread.thread_id, CreateTurnRequest(input="context"))
    before = kernel.events.watermark(thread.thread_id)

    with pytest.raises(ConflictError, match="snapshot context is immutable"):
        kernel.events.append(
            thread_id=thread.thread_id,
            turn_id=created.turn.turn_id,
            event_type="test.snapshot_drift",
            config_snapshot_id="cfg_replaced_after_acceptance",
        )

    assert kernel.events.watermark(thread.thread_id) == before
    RuntimeInvariantAuditor(path).audit().raise_if_invalid()


def test_invariant_auditor_validates_execution_batch_payload_authority(
    tmp_path,
) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="batch context", client_message_id="batch-message"),
    )
    context = TurnSnapshotContext(
        config_snapshot_id="config-batch",
        capability_snapshot_id="capability-batch",
        permission_snapshot_id="permission-batch",
        model_catalog_snapshot_id="models-batch",
        extension_snapshot_id="extensions-batch",
    )
    batch = kernel.turn_execution_batches.create(
        turn_id=created.turn.turn_id,
        first_revision_ordinal=0,
        last_revision_ordinal=0,
        snapshot_context=context,
    )
    kernel.events.append(
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        event_type="turn.execution_batch.bound",
        payload={
            "execution_batch_id": batch.batch_id,
            "first_revision_ordinal": 0,
            "last_revision_ordinal": 0,
            **context.to_dict(),
        },
    )
    RuntimeInvariantAuditor(kernel.database).audit().raise_if_invalid()

    drifted = kernel.events.append(
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        event_type="model.requested",
        payload={
            "execution_batch_id": batch.batch_id,
            "first_revision_ordinal": 1,
            "last_revision_ordinal": 0,
            "capability_snapshot_id": "capability-drift",
        },
    )
    report = RuntimeInvariantAuditor(kernel.database).audit()
    violations = [
        value
        for value in report.violations
        if value.entity_id == drifted.event_id
    ]
    assert {value.code for value in violations} == {
        "execution_batch_payload_drift"
    }
    assert "capability_snapshot_id" in violations[0].detail
    assert "first_revision_ordinal" in violations[0].detail

    incomplete = kernel.events.append(
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        event_type="model.continuation_requested",
        payload={"execution_batch_id": batch.batch_id},
    )
    report = RuntimeInvariantAuditor(kernel.database).audit()
    assert {
        value.code
        for value in report.violations
        if value.entity_id == incomplete.event_id
    } == {"execution_batch_payload_incomplete"}


def test_terminal_commit_fault_is_replayable_without_partial_dependents(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    _thread, created, leased = _running_turn(kernel)
    open_item = kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.TOOL_CALL,
        content=PublicToolActivity(
            tool_call_id="terminal-commit-tool-call",
            tool_id="read",
            tool_name="read",
            display_label="读取工作资料",
            phase="running",
            status="in_progress",
            risk="low",
            argument_summary="正在读取工作资料",
            argument_sha256="0" * 64,
        ).model_dump(mode="json"),
        status=ItemStatus.IN_PROGRESS,
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.FINALIZING)
    crash = CrashOnEvent("item.status_changed")
    kernel.events.event_sink = crash

    with pytest.raises(SimulatedCommitCrash):
        kernel.finish_turn_job(
            job_id=leased.job_id,
            worker_id="worker-1",
            lease_token=leased.lease_token,
            target=TurnStatus.COMPLETED,
        )

    restarted = RuntimeKernel(path)
    assert restarted.get_turn(created.turn.turn_id).status is TurnStatus.FINALIZING
    assert restarted.jobs.get(leased.job_id).status.value == "running"
    assert next(
        value
        for value in restarted.projection(created.turn.thread_id).items
        if value.item_id == open_item.item_id
    ).status is ItemStatus.IN_PROGRESS
    RuntimeInvariantAuditor(path).audit().raise_if_invalid()

    restarted.finish_turn_job(
        job_id=leased.job_id,
        worker_id="worker-1",
        lease_token=leased.lease_token,
        target=TurnStatus.COMPLETED,
    )
    RuntimeInvariantAuditor(path).audit().raise_if_invalid()


def test_invariant_auditor_detects_projection_and_lease_authority_drift(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    _thread, created, leased = _running_turn(kernel)
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'dead_letter', lease_owner = NULL, "
            "lease_token = NULL, lease_expires_at = NULL, heartbeat_at = NULL "
            "WHERE job_id = ?",
            (leased.job_id,),
        )
        connection.execute(
            "UPDATE turns SET status = 'completed' WHERE turn_id = ?",
            (created.turn.turn_id,),
        )

    report = RuntimeInvariantAuditor(path).audit()
    assert not report.ok
    codes = {value.code for value in report.violations}
    assert {
        "failed_job_turn_mismatch",
        "job_projection_drift",
        "turn_projection_drift",
    }.issubset(codes)
    with pytest.raises(RuntimeInvariantError):
        report.raise_if_invalid()


def test_database_rejects_a_partial_lease_authority_tuple(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    _thread, _created, leased = _running_turn(kernel)

    with pytest.raises(sqlite3.IntegrityError, match="invalid durable job lease state"):
        with kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE jobs SET lease_token = NULL WHERE job_id = ?",
                (leased.job_id,),
            )

    assert kernel.jobs.get(leased.job_id).lease_token == leased.lease_token
    RuntimeInvariantAuditor(kernel.database).audit().raise_if_invalid()


def test_final_lease_expiry_atomically_dead_letters_and_fails_turn(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    thread, created, leased = _running_turn(kernel, max_attempts=1)

    assert kernel.jobs.reclaim_expired(now=_after_lease_expiry(leased)) == [
        leased.job_id
    ]
    assert kernel.jobs.get(leased.job_id).status.value == "dead_letter"
    assert kernel.get_turn(created.turn.turn_id).status is TurnStatus.FAILED
    terminal = kernel.events.page(thread.thread_id).events[-1]
    assert terminal.event_type == "turn.status_changed"
    assert terminal.causation_id is not None
    assert kernel.events.get(terminal.causation_id).event_type == "job.dead_lettered"
    projection = kernel.projection(thread.thread_id)
    assert all(item.status in {ItemStatus.COMPLETED, ItemStatus.FAILED} for item in projection.items)
    RuntimeInvariantAuditor(path).audit().raise_if_invalid()


def test_retryable_lease_expiry_moves_unsafe_phase_to_retry_wait(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    _thread, created, leased = _running_turn(kernel, max_attempts=3)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.TOOL_PENDING)

    assert kernel.jobs.reclaim_expired(now=_after_lease_expiry(leased)) == [
        leased.job_id
    ]
    assert kernel.jobs.get(leased.job_id).status.value == "queued"
    assert kernel.get_turn(created.turn.turn_id).status is TurnStatus.RETRY_WAIT
    RuntimeInvariantAuditor(path).audit().raise_if_invalid()


def test_finalizing_response_fact_wins_over_final_attempt_lease_expiry(tmp_path) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    thread, created, leased = _running_turn(kernel, max_attempts=1)
    pending = kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.MESSAGE,
        content={"role": "assistant", "text": "already committed"},
        status=ItemStatus.IN_PROGRESS,
    )
    kernel.events.append(
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        event_type="model.response_completed",
        payload={"response_id": "response-committed", "usage": {}},
        idempotency_key="response-committed:terminal",
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.FINALIZING)

    assert kernel.jobs.reclaim_expired(now=_after_lease_expiry(leased)) == [
        leased.job_id
    ]
    assert kernel.jobs.get(leased.job_id).status.value == "completed"
    assert kernel.get_turn(created.turn.turn_id).status is TurnStatus.COMPLETED
    terminal = kernel.events.page(thread.thread_id).events[-1]
    assert terminal.causation_id is not None
    assert kernel.events.get(terminal.causation_id).event_type == "job.completed"
    assert next(
        value
        for value in kernel.projection(thread.thread_id).items
        if value.item_id == pending.item_id
    ).status is ItemStatus.COMPLETED
    RuntimeInvariantAuditor(path).audit().raise_if_invalid()
