from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ecorex.artifacts import ArtifactService
from ecorex.input_attachments import (
    InputAttachmentConflict,
    InputAttachmentError,
    InputAttachmentService,
    InputAttachmentUnavailable,
)
from ecorex.runtime import RuntimeSettings, create_app


TOKEN = "p" * 32
CSRF = "q" * 32
ORIGIN = "http://testserver"


def _png(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(output, format="PNG")
    return output.getvalue()


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Origin": ORIGIN,
        "X-EcoreX-CSRF": CSRF,
    }


def test_input_attachment_is_idempotent_internal_and_readable_by_owner(tmp_path) -> None:
    service = InputAttachmentService(
        ArtifactService(tmp_path / "artifacts"),
        account_id="account-a",
    )

    first = service.upload(
        b"quarterly notes",
        filename="notes.txt",
        mime_type="text/plain",
        client_request_id="attachment_001",
    )
    replay = service.upload(
        b"quarterly notes",
        filename="notes.txt",
        mime_type="text/plain",
        client_request_id="attachment_001",
    )

    assert replay == first
    internal = service.artifacts.get_internal_artifact(first.attachment_id)
    assert internal.visibility.value == "internal"
    assert internal.role.value == "source"
    assert service.artifacts.list_user_artifacts(account_id="account-a") == ()
    projection, content = service.read(first.attachment_id)
    assert projection == first
    assert content == b"quarterly notes"


def test_input_attachment_rejects_identity_reuse_and_cross_account_access(tmp_path) -> None:
    artifacts = ArtifactService(tmp_path / "artifacts")
    owner = InputAttachmentService(artifacts, account_id="account-a")
    other = InputAttachmentService(artifacts, account_id="account-b")
    uploaded = owner.upload(
        b"{" + b'"ok":true' + b"}",
        filename="brief.json",
        mime_type="application/json",
        client_request_id="attachment_002",
    )

    with pytest.raises(InputAttachmentConflict):
        owner.upload(
            b"different",
            filename="brief.json",
            mime_type="application/json",
            client_request_id="attachment_002",
        )
    with pytest.raises(InputAttachmentUnavailable):
        other.resolve([uploaded.attachment_id])
    with pytest.raises(InputAttachmentUnavailable):
        other.read_thumbnail(uploaded.attachment_id)


def test_turn_attachment_selection_rejects_more_than_four_images(tmp_path) -> None:
    service = InputAttachmentService(
        ArtifactService(tmp_path / "artifacts"),
        account_id="account-a",
    )
    images = [
        service.upload(
            b"image-" + str(index).encode("ascii"),
            filename=f"image-{index}.png",
            mime_type="image/png",
            client_request_id=f"attachment-image-{index}",
        )
        for index in range(5)
    ]

    with pytest.raises(InputAttachmentError, match="at most four image"):
        service.resolve(item.attachment_id for item in images)


def test_runtime_input_attachment_route_is_csrf_fenced_and_opaque(tmp_path) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
        )
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/input-attachments",
        headers=_headers(),
        files={"file": ("brief.txt", b"confidential brief", "text/plain")},
        data={"client_request_id": "attachment_route_001"},
    )

    assert response.status_code == 201
    attachment = response.json()
    assert set(attachment) == {
        "attachment_id", "revision_id", "display_name", "mime_type",
        "size_bytes", "media_kind", "sha256", "thumbnail_url", "created_at",
    }
    assert attachment["display_name"] == "brief.txt"
    assert "path" not in response.text

    content = client.get(
        f"/api/v1/input-attachments/{attachment['attachment_id']}/content",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert content.status_code == 200
    assert content.content == b"confidential brief"


def test_image_thumbnail_is_authenticated_bounded_oriented_and_cas_cached(
    tmp_path, monkeypatch
) -> None:
    from PIL import Image

    source = io.BytesIO()
    image = Image.new("RGB", (1600, 900), (24, 96, 180))
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, format="JPEG", quality=96, exif=exif)
    source_bytes = source.getvalue()
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
        )
    )
    client = TestClient(app)
    uploaded = client.post(
        "/api/v1/input-attachments",
        headers=_headers(),
        files={"file": ("photo.jpg", source_bytes, "image/jpeg")},
        data={"client_request_id": "thumbnail-route-001"},
    )
    assert uploaded.status_code == 201
    projection = uploaded.json()
    assert projection["thumbnail_url"].endswith("/thumbnail")

    source_reads = 0
    original_read = app.state.artifact_service.read_internal_revision_content

    def count_source_read(revision_id: str) -> bytes:
        nonlocal source_reads
        source_reads += 1
        return original_read(revision_id)

    monkeypatch.setattr(
        app.state.artifact_service,
        "read_internal_revision_content",
        count_source_read,
    )

    denied = client.get(projection["thumbnail_url"])
    assert denied.status_code == 401
    first = client.get(
        projection["thumbnail_url"],
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    second = client.get(
        projection["thumbnail_url"],
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert source_reads == 1
    assert first.content != source_bytes
    assert len(first.content) <= 64 * 1024
    assert first.headers["content-type"].startswith("image/jpeg")
    assert first.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert first.headers["etag"] == second.headers["etag"]
    # EXIF orientation swaps the source axes before the 320 px thumbnail fit.
    assert int(first.headers["x-ecorex-image-width"]) <= 320
    assert int(first.headers["x-ecorex-image-height"]) <= 320
    assert int(first.headers["x-ecorex-image-height"]) > int(first.headers["x-ecorex-image-width"])
    digest = first.headers["etag"].strip('"')
    assert app.state.artifact_service.blobs.exists(digest)


def test_thumbnail_rejects_oversized_pixel_canvas_without_returning_source(tmp_path) -> None:
    from PIL import Image

    source = io.BytesIO()
    Image.new("1", (7000, 6000), 1).save(source, format="PNG")
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
        )
    )
    client = TestClient(app)
    uploaded = client.post(
        "/api/v1/input-attachments",
        headers=_headers(),
        files={"file": ("large-canvas.png", source.getvalue(), "image/png")},
        data={"client_request_id": "thumbnail-pixel-limit"},
    ).json()
    response = client.get(
        uploaded["thumbnail_url"],
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 404
    assert source.getvalue() not in response.content


def test_ocr_reads_only_turn_bound_attachment_and_calls_local_provider(
    tmp_path, monkeypatch
) -> None:
    app = create_app(
        settings=RuntimeSettings(
            database_path=tmp_path / "runtime.db",
            runtime_bearer_token=TOKEN,
            csrf_token=CSRF,
            webui_origins=(ORIGIN,),
            installed_capability_packs=frozenset({"ocr"}),
        )
    )
    client = TestClient(app)
    ocr_source = _png((240, 120, 20))
    uploaded = client.post(
        "/api/v1/input-attachments",
        headers=_headers(),
        files={"file": ("ocr.png", ocr_source, "image/png")},
        data={"client_request_id": "ocr-bound-upload"},
    ).json()
    thread = client.post("/api/v1/threads", headers=_headers(), json={}).json()
    turn = client.post(
        f"/api/v1/threads/{thread['thread_id']}/turns",
        headers=_headers(),
        json={
            "input": "识别图片文字",
            "attachment_ids": [uploaded["attachment_id"]],
            "client_message_id": "ocr-bound-message",
        },
    ).json()["turn"]
    calls: list[tuple[bytes, float]] = []

    def fake_ocr(content: bytes, *, timeout_seconds: float):
        calls.append((content, timeout_seconds))
        return {
            "status": "success",
            "provider": "test-provider",
            "text": "EcoreX OCR https://example.com/ready",
            "latencyMs": 7,
            "cacheHit": False,
        }

    monkeypatch.setattr("ecorex.integration.ocr.extract_image_text", fake_ocr)
    runtime = app.state.runtime_composition.input_attachment_ocr_runtime
    context = SimpleNamespace(
        tool_id="ocr",
        execution_scope=SimpleNamespace(
            thread_id=thread["thread_id"], turn_id=turn["turn_id"], job_id="job-test"
        ),
    )
    result = asyncio.run(
        runtime.extract(
            {
                "attachment_ids": [uploaded["attachment_id"]],
                "action": "extract_text",
                "timeout_seconds": 3,
            },
            context,
        )
    )
    assert calls[0][0].startswith(b"\xff\xd8")
    assert calls[0][1] == 3.0
    assert result["images"][0]["text"].startswith("EcoreX OCR")
    assert result["urls"] == ["https://example.com/ready"]

    steer_source = _png((20, 100, 230))
    steered_upload = client.post(
        "/api/v1/input-attachments",
        headers=_headers(),
        files={"file": ("steer.png", steer_source, "image/png")},
        data={"client_request_id": "ocr-steer-upload"},
    ).json()
    steered = client.post(
        f"/api/v1/turns/{turn['turn_id']}/steer",
        headers=_headers(),
        json={
            "input": "再识别这张图",
            "attachment_ids": [steered_upload["attachment_id"]],
            "client_message_id": "ocr-steer-message",
        },
    )
    assert steered.status_code == 202
    steered_result = asyncio.run(
        runtime.extract(
            {
                "attachment_ids": [steered_upload["attachment_id"]],
                "action": "extract_text",
            },
            context,
        )
    )
    assert steered_result["images"][0]["attachment_id"] == steered_upload["attachment_id"]
    assert calls[-1][0].startswith(b"\xff\xd8")

    other_thread = client.post("/api/v1/threads", headers=_headers(), json={}).json()
    context.execution_scope.thread_id = other_thread["thread_id"]
    with pytest.raises(Exception, match="not bound"):
        asyncio.run(
            runtime.extract(
                {"attachment_ids": [uploaded["attachment_id"]], "action": "extract_text"},
                context,
            )
        )
