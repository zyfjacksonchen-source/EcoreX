from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

import ecorex.control_plane.shares as cloud_share_module
from ecorex.control_plane import (
    CloudShareKeyRing,
    CloudShareRepository,
    CloudShareSchemaManager,
    ControlPlaneRepository,
    ControlPrincipal,
    LocalShareObjectStore,
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


TOKEN = "client-share-token-" + "x" * 32
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class Verifier:
    def verify(self, payload, signature):
        return bool(payload and signature)


class Authenticator:
    def authenticate(self, bearer_token):
        if bearer_token == TOKEN:
            return ControlPrincipal(
                subject="user-1",
                client_id="client-1",
                account_id="account-1",
            )
        if bearer_token == "other-" + "y" * 32:
            return ControlPrincipal(
                subject="user-2",
                client_id="client-2",
                account_id="account-2",
            )
        raise PermissionError("invalid")


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 10, 15, 34, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def make_payload(suffix: str, clock: Clock) -> SharePayload:
    return SharePayload(
        schema_version=2,
        share_id="shr_" + suffix * 32,
        thread_id=f"thread-{suffix}",
        title=f"会话 {suffix}",
        source_watermark=2,
        messages=[
            SharedMessage(
                item_id=f"item-{suffix}",
                turn_id=f"turn-{suffix}",
                role="assistant",
                text="安全内容 <script>alert('x')</script>",
                created_at=clock.value,
            )
        ],
        created_at=clock.value,
        expires_at=clock.value + timedelta(days=1),
    )


def make_media_payload(suffix: str, clock: Clock) -> SharePayload:
    media_id = "shm_" + suffix * 32
    turn_id = f"turn-{suffix}"
    return SharePayload(
        schema_version=2,
        share_id="shr_" + suffix * 32,
        thread_id=f"thread-{suffix}",
        title=f"真实会话 {suffix}",
        source_watermark=4,
        messages=[
            SharedMessage(
                item_id=f"item-user-{suffix}",
                turn_id=turn_id,
                role="user",
                text="请修改这张图 <script>alert('instruction')</script>",
                created_at=clock.value,
            ),
            SharedMessage(
                item_id=f"item-agent-{suffix}",
                turn_id=turn_id,
                role="assistant",
                text="已经修改明暗和构图，请检查主体边缘。",
                created_at=clock.value + timedelta(seconds=2),
            ),
        ],
        artifacts=[
            SharedArtifact(
                artifact_id=f"artifact-{suffix}",
                revision_id=f"revision-{suffix}",
                family="image",
                display_name="精修结果.png",
                mime_type="image/png",
                size_bytes=len(PNG_BYTES),
                turn_id=turn_id,
                created_at=clock.value + timedelta(seconds=2),
                preview=SharedMediaRendition(
                    media_id=media_id,
                    kind="preview",
                    mime_type="image/png",
                    size_bytes=len(PNG_BYTES),
                    sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
                ),
            )
        ],
        created_at=clock.value,
        expires_at=clock.value + timedelta(days=1),
    )


def test_public_share_prioritizes_only_the_first_chat_image() -> None:
    clock = Clock()
    payload = make_media_payload("a", clock)
    first = payload.artifacts[0]
    assert first.preview is not None
    second = first.model_copy(
        update={
            "artifact_id": "artifact-second",
            "revision_id": "revision-second",
            "display_name": "精修结果 2.png",
            "preview": first.preview.model_copy(
                update={"media_id": "shm_" + "b" * 32}
            ),
        }
    )
    rendered = cloud_share_module.render_public_share(
        payload.model_copy(update={"artifacts": [first, second]}),
        public_token="A" * 43,
    ).decode("utf-8")

    assert rendered.count('loading="eager" fetchpriority="high"') == 1
    assert rendered.count('loading="lazy"') == 1


def setup(tmp_path):
    clock = Clock()
    database = tmp_path / "control.db"
    migrate_control_plane_database(database)
    keyring = CloudShareKeyRing(
        active_key_id="test",
        keys={"test": b"k" * 32},
    )
    CloudShareSchemaManager(database, keyring=keyring).migrate()
    shares = CloudShareRepository(
        database,
        keyring=keyring,
        public_base_url="https://share.ecorex.test/s",
        clock=clock,
    )
    app = create_control_plane_app(
        ControlPlaneRepository(database, verifier=Verifier()),
        authenticator=Authenticator(),
        share_repository=shares,
    )
    return TestClient(app), shares, clock, database


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def publish(client, payload):
    return client.post(
        "/api/v1/shares",
        headers={**auth(), "Idempotency-Key": payload.share_id},
        json=payload.model_dump(mode="json"),
    )


def upload_media(client, payload, content=PNG_BYTES, *, token=TOKEN, **header_overrides):
    preview = payload.artifacts[0].preview
    assert preview is not None
    headers = {
        **auth(token),
        "Idempotency-Key": f"{payload.share_id}:{preview.media_id}",
        "Content-Type": preview.mime_type,
        "Content-Length": str(len(content)),
        "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
        "X-Share-Media-Kind": preview.kind,
        **header_overrides,
    }
    return client.put(
        f"/api/v1/shares/{payload.share_id}/media/{preview.media_id}",
        headers=headers,
        content=content,
    )


def test_two_cloud_shares_have_unique_tokens_idempotent_url_and_safe_public_html(tmp_path) -> None:
    client, _shares, _clock, database = setup(tmp_path)
    first_payload = make_payload("a", _clock)
    second_payload = make_payload("b", _clock)
    first = publish(client, first_payload)
    duplicate = publish(client, first_payload)
    second = publish(client, second_payload)
    assert first.status_code == duplicate.status_code == second.status_code == 200
    assert first.json() == duplicate.json()
    assert first.json()["public_url"] != second.json()["public_url"]
    assert first.json()["remote_snapshot_id"] != second.json()["remote_snapshot_id"]

    token = urlsplit(first.json()["public_url"]).path.rsplit("/", 1)[-1]
    raw_database = database.read_bytes()
    wal = database.with_name(database.name + "-wal")
    if wal.exists():
        raw_database += wal.read_bytes()
    assert token.encode("ascii") not in raw_database
    public = client.get(f"/s/{token}")
    assert public.status_code == 200
    assert "安全内容" in public.text
    assert "<script>alert" not in public.text
    assert "&lt;script&gt;" in public.text
    assert public.headers["content-security-policy"].startswith("default-src 'none'")
    assert public.headers["x-robots-tag"] == "noindex, nofollow"


def test_public_share_preserves_role_order_and_safe_markdown_parity(tmp_path) -> None:
    client, _shares, clock, _database = setup(tmp_path)
    turn_id = "turn-markdown"
    payload = SharePayload(
        schema_version=2,
        share_id="shr_" + "c" * 32,
        thread_id="thread-markdown",
        title="Markdown 会话",
        source_watermark=4,
        messages=[
            SharedMessage(
                item_id="item-markdown-user",
                turn_id=turn_id,
                role="user",
                text="请对比 **Q1** 和 **Q2**",
                created_at=clock.value,
            ),
            SharedMessage(
                item_id="item-markdown-agent",
                turn_id=turn_id,
                role="assistant",
                text=(
                    "## 对比结果\n\n"
                    "| 季度 | 收入 |\n| --- | ---: |\n| Q1 | 10 |\n| Q2 | 12 |\n\n"
                    "- 环比增长\n- [查看来源](https://example.com/source)\n\n"
                    "`<script>` 只是代码，<img src=x onerror=alert(1)> 只是文本。"
                ),
                created_at=clock.value + timedelta(seconds=2),
            ),
        ],
        created_at=clock.value,
        expires_at=clock.value + timedelta(days=1),
    )

    created = publish(client, payload)
    assert created.status_code == 200
    token = urlsplit(created.json()["public_url"]).path.rsplit("/", 1)[-1]
    public = client.get(f"/s/{token}")

    assert public.status_code == 200
    assert public.text.index("你的指令") < public.text.index("EcoreX")
    assert "请对比 <strong>Q1</strong> 和 <strong>Q2</strong>" in public.text
    assert "<h2>对比结果</h2>" in public.text
    assert "<table>" in public.text
    assert "<ul><li>环比增长</li>" in public.text
    assert 'href="https://example.com/source"' in public.text
    assert "<code>&lt;script&gt;</code>" in public.text
    assert "<img src=x" not in public.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in public.text
    assert public.headers["content-security-policy"].startswith("default-src 'none'")


def test_public_share_markdown_rendered_size_ceiling_fails_closed() -> None:
    clock = Clock()
    payload = SharePayload(
        share_id="shr_" + "d" * 32,
        thread_id="thread-long-markdown",
        title="Long Markdown",
        source_watermark=4,
        messages=[
            SharedMessage(
                item_id=f"item-long-{index}",
                turn_id=f"turn-long-{index}",
                role="assistant",
                text="<" * 1_000_000,
                created_at=clock.value + timedelta(seconds=index),
            )
            for index in range(4)
        ],
        created_at=clock.value,
        expires_at=clock.value + timedelta(days=1),
    )

    with pytest.raises(cloud_share_module.CloudShareConflict, match="rendered cloud share"):
        cloud_share_module.render_public_share(payload)


def test_cloud_share_conflict_cross_account_revoke_and_expiry_fail_closed(tmp_path) -> None:
    client, _shares, clock, _database = setup(tmp_path)
    payload = make_payload("c", clock)
    created = publish(client, payload)
    assert created.status_code == 200
    remote_id = created.json()["remote_snapshot_id"]
    token = urlsplit(created.json()["public_url"]).path.rsplit("/", 1)[-1]

    changed = payload.model_copy(update={"title": "different"})
    conflict = publish(client, changed)
    assert conflict.status_code == 409

    denied = client.post(
        f"/api/v1/shares/{remote_id}/revoke",
        headers={
            **auth("other-" + "y" * 32),
            "Idempotency-Key": "revoke-other",
        },
    )
    assert denied.status_code == 404
    assert client.get(f"/s/{token}").status_code == 200

    revoked = client.post(
        f"/api/v1/shares/{remote_id}/revoke",
        headers={**auth(), "Idempotency-Key": "revoke-owner"},
    )
    assert revoked.status_code == 204
    assert client.get(f"/s/{token}").status_code == 404

    expiring = make_payload("d", clock)
    expiring_response = publish(client, expiring)
    expiring_token = urlsplit(expiring_response.json()["public_url"]).path.rsplit("/", 1)[-1]
    clock.value += timedelta(days=2)
    assert client.get(f"/s/{expiring_token}").status_code == 404


def test_cloud_share_audit_is_append_only_and_tamper_blocks_future_mutation(tmp_path) -> None:
    client, _shares, clock, database = setup(tmp_path)
    payload = make_payload("e", clock)
    created = publish(client, payload)
    assert created.status_code == 200
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE cloud_share_audit SET action='forged' WHERE sequence=1"
            )
        connection.execute("DROP TRIGGER cloud_share_audit_no_update")
        connection.execute(
            "UPDATE cloud_share_audit SET action='forged' WHERE sequence=1"
        )
    second = publish(client, make_payload("f", clock))
    assert second.status_code == 409


def test_cloud_share_same_source_id_isolated_by_account_and_publish_is_concurrent_idempotent(
    tmp_path,
) -> None:
    _client, shares, clock, database = setup(tmp_path)
    payload = make_payload("7", clock)

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(
            executor.map(
                lambda _index: shares.publish(
                    "account-1", payload, idempotency_key=payload.share_id
                ),
                range(6),
            )
        )
    assert all(result == results[0] for result in results)

    other = shares.publish("account-2", payload, idempotency_key=payload.share_id)
    assert other.remote_snapshot_id != results[0].remote_snapshot_id
    assert other.public_url != results[0].public_url
    assert shares.resolve_public(results[0].public_url.rsplit("/", 1)[-1]) == payload
    assert shares.resolve_public(other.public_url.rsplit("/", 1)[-1]) == payload

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_snapshots").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_audit").fetchone()[0] == 2


def test_cloud_share_snapshot_state_tamper_or_delete_fails_closed(tmp_path) -> None:
    client, _shares, clock, database = setup(tmp_path)
    payload = make_payload("8", clock)
    created = publish(client, payload)
    assert created.status_code == 200
    token = urlsplit(created.json()["public_url"]).path.rsplit("/", 1)[-1]

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM cloud_share_snapshots")
        connection.execute("DROP TRIGGER cloud_share_status_transition")
        connection.execute(
            "UPDATE cloud_share_snapshots SET status='revoked', revoked_at=?",
            (clock.value.isoformat(),),
        )

    assert client.get(f"/s/{token}").status_code == 404
    assert publish(client, make_payload("9", clock)).status_code == 409


def test_expired_cloud_share_cannot_be_republished_under_same_identity(tmp_path) -> None:
    client, _shares, clock, _database = setup(tmp_path)
    payload = make_payload("0", clock)
    created = publish(client, payload)
    assert created.status_code == 200
    token = urlsplit(created.json()["public_url"]).path.rsplit("/", 1)[-1]
    clock.value += timedelta(days=2)
    assert publish(client, payload).status_code == 409
    assert client.get(f"/s/{token}").status_code == 404


def test_control_plane_rejects_oversized_share_before_json_materialization(tmp_path) -> None:
    client, _shares, _clock, database = setup(tmp_path)
    response = client.post(
        "/api/v1/shares",
        headers={
            **auth(),
            "Idempotency-Key": "shr_" + "a" * 32,
            "Content-Type": "application/json",
            "Content-Length": str(8 * 1024 * 1024 + 1),
        },
        content=b"{}",
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "share_payload_too_large"

    def oversized_stream():
        for _index in range(9):
            yield b"x" * (1024 * 1024)

    streamed = client.post(
        "/api/v1/shares",
        headers={
            **auth(),
            "Idempotency-Key": "shr_" + "b" * 32,
            "Content-Type": "application/json",
        },
        content=oversized_stream(),
    )
    assert streamed.status_code == 413
    assert streamed.json()["detail"]["code"] == "share_payload_too_large"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_snapshots").fetchone()[0] == 0


def test_control_plane_validation_never_echoes_share_payload_secrets(tmp_path) -> None:
    client, _shares, clock, _database = setup(tmp_path)
    payload = make_payload("a", clock).model_dump(mode="json")
    payload["artifacts"] = [
        {
            "artifact_id": "art-secret",
            "revision_id": "rev-secret",
            "family": "pdf",
            "display_name": r"C:\\Users\\private\\DO-NOT-ECHO.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1,
        }
    ]
    response = client.post(
        "/api/v1/shares",
        headers={**auth(), "Idempotency-Key": payload["share_id"]},
        json=payload,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_share_snapshot"
    assert "DO-NOT-ECHO" not in response.text


@pytest.mark.parametrize(
    ("mutation", "expected_code", "expected_action"),
    [
        ("missing", "share_image_preview_missing", "wait_for_preview_then_retry"),
        ("oversized", "share_image_preview_too_large", "regenerate_preview_then_retry"),
        ("unsupported", "share_image_preview_unsupported", "regenerate_preview_then_retry"),
    ],
)
def test_control_plane_refuses_v2_images_without_a_publishable_rendition(
    tmp_path,
    mutation: str,
    expected_code: str,
    expected_action: str,
) -> None:
    client, _shares, clock, database = setup(tmp_path)
    payload = make_media_payload("a", clock)
    artifact = payload.artifacts[0]
    preview = artifact.preview
    assert preview is not None
    if mutation == "missing":
        artifact = artifact.model_copy(update={"preview": None})
    elif mutation == "oversized":
        artifact = artifact.model_copy(
            update={
                "preview": preview.model_copy(
                    update={"size_bytes": 16 * 1024 * 1024 + 1}
                )
            }
        )
    else:
        artifact = artifact.model_copy(
            update={
                "preview": preview.model_copy(update={"mime_type": "image/svg+xml"})
            }
        )
    payload = payload.model_copy(
        update={
            "title": "DO-NOT-ECHO-INTERNAL-PATH",
            "artifacts": [artifact],
        }
    )

    response = publish(client, payload)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == expected_code
    assert detail["retryable"] is True
    assert detail["action"] == expected_action
    assert "DO-NOT-ECHO" not in response.text
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cloud_share_snapshots"
        ).fetchone()[0] == 0


def test_control_plane_refuses_more_than_64_mib_of_declared_previews(tmp_path) -> None:
    client, _shares, clock, database = setup(tmp_path)
    payload = make_media_payload("a", clock)
    source = payload.artifacts[0]
    preview = source.preview
    assert preview is not None
    artifacts = []
    for index, suffix in enumerate("abcde"):
        artifacts.append(
            source.model_copy(
                update={
                    "artifact_id": f"artifact-total-{index}",
                    "revision_id": f"revision-total-{index}",
                    "preview": preview.model_copy(
                        update={
                            "media_id": "shm_" + suffix * 32,
                            "size_bytes": 13 * 1024 * 1024,
                        }
                    ),
                }
            )
        )
    payload = payload.model_copy(update={"artifacts": artifacts})

    response = publish(client, payload)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail == {
        "code": "share_media_total_too_large",
        "message": "本次图片预览总量超过分享上限。请减少图片数量或生成较小预览后重试。",
        "retryable": True,
        "action": "reduce_images_then_retry",
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM cloud_share_snapshots"
        ).fetchone()[0] == 0


def test_schema_v1_is_historical_read_only_not_a_new_publish_protocol(tmp_path) -> None:
    client, _shares, clock, _database = setup(tmp_path)
    legacy = make_payload("a", clock).model_copy(update={"schema_version": 1})

    response = publish(client, legacy)

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "share_schema_upgrade_required",
        "message": "这个分享数据版本已停止发布。请从当前会话重新创建分享链接。",
        "retryable": False,
        "action": "recreate_share",
    }
    assert cloud_share_module.render_public_share(legacy).startswith(b"<!doctype html>")


def test_media_share_is_staged_before_publish_and_renders_as_real_chat(tmp_path) -> None:
    client, _shares, clock, _database = setup(tmp_path)
    payload = make_media_payload("a", clock)
    preview = payload.artifacts[0].preview
    assert preview is not None

    # Another account's identically named staging row cannot satisfy this
    # account's snapshot declaration.
    assert upload_media(client, payload, token="other-" + "y" * 32).status_code == 204
    assert publish(client, payload).status_code == 409

    uploaded = upload_media(client, payload)
    duplicate = upload_media(client, payload)
    assert uploaded.status_code == duplicate.status_code == 204

    extra_media_id = "shm_" + "e" * 32
    extra = client.put(
        f"/api/v1/shares/{payload.share_id}/media/{extra_media_id}",
        headers={
            **auth(),
            "Idempotency-Key": f"{payload.share_id}:{extra_media_id}",
            "Content-Type": "image/png",
            "Content-Length": str(len(PNG_BYTES)),
            "X-Content-SHA256": hashlib.sha256(PNG_BYTES).hexdigest(),
            "X-Share-Media-Kind": "thumbnail",
        },
        content=PNG_BYTES,
    )
    assert extra.status_code == 204

    created = publish(client, payload)
    assert created.status_code == 200
    token = urlsplit(created.json()["public_url"]).path.rsplit("/", 1)[-1]
    public = client.get(f"/s/{token}")
    assert public.status_code == 200
    assert public.text.index("你的指令") < public.text.index("EcoreX")
    assert "请修改这张图" in public.text
    assert "已经修改明暗和构图" in public.text
    assert "<script>alert('instruction')</script>" not in public.text
    assert "&lt;script&gt;" in public.text
    media_path = f"/s/{token}/media/{preview.media_id}"
    assert f'src="{media_path}"' in public.text
    assert f'href="{media_path}"' in public.text
    assert 'loading="eager" fetchpriority="high"' in public.text
    assert "object-fit:contain" in public.text
    assert "data:image" not in public.text
    assert "img-src 'self'" in public.headers["content-security-policy"]

    media = client.get(media_path)
    assert media.status_code == 200
    assert media.content == PNG_BYTES
    assert media.headers["content-type"] == "image/png"
    assert media.headers["cache-control"] == "private, no-cache, must-revalidate"
    assert media.headers["accept-ranges"] == "bytes"
    assert media.headers["etag"] == f'"{preview.sha256}"'
    assert media.headers["x-content-type-options"] == "nosniff"
    assert media.headers["cross-origin-resource-policy"] == "same-origin"
    assert client.get(f"/s/{token}/media/{extra_media_id}").status_code == 404

    revoked = client.post(
        f"/api/v1/shares/{created.json()['remote_snapshot_id']}/revoke",
        headers={**auth(), "Idempotency-Key": "revoke-media-share"},
    )
    assert revoked.status_code == 204
    assert client.get(media_path).status_code == 404

    expiring = make_media_payload("f", clock)
    assert upload_media(client, expiring).status_code == 204
    expiring_share = publish(client, expiring)
    expiring_token = urlsplit(expiring_share.json()["public_url"]).path.rsplit("/", 1)[-1]
    expiring_preview = expiring.artifacts[0].preview
    assert expiring_preview is not None
    clock.value += timedelta(days=2)
    assert (
        client.get(f"/s/{expiring_token}/media/{expiring_preview.media_id}").status_code
        == 404
    )


def test_media_identity_kind_mime_digest_and_integrity_fail_closed(tmp_path) -> None:
    client, _shares, clock, database = setup(tmp_path)
    payload = make_media_payload("b", clock)
    preview = payload.artifacts[0].preview
    assert preview is not None
    assert upload_media(client, payload).status_code == 204

    changed = PNG_BYTES + b"different"
    conflict = upload_media(client, payload, content=changed)
    assert conflict.status_code == 409
    kind_conflict = upload_media(
        client,
        payload,
        **{"X-Share-Media-Kind": "thumbnail"},
    )
    assert kind_conflict.status_code == 409

    mismatched_preview = preview.model_copy(update={"sha256": "0" * 64})
    mismatched_artifact = payload.artifacts[0].model_copy(
        update={"preview": mismatched_preview}
    )
    mismatched_payload = payload.model_copy(update={"artifacts": [mismatched_artifact]})
    assert publish(client, mismatched_payload).status_code == 409

    created = publish(client, payload)
    assert created.status_code == 200
    token = urlsplit(created.json()["public_url"]).path.rsplit("/", 1)[-1]
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(cloud_share_media)")
        }
        assert "content" not in columns
        assert "object_key" in columns
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE cloud_share_media SET sha256=?", ("0" * 64,))
        connection.execute("DROP TRIGGER cloud_share_media_no_update")
        connection.execute("UPDATE cloud_share_media SET sha256=?", ("0" * 64,))
    assert client.get(f"/s/{token}").status_code == 404
    assert client.get(f"/s/{token}/media/{preview.media_id}").status_code == 404


def test_media_upload_rejects_svg_oversize_and_missing_contract_headers(tmp_path) -> None:
    client, _shares, clock, _database = setup(tmp_path)
    payload = make_media_payload("c", clock)
    preview = payload.artifacts[0].preview
    assert preview is not None
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    rejected_svg = upload_media(
        client,
        payload,
        content=svg,
        **{"Content-Type": "image/svg+xml"},
    )
    assert rejected_svg.status_code == 409

    missing_kind = client.put(
        f"/api/v1/shares/{payload.share_id}/media/{preview.media_id}",
        headers={
            **auth(),
            "Idempotency-Key": f"{payload.share_id}:{preview.media_id}",
            "Content-Type": "image/png",
            "Content-Length": str(len(PNG_BYTES)),
            "X-Content-SHA256": hashlib.sha256(PNG_BYTES).hexdigest(),
        },
        content=PNG_BYTES,
    )
    assert missing_kind.status_code == 422

    oversized = client.put(
        f"/api/v1/shares/{payload.share_id}/media/{preview.media_id}",
        headers={
            **auth(),
            "Idempotency-Key": f"{payload.share_id}:{preview.media_id}",
            "Content-Type": "image/png",
            "Content-Length": str(16 * 1024 * 1024 + 1),
            "X-Content-SHA256": hashlib.sha256(PNG_BYTES).hexdigest(),
            "X-Share-Media-Kind": "preview",
        },
        content=PNG_BYTES,
    )
    assert oversized.status_code == 413
    assert oversized.json()["detail"]["code"] == "share_media_too_large"


def test_media_upload_backpressure_and_failure_release_its_slot(tmp_path) -> None:
    client, _shares, clock, _database = setup(tmp_path)
    payload = make_media_payload("d", clock)
    assert client.app.state.share_media_slots._value == 4

    class BusySlots:
        def __init__(self) -> None:
            self.releases = 0

        async def acquire(self) -> None:
            await asyncio.sleep(60)

        def release(self) -> None:
            self.releases += 1

    busy = BusySlots()
    client.app.state.share_media_slots = busy
    unavailable = upload_media(client, payload)
    assert unavailable.status_code == 429
    assert unavailable.headers["retry-after"] == "1"
    assert busy.releases == 0

    class CountingSlots:
        def __init__(self) -> None:
            self.acquires = 0
            self.releases = 0

        async def acquire(self) -> None:
            self.acquires += 1

        def release(self) -> None:
            self.releases += 1

    counting = CountingSlots()
    client.app.state.share_media_slots = counting
    rejected = upload_media(
        client,
        payload,
        content=b"not-a-png",
    )
    assert rejected.status_code == 409
    assert counting.acquires == counting.releases == 1


def test_old_orphan_media_is_reclaimed_but_published_media_cannot_be_deleted(
    tmp_path,
) -> None:
    client, _shares, clock, database = setup(tmp_path)
    orphan = make_media_payload("0", clock)
    assert upload_media(client, orphan).status_code == 204

    published = make_media_payload("1", clock)
    assert upload_media(client, published).status_code == 204
    published_extra_id = "shm_" + "e" * 32
    assert client.put(
        f"/api/v1/shares/{published.share_id}/media/{published_extra_id}",
        headers={
            **auth(),
            "Idempotency-Key": f"{published.share_id}:{published_extra_id}",
            "Content-Type": "image/png",
            "Content-Length": str(len(PNG_BYTES)),
            "X-Content-SHA256": hashlib.sha256(PNG_BYTES).hexdigest(),
            "X-Share-Media-Kind": "thumbnail",
        },
        content=PNG_BYTES,
    ).status_code == 204
    assert publish(client, published).status_code == 200
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="published.*immutable"):
            connection.execute(
                "DELETE FROM cloud_share_media WHERE source_share_id=?",
                (published.share_id,),
            )

    clock.value += timedelta(hours=25)
    fresh = make_media_payload("2", clock)
    assert upload_media(client, fresh).status_code == 204
    with sqlite3.connect(database) as connection:
        stored = {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT source_share_id, media_id FROM cloud_share_media"
            ).fetchall()
        }
    assert all(source_id != orphan.share_id for source_id, _media_id in stored)
    published_preview = published.artifacts[0].preview
    fresh_preview = fresh.artifacts[0].preview
    assert published_preview is not None and fresh_preview is not None
    assert (published.share_id, published_preview.media_id) in stored
    assert (published.share_id, published_extra_id) not in stored
    assert (fresh.share_id, fresh_preview.media_id) in stored


def test_fresh_orphan_sources_and_account_bytes_are_bounded(tmp_path, monkeypatch) -> None:
    client, _shares, clock, database = setup(tmp_path / "source-limit")
    for suffix in "01234567":
        assert upload_media(client, make_media_payload(suffix, clock)).status_code == 204
    refused = upload_media(client, make_media_payload("8", clock))
    assert refused.status_code == 409
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(DISTINCT source_share_id) FROM cloud_share_media"
        ).fetchone()[0] == 8

    byte_client, _shares, byte_clock, byte_database = setup(tmp_path / "byte-limit")
    monkeypatch.setattr(
        cloud_share_module,
        "_MAX_ACCOUNT_ORPHAN_BYTES",
        len(PNG_BYTES) * 2,
    )
    assert upload_media(byte_client, make_media_payload("a", byte_clock)).status_code == 204
    assert upload_media(byte_client, make_media_payload("b", byte_clock)).status_code == 204
    refused_bytes = upload_media(byte_client, make_media_payload("c", byte_clock))
    assert refused_bytes.status_code == 409
    with sqlite3.connect(byte_database) as connection:
        assert connection.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM cloud_share_media"
        ).fetchone()[0] == len(PNG_BYTES) * 2


def test_public_media_stream_supports_ranges_etag_and_conditional_reads(tmp_path) -> None:
    client, _shares, clock, _database = setup(tmp_path)
    payload = make_media_payload("7", clock)
    preview = payload.artifacts[0].preview
    assert preview is not None
    assert upload_media(client, payload).status_code == 204
    created = publish(client, payload)
    token = urlsplit(created.json()["public_url"]).path.rsplit("/", 1)[-1]
    path = f"/s/{token}/media/{preview.media_id}"

    ranged = client.get(path, headers={"Range": "bytes=0-7"})
    assert ranged.status_code == 206
    assert ranged.content == PNG_BYTES[:8]
    assert ranged.headers["content-range"] == f"bytes 0-7/{len(PNG_BYTES)}"
    assert ranged.headers["content-length"] == "8"
    etag = ranged.headers["etag"]

    suffix = client.get(path, headers={"Range": "bytes=-5"})
    assert suffix.status_code == 206
    assert suffix.content == PNG_BYTES[-5:]
    assert client.get(path, headers={"Range": "bytes=999-1000"}).status_code == 416
    unchanged = client.get(path, headers={"If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    ignored_range = client.get(
        path,
        headers={"Range": "bytes=0-1", "If-Range": '"different"'},
    )
    assert ignored_range.status_code == 200
    assert ignored_range.content == PNG_BYTES
    exposed = "\n".join(f"{key}:{value}" for key, value in ranged.headers.items())
    assert ".share-objects" not in exposed
    assert "C:\\" not in exposed


def test_public_media_download_capacity_is_bounded_retryable_and_does_not_leak_identity(
    tmp_path,
) -> None:
    client, shares, clock, database = setup(tmp_path)
    payload = make_media_payload("d", clock)
    preview = payload.artifacts[0].preview
    assert preview is not None
    assert upload_media(client, payload).status_code == 204
    created = publish(client, payload)
    token = urlsplit(created.json()["public_url"]).path.rsplit("/", 1)[-1]
    with sqlite3.connect(database) as connection:
        object_key = connection.execute(
            "SELECT object_key FROM cloud_share_media WHERE media_id=?",
            (preview.media_id,),
        ).fetchone()[0]

    held = []
    try:
        for _index in range(32):
            held.append(
                shares.object_store.open(
                    object_key,
                    sha256=preview.sha256,
                    size_bytes=preview.size_bytes,
                    mime_type=preview.mime_type,
                )
            )
        busy = client.get(f"/s/{token}/media/{preview.media_id}")
        assert busy.status_code == 503
        assert busy.headers["retry-after"] == "1"
        assert busy.json()["detail"]["code"] == "share_media_capacity_busy"
        # Authorization and declaration lookup happen before capacity is
        # surfaced; an unknown token remains indistinguishable from a missing
        # resource even while the verified-stream pool is saturated.
        assert client.get(f"/s/{'A' * 43}/media/{preview.media_id}").status_code == 404
    finally:
        for stream in held:
            stream.close()
    recovered = client.get(f"/s/{token}/media/{preview.media_id}")
    assert recovered.status_code == 200
    assert recovered.content == PNG_BYTES


def test_shared_object_deduplicates_and_revoke_or_expiry_releases_safely(tmp_path) -> None:
    client, shares, clock, database = setup(tmp_path)
    first = make_media_payload("3", clock)
    second = make_media_payload("4", clock)
    for payload in (first, second):
        assert upload_media(client, payload).status_code == 204
    first_share = publish(client, first)
    second_share = publish(client, second)
    assert first_share.status_code == second_share.status_code == 200
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_objects").fetchone()[0] == 1
        assert connection.execute("SELECT ref_count FROM cloud_share_objects").fetchone()[0] == 2

    revoked = client.post(
        f"/api/v1/shares/{first_share.json()['remote_snapshot_id']}/revoke",
        headers={**auth(), "Idempotency-Key": "revoke-first-deduplicated"},
    )
    assert revoked.status_code == 204
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT ref_count FROM cloud_share_objects").fetchone()[0] == 1
    second_token = urlsplit(second_share.json()["public_url"]).path.rsplit("/", 1)[-1]
    second_preview = second.artifacts[0].preview
    assert second_preview is not None
    assert client.get(f"/s/{second_token}/media/{second_preview.media_id}").content == PNG_BYTES

    clock.value += timedelta(days=2)
    assert shares.reap_expired_media() == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_objects").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_media").fetchone()[0] == 0


def test_concurrent_staging_has_one_object_and_exact_reference_count(tmp_path) -> None:
    _client, shares, clock, database = setup(tmp_path)
    payloads = [make_media_payload(value, clock) for value in "01234567"]

    def stage(payload):
        preview = payload.artifacts[0].preview
        assert preview is not None
        shares.stage_media(
            "account-1",
            payload.share_id,
            preview.media_id,
            content=PNG_BYTES,
            kind=preview.kind,
            mime_type=preview.mime_type,
            content_sha256=preview.sha256,
            idempotency_key=f"{payload.share_id}:{preview.media_id}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(stage, payloads))
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_objects").fetchone()[0] == 1
        assert connection.execute("SELECT ref_count FROM cloud_share_objects").fetchone()[0] == 8
    clock.value += timedelta(hours=25)
    shares.reap_expired_media()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_objects").fetchone()[0] == 0


def test_object_store_failures_do_not_publish_metadata_and_delete_is_retryable(
    tmp_path,
) -> None:
    class FaultStore:
        def __init__(self, root):
            self.delegate = LocalShareObjectStore(root)
            self.fail_put = True
            self.fail_delete = False

        def put(self, content, *, sha256, mime_type):
            if self.fail_put:
                raise ShareObjectError("injected put failure")
            return self.delegate.put(content, sha256=sha256, mime_type=mime_type)

        def open(self, object_key, *, sha256, size_bytes, mime_type):
            return self.delegate.open(
                object_key,
                sha256=sha256,
                size_bytes=size_bytes,
                mime_type=mime_type,
            )

        def delete(self, object_key, *, sha256):
            if self.fail_delete:
                raise ShareObjectError("injected delete failure")
            return self.delegate.delete(object_key, sha256=sha256)

    clock = Clock()
    database = tmp_path / "control.db"
    migrate_control_plane_database(database)
    keyring = CloudShareKeyRing(active_key_id="test", keys={"test": b"k" * 32})
    CloudShareSchemaManager(database, keyring=keyring).migrate()
    store = FaultStore(tmp_path / "objects")
    shares = CloudShareRepository(
        database,
        keyring=keyring,
        public_base_url="https://share.ecorex.test/s",
        object_store=store,
        clock=clock,
    )
    client = TestClient(
        create_control_plane_app(
            ControlPlaneRepository(database, verifier=Verifier()),
            authenticator=Authenticator(),
            share_repository=shares,
        )
    )
    payload = make_media_payload("9", clock)
    assert upload_media(client, payload).status_code == 409
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_media").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_objects").fetchone()[0] == 0

    store.fail_put = False
    assert upload_media(client, payload).status_code == 204
    created = publish(client, payload)
    store.fail_delete = True
    assert client.post(
        f"/api/v1/shares/{created.json()['remote_snapshot_id']}/revoke",
        headers={**auth(), "Idempotency-Key": "fault-delete-revoke"},
    ).status_code == 204
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT state,ref_count FROM cloud_share_objects"
        ).fetchone() == ("deleting", 0)
    store.fail_delete = False
    shares.reap_expired_media()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_share_objects").fetchone()[0] == 0
