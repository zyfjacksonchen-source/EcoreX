from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace

import pytest

from ecorex.protocol import (
    CreateThreadRequest,
    CreateTurnRequest,
    ItemKind,
    ItemStatus,
    PublicToolActivity,
    TurnStatus,
)
from ecorex.runtime import RuntimeKernel
from ecorex.sharing import (
    DiagnosticSnapshotService,
    PublishedShare,
    ShareConflict,
    ShareNotFound,
    ShareOperationWorker,
    ShareRepository,
    ShareSnapshotService,
    ShareStatus,
    SharedArtifact,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 10, 15, 34, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class Publisher:
    def __init__(self) -> None:
        self.published = []
        self.revoked = []
        self.fail_once = False

    async def publish(self, payload, *, idempotency_key):
        self.published.append((payload, idempotency_key))
        if self.fail_once:
            self.fail_once = False
            raise TimeoutError("provider secret must not escape")
        return PublishedShare(
            remote_snapshot_id="remote_" + payload.share_id,
            public_url=f"https://share.ecorex.test/s/{payload.share_id}",
        )

    async def revoke(self, remote_snapshot_id, *, idempotency_key):
        self.revoked.append((remote_snapshot_id, idempotency_key))


def thread_with_messages(kernel: RuntimeKernel, title: str, suffix: str):
    thread = kernel.create_thread(
        CreateThreadRequest(title=title, client_request_id=f"thread-{suffix}")
    )
    created = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input=f"用户消息 {suffix}",
            agent_model_id="ecorex-chat",
            client_message_id=f"message-{suffix}",
        ),
    )
    kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.MESSAGE,
        status=ItemStatus.COMPLETED,
        content={"role": "assistant", "text": f"助手回复 {suffix}"},
    )
    kernel.create_item(
        turn_id=created.turn.turn_id,
        kind=ItemKind.TOOL_CALL,
        status=ItemStatus.COMPLETED,
        content=PublicToolActivity(
            tool_call_id=f"share-tool-{suffix}",
            tool_id="shell",
            tool_name="shell",
            display_label="执行已批准的命令",
            phase="completed",
            status="completed",
            effects=["execute"],
            risk="high",
            argument_summary="正在执行已批准的命令",
            result_summary="命令执行已完成",
            argument_sha256="0" * 64,
            result_sha256="1" * 64,
        ).model_dump(mode="json"),
    )
    leased = kernel.jobs.lease_next(f"source-{suffix}", kinds=["agent_turn"])
    assert leased is not None and leased.lease_token
    kernel.jobs.start(leased.job_id, f"source-{suffix}", leased.lease_token)
    for status in (
        TurnStatus.PREPARING,
        TurnStatus.MODEL_REQUESTED,
        TurnStatus.STREAMING,
        TurnStatus.FINALIZING,
    ):
        kernel.transition_turn(created.turn.turn_id, status)
    kernel.finish_turn_job(
        job_id=leased.job_id,
        worker_id=f"source-{suffix}",
        lease_token=leased.lease_token,
        target=TurnStatus.COMPLETED,
    )
    return thread


def services(tmp_path):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    repository = ShareRepository(kernel.database)
    publisher = Publisher()
    clock = Clock()
    sharing = ShareSnapshotService(
        kernel,
        repository=repository,
        publisher=publisher,
        account_id="account-1",
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=clock,
    )
    worker = ShareOperationWorker(
        repository,
        publisher,
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=clock,
        retry_delay_seconds=0,
    )
    diagnostics = DiagnosticSnapshotService(
        kernel,
        repository=repository,
        account_id="account-1",
        clock=clock,
    )
    return kernel, repository, publisher, clock, sharing, diagnostics, worker


async def create_published(sharing, worker, thread_id, *, hours, request_id):
    state = await sharing.create(
        thread_id, expires_in_hours=hours, client_request_id=request_id
    )
    if state.status is ShareStatus.PUBLISHING:
        await worker.run_once(f"share-{request_id}")
    return sharing.get(state.share_id)


async def revoke_settled(sharing, worker, share_id, *, request_id):
    state = await sharing.revoke(share_id, client_request_id=request_id)
    if state.status is ShareStatus.REVOKING:
        await worker.run_once(f"revoke-{request_id}")
    return sharing.get(share_id)


def test_two_threads_receive_unique_backend_share_id_url_and_safe_payload(tmp_path) -> None:
    kernel, repository, publisher, _clock, sharing, _diagnostics, worker = services(tmp_path)
    first_thread = thread_with_messages(kernel, "第一会话", "a")
    second_thread = thread_with_messages(kernel, "第二会话", "b")

    first = asyncio.run(
        create_published(sharing, worker, first_thread.thread_id, hours=24, request_id="share-a")
    )
    second = asyncio.run(
        create_published(sharing, worker, second_thread.thread_id, hours=24, request_id="share-b")
    )
    assert first.status is second.status is ShareStatus.PUBLISHED
    assert first.share_id != second.share_id
    assert first.public_url != second.public_url
    assert first.thread_id != second.thread_id
    payload = repository.read_payload(first.share_id, account_id="account-1")
    assert [message.role for message in payload.messages] == ["user", "assistant"]
    assert "DO-NOT-SHARE" not in payload.canonical_bytes().decode("utf-8")
    assert payload.artifacts == []
    assert len(publisher.published) == 2


def test_share_create_is_idempotent_and_failed_publish_retries_same_snapshot(tmp_path) -> None:
    kernel, repository, publisher, _clock, sharing, _diagnostics, worker = services(tmp_path)
    thread = thread_with_messages(kernel, "重试", "retry")
    publisher.fail_once = True
    queued = asyncio.run(
        sharing.create(thread.thread_id, expires_in_hours=12, client_request_id="same")
    )
    assert queued.status is ShareStatus.PUBLISHING
    assert asyncio.run(worker.run_once("retry-first")).outcome.value == "retry_scheduled"
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        failed_id = connection.execute(
            "SELECT share_id FROM share_snapshots WHERE client_request_id='same'"
        ).fetchone()[0]
    assert asyncio.run(worker.run_once("retry-second")).outcome.value == "completed"
    retried = sharing.get(failed_id)
    duplicate = asyncio.run(
        sharing.create(thread.thread_id, expires_in_hours=12, client_request_id="same")
    )
    assert retried == duplicate
    assert retried.share_id == failed_id
    assert [call[0].share_id for call in publisher.published] == [failed_id, failed_id]

    other = thread_with_messages(kernel, "其他", "other")
    with pytest.raises(ShareConflict, match="different input"):
        asyncio.run(
            sharing.create(other.thread_id, expires_in_hours=12, client_request_id="same")
        )
    assert repository.read_payload(failed_id, account_id="account-1").thread_id == thread.thread_id


def test_share_revoke_and_expiry_never_expose_a_stale_url(tmp_path) -> None:
    kernel, repository, publisher, clock, sharing, _diagnostics, worker = services(tmp_path)
    thread = thread_with_messages(kernel, "撤销", "revoke")
    created = asyncio.run(
        create_published(sharing, worker, thread.thread_id, hours=1, request_id="create")
    )
    revoked = asyncio.run(sharing.revoke(created.share_id, client_request_id="revoke"))
    repeated = asyncio.run(sharing.revoke(created.share_id, client_request_id="revoke"))
    assert revoked == repeated
    assert revoked.status is ShareStatus.REVOKING
    assert asyncio.run(worker.run_once("revoke-worker")).outcome.value == "completed"
    revoked = sharing.get(created.share_id)
    assert revoked.status is ShareStatus.REVOKED
    assert revoked.public_url is None
    assert len(publisher.revoked) == 1

    expiring = asyncio.run(
        create_published(sharing, worker, thread.thread_id, hours=1, request_id="expire")
    )
    clock.value += timedelta(hours=2)
    expired = sharing.get(expiring.share_id)
    assert expired.status is ShareStatus.EXPIRED
    assert expired.public_url is None

    listed, count = sharing.list(thread.thread_id)
    assert count == 2
    assert [item.share_id for item in listed] == [expiring.share_id, created.share_id]
    assert listed[0].status is ShareStatus.EXPIRED
    assert listed[0].public_url is None
    assert listed[1].status is ShareStatus.REVOKED
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        durable = connection.execute(
            "SELECT status, public_url FROM share_snapshots WHERE share_id=?",
            (expiring.share_id,),
        ).fetchone()
    assert durable is not None
    assert durable[0] == ShareStatus.PUBLISHED.value
    assert durable[1] is not None
    assert repository.expire_due(now=clock()) == 1
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        durable = connection.execute(
            "SELECT status, public_url FROM share_snapshots WHERE share_id=?",
            (expiring.share_id,),
        ).fetchone()
    assert durable == (ShareStatus.EXPIRED.value, None)

    clock.value -= timedelta(hours=2)
    racing = asyncio.run(
        create_published(sharing, worker, thread.thread_id, hours=1, request_id="race-expiry")
    )
    repository.begin_revoke(
        racing.share_id,
        account_id="account-1",
        client_request_id="race-revoke",
        now=clock(),
    )
    clock.value += timedelta(hours=2)
    assert repository.get(
        racing.share_id, account_id="account-1", now=clock()
    ).status is ShareStatus.EXPIRED
    settled = repository.mark_revoked(
        racing.share_id, account_id="account-1", now=clock()
    )
    assert settled.status is ShareStatus.REVOKED
    assert settled.public_url is None


def test_diagnostic_snapshot_is_private_metadata_only_and_separate_from_share(tmp_path) -> None:
    kernel, repository, _publisher, _clock, sharing, diagnostics, _worker = services(tmp_path)
    thread = thread_with_messages(kernel, "诊断", "diag")
    share = asyncio.run(
        sharing.create(thread.thread_id, expires_in_hours=24, client_request_id="public")
    )
    diagnostic = diagnostics.create(
        thread.thread_id,
        reason_code="artifact.feedback.down",
        client_request_id="diagnostic",
    )
    assert share.share_id.startswith("shr_")
    assert diagnostic.diagnostic_id.startswith("diag_")
    assert diagnostic.diagnostic_id != share.share_id
    private = repository.read_diagnostic(
        diagnostic.diagnostic_id, account_id="account-1"
    )
    encoded = private.canonical_bytes().decode("utf-8")
    assert "DO-NOT-SHARE" not in encoded
    assert "public_url" not in encoded
    assert all(not hasattr(event, "payload") for event in private.events)


def test_snapshot_identity_is_immutable_and_digest_tamper_is_detected(tmp_path) -> None:
    kernel, repository, _publisher, _clock, sharing, _diagnostics, _worker = services(tmp_path)
    thread = thread_with_messages(kernel, "完整性", "integrity")
    created = asyncio.run(
        sharing.create(thread.thread_id, expires_in_hours=24, client_request_id="integrity")
    )
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute(
                "UPDATE share_snapshots SET thread_id='other' WHERE share_id=?",
                (created.share_id,),
            )
        connection.execute("DROP TRIGGER share_snapshot_identity_immutable")
        connection.execute(
            "UPDATE share_snapshots SET payload_json='{}' WHERE share_id=?",
            (created.share_id,),
        )
    with pytest.raises(ShareConflict, match="integrity"):
        repository.read_payload(created.share_id, account_id="account-1")


def test_local_share_and_diagnostic_reads_are_account_scoped(tmp_path) -> None:
    kernel, repository, _publisher, _clock, sharing, diagnostics, _worker = services(tmp_path)
    thread = thread_with_messages(kernel, "租户隔离", "tenant")
    share = asyncio.run(
        sharing.create(thread.thread_id, expires_in_hours=24, client_request_id="tenant-share")
    )
    diagnostic = diagnostics.create(
        thread.thread_id,
        reason_code="security.audit",
        client_request_id="tenant-diagnostic",
    )

    with pytest.raises(ShareNotFound):
        repository.get(share.share_id, account_id="account-2", now=_clock())
    with pytest.raises(ShareNotFound):
        repository.read_payload(share.share_id, account_id="account-2")
    with pytest.raises(ShareNotFound):
        repository.read_diagnostic(diagnostic.diagnostic_id, account_id="account-2")
    assert repository.list_for_thread(
        thread.thread_id, account_id="account-2", now=_clock()
    ) == ([], 0)


def test_concurrent_same_request_converges_on_one_durable_share_identity(tmp_path) -> None:
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    repository = ShareRepository(kernel.database)
    clock = Clock()
    publisher = Publisher()
    sharing = ShareSnapshotService(
        kernel,
        repository=repository,
        publisher=publisher,
        account_id="account-1",
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=clock,
    )
    worker = ShareOperationWorker(
        repository,
        publisher,
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=clock,
    )
    thread = thread_with_messages(kernel, "并发", "concurrent")

    async def create_twice():
        return await asyncio.gather(
            sharing.create(
                thread.thread_id,
                expires_in_hours=24,
                client_request_id="same-concurrent-request",
            ),
            sharing.create(
                thread.thread_id,
                expires_in_hours=24,
                client_request_id="same-concurrent-request",
            ),
        )

    first, second = asyncio.run(create_twice())
    assert first == second
    assert first.status is ShareStatus.PUBLISHING
    assert asyncio.run(worker.run_once("concurrent-worker")).outcome.value == "completed"
    assert sharing.get(first.share_id).status is ShareStatus.PUBLISHED
    assert len(publisher.published) == 1
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM share_snapshots").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='share_publish'"
        ).fetchone()[0] == 1


def test_publish_completion_is_fenced_after_expiry(tmp_path) -> None:
    kernel, repository, _publisher, clock, _sharing, _diagnostics, _worker = services(tmp_path)

    class LatePublisher:
        async def publish(self, payload, *, idempotency_key):
            clock.value += timedelta(hours=2)
            return PublishedShare(
                remote_snapshot_id="late-remote",
                public_url=f"https://share.ecorex.test/s/{payload.share_id}",
            )

        async def revoke(self, remote_snapshot_id, *, idempotency_key):
            return None

    sharing = ShareSnapshotService(
        kernel,
        repository=repository,
        publisher=LatePublisher(),
        account_id="account-1",
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=clock,
    )
    worker = ShareOperationWorker(
        repository,
        sharing.publisher,
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=clock,
    )
    thread = thread_with_messages(kernel, "到期栅栏", "late")
    queued = asyncio.run(
        sharing.create(
            thread.thread_id,
            expires_in_hours=1,
            client_request_id="late-publish",
        )
    )
    assert queued.status is ShareStatus.PUBLISHING
    clock.value += timedelta(hours=2)
    assert asyncio.run(worker.run_once("late-worker")).outcome.value == "idle"
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        share_id = connection.execute(
            "SELECT share_id FROM share_snapshots WHERE client_request_id='late-publish'"
        ).fetchone()[0]
    expired = sharing.get(share_id)
    assert expired.status is ShareStatus.EXPIRED
    assert expired.public_url is None


def test_public_artifact_contract_strips_paths_and_rejects_unsafe_names(tmp_path) -> None:
    with pytest.raises(ValueError, match="safe basename"):
        SharedArtifact(
            artifact_id="art-1",
            revision_id="rev-1",
            family="pdf",
            display_name=r"C:\\Users\\secret\\report.pdf",
            mime_type="application/pdf",
            size_bytes=1,
        )

    kernel, repository, publisher, clock, _sharing, _diagnostics, _worker = services(tmp_path)
    artifacts = SimpleNamespace(
        list_user_artifacts=lambda **_kwargs: [
            SimpleNamespace(
                artifact_id="art-1",
                revision_id="rev-1",
                family=SimpleNamespace(value="pdf"),
                visibility=SimpleNamespace(value="primary"),
                display_name=r"C:\\Users\\secret\\report.pdf",
                mime_type="application/pdf",
                size_bytes=42,
            )
        ]
    )
    sharing = ShareSnapshotService(
        kernel,
        repository=repository,
        publisher=publisher,
        account_id="account-1",
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        artifacts=artifacts,
        clock=clock,
    )
    thread = thread_with_messages(kernel, "路径", "path")
    created = asyncio.run(
        sharing.create(thread.thread_id, expires_in_hours=24, client_request_id="path-share")
    )
    payload = repository.read_payload(created.share_id, account_id="account-1")
    assert payload.artifacts[0].display_name == "report.pdf"
    assert "Users" not in payload.canonical_bytes().decode("utf-8")


def test_projection_and_diagnostic_metadata_tamper_fail_closed(tmp_path) -> None:
    kernel, repository, _publisher, clock, sharing, diagnostics, _worker = services(tmp_path)
    thread = thread_with_messages(kernel, "篡改", "metadata")
    share = asyncio.run(
        sharing.create(thread.thread_id, expires_in_hours=24, client_request_id="metadata-share")
    )
    diagnostic = diagnostics.create(
        thread.thread_id,
        reason_code="security.audit",
        client_request_id="metadata-diagnostic",
    )
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        connection.execute(
            "UPDATE share_snapshots SET public_url='http://evil.test/leak' WHERE share_id=?",
            (share.share_id,),
        )
        connection.execute("DROP TRIGGER diagnostic_snapshots_no_update")
        connection.execute(
            "UPDATE diagnostic_snapshots SET thread_id='forged' WHERE diagnostic_id=?",
            (diagnostic.diagnostic_id,),
        )
    with pytest.raises(ShareConflict, match="projection"):
        sharing.get(share.share_id)
    with pytest.raises(ShareConflict, match="metadata"):
        repository.read_diagnostic(diagnostic.diagnostic_id, account_id="account-1")
