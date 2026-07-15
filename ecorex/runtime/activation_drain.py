"""Linearized Runtime admission drain for signed product activation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re

from .invariant_guard import (
    RuntimeDrainPermit,
    RuntimeExecutionDenied,
    RuntimeExecutionGate,
)
from .jobs import DurableJobStore


_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class RuntimeActivationDrainError(RuntimeError):
    """The Runtime could not establish a safe update activation boundary."""


class RuntimeActivationDrainTimeout(RuntimeActivationDrainError):
    """Existing durable work did not reach a recoverable boundary in time."""


@dataclass(frozen=True, slots=True)
class RuntimeActivationDrainLease:
    transaction_id: str
    permit: RuntimeDrainPermit | None
    allowed_durable_subjects: frozenset[str]


class RuntimeActivationDrainController:
    """Blocks new work and waits for already-leased work to checkpoint/finish.

    The write-lock/gate ordering is deliberate. The active durable subjects
    are read under ``BEGIN IMMEDIATE`` and the process gate is closed before
    that database lock is released. A lease whose transaction has not yet
    committed is consequently rejected by its commit fence and can never
    appear after a successful zero-active check.
    """

    def __init__(
        self,
        jobs: DurableJobStore,
        gate: RuntimeExecutionGate,
        *,
        timeout_seconds: float = 120.0,
        poll_seconds: float = 0.05,
    ) -> None:
        if not isinstance(jobs, DurableJobStore):
            raise TypeError("activation drain requires the durable job authority")
        if not isinstance(gate, RuntimeExecutionGate):
            raise TypeError("activation drain requires the Runtime execution gate")
        if not 0.1 <= timeout_seconds <= 900:
            raise ValueError("activation drain timeout is invalid")
        if not 0.01 <= poll_seconds <= min(5.0, timeout_seconds):
            raise ValueError("activation drain poll interval is invalid")
        self.jobs = jobs
        self.gate = gate
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds

    async def acquire(self, transaction_id: str) -> RuntimeActivationDrainLease:
        lease = await asyncio.to_thread(self._begin, transaction_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        try:
            while True:
                if await asyncio.to_thread(self._active_count, lease) == 0:
                    await asyncio.to_thread(self.assert_drained, lease)
                    return lease
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RuntimeActivationDrainTimeout(
                        "active work did not reach its durable checkpoint before timeout"
                    )
                await asyncio.sleep(min(self.poll_seconds, remaining))
        except BaseException:
            await asyncio.to_thread(self.release, lease)
            raise

    def release(self, lease: RuntimeActivationDrainLease) -> None:
        if not isinstance(lease, RuntimeActivationDrainLease):
            raise TypeError("activation drain lease is invalid")
        if lease.permit is None:
            if self.gate.snapshot().status != "critical":
                raise RuntimeActivationDrainError(
                    "pre-closed activation drain unexpectedly reopened"
                )
            return
        try:
            self.gate.cancel_drain(lease.permit)
        except RuntimeExecutionDenied:
            # A concurrent invariant failure is already a stronger, latched
            # boundary. It must never be converted back into healthy admission.
            if self.gate.snapshot().status != "critical":
                raise

    def assert_drained(self, lease: RuntimeActivationDrainLease) -> None:
        if not isinstance(lease, RuntimeActivationDrainLease):
            raise TypeError("activation drain lease is invalid")
        if lease.permit is None:
            if self.gate.snapshot().status != "critical":
                raise RuntimeActivationDrainError(
                    "pre-closed activation drain unexpectedly reopened"
                )
        else:
            self.gate.assert_drain(lease.permit)
        if self._active_count(lease) != 0:
            raise RuntimeActivationDrainError("Runtime work is still active")
        if lease.permit is None:
            if self.gate.snapshot().status != "critical":
                raise RuntimeActivationDrainError(
                    "pre-closed activation drain unexpectedly reopened"
                )
        else:
            self.gate.assert_drain(lease.permit)

    def _begin(self, transaction_id: str) -> RuntimeActivationDrainLease:
        if not isinstance(transaction_id, str) or _TRANSACTION_ID.fullmatch(
            transaction_id
        ) is None:
            raise ValueError("update transaction identity is invalid")
        permit: RuntimeDrainPermit | None = None
        try:
            with self.jobs.database.transaction() as connection:
                rows = connection.execute(
                    "SELECT job_id,lease_token FROM jobs "
                    "WHERE status IN ('leased','running')"
                ).fetchall()
                allowed = frozenset(
                    self.jobs.durable_permit_subject(
                        str(row["job_id"]), str(row["lease_token"])
                    )
                    for row in rows
                    if isinstance(row["lease_token"], str) and row["lease_token"]
                )
                if len(allowed) != len(rows):
                    raise RuntimeActivationDrainError(
                        "active work has no valid execution lease"
                    )
                if self.gate.snapshot().status == "critical":
                    # Recovery-lane activation remains available after a
                    # latched invariant/logout close. No new business permit
                    # can exist in this state, so zero active rows are already
                    # a stronger boundary than a reversible maintenance drain.
                    permit = None
                else:
                    permit = self.gate.begin_drain(
                        subject=transaction_id,
                        allowed_durable_subjects=allowed,
                    )
            return RuntimeActivationDrainLease(
                transaction_id=transaction_id,
                permit=permit,
                allowed_durable_subjects=allowed,
            )
        except BaseException:
            if permit is not None:
                try:
                    self.gate.cancel_drain(permit)
                except RuntimeExecutionDenied:
                    pass
            raise

    def _active_count(self, lease: RuntimeActivationDrainLease) -> int:
        if lease.permit is None:
            if self.gate.snapshot().status != "critical":
                raise RuntimeActivationDrainError(
                    "pre-closed activation drain unexpectedly reopened"
                )
        else:
            self.gate.assert_drain(lease.permit)
        with self.jobs.database.reader() as connection:
            rows = connection.execute(
                "SELECT job_id,lease_token FROM jobs "
                "WHERE status IN ('leased','running')"
            ).fetchall()
        observed = frozenset(
            self.jobs.durable_permit_subject(
                str(row["job_id"]), str(row["lease_token"])
            )
            for row in rows
            if isinstance(row["lease_token"], str) and row["lease_token"]
        )
        if len(observed) != len(rows) or not observed.issubset(
            lease.allowed_durable_subjects
        ):
            self.gate.request_critical(error_code="update_drain_lease_race")
            raise RuntimeActivationDrainError(
                "new work crossed the update activation drain boundary"
            )
        if lease.permit is None:
            if self.gate.snapshot().status != "critical":
                raise RuntimeActivationDrainError(
                    "pre-closed activation drain unexpectedly reopened"
                )
        else:
            self.gate.assert_drain(lease.permit)
        return len(rows)


__all__ = [
    "RuntimeActivationDrainController",
    "RuntimeActivationDrainError",
    "RuntimeActivationDrainLease",
    "RuntimeActivationDrainTimeout",
]
