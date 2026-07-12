from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ecorex.artifacts import ArtifactService
from ecorex.artifacts.api import (
    ArtifactApiEvent,
    create_artifact_router,
)
from ecorex.integration import (
    ArtifactEventOutbox,
    ArtifactEventOutboxConflict,
    ArtifactEventOutboxError,
    ArtifactEventOutboxSupervisor,
    ArtifactEventPublishTimeout,
)
from ecorex.runtime import RuntimeExecutionGate, RuntimeKernel
from ecorex.runtime.database import SQLiteDatabase
from ecorex.runtime.errors import SchemaVersionError


def _event(*, signal: str = "thumbs_up") -> ArtifactApiEvent:
    return ArtifactApiEvent(
        event_type="artifact.feedback.recorded",
        idempotency_key="artifact.feedback:art_1:req_1",
        artifact_id="art_1",
        revision_id="rev_1",
        client_request_id="req_1",
        payload={"signal": signal},
    )


class InspectingPublisher:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.calls: list[str] = []

    def publish(self, event_id: str, event: ArtifactApiEvent) -> None:
        del event
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT published_at FROM artifact_event_outbox WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        assert row is not None
        assert row[0] is None
        self.calls.append(event_id)


class RejectingTransactionalOutbox(ArtifactEventOutbox):
    def persist_in_transaction(self, connection, event) -> str:
        del connection, event
        raise OSError("intent storage unavailable")


def _artifact_client(service: ArtifactService, outbox: ArtifactEventOutbox) -> TestClient:
    app = FastAPI()
    app.include_router(create_artifact_router(service, event_sink=outbox))
    return TestClient(app)


def test_feedback_and_event_intent_roll_back_together_then_restart_recovers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    service = ArtifactService(
        tmp_path / "artifacts",
        database_path=path,
    )
    artifact = service.create_artifact(
        b"report",
        requested_name="report.pdf",
        mime_type="application/pdf",
    )
    payload = {
        "revision_id": artifact.revision_id,
        "signal": "thumbs_up",
        "client_request_id": "feedback-atomic-intent",
    }

    rejected = _artifact_client(service, RejectingTransactionalOutbox(kernel.database))
    response = rejected.post(
        f"/api/v1/artifacts/{artifact.artifact_id}/feedback",
        json=payload,
    )
    assert response.status_code == 503
    with kernel.database.reader() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_feedback").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_event_outbox"
        ).fetchone()[0] == 0

    class OfflinePublisher:
        def publish(self, _event_id: str, _event: ArtifactApiEvent) -> None:
            raise OSError("offline")

    offline = ArtifactEventOutbox(kernel.database, publisher=OfflinePublisher())
    retry = _artifact_client(service, offline).post(
        f"/api/v1/artifacts/{artifact.artifact_id}/feedback",
        json=payload,
    )
    assert retry.status_code == 503
    with kernel.database.reader() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifact_feedback").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_event_outbox WHERE published_at IS NULL"
        ).fetchone()[0] == 1

    publisher = InspectingPublisher(path)
    restarted = ArtifactEventOutbox(kernel.database, publisher=publisher)
    assert asyncio.run(restarted.drain()) == 1
    assert restarted.pending() == ()
    assert len(publisher.calls) == 1
    completed = _artifact_client(service, restarted).post(
        f"/api/v1/artifacts/{artifact.artifact_id}/feedback",
        json=payload,
    )
    assert completed.status_code == 200
    assert len(publisher.calls) == 1


def test_retouch_job_annotation_and_event_intent_share_one_transaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    kernel = RuntimeKernel(path)
    service = ArtifactService(tmp_path / "artifacts", database_path=path)
    image = service.create_artifact(
        b"\x89PNG\r\n\x1a\nimage",
        requested_name="poster.png",
        mime_type="image/png",
    )
    payload = {
        "base_revision_id": image.revision_id,
        "selected_artifact_ids": [image.artifact_id],
        "agent_model_id": "ecorex-chat",
        "image_model_id": "gpt-image-2",
        "annotations": [],
        "reference_artifact_ids": [],
        "global_instruction": "保持布局，只修改标题",
        "client_request_id": "retouch-atomic-intent",
    }

    rejected = _artifact_client(service, RejectingTransactionalOutbox(kernel.database))
    response = rejected.post(
        f"/api/v1/artifacts/{image.artifact_id}/retouch",
        json=payload,
    )
    assert response.status_code == 503
    with kernel.database.reader() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_retouch_jobs"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_entities WHERE visibility='internal'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_event_outbox"
        ).fetchone()[0] == 0

    outbox = ArtifactEventOutbox(kernel.database)
    retry = _artifact_client(service, outbox).post(
        f"/api/v1/artifacts/{image.artifact_id}/retouch",
        json=payload,
    )
    assert retry.status_code == 202
    with kernel.database.reader() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_retouch_jobs"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM artifact_event_outbox"
        ).fetchone()[0] == 1


def test_outbox_commits_before_publish_and_deduplicates_retry(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    publisher = InspectingPublisher(path)
    outbox = ArtifactEventOutbox(path, publisher=publisher)

    asyncio.run(outbox.persist_then_publish(_event()))
    asyncio.run(outbox.persist_then_publish(_event()))

    assert len(publisher.calls) == 1
    record = outbox.get(publisher.calls[0])
    assert record.published_at is not None
    assert record.attempts == 1
    assert outbox.pending() == ()


def test_same_idempotency_key_with_different_payload_conflicts(tmp_path: Path) -> None:
    outbox = ArtifactEventOutbox(tmp_path / "runtime.db")
    asyncio.run(outbox.persist_then_publish(_event()))
    with pytest.raises(ArtifactEventOutboxConflict):
        asyncio.run(outbox.persist_then_publish(_event(signal="thumbs_down")))


def test_transactional_intent_rejects_autocommit_and_foreign_database(
    tmp_path: Path,
) -> None:
    outbox = ArtifactEventOutbox(tmp_path / "runtime.db")
    connection = outbox.database.connect()
    try:
        with pytest.raises(ArtifactEventOutboxError, match="active Runtime transaction"):
            outbox.persist_in_transaction(connection, _event())
    finally:
        connection.close()

    foreign = SQLiteDatabase(tmp_path / "foreign.db")
    with foreign.transaction() as foreign_connection:
        with pytest.raises(ArtifactEventOutboxError, match="another database"):
            outbox.persist_in_transaction(foreign_connection, _event())


def test_publish_failure_is_durable_and_restart_can_drain(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"

    class FailingPublisher:
        def publish(self, event_id: str, event: ArtifactApiEvent) -> None:
            del event_id, event
            raise OSError("offline")

    failing = ArtifactEventOutbox(path, publisher=FailingPublisher())
    with pytest.raises(OSError, match="offline"):
        asyncio.run(failing.persist_then_publish(_event()))
    pending = failing.pending()
    assert len(pending) == 1
    assert pending[0].attempts == 1
    assert pending[0].last_error_code == "OSError"

    publisher = InspectingPublisher(path)
    restarted = ArtifactEventOutbox(path, publisher=publisher)
    assert asyncio.run(restarted.drain()) == 1
    assert restarted.pending() == ()
    assert len(publisher.calls) == 1


def test_lifecycle_supervisor_drains_pending_event_without_user_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"

    class FailingPublisher:
        def publish(self, _event_id: str, _event: ArtifactApiEvent) -> None:
            raise OSError("offline")

    failing = ArtifactEventOutbox(path, publisher=FailingPublisher())
    with pytest.raises(OSError, match="offline"):
        asyncio.run(failing.persist_then_publish(_event()))

    publisher = InspectingPublisher(path)
    restarted = ArtifactEventOutbox(path, publisher=publisher)
    gate = RuntimeExecutionGate()
    gate.record_report(RuntimeKernel(path).invariants.audit())
    supervisor = ArtifactEventOutboxSupervisor(
        restarted,
        execution_gate=gate,
        interval_seconds=60,
    )

    async def run() -> None:
        await supervisor.start()
        for _ in range(200):
            if not restarted.pending() and supervisor.snapshot().published == 1:
                break
            await asyncio.sleep(0.01)
        assert restarted.pending() == ()
        assert supervisor.snapshot().published == 1
        await supervisor.stop()

    asyncio.run(run())
    assert len(publisher.calls) == 1


def test_supervisor_does_not_acknowledge_provider_result_after_epoch_close(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    staged = ArtifactEventOutbox(path)
    assert asyncio.run(staged.persist_then_publish(_event())) is False
    entered = threading.Event()
    release = threading.Event()

    class BlockingPublisher:
        def publish(self, _event_id: str, _event: ArtifactApiEvent) -> None:
            entered.set()
            assert release.wait(timeout=5)

    outbox = ArtifactEventOutbox(path, publisher=BlockingPublisher())
    gate = RuntimeExecutionGate()
    gate.record_report(RuntimeKernel(path).invariants.audit())
    supervisor = ArtifactEventOutboxSupervisor(
        outbox,
        execution_gate=gate,
        interval_seconds=60,
    )

    async def run() -> None:
        start = asyncio.create_task(supervisor.start())
        assert await asyncio.to_thread(entered.wait, 5)
        gate.mark_critical(error_code="test_artifact_outbox_closed")
        release.set()
        await start
        await supervisor.stop()
        assert outbox.pending()[0].published_at is None

    asyncio.run(run())


def test_supervisor_revalidates_after_claim_before_provider_dispatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "runtime.db"
    staged = ArtifactEventOutbox(path)
    assert asyncio.run(staged.persist_then_publish(_event())) is False
    calls: list[str] = []

    class Publisher:
        def publish(self, event_id: str, _event: ArtifactApiEvent) -> None:
            calls.append(event_id)

    outbox = ArtifactEventOutbox(path, publisher=Publisher())
    gate = RuntimeExecutionGate()
    gate.record_report(RuntimeKernel(path).invariants.audit())
    original_claim = outbox._claim

    def close_after_claim(event_id: str, expected_digest: str) -> str | None:
        lease_token = original_claim(event_id, expected_digest)
        if lease_token is not None:
            gate.mark_critical(error_code="test_close_after_artifact_claim")
        return lease_token

    monkeypatch.setattr(outbox, "_claim", close_after_claim)
    supervisor = ArtifactEventOutboxSupervisor(
        outbox,
        execution_gate=gate,
        interval_seconds=60,
    )

    async def run() -> None:
        await supervisor.start()
        for _ in range(200):
            if gate.snapshot().status == "critical":
                await asyncio.sleep(0.05)
                break
            await asyncio.sleep(0.01)
        await supervisor.stop()

    asyncio.run(run())
    assert calls == []
    assert outbox.pending()[0].published_at is None


def test_slow_provider_renews_lease_and_blocks_concurrent_drain(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    staged = ArtifactEventOutbox(path)
    assert asyncio.run(staged.persist_then_publish(_event())) is False
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class SlowPublisher:
        def publish(self, event_id: str, _event: ArtifactApiEvent) -> None:
            calls.append(event_id)
            entered.set()
            assert release.wait(timeout=5)

    publisher = SlowPublisher()
    first = ArtifactEventOutbox(
        path,
        publisher=publisher,
        lease_seconds=1,
        provider_timeout_seconds=5,
        heartbeat_interval_seconds=0.1,
    )
    second = ArtifactEventOutbox(
        path,
        publisher=publisher,
        lease_seconds=1,
        provider_timeout_seconds=5,
        heartbeat_interval_seconds=0.1,
    )

    async def run() -> tuple[int, int]:
        first_task = asyncio.create_task(first.drain())
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.sleep(1.2)
        second_result = await second.drain()
        release.set()
        return await first_task, second_result

    first_result, second_result = asyncio.run(run())
    assert (first_result, second_result) == (1, 0)
    assert len(calls) == 1
    assert first.pending() == ()


def test_provider_timeout_leaves_pending_event_immediately_recoverable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class HangingPublisher:
        async def publish(self, _event_id: str, _event: ArtifactApiEvent) -> None:
            entered.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def run() -> None:
        timed_out = ArtifactEventOutbox(
            path,
            publisher=HangingPublisher(),
            lease_seconds=1,
            provider_timeout_seconds=0.05,
            heartbeat_interval_seconds=0.02,
        )
        with pytest.raises(ArtifactEventPublishTimeout):
            await timed_out.persist_then_publish(_event())
        assert entered.is_set()
        assert cancelled.is_set()
        pending = timed_out.pending()
        assert len(pending) == 1
        assert pending[0].attempts == 1
        assert pending[0].last_error_code == "ArtifactEventPublishTimeout"

        recovered = ArtifactEventOutbox(
            path,
            publisher=InspectingPublisher(path),
            lease_seconds=1,
        )
        assert await recovered.drain() == 1
        assert recovered.pending() == ()

    asyncio.run(run())


def test_concurrent_workers_publish_one_event_once(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingPublisher:
        def publish(self, event_id: str, event: ArtifactApiEvent) -> None:
            del event
            calls.append(event_id)
            entered.set()
            assert release.wait(timeout=5)

    first = ArtifactEventOutbox(path, publisher=BlockingPublisher())
    second = ArtifactEventOutbox(path, publisher=BlockingPublisher())

    async def run() -> tuple[bool, bool]:
        first_task = asyncio.create_task(first.persist_then_publish(_event()))
        assert await asyncio.to_thread(entered.wait, 5)
        second_result = await second.persist_then_publish(_event())
        release.set()
        return await first_task, second_result

    first_result, second_result = asyncio.run(run())
    assert first_result is True
    assert second_result is False
    assert len(calls) == 1
    assert first.pending() == ()


def test_existing_prelease_table_fails_closed_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    SQLiteDatabase(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            DROP TABLE artifact_event_outbox;
            CREATE TABLE artifact_event_outbox (
                event_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                published_at TEXT,
                last_error_code TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX artifact_event_outbox_pending
            ON artifact_event_outbox(published_at, created_at)
            WHERE published_at IS NULL;
            """
        )
        connection.execute(
            "INSERT INTO artifact_event_outbox("
            "event_id,idempotency_key,event_type,artifact_id,payload_sha256,"
            "payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                "event-legacy",
                "key-legacy",
                "artifact.legacy",
                "artifact-legacy",
                "0" * 64,
                "{}",
                "2026-07-11T00:00:00Z",
            ),
        )
        before_schema = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE name IN ('artifact_event_outbox','artifact_event_outbox_pending') "
                "ORDER BY type,name"
            )
        )
        before_rows = tuple(connection.execute("SELECT * FROM artifact_event_outbox"))

    with pytest.raises(
        SchemaVersionError,
        match="product schema fragment integration is incompatible",
    ):
        ArtifactEventOutbox(path)

    with sqlite3.connect(path) as connection:
        after_schema = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE name IN ('artifact_event_outbox','artifact_event_outbox_pending') "
                "ORDER BY type,name"
            )
        )
        after_rows = tuple(connection.execute("SELECT * FROM artifact_event_outbox"))
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(artifact_event_outbox)"
            ).fetchall()
        }
    assert after_schema == before_schema
    assert after_rows == before_rows
    assert {
        "lease_token",
        "lease_expires_at",
        "account_id",
        "thread_id",
        "turn_id",
    }.isdisjoint(columns)
