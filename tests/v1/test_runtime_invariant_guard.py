from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from ecorex.protocol import CreateTurnRequest
from ecorex.runtime import (
    RuntimeExecutionGate,
    RuntimeInvariantAuditor,
    RuntimeInvariantSupervisor,
    RuntimeKernel,
)


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("invariant guard condition was not reached")
        await asyncio.sleep(0.005)


def _owned_tasks() -> list[asyncio.Task]:
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and not task.done()
        and task.get_name().startswith("ecorex-runtime-invariant")
    ]


def _queued_turn(kernel: RuntimeKernel, suffix: str = "one"):
    thread = kernel.create_thread()
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input=f"queued invariant work {suffix}",
            client_message_id=f"invariant-message-{suffix}",
        ),
    )
    return thread, created


def _inject_turn_projection_drift(kernel: RuntimeKernel, turn_id: str) -> None:
    with kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE turns SET status = 'completed' WHERE turn_id = ?",
            (turn_id,),
        )


def test_healthy_preflight_opens_gate_and_stop_is_idempotent(tmp_path) -> None:
    async def scenario() -> None:
        kernel = RuntimeKernel(tmp_path / "runtime.db")
        _thread, created = _queued_turn(kernel)
        gate = RuntimeExecutionGate()
        kernel.jobs.bind_execution_gate(gate)
        assert gate.snapshot().status == "critical"
        assert gate.snapshot().last_error_code == "invariant_audit_required"
        assert kernel.jobs.lease_next("before-audit") is None

        supervisor = RuntimeInvariantSupervisor(
            kernel.invariants,
            gate,
            audit_interval_seconds=60,
            audit_timeout_seconds=1,
            shutdown_timeout_seconds=0.2,
        )
        await asyncio.gather(supervisor.start(), supervisor.start())
        assert supervisor.running is True
        assert supervisor.snapshot().audit_count == 1
        snapshot = gate.snapshot()
        assert snapshot.healthy is True
        assert snapshot.checked_at is not None
        assert snapshot.violation_codes == ()
        assert snapshot.violation_count == 0
        assert snapshot.last_error_code is None
        assert supervisor.provider_dict()["status"] == "healthy"

        leased = kernel.jobs.lease_next("healthy-worker")
        assert leased is not None
        assert leased.job_id == created.job.job_id

        await supervisor.stop()
        await supervisor.stop()
        assert supervisor.running is False
        assert gate.snapshot().status == "critical"
        assert gate.snapshot().last_error_code == "invariant_supervisor_stopped"
        assert _owned_tasks() == []

    asyncio.run(scenario())


def test_background_violation_latches_gate_but_keeps_reads_available(tmp_path) -> None:
    async def scenario() -> None:
        kernel = RuntimeKernel(tmp_path / "runtime.db")
        thread, created = _queued_turn(kernel)
        gate = RuntimeExecutionGate()
        kernel.jobs.bind_execution_gate(gate)
        supervisor = RuntimeInvariantSupervisor(
            kernel.invariants,
            gate,
            audit_interval_seconds=0.01,
            audit_timeout_seconds=1,
            shutdown_timeout_seconds=0.2,
        )
        await supervisor.start()
        assert gate.snapshot().healthy is True

        _inject_turn_projection_drift(kernel, created.turn.turn_id)
        await _wait_until(lambda: gate.snapshot().status == "critical")
        critical = gate.snapshot()
        assert "turn_projection_drift" in critical.violation_codes
        assert critical.violation_count >= 1
        assert critical.last_error_code is None
        assert kernel.jobs.lease_next("isolated-worker") is None

        projection = kernel.projection(thread.thread_id)
        assert projection.turns[0].status.value == "completed"
        report = kernel.invariants.audit()
        assert report.ok is False

        with kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE turns SET status = 'queued' WHERE turn_id = ?",
                (created.turn.turn_id,),
            )
        healthy_again = kernel.invariants.audit()
        assert healthy_again.ok is True
        gate.record_report(healthy_again)
        assert gate.snapshot().status == "critical"
        assert "turn_projection_drift" in gate.snapshot().violation_codes

        await supervisor.stop()
        assert _owned_tasks() == []

    asyncio.run(scenario())


def test_gate_close_never_waits_on_paused_lease_and_precommit_rolls_back(
    tmp_path, monkeypatch
) -> None:
    close_first = RuntimeKernel(tmp_path / "close-first.db")
    _thread, close_first_turn = _queued_turn(close_first, "close-first")
    close_first_gate = RuntimeExecutionGate()
    close_first.jobs.bind_execution_gate(close_first_gate)
    close_first_gate.record_report(close_first.invariants.audit())
    _inject_turn_projection_drift(close_first, close_first_turn.turn.turn_id)
    close_first_gate.record_report(close_first.invariants.audit())
    assert close_first_gate.snapshot().status == "critical"
    assert close_first.jobs.lease_next("close-first-worker") is None

    admitted_first = RuntimeKernel(tmp_path / "admitted-first.db")
    _thread, admitted_turn = _queued_turn(admitted_first, "admitted-first")
    admitted_gate = RuntimeExecutionGate()
    admitted_first.jobs.bind_execution_gate(admitted_gate)
    admitted_gate.record_report(admitted_first.invariants.audit())
    _inject_turn_projection_drift(admitted_first, admitted_turn.turn.turn_id)
    bad_report = admitted_first.invariants.audit()
    assert bad_report.ok is False

    admission_entered = threading.Event()
    release_admission = threading.Event()
    close_started = threading.Event()
    close_finished = threading.Event()
    original_lease = admitted_first.jobs._lease_next_in_admission

    def paused_lease(worker_id, *, lease_seconds, kinds, now, before_commit=None):
        admission_entered.set()
        if not release_admission.wait(timeout=5):
            raise TimeoutError("test did not release lease admission")
        return original_lease(
            worker_id,
            lease_seconds=lease_seconds,
            kinds=kinds,
            now=now,
            before_commit=before_commit,
        )

    monkeypatch.setattr(
        admitted_first.jobs,
        "_lease_next_in_admission",
        paused_lease,
    )

    def close_gate() -> None:
        close_started.set()
        admitted_gate.record_report(bad_report)
        close_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        lease_future = executor.submit(
            admitted_first.jobs.lease_next,
            "admitted-first-worker",
        )
        assert admission_entered.wait(timeout=5)
        close_future = executor.submit(close_gate)
        assert close_started.wait(timeout=5)
        assert close_finished.wait(timeout=1)
        release_admission.set()
        leased = lease_future.result(timeout=5)
        close_future.result(timeout=5)

    assert leased is None
    assert admitted_first.jobs.get(admitted_turn.job.job_id).status.value == "queued"
    assert admitted_gate.snapshot().status == "critical"
    assert admitted_first.jobs.lease_next("after-close-worker") is None


def test_audit_exception_is_redacted_and_new_process_gate_reaudits(tmp_path) -> None:
    class ThrowingAuditor:
        def audit(self):
            raise RuntimeError("secret prompt and C:/private/path must not escape")

    async def scenario() -> None:
        path = tmp_path / "runtime.db"
        kernel = RuntimeKernel(path)
        _thread, created = _queued_turn(kernel)
        failed_gate = RuntimeExecutionGate()
        kernel.jobs.bind_execution_gate(failed_gate)
        failed = RuntimeInvariantSupervisor(
            ThrowingAuditor(),  # type: ignore[arg-type]
            failed_gate,
            audit_interval_seconds=60,
            audit_timeout_seconds=1,
            shutdown_timeout_seconds=0.2,
        )
        await failed.start()
        failed_snapshot = failed_gate.snapshot()
        assert failed.running is True
        assert failed_snapshot.status == "critical"
        assert failed_snapshot.last_error_code == "invariant_audit_failed:runtimeerror"
        assert "secret" not in str(failed.provider_dict()).casefold()
        assert "private" not in str(failed.provider_dict()).casefold()
        assert kernel.jobs.lease_next("failed-audit-worker") is None
        await failed.stop()
        assert _owned_tasks() == []

        restarted = RuntimeKernel(path)
        restarted_gate = RuntimeExecutionGate()
        restarted.jobs.bind_execution_gate(restarted_gate)
        healthy = RuntimeInvariantSupervisor(
            RuntimeInvariantAuditor(path),
            restarted_gate,
            audit_interval_seconds=60,
            audit_timeout_seconds=1,
            shutdown_timeout_seconds=0.2,
        )
        await healthy.start()
        assert restarted_gate.snapshot().healthy is True
        leased = restarted.jobs.lease_next("restarted-worker")
        assert leased is not None
        assert leased.job_id == created.job.job_id
        await healthy.stop()
        assert _owned_tasks() == []

    asyncio.run(scenario())


def test_stop_cancels_hung_background_audit_without_task_leak(tmp_path) -> None:
    class BlockingAuditor:
        def __init__(self, healthy_auditor) -> None:
            self.healthy_auditor = healthy_auditor
            self.calls = 0
            self.entered = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def audit(self):
            self.calls += 1
            if self.calls == 1:
                return self.healthy_auditor.audit()
            self.entered.set()
            self.release.wait(timeout=5)
            self.finished.set()
            return self.healthy_auditor.audit()

    async def scenario() -> None:
        kernel = RuntimeKernel(tmp_path / "runtime.db")
        _queued_turn(kernel)
        gate = RuntimeExecutionGate()
        kernel.jobs.bind_execution_gate(gate)
        auditor = BlockingAuditor(kernel.invariants)
        supervisor = RuntimeInvariantSupervisor(
            auditor,  # type: ignore[arg-type]
            gate,
            audit_interval_seconds=0.01,
            audit_timeout_seconds=2,
            shutdown_timeout_seconds=0.1,
        )
        await supervisor.start()
        await _wait_until(auditor.entered.is_set)

        started = time.monotonic()
        await supervisor.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert supervisor.running is False
        assert gate.snapshot().status == "critical"
        assert _owned_tasks() == []

        auditor.release.set()
        await _wait_until(auditor.finished.is_set)
        assert gate.snapshot().status == "critical"

    asyncio.run(scenario())


def test_timed_out_audit_latches_critical_before_late_healthy_result(tmp_path) -> None:
    class LateHealthyAuditor:
        def __init__(self, healthy_auditor) -> None:
            self.healthy_auditor = healthy_auditor
            self.entered = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def audit(self):
            self.entered.set()
            self.release.wait(timeout=5)
            report = self.healthy_auditor.audit()
            self.finished.set()
            return report

    async def scenario() -> None:
        kernel = RuntimeKernel(tmp_path / "runtime.db")
        _queued_turn(kernel)
        gate = RuntimeExecutionGate()
        kernel.jobs.bind_execution_gate(gate)
        auditor = LateHealthyAuditor(kernel.invariants)
        supervisor = RuntimeInvariantSupervisor(
            auditor,  # type: ignore[arg-type]
            gate,
            audit_interval_seconds=60,
            audit_timeout_seconds=0.05,
            shutdown_timeout_seconds=0.1,
        )

        await supervisor.start()
        assert auditor.entered.is_set()
        timed_out = gate.snapshot()
        assert timed_out.status == "critical"
        assert timed_out.last_error_code == "invariant_audit_timeout"
        assert kernel.jobs.lease_next("timed-out-audit-worker") is None

        auditor.release.set()
        await _wait_until(auditor.finished.is_set)
        await asyncio.sleep(0.01)
        late = gate.snapshot()
        assert late.status == "critical"
        assert late.last_error_code == "invariant_audit_timeout"

        await supervisor.stop()
        assert _owned_tasks() == []

    asyncio.run(scenario())
