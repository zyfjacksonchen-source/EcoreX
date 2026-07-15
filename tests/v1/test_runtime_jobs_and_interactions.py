from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ecorex.protocol import InteractionKind, InteractionStatus, JobStatus
from ecorex.runtime import DurableJobStore, InteractionStore, SQLiteDatabase
from ecorex.runtime.errors import IdempotencyConflictError, LeaseError


def test_job_lease_heartbeat_reclaim_and_restart(tmp_path):
    path = tmp_path / "runtime.db"
    jobs = DurableJobStore(path)
    base = datetime(2026, 7, 10, 4, 0, tzinfo=timezone.utc)
    created = jobs.enqueue(
        kind="agent_turn",
        payload={"turn": "one"},
        thread_id="thread-a",
        turn_id="turn-a",
        idempotency_key="turn-a:execute",
        max_attempts=3,
        available_at=base,
    )
    duplicate = jobs.enqueue(
        kind="agent_turn",
        payload={"turn": "one"},
        thread_id="thread-a",
        turn_id="turn-a",
        idempotency_key="turn-a:execute",
        max_attempts=3,
        available_at=base,
    )
    assert duplicate.job_id == created.job_id
    with pytest.raises(IdempotencyConflictError):
        jobs.enqueue(
            kind="agent_turn",
            payload={"turn": "different"},
            thread_id="thread-a",
            turn_id="turn-a",
                idempotency_key="turn-a:execute",
                max_attempts=3,
                available_at=base,
            )

    leased = jobs.lease_next("worker-a", lease_seconds=10, now=base)
    assert leased.status == JobStatus.LEASED
    running = jobs.start(
        leased.job_id,
        "worker-a",
        leased.lease_token,
        now=base + timedelta(seconds=1),
    )
    assert running.status == JobStatus.RUNNING
    heartbeat = jobs.heartbeat(
        leased.job_id,
        "worker-a",
        leased.lease_token,
        lease_seconds=20,
        checkpoint={"offset": 4},
        now=base + timedelta(seconds=2),
    )
    assert heartbeat.checkpoint == {"offset": 4}

    restarted = DurableJobStore(path)
    assert restarted.reclaim_expired(now=base + timedelta(seconds=15)) == []
    assert restarted.reclaim_expired(now=base + timedelta(seconds=23)) == [leased.job_id]
    reclaimed = restarted.lease_next(
        "worker-b", lease_seconds=10, now=base + timedelta(seconds=24)
    )
    assert reclaimed.job_id == leased.job_id
    assert reclaimed.attempt == 2
    with pytest.raises(LeaseError):
        restarted.start(
            reclaimed.job_id,
            "worker-a",
            leased.lease_token,
            now=base + timedelta(seconds=25),
        )
    restarted.start(
        reclaimed.job_id,
        "worker-b",
        reclaimed.lease_token,
        now=base + timedelta(seconds=25),
    )
    assert (
        restarted.complete(
            reclaimed.job_id,
            "worker-b",
            reclaimed.lease_token,
            now=base + timedelta(seconds=26),
        ).status
        == JobStatus.COMPLETED
    )


def test_job_fifo_per_thread_and_retry_dead_letter(tmp_path):
    jobs = DurableJobStore(tmp_path / "runtime.db")
    first = jobs.enqueue(
        kind="turn", payload={"n": 1}, thread_id="t", idempotency_key="one", max_attempts=1
    )
    second = jobs.enqueue(
        kind="turn", payload={"n": 2}, thread_id="t", idempotency_key="two"
    )
    other = jobs.enqueue(
        kind="turn",
        payload={"n": 3},
        thread_id="other",
        idempotency_key="other",
        priority=10,
    )
    other_lease = jobs.lease_next("w")
    assert other_lease.job_id == other.job_id
    jobs.start(other.job_id, "w", other_lease.lease_token)
    jobs.complete(other.job_id, "w", other_lease.lease_token)
    first_lease = jobs.lease_next("w")
    assert first_lease.job_id == first.job_id
    jobs.start(first.job_id, "w", first_lease.lease_token)
    dead = jobs.fail(
        first.job_id,
        "w",
        first_lease.lease_token,
        error="boom",
        retryable=True,
    )
    assert dead.status == JobStatus.DEAD_LETTER
    assert jobs.lease_next("w").job_id == second.job_id


def test_interaction_survives_restart_and_resolution_is_idempotent(tmp_path):
    path = tmp_path / "runtime.db"
    interactions = InteractionStore(SQLiteDatabase(path))
    request = interactions.create(
        kind=InteractionKind.PERMISSION_APPROVAL,
        prompt="允许写入吗？",
        options=[{"id": "allow", "label": "允许"}, {"id": "deny", "label": "拒绝"}],
        thread_id="thread-a",
        turn_id="turn-a",
        job_id="job-a",
        idempotency_key="approval-a",
    )
    restarted = InteractionStore(SQLiteDatabase(path))
    assert restarted.list_pending(thread_id="thread-a")[0].interaction_id == request.interaction_id
    resolved = restarted.respond(
        request.interaction_id,
        {"action_id": "allow", "values": {}},
        client_request_id="interaction-response-a",
    )
    assert resolved.status == InteractionStatus.RESOLVED
    assert restarted.respond(
        request.interaction_id,
        {"action_id": "allow", "values": {}},
        client_request_id="interaction-response-a",
    ) == resolved
    with pytest.raises(IdempotencyConflictError):
        restarted.respond(
            request.interaction_id,
            {"action_id": "deny", "values": {}},
            client_request_id="interaction-response-a",
        )
