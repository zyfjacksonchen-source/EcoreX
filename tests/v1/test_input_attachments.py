from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ecorex.artifacts import ArtifactService
from ecorex.input_attachments import (
    InputAttachmentConflict,
    InputAttachmentService,
    InputAttachmentUnavailable,
)
from ecorex.runtime import RuntimeSettings, create_app


TOKEN = "p" * 32
CSRF = "q" * 32
ORIGIN = "http://testserver"


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
        "size_bytes", "media_kind", "sha256", "created_at",
    }
    assert attachment["display_name"] == "brief.txt"
    assert "path" not in response.text

    content = client.get(
        f"/api/v1/input-attachments/{attachment['attachment_id']}/content",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert content.status_code == 200
    assert content.content == b"confidential brief"
