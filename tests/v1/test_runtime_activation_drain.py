from __future__ import annotations

import asyncio

import pytest

from ecorex.runtime.activation_drain import (
    RuntimeActivationDrainController,
    RuntimeActivationDrainTimeout,
)
from ecorex.runtime.errors import LeaseError
from ecorex.runtime.invariant_guard import RuntimeExecutionDenied, RuntimeExecutionGate
from ecorex.runtime.kernel import RuntimeKernel


def _runtime(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime-drain.db")
    gate = RuntimeExecutionGate()
    kernel.jobs.bind_execution_gate(gate)
    gate.record_report(kernel.invariants.audit())
    return kernel, gate


def test_update_drain_blocks_new_work_but_existing_lease_can_checkpoint(
    tmp_path,
) -> None:
    kernel, gate = _runtime(tmp_path)
    job = kernel.jobs.enqueue(
        kind="maintenance",
        payload={},
        idempotency_key="existing-before-drain",
    )
    leased = kernel.jobs.lease_next("existing-worker", lease_seconds=30)
    assert leased is not None and leased.lease_token is not None
    kernel.jobs.start(leased.job_id, "existing-worker", leased.lease_token)
    controller = RuntimeActivationDrainController(
        kernel.jobs,
        gate,
        timeout_seconds=2,
        poll_seconds=0.01,
    )

    async def scenario():
        pending = asyncio.create_task(controller.acquire("update-transaction-1"))
        for _ in range(100):
            if gate.snapshot().draining:
                break
            await asyncio.sleep(0.01)
        assert gate.snapshot().draining is True
        assert gate.snapshot().healthy is False
        assert kernel.jobs.lease_next("new-worker", lease_seconds=30) is None
        with pytest.raises(LeaseError, match="epoch is closed"):
            kernel.jobs.enqueue(
                kind="maintenance",
                payload={},
                idempotency_key="must-not-cross-drain",
            )
        checkpointed = kernel.jobs.heartbeat(
            leased.job_id,
            "existing-worker",
            leased.lease_token,
            checkpoint={"phase": "safe-update-checkpoint"},
        )
        assert checkpointed.checkpoint == {"phase": "safe-update-checkpoint"}
        kernel.jobs.complete(
            leased.job_id,
            "existing-worker",
            leased.lease_token,
        )
        drain = await pending
        controller.assert_drained(drain)
        controller.release(drain)

    asyncio.run(scenario())
    assert kernel.jobs.get(job.job_id).status.value == "completed"
    assert gate.snapshot().healthy is True


def test_pre_drain_control_permit_cannot_commit_after_boundary(tmp_path) -> None:
    kernel, gate = _runtime(tmp_path)
    stale = gate.issue_permit(scope="permission_update", subject="before-drain")
    controller = RuntimeActivationDrainController(
        kernel.jobs,
        gate,
        timeout_seconds=1,
        poll_seconds=0.01,
    )
    drain = asyncio.run(controller.acquire("update-transaction-2"))

    with pytest.raises(RuntimeExecutionDenied, match="draining"):
        gate.assert_permit(stale)
    assert kernel.jobs.lease_next("post-drain-worker", lease_seconds=30) is None

    controller.release(drain)
    gate.assert_permit(stale)


def test_drain_timeout_reopens_admission_and_keeps_checkpointed_job(tmp_path) -> None:
    kernel, gate = _runtime(tmp_path)
    kernel.jobs.enqueue(
        kind="maintenance",
        payload={},
        idempotency_key="long-running-before-drain",
    )
    leased = kernel.jobs.lease_next("long-worker", lease_seconds=30)
    assert leased is not None and leased.lease_token is not None
    kernel.jobs.start(leased.job_id, "long-worker", leased.lease_token)
    kernel.jobs.heartbeat(
        leased.job_id,
        "long-worker",
        leased.lease_token,
        checkpoint={"phase": "provider_wait"},
    )
    controller = RuntimeActivationDrainController(
        kernel.jobs,
        gate,
        timeout_seconds=0.1,
        poll_seconds=0.01,
    )

    with pytest.raises(RuntimeActivationDrainTimeout):
        asyncio.run(controller.acquire("update-transaction-timeout"))

    assert gate.snapshot().healthy is True
    assert kernel.jobs.get(leased.job_id).checkpoint == {"phase": "provider_wait"}


def test_critical_close_during_drain_can_never_reopen_runtime(tmp_path) -> None:
    kernel, gate = _runtime(tmp_path)
    controller = RuntimeActivationDrainController(
        kernel.jobs,
        gate,
        timeout_seconds=1,
        poll_seconds=0.01,
    )
    drain = asyncio.run(controller.acquire("update-transaction-critical"))
    gate.request_critical(error_code="invariant_failed_during_update_drain")

    controller.release(drain)

    assert gate.snapshot().status == "critical"
    assert gate.snapshot().healthy is False
