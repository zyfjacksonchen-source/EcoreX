from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import threading

import pytest

from ecorex.protocol import CreateTurnRequest, ItemKind, ItemStatus, TurnStatus
from ecorex.runtime.commit_guard import transaction_commit_guard
from ecorex.runtime.errors import LeaseError
from ecorex.runtime.invariant_guard import RuntimeExecutionGate
from ecorex.runtime.kernel import RuntimeKernel


def test_commit_guard_and_control_transaction_have_no_lock_order_inversion(
    tmp_path,
) -> None:
    kernel = RuntimeKernel(tmp_path / "lock-order.db")
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    background_permit = gate.issue_permit(
        scope="background_commit",
        subject="database_first",
    )
    database_owned = threading.Event()
    release_database = threading.Event()
    control_started = threading.Event()

    def database_first() -> None:
        connection = kernel.database.connect()
        try:
            with transaction_commit_guard(
                lambda: gate.assert_permit(background_permit)
            ):
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO runtime_meta(key,value) VALUES "
                    "('lock_order_database_first','1')"
                )
                database_owned.set()
                assert release_database.wait(timeout=5)
                connection.commit()
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def control_second() -> None:
        control_started.set()
        with kernel.jobs.control_transaction(
            scope="lock_order_control",
            subject="control_second",
        ) as connection:
            connection.execute(
                "INSERT INTO runtime_meta(key,value) VALUES "
                "('lock_order_control_second','1')"
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(database_first)
        assert database_owned.wait(timeout=5)
        second = executor.submit(control_second)
        assert control_started.wait(timeout=5)
        release_database.set()
        first.result(timeout=5)
        second.result(timeout=5)

    with kernel.database.reader() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runtime_meta "
            "WHERE key IN ('lock_order_database_first','lock_order_control_second')"
        ).fetchone()[0] == 2


def test_concurrent_critical_requests_publish_one_atomic_diagnostic_and_closer(
    tmp_path,
    monkeypatch,
) -> None:
    kernel = RuntimeKernel(tmp_path / "critical-request-race.db")
    gate = RuntimeExecutionGate()
    gate.record_report(kernel.invariants.audit())
    healthy_epoch = gate.snapshot().epoch
    base = datetime(2026, 7, 12, 8, 0, tzinfo=UTC)
    candidates = {
        (f"concurrent_failure_{index}", base + timedelta(microseconds=index))
        for index in range(32)
    }
    closer_calls = 0
    closer_lock = threading.Lock()
    original_closer = gate._finish_requested_critical

    def counted_closer() -> None:
        nonlocal closer_calls
        with closer_lock:
            closer_calls += 1
        original_closer()

    monkeypatch.setattr(gate, "_finish_requested_critical", counted_closer)

    gate._lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=16) as executor:
            requests = [
                executor.submit(
                    gate.request_critical,
                    error_code=error_code,
                    checked_at=checked_at,
                )
                for error_code, checked_at in candidates
            ]
            for request in requests:
                request.result(timeout=5)

        projected = {
            (snapshot.last_error_code, snapshot.checked_at)
            for snapshot in (gate.snapshot() for _ in range(32))
        }
        assert len(projected) == 1
        assert projected.pop() in candidates
        assert gate.snapshot().status == "critical"
        assert gate.snapshot().epoch == healthy_epoch + 1
    finally:
        gate._lock.release()

    assert gate._critical_completion.wait(timeout=5)
    completed = gate.snapshot()
    assert (completed.last_error_code, completed.checked_at) in candidates
    assert completed.epoch == healthy_epoch + 1
    assert closer_calls == 1


def test_admission_cannot_issue_permit_after_critical_closure_is_requested(
    tmp_path,
) -> None:
    kernel = RuntimeKernel(tmp_path / "closure-requested-admission.db")
    gate = RuntimeExecutionGate()
    gate.record_report(kernel.invariants.audit())
    with gate.admission() as captured:
        assert captured.allowed is True

    gate._lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            requested = executor.submit(
                gate.request_critical,
                error_code="closure_requested_before_permit",
            )
            requested.result(timeout=5)
        assert gate._closure_requested.is_set()
        with pytest.raises(RuntimeError, match="admission is stale"):
            gate.issue_permit(
                scope="stale_admission",
                subject="closure-requested",
                admission=captured,
            )
    finally:
        gate._lock.release()

    assert gate._critical_completion.wait(timeout=5)


def test_nested_commit_guards_preserve_outer_job_authority(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "nested-guards.db")
    calls: list[str] = []

    with transaction_commit_guard(lambda: calls.append("job")):
        with transaction_commit_guard(lambda: calls.append("connector")):
            with kernel.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO runtime_meta(key,value) VALUES "
                    "('nested_commit_guard','1')"
                )

    assert calls == ["job", "connector"]


def test_delayed_old_lease_publisher_cannot_replace_new_generation(
    tmp_path,
    monkeypatch,
) -> None:
    kernel = RuntimeKernel(tmp_path / "lease-generation.db")
    job = kernel.jobs.enqueue(
        kind="maintenance",
        payload={},
        idempotency_key="lease-generation-race",
        max_attempts=4,
    )
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    base = datetime.now(UTC)
    first_entered = threading.Event()
    release_first = threading.Event()
    original = kernel.jobs._remember_execution_permit
    call_lock = threading.Lock()
    calls = 0

    def delayed_publish(job_id, lease_token, permit):
        nonlocal calls
        with call_lock:
            calls += 1
            ordinal = calls
        if ordinal == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        return original(job_id, lease_token, permit)

    monkeypatch.setattr(
        kernel.jobs,
        "_remember_execution_permit",
        delayed_publish,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        old_future = executor.submit(
            kernel.jobs.lease_next,
            "old-worker",
            lease_seconds=1,
            now=base,
        )
        assert first_entered.wait(timeout=5)
        current = kernel.jobs.lease_next(
            "new-worker",
            lease_seconds=30,
            now=base + timedelta(seconds=2),
        )
        assert current is not None and current.lease_token is not None
        release_first.set()
        assert old_future.result(timeout=5) is None

    durable = kernel.jobs.get(job.job_id)
    assert durable.lease_token == current.lease_token
    assert kernel.jobs.capture_execution_permit(
        current.job_id,
        current.lease_token,
    ) is not None


def test_rolled_back_terminal_transition_keeps_current_permit(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "permit-rollback.db")
    base = datetime.now(UTC)
    job = kernel.jobs.enqueue(
        kind="maintenance",
        payload={},
        idempotency_key="permit-rollback",
        deadline=base + timedelta(minutes=10),
    )
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    leased = kernel.jobs.lease_next(
        "rollback-worker",
        lease_seconds=300,
        # Enqueue assigns available_at with its own current timestamp, which
        # is necessarily just after ``base``.
        now=base + timedelta(seconds=1),
    )
    assert leased is not None and leased.lease_token is not None

    with pytest.raises(RuntimeError, match="rollback after retirement schedule"):
        with kernel.database.transaction() as connection:
            assert kernel.jobs._expire_deadlines_in_transaction(
                connection,
                base + timedelta(minutes=20),
                job_id=job.job_id,
            ) == [job.job_id]
            raise RuntimeError("rollback after retirement schedule")

    assert kernel.jobs.get(job.job_id).lease_token == leased.lease_token
    assert kernel.jobs.capture_execution_permit(
        leased.job_id,
        leased.lease_token,
    ) is not None


def _background_job(tmp_path, suffix: str):
    kernel = RuntimeKernel(tmp_path / f"{suffix}.db")
    thread = kernel.create_thread()
    job = kernel.jobs.enqueue(
        kind="maintenance",
        payload={"operation": suffix},
        idempotency_key=f"permit-{suffix}",
        thread_id=thread.thread_id,
    )
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    leased = kernel.jobs.lease_next(f"worker-{suffix}", lease_seconds=30)
    assert leased is not None and leased.lease_token is not None
    return kernel, gate, job, leased


def _event_count(kernel: RuntimeKernel, thread_id: str) -> int:
    with kernel.database.reader() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE thread_id=?", (thread_id,)
            ).fetchone()[0]
        )


def test_invariant_close_revokes_every_operation_on_an_existing_lease(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="permit drift", client_message_id="permit-drift"),
    )
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    leased = kernel.jobs.lease_next("epoch-worker", lease_seconds=30)
    assert leased is not None and leased.lease_token is not None
    healthy_epoch = gate.snapshot().epoch

    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE turns SET status='completed' WHERE turn_id=?",
            (created.turn.turn_id,),
        )
    report = kernel.invariants.audit()
    assert report.ok is False
    gate.record_report(report)
    assert gate.snapshot().status == "critical"
    assert gate.snapshot().epoch > healthy_epoch

    before = kernel.jobs.get(leased.job_id)
    before_events = _event_count(kernel, thread.thread_id)
    operations = (
        lambda: kernel.jobs.start(
            leased.job_id, "epoch-worker", leased.lease_token
        ),
        lambda: kernel.jobs.heartbeat(
            leased.job_id,
            "epoch-worker",
            leased.lease_token,
            checkpoint={"secret": "must-not-commit"},
        ),
        lambda: kernel.jobs.complete(
            leased.job_id, "epoch-worker", leased.lease_token
        ),
        lambda: kernel.jobs.fail(
            leased.job_id,
            "epoch-worker",
            leased.lease_token,
            error="must-not-commit",
            retryable=False,
        ),
        lambda: kernel.jobs.wait_for_human(
            leased.job_id, "epoch-worker", leased.lease_token
        ),
    )
    for operation in operations:
        with pytest.raises(LeaseError, match="execution epoch"):
            operation()
    with pytest.raises(LeaseError, match="epoch is closed"):
        kernel.jobs.cancel(leased.job_id, reason="must-not-commit")
    with pytest.raises(LeaseError, match="epoch is closed"):
        kernel.jobs.reclaim_expired(
            now=(leased.lease_expires_at or datetime.now(UTC))
            + timedelta(seconds=1)
        )
    assert kernel.jobs.get(leased.job_id) == before
    assert _event_count(kernel, thread.thread_id) == before_events


def test_precommit_epoch_recheck_rolls_back_business_mutation(tmp_path, monkeypatch) -> None:
    kernel, gate, _job, leased = _background_job(tmp_path, "precommit")
    kernel.jobs.start(
        leased.job_id, "worker-precommit", leased.lease_token
    )
    before = kernel.jobs.get(leased.job_id)
    before_events = _event_count(kernel, before.thread_id or "")
    original_assert = gate.assert_permit
    injected = False

    def close_before_commit(permit) -> None:
        nonlocal injected
        if not injected:
            injected = True
            gate.request_critical(error_code="test_precommit_close")
        original_assert(permit)

    monkeypatch.setattr(gate, "assert_permit", close_before_commit)
    with pytest.raises(LeaseError, match="execution epoch"):
        kernel.jobs.heartbeat(
            leased.job_id,
            "worker-precommit",
            leased.lease_token,
            checkpoint={"phase": "must-roll-back"},
        )
    after = kernel.jobs.get(leased.job_id)
    assert after.checkpoint == before.checkpoint
    assert after.heartbeat_at == before.heartbeat_at
    assert after.updated_at == before.updated_at
    assert _event_count(kernel, before.thread_id or "") == before_events


def test_forged_or_cross_process_permit_cannot_execute(tmp_path) -> None:
    kernel, _gate, _job, leased = _background_job(tmp_path, "forged")
    key = (leased.job_id, leased.lease_token)
    valid = kernel.jobs._execution_permits[key]
    forged = replace(valid, signature="0" * 64)
    kernel.jobs._execution_permits[key] = forged
    with pytest.raises(LeaseError, match="execution epoch"):
        with kernel.jobs.execution_admission(
            leased.job_id, leased.lease_token
        ):
            raise AssertionError("forged permit entered execution")

    path = tmp_path / "restart.db"
    first = RuntimeKernel(path)
    thread = first.create_thread()
    first.jobs.enqueue(
        kind="maintenance",
        payload={},
        idempotency_key="restart-permit",
        thread_id=thread.thread_id,
    )
    first_gate = RuntimeExecutionGate()
    first.jobs.bind_execution_gate(first_gate)
    first_gate.record_report(first.invariants.audit())
    old = first.jobs.lease_next(
        "old-process", lease_seconds=1, now=datetime.now(UTC)
    )
    assert old is not None and old.lease_token is not None

    restarted = RuntimeKernel(path)
    restarted_gate = RuntimeExecutionGate()
    restarted.jobs.bind_execution_gate(restarted_gate)
    restarted_gate.record_report(restarted.invariants.audit())
    with pytest.raises(LeaseError, match="no current execution permit"):
        restarted.jobs.start(old.job_id, "old-process", old.lease_token)
    replacement = restarted.jobs.lease_next(
        "new-process",
        lease_seconds=30,
        now=(old.lease_expires_at or datetime.now(UTC)) + timedelta(seconds=1),
    )
    assert replacement is not None and replacement.lease_token is not None
    assert replacement.lease_token != old.lease_token
    assert restarted.jobs.start(
        replacement.job_id, "new-process", replacement.lease_token
    ).status.value == "running"


def test_late_provider_result_is_rejected_and_cannot_complete_job(tmp_path) -> None:
    kernel, gate, _job, leased = _background_job(tmp_path, "provider")
    kernel.jobs.start(
        leased.job_id, "worker-provider", leased.lease_token
    )
    entered = threading.Event()
    release = threading.Event()
    provider_effects: list[str] = []

    def provider_call() -> None:
        with kernel.jobs.execution_admission(
            leased.job_id, leased.lease_token
        ):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("provider test was not released")
            provider_effects.append("provider-may-have-completed")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(provider_call)
        assert entered.wait(timeout=5)
        gate.request_critical(error_code="provider_epoch_closed")
        release.set()
        with pytest.raises(LeaseError, match="execution epoch"):
            future.result(timeout=5)

    assert provider_effects == ["provider-may-have-completed"]
    with pytest.raises(LeaseError, match="execution epoch"):
        kernel.jobs.complete(
            leased.job_id, "worker-provider", leased.lease_token
        )
    assert kernel.jobs.get(leased.job_id).status.value == "running"


def test_async_provider_checkpoints_do_not_hold_thread_lock_across_await(
    tmp_path,
) -> None:
    kernel, gate, _job, leased = _background_job(tmp_path, "async-provider")
    kernel.jobs.start(
        leased.job_id, "worker-async-provider", leased.lease_token
    )

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        loop_progressed = asyncio.Event()

        async def provider() -> None:
            permit = kernel.jobs.capture_execution_permit(
                leased.job_id, leased.lease_token
            )
            entered.set()
            await release.wait()
            kernel.jobs.assert_execution_permit(
                leased.job_id, leased.lease_token, permit
            )

        async def ticker() -> None:
            await asyncio.sleep(0)
            loop_progressed.set()

        provider_task = asyncio.create_task(provider())
        await entered.wait()
        await ticker()
        assert loop_progressed.is_set()
        # This call runs on the same event-loop thread. It cannot deadlock on
        # an RLock retained by the suspended provider coroutine.
        gate.mark_critical(error_code="async_provider_epoch_closed")
        release.set()
        with pytest.raises(LeaseError, match="execution epoch"):
            await provider_task

    asyncio.run(scenario())
    assert kernel.jobs.get(leased.job_id).status.value == "running"


def test_unleased_resume_is_a_current_epoch_control_mutation(tmp_path) -> None:
    kernel, gate, _job, leased = _background_job(tmp_path, "resume")
    kernel.jobs.start(leased.job_id, "worker-resume", leased.lease_token)
    waiting = kernel.jobs.wait_for_human(
        leased.job_id, "worker-resume", leased.lease_token
    )
    assert waiting.status.value == "waiting_human"
    gate.mark_critical(error_code="resume_epoch_closed")
    with pytest.raises(LeaseError, match="epoch is closed"):
        kernel.jobs.resume_waiting(leased.job_id)
    assert kernel.jobs.get(leased.job_id).status.value == "waiting_human"


def test_control_transaction_uses_scope_when_optional_subject_is_absent(tmp_path) -> None:
    kernel, gate, _job, leased = _background_job(tmp_path, "control-subject")
    thread_id = leased.thread_id
    assert thread_id is not None
    created = kernel.create_turn(
        thread_id,
        CreateTurnRequest(input="subject fallback", client_message_id=None),
    )
    assert created.turn.thread_id == thread_id
    gate.mark_critical(error_code="control_subject_epoch_closed")
    with pytest.raises(LeaseError, match="epoch is closed"):
        kernel.create_turn(
            thread_id,
            CreateTurnRequest(input="must not commit", client_message_id=None),
        )


def test_reclaim_and_terminal_paths_keep_only_the_current_permit(tmp_path) -> None:
    kernel, _gate, _job, first = _background_job(tmp_path, "permit-cleanup")
    assert len(kernel.jobs._execution_permits) == 1
    assert kernel.jobs.reclaim_expired(
        now=(first.lease_expires_at or datetime.now(UTC)) + timedelta(seconds=1)
    ) == [first.job_id]
    assert len(kernel.jobs._execution_permits) == 0
    second = kernel.jobs.lease_next(
        "worker-permit-cleanup-2",
        now=(first.lease_expires_at or datetime.now(UTC)) + timedelta(seconds=2),
    )
    assert second is not None and second.lease_token is not None
    assert list(kernel.jobs._execution_permits) == [
        (second.job_id, second.lease_token)
    ]
    kernel.jobs.start(
        second.job_id, "worker-permit-cleanup-2", second.lease_token
    )
    kernel.jobs.complete(
        second.job_id, "worker-permit-cleanup-2", second.lease_token
    )
    assert kernel.jobs._execution_permits == {}


def test_finalizing_recovery_retires_the_expired_execution_permit(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "finalizing.db")
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(input="finalizing permit cleanup"),
    )
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    leased = kernel.jobs.lease_next("finalizing-worker", lease_seconds=1)
    assert leased is not None and leased.lease_token is not None
    kernel.jobs.start(
        leased.job_id, "finalizing-worker", leased.lease_token
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.PREPARING)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.MODEL_REQUESTED)
    kernel.transition_turn(created.turn.turn_id, TurnStatus.STREAMING)
    kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.MESSAGE,
        content={"role": "assistant", "text": "durable"},
        status=ItemStatus.IN_PROGRESS,
    )
    kernel.events.append(
        thread_id=thread.thread_id,
        turn_id=created.turn.turn_id,
        event_type="model.response_completed",
        payload={"response_id": "permit-finalizing", "usage": {}},
    )
    kernel.transition_turn(created.turn.turn_id, TurnStatus.FINALIZING)
    assert len(kernel.jobs._execution_permits) == 1
    assert leased.lease_expires_at is not None
    assert kernel.jobs.reclaim_expired(
        now=leased.lease_expires_at + timedelta(seconds=1)
    ) == [leased.job_id]
    assert kernel.jobs.get(leased.job_id).status.value == "completed"
    assert kernel.jobs._execution_permits == {}
