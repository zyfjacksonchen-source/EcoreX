from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json

import httpx
import pytest

from ecorex.artifacts import ArtifactScope, ArtifactService, RenditionKind
from ecorex.protocol import (
    CreateThreadRequest,
    CreateTurnRequest,
    ItemKind,
    ItemStatus,
    TurnStatus,
)
from ecorex.runtime import RuntimeKernel
from ecorex.sharing import (
    HTTPSSharePublisher,
    PublishedShare,
    ShareOperationWorker,
    SharePayload,
    ShareRepository,
    ShareMediaContractError,
    ShareSnapshotService,
    SharedArtifact,
    SharedMediaRendition,
)


NOW = datetime.now(timezone.utc)
PNG = b"\x89PNG\r\n\x1a\n" + b"shared-image"


class Publisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.media_failures = 0

    async def upload_media(self, share_id, media, content, *, idempotency_key):
        assert hashlib.sha256(content).hexdigest() == media.sha256
        self.calls.append(("media", idempotency_key))
        if self.media_failures:
            self.media_failures -= 1
            raise TimeoutError("transient media transport")

    async def publish(self, payload, *, idempotency_key):
        self.calls.append(("snapshot", idempotency_key))
        return PublishedShare(
            remote_snapshot_id="remote-" + payload.share_id,
            public_url=f"https://share.ecorex.test/s/{payload.share_id}",
        )

    async def revoke(self, remote_snapshot_id, *, idempotency_key):
        return None


class Credentials:
    def bearer_token(self) -> str:
        return "session-" + "x" * 32


def _stack(tmp_path, *, attach_preview: bool = True):
    kernel = RuntimeKernel(tmp_path / "runtime.db")
    thread = kernel.create_thread(
        CreateThreadRequest(title="图片任务", client_request_id="thread-create")
    )
    turn = kernel.create_turn(
        thread.thread_id,
        CreateTurnRequest(
            input="把这张图片改成更明亮的版本",
            agent_model_id="ecorex-chat",
            client_message_id="message-create",
        ),
    )
    kernel.create_item(
        turn_id=turn.turn.turn_id,
        kind=ItemKind.MESSAGE,
        status=ItemStatus.COMPLETED,
        content={"role": "assistant", "text": "已经完成修改，请查看图片。"},
    )
    agent_job = kernel.jobs.lease_next("agent-worker", kinds=["agent_turn"])
    assert agent_job is not None and agent_job.lease_token
    kernel.jobs.start(agent_job.job_id, "agent-worker", agent_job.lease_token)
    for status in (
        TurnStatus.PREPARING,
        TurnStatus.MODEL_REQUESTED,
        TurnStatus.STREAMING,
        TurnStatus.FINALIZING,
    ):
        kernel.transition_turn(turn.turn.turn_id, status)
    kernel.finish_turn_job(
        job_id=agent_job.job_id,
        worker_id="agent-worker",
        lease_token=agent_job.lease_token,
        target=TurnStatus.COMPLETED,
    )
    artifacts = ArtifactService(
        tmp_path / "artifacts",
        database_path=kernel.database.path,
        clock=lambda: NOW,
    )
    artifact = artifacts.create_artifact(
        PNG,
        requested_name="明亮版本.png",
        mime_type="image/png",
        scope=ArtifactScope(
            account_id="account-1",
            thread_id=thread.thread_id,
            turn_id=turn.turn.turn_id,
            created_by_tool_id="imagegen",
        ),
    )
    if attach_preview:
        artifacts.attach_rendition(
            artifact.artifact_id,
            content=PNG,
            requested_name="明亮版_预览.png",
            mime_type="image/png",
            kind=RenditionKind.PREVIEW,
            parent_revision_id=artifact.revision_id,
        )
    repository = ShareRepository(kernel.database, jobs=kernel.jobs)
    publisher = Publisher()
    service = ShareSnapshotService(
        kernel,
        repository=repository,
        publisher=publisher,
        account_id="account-1",
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        artifacts=artifacts,
        clock=lambda: NOW,
    )
    return kernel, thread, turn, artifact, artifacts, repository, publisher, service


def test_local_share_refuses_an_image_without_a_rendition_and_leaks_no_path(
    tmp_path,
) -> None:
    _kernel, thread, _turn, _artifact, _artifacts, repository, publisher, service = (
        _stack(tmp_path, attach_preview=False)
    )
    with pytest.raises(ShareMediaContractError) as failure:
        asyncio.run(
            service.create(
                thread.thread_id,
                expires_in_hours=24,
                client_request_id="share-image-without-preview",
            )
        )
    assert failure.value.code == "share_image_preview_missing"
    assert failure.value.retryable is True
    assert failure.value.action == "wait_for_preview_then_retry"
    assert "\\" not in str(failure.value)
    assert repository.list_for_thread(
        thread.thread_id,
        account_id="account-1",
        now=NOW,
    )[1] == 0
    assert publisher.calls == []


def test_shared_media_mime_type_is_canonical_before_transport_signing() -> None:
    media = SharedMediaRendition(
        media_id="shm_" + "a" * 32,
        kind="preview",
        mime_type="IMAGE/PNG;charset=binary",
        size_bytes=len(PNG),
        sha256=hashlib.sha256(PNG).hexdigest(),
    )

    assert media.mime_type == "image/png"


def test_share_v2_binds_image_to_turn_and_uploads_media_before_snapshot(tmp_path) -> None:
    _kernel, thread, turn, artifact, artifacts, repository, publisher, service = _stack(
        tmp_path
    )
    queued = asyncio.run(
        service.create(
            thread.thread_id,
            expires_in_hours=24,
            client_request_id="share-image",
        )
    )
    payload = repository.read_payload(queued.share_id, account_id="account-1")
    assert payload.schema_version == 2
    assert [message.role for message in payload.messages] == ["user", "assistant"]
    assert payload.artifacts[0].artifact_id == artifact.artifact_id
    assert payload.artifacts[0].turn_id == turn.turn.turn_id
    assert payload.artifacts[0].preview is not None
    assert payload.artifacts[0].preview.mime_type == "image/png"

    worker = ShareOperationWorker(
        repository,
        publisher,
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=lambda: NOW,
        retry_delay_seconds=0,
        media_loader=artifacts.blobs.read_bytes,
    )
    result = asyncio.run(worker.run_once("share-worker"))
    assert result.outcome.value == "completed"
    assert publisher.calls == [
        ("media", f"{queued.share_id}:{payload.artifacts[0].preview.media_id}"),
        ("snapshot", queued.share_id),
    ]


def test_media_integrity_failure_fails_closed_before_public_snapshot(tmp_path) -> None:
    _kernel, thread, _turn, _artifact, _artifacts, repository, publisher, service = _stack(
        tmp_path
    )
    queued = asyncio.run(
        service.create(
            thread.thread_id,
            expires_in_hours=24,
            client_request_id="share-tamper",
        )
    )
    worker = ShareOperationWorker(
        repository,
        publisher,
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=lambda: NOW,
        retry_delay_seconds=0,
        media_loader=lambda _digest: PNG + b"tampered",
    )
    result = asyncio.run(worker.run_once("share-worker"))
    assert result.outcome.value == "failed"
    assert result.reason == "share_media_integrity_invalid"
    assert publisher.calls == []
    assert service.get(queued.share_id).status.value == "failed"


def test_media_retry_reuses_the_same_key_and_only_then_publishes_snapshot(tmp_path) -> None:
    _kernel, thread, _turn, _artifact, artifacts, repository, publisher, service = _stack(
        tmp_path
    )
    publisher.media_failures = 1
    queued = asyncio.run(
        service.create(
            thread.thread_id,
            expires_in_hours=24,
            client_request_id="share-media-retry",
        )
    )
    payload = repository.read_payload(queued.share_id, account_id="account-1")
    assert payload.artifacts[0].preview is not None
    media_key = f"{queued.share_id}:{payload.artifacts[0].preview.media_id}"
    worker = ShareOperationWorker(
        repository,
        publisher,
        allowed_public_hosts=frozenset({"share.ecorex.test"}),
        clock=lambda: NOW,
        retry_delay_seconds=0,
        media_loader=artifacts.blobs.read_bytes,
    )
    assert asyncio.run(worker.run_once("share-worker")).outcome.value == "retry_scheduled"
    assert asyncio.run(worker.run_once("share-worker")).outcome.value == "completed"
    assert publisher.calls == [
        ("media", media_key),
        ("media", media_key),
        ("snapshot", queued.share_id),
    ]


def test_schema_v1_canonical_bytes_do_not_gain_v2_nullable_fields() -> None:
    payload = SharePayload(
        share_id="shr_" + "a" * 32,
        thread_id="thread-1",
        source_watermark=1,
        artifacts=[
            SharedArtifact(
                artifact_id="artifact-1",
                revision_id="revision-1",
                family="image",
                display_name="图片.png",
                mime_type="image/png",
                size_bytes=12,
            )
        ],
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    encoded = payload.canonical_bytes()
    decoded = json.loads(encoded)
    assert decoded["schema_version"] == 1
    assert set(decoded["artifacts"][0]) == {
        "artifact_id",
        "revision_id",
        "family",
        "display_name",
        "mime_type",
        "size_bytes",
    }
    assert SharePayload.model_validate_json(encoded).canonical_bytes() == encoded


def test_https_transport_uploads_bounded_digest_bound_media_without_redirects() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = HTTPSSharePublisher(
        "https://control.ecorex.test/api/v1/shares",
        credentials=Credentials(),
        allowed_hosts=frozenset({"control.ecorex.test"}),
        client=client,
    )
    media = SharedMediaRendition(
        media_id="shm_" + "b" * 32,
        kind="preview",
        mime_type="image/png",
        size_bytes=len(PNG),
        sha256=hashlib.sha256(PNG).hexdigest(),
    )
    asyncio.run(
        publisher.upload_media(
            "shr_" + "a" * 32,
            media,
            PNG,
            idempotency_key="shr_" + "a" * 32 + ":" + media.media_id,
        )
    )
    asyncio.run(client.aclose())
    request = seen[0]
    assert request.method == "PUT"
    assert request.url.path.endswith("/media/" + media.media_id)
    assert request.headers["content-type"] == "image/png"
    assert request.headers["x-content-sha256"] == media.sha256
    assert request.content == PNG
