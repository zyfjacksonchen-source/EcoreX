from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import io
import sqlite3
import threading
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

from ecorex.control_plane import (
    CloudShareKeyRing,
    CloudShareRepository,
    CloudShareSchemaManager,
    ControlPlaneRepository,
    S3ShareObjectPreconditionFailed,
    S3ShareObjectStore,
    ShareObjectCapacityError,
    ShareObjectError,
    create_control_plane_app,
    migrate_control_plane_database,
)
from ecorex.sharing import (
    SharePayload,
    SharedArtifact,
    SharedMediaRendition,
    SharedMessage,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_SHA256 = hashlib.sha256(PNG).hexdigest()


@dataclass
class _Object:
    payload: bytes
    mime_type: str
    checksum: str
    metadata: dict[str, str]
    etag: str


class _Body:
    def __init__(self, payload: bytes) -> None:
        self.handle = io.BytesIO(payload)
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self.handle.read(amount)

    def close(self) -> None:
        self.closed = True
        self.handle.close()


class FakeS3Client:
    """Thread-safe boto-shaped private S3 fixture with fault injection."""

    def __init__(self) -> None:
        # Deliberately present only on the caller-owned client.  Store requests,
        # metadata and SQLite must never copy these fields.
        self.access_key = "AKIA_CALLER_OWNED_SECRET"
        self.secret_key = "caller-owned-secret"
        self.objects: dict[tuple[str, str], _Object] = {}
        self.calls: list[tuple[str, dict]] = []
        self.lock = threading.RLock()
        self.generation = 0
        self.fake_head_checksum = False
        self.fake_get_checksum = False
        self.short_read = False
        self.get_etag_drift = False
        self.get_error = False
        self.delete_etag_drift = False
        self.last_body: _Body | None = None

    def put_object(self, **kwargs):
        with self.lock:
            self.calls.append(("put", dict(kwargs)))
            identity = (kwargs["Bucket"], kwargs["Key"])
            if kwargs.get("IfNoneMatch") != "*":
                raise AssertionError("conditional create is required")
            if identity in self.objects:
                raise S3ShareObjectPreconditionFailed("winner exists")
            content = bytes(kwargs["Body"])
            expected = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
            if kwargs.get("ChecksumSHA256") != expected:
                raise AssertionError("client received a false checksum")
            self.generation += 1
            etag = f'"provider-{self.generation}"'
            self.objects[identity] = _Object(
                payload=content,
                mime_type=kwargs["ContentType"],
                checksum=kwargs["ChecksumSHA256"],
                metadata=dict(kwargs["Metadata"]),
                etag=etag,
            )
            return {"ETag": etag, "ChecksumSHA256": expected}

    def head_object(self, **kwargs):
        with self.lock:
            self.calls.append(("head", dict(kwargs)))
            item = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
            if item is None:
                raise _ClientError("NoSuchKey", 404)
            return self._response(item, fake_checksum=self.fake_head_checksum)

    def get_object(self, **kwargs):
        with self.lock:
            self.calls.append(("get", dict(kwargs)))
            if self.get_error:
                raise RuntimeError("injected transport failure")
            item = self.objects.get((kwargs["Bucket"], kwargs["Key"]))
            if item is None:
                raise _ClientError("NoSuchKey", 404)
            if self.get_etag_drift:
                self.generation += 1
                item.etag = f'"provider-{self.generation}"'
            if kwargs.get("IfMatch") != item.etag:
                raise _ClientError("PreconditionFailed", 412)
            expected_range = f"bytes=0-{len(item.payload) - 1}"
            if kwargs.get("Range") != expected_range:
                raise AssertionError("bounded full-object Range is required")
            body = _Body(item.payload[:-1] if self.short_read else item.payload)
            self.last_body = body
            return {
                **self._response(item, fake_checksum=self.fake_get_checksum),
                "ContentRange": f"bytes 0-{len(item.payload) - 1}/{len(item.payload)}",
                "Body": body,
            }

    def delete_object(self, **kwargs):
        with self.lock:
            self.calls.append(("delete", dict(kwargs)))
            identity = (kwargs["Bucket"], kwargs["Key"])
            item = self.objects.get(identity)
            if item is None:
                raise _ClientError("NoSuchKey", 404)
            if self.delete_etag_drift:
                self.generation += 1
                item.etag = f'"provider-{self.generation}"'
            if kwargs.get("IfMatch") != item.etag:
                raise _ClientError("PreconditionFailed", 412)
            del self.objects[identity]
            return {}

    @staticmethod
    def _response(item: _Object, *, fake_checksum: bool) -> dict:
        return {
            "ContentLength": len(item.payload),
            "ContentType": item.mime_type,
            "ChecksumSHA256": (
                base64.b64encode(b"x" * 32).decode("ascii")
                if fake_checksum
                else item.checksum
            ),
            "Metadata": dict(item.metadata),
            "ETag": item.etag,
        }


class _ClientError(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


def store(client: FakeS3Client, **overrides) -> S3ShareObjectStore:
    return S3ShareObjectStore(
        client,
        bucket="private-ecorex-share",
        prefix="tenant-a/share",
        **overrides,
    )


def test_s3_store_has_no_credential_surface_and_conditional_put_deduplicates_concurrently() -> None:
    signature = inspect.signature(S3ShareObjectStore)
    assert not {
        "access_key",
        "secret_key",
        "session_token",
        "endpoint_url",
        "verify",
    } & set(signature.parameters)
    client = FakeS3Client()
    objects = store(client)

    with ThreadPoolExecutor(max_workers=8) as pool:
        descriptors = list(
            pool.map(
                lambda _index: objects.put(
                    PNG, sha256=PNG_SHA256, mime_type="image/png"
                ),
                range(8),
            )
        )
    assert all(descriptor == descriptors[0] for descriptor in descriptors)
    assert descriptors[0].object_key == f"tenant-a/share/sha256/{PNG_SHA256}"
    assert len(client.objects) == 1
    put_calls = [arguments for operation, arguments in client.calls if operation == "put"]
    assert len(put_calls) == 8
    assert all(arguments["IfNoneMatch"] == "*" for arguments in put_calls)
    assert all(arguments["ChecksumAlgorithm"] == "SHA256" for arguments in put_calls)
    serialized = repr(put_calls).casefold()
    assert "akia_caller_owned_secret" not in serialized
    assert "caller-owned-secret" not in serialized
    assert all("ACL" not in arguments for arguments in put_calls)


def test_s3_open_uses_etag_fenced_range_and_bounded_disk_spool_with_slot_release() -> None:
    client = FakeS3Client()
    payload = b"z" * (512 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    objects = store(
        client,
        max_open_streams=1,
        max_object_bytes=1024 * 1024,
        max_total_spool_bytes=1024 * 1024,
        memory_spool_bytes=1024,
    )
    descriptor = objects.put(payload, sha256=digest, mime_type="image/png")
    opened = objects.open(
        descriptor.object_key,
        sha256=digest,
        size_bytes=len(payload),
        mime_type="image/png",
    )
    assert getattr(opened._handle, "_rolled", False) is True
    get_call = [arguments for operation, arguments in client.calls if operation == "get"][-1]
    assert get_call["Range"] == f"bytes=0-{len(payload) - 1}"
    assert get_call["IfMatch"].startswith('"provider-')
    assert get_call["ChecksumMode"] == "ENABLED"
    with pytest.raises(ShareObjectCapacityError, match="capacity"):
        objects.open(
            descriptor.object_key,
            sha256=digest,
            size_bytes=len(payload),
            mime_type="image/png",
        )
    iterator = opened.iter_range(13, 112, chunk_bytes=17)
    assert b"".join(iterator) == payload[13:113]
    reopened = objects.open(
        descriptor.object_key,
        sha256=digest,
        size_bytes=len(payload),
        mime_type="image/png",
    )
    reopened.close()
    assert client.last_body is not None and client.last_body.closed is True


def test_s3_single_object_and_aggregate_spool_byte_limits_are_independent() -> None:
    client = FakeS3Client()
    payload = b"p" * (400 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    objects = store(
        client,
        max_open_streams=2,
        max_object_bytes=512 * 1024,
        max_total_spool_bytes=512 * 1024,
        memory_spool_bytes=1024,
    )
    descriptor = objects.put(payload, sha256=digest, mime_type="image/png")
    first = objects.open(
        descriptor.object_key,
        sha256=digest,
        size_bytes=len(payload),
        mime_type="image/png",
    )
    with pytest.raises(ShareObjectCapacityError, match="spool capacity"):
        objects.open(
            descriptor.object_key,
            sha256=digest,
            size_bytes=len(payload),
            mime_type="image/png",
        )
    first.close()
    oversized = b"x" * (512 * 1024 + 1)
    with pytest.raises(ShareObjectError, match="identity"):
        objects.put(
            oversized,
            sha256=hashlib.sha256(oversized).hexdigest(),
            mime_type="image/png",
        )


@pytest.mark.parametrize("fault", ["fake_head_checksum", "fake_get_checksum", "short_read", "get_etag_drift", "get_error"])
def test_s3_checksum_etag_short_read_and_client_failures_release_all_capacity(fault) -> None:
    client = FakeS3Client()
    objects = store(
        client,
        max_open_streams=1,
        max_object_bytes=1024,
        max_total_spool_bytes=1024,
        memory_spool_bytes=32,
    )
    descriptor = objects.put(PNG, sha256=PNG_SHA256, mime_type="image/png")
    setattr(client, fault, True)
    with pytest.raises(ShareObjectError):
        objects.open(
            descriptor.object_key,
            sha256=PNG_SHA256,
            size_bytes=len(PNG),
            mime_type="image/png",
        )
    setattr(client, fault, False)
    # If either semaphore or byte budget leaked, this retry would report busy.
    recovered = objects.open(
        descriptor.object_key,
        sha256=PNG_SHA256,
        size_bytes=len(PNG),
        mime_type="image/png",
    )
    recovered.close()


def test_s3_delete_is_head_verified_etag_conditional_and_retryable() -> None:
    client = FakeS3Client()
    objects = store(client)
    descriptor = objects.put(PNG, sha256=PNG_SHA256, mime_type="image/png")
    client.delete_etag_drift = True
    with pytest.raises(S3ShareObjectPreconditionFailed):
        objects.delete(descriptor.object_key, sha256=PNG_SHA256)
    assert len(client.objects) == 1
    client.delete_etag_drift = False
    assert objects.delete(descriptor.object_key, sha256=PNG_SHA256) is True
    delete_call = [arguments for operation, arguments in client.calls if operation == "delete"][-1]
    assert delete_call["IfMatch"].startswith('"provider-')
    assert objects.delete(descriptor.object_key, sha256=PNG_SHA256) is False


class _Verifier:
    def verify(self, payload, signature):
        return bool(payload and signature)


class _Authenticator:
    def authenticate(self, bearer_token):
        raise PermissionError("public route only")


def _payload(suffix: str, now: datetime) -> SharePayload:
    media_id = "shm_" + suffix * 32
    turn_id = "turn-" + suffix
    return SharePayload(
        schema_version=2,
        share_id="shr_" + suffix * 32,
        thread_id="thread-" + suffix,
        title="S3 分享 " + suffix,
        source_watermark=2,
        messages=[
            SharedMessage(
                item_id="item-" + suffix,
                turn_id=turn_id,
                role="assistant",
                text="S3 图片",
                created_at=now,
            )
        ],
        artifacts=[
            SharedArtifact(
                artifact_id="artifact-" + suffix,
                revision_id="revision-" + suffix,
                family="image",
                display_name="结果.png",
                mime_type="image/png",
                size_bytes=len(PNG),
                turn_id=turn_id,
                created_at=now,
                preview=SharedMediaRendition(
                    media_id=media_id,
                    kind="preview",
                    mime_type="image/png",
                    size_bytes=len(PNG),
                    sha256=PNG_SHA256,
                ),
            )
        ],
        created_at=now,
        expires_at=now + timedelta(days=1),
    )


def test_s3_store_integrates_cross_snapshot_dedup_http_range_if_range_and_no_secret_persistence(tmp_path) -> None:
    database = tmp_path / "control.sqlite3"
    migrate_control_plane_database(database)
    ring = CloudShareKeyRing(active_key_id="test", keys={"test": b"k" * 32})
    CloudShareSchemaManager(database, keyring=ring).migrate()
    client = FakeS3Client()
    objects = store(client)
    shares = CloudShareRepository(
        database,
        keyring=ring,
        public_base_url="https://share.ecorex.test/s",
        object_store=objects,
        clock=lambda: datetime(2026, 7, 11, tzinfo=timezone.utc),
    )
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    published = []
    for suffix in ("a", "b"):
        payload = _payload(suffix, now)
        preview = payload.artifacts[0].preview
        assert preview is not None
        shares.stage_media(
            "account-1",
            payload.share_id,
            preview.media_id,
            content=PNG,
            kind=preview.kind,
            mime_type=preview.mime_type,
            content_sha256=preview.sha256,
            idempotency_key=payload.share_id + ":" + preview.media_id,
        )
        published.append(
            (payload, shares.publish("account-1", payload, idempotency_key=payload.share_id))
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_objects").fetchone()[0] == 1
        assert connection.execute("SELECT ref_count FROM cloud_share_objects").fetchone()[0] == 2
    app = create_control_plane_app(
        ControlPlaneRepository(database, verifier=_Verifier()),
        authenticator=_Authenticator(),
        share_repository=shares,
    )
    http = TestClient(app)
    payload, projection = published[0]
    preview = payload.artifacts[0].preview
    assert preview is not None
    token = urlsplit(projection.public_url).path.rsplit("/", 1)[-1]
    path = f"/s/{token}/media/{preview.media_id}"
    ranged = http.get(path, headers={"Range": "bytes=1-8"})
    assert ranged.status_code == 206
    assert ranged.content == PNG[1:9]
    etag = ranged.headers["etag"]
    full = http.get(path, headers={"Range": "bytes=1-8", "If-Range": '"stale"'})
    assert full.status_code == 200 and full.content == PNG
    conditional = http.get(path, headers={"If-None-Match": etag})
    assert conditional.status_code == 304

    persisted = database.read_bytes()
    wal = database.with_name(database.name + "-wal")
    if wal.exists():
        persisted += wal.read_bytes()
    assert client.access_key.encode() not in persisted
    assert client.secret_key.encode() not in persisted
    request_dump = repr(client.calls).casefold()
    assert "akia_caller_owned_secret" not in request_dump
    assert "caller-owned-secret" not in request_dump
