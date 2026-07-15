"""Loopback-only visual fixture for the immutable public ShareSnapshot page."""

from __future__ import annotations

import atexit
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
import uuid

from fastapi.responses import RedirectResponse

from ecorex.control_plane import (
    CloudShareKeyRing,
    CloudShareRepository,
    CloudShareSchemaManager,
    ControlPlaneRepository,
    create_control_plane_app,
    migrate_control_plane_database,
)
from ecorex.sharing import (
    SharePayload,
    SharedArtifact,
    SharedMediaRendition,
    SharedMessage,
)


_PREVIEW_ASSETS = tuple(
    (Path(__file__).resolve().parents[2] / "deploy/ecorex-site/assets").glob(
        "ecorex-app-preview.*.png"
    )
)
if len(_PREVIEW_ASSETS) != 1:
    raise RuntimeError("share browser fixture requires one content-addressed preview")
PNG_BYTES = _PREVIEW_ASSETS[0].read_bytes()
NOW = datetime.now(timezone.utc)
DATABASE = Path(tempfile.gettempdir()) / f"ecorex-share-browser-{uuid.uuid4().hex}.db"


class _Verifier:
    def verify(self, payload: bytes, signature: str) -> bool:
        return bool(payload and signature)


class _RejectingAuthenticator:
    def authenticate(self, _bearer_token: str):
        raise PermissionError("fixture has no authenticated routes")


def _cleanup() -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            DATABASE.with_name(DATABASE.name + suffix).unlink(missing_ok=True)
        except OSError:
            pass


atexit.register(_cleanup)

migrate_control_plane_database(DATABASE)
share_keyring = CloudShareKeyRing(
    active_key_id="fixture",
    keys={"fixture": b"b" * 32},
)
CloudShareSchemaManager(DATABASE, keyring=share_keyring).migrate()
shares = CloudShareRepository(
    DATABASE,
    keyring=share_keyring,
    public_base_url="https://share.ecorex.test/s",
    clock=lambda: NOW,
)
media = SharedMediaRendition(
    media_id="shm_" + "e" * 32,
    kind="preview",
    mime_type="image/png",
    size_bytes=len(PNG_BYTES),
    sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
)
payload = SharePayload(
    schema_version=2,
    share_id="shr_" + "f" * 32,
    thread_id="thread-browser-share",
    title="季度活动主视觉精修",
    source_watermark=18,
    messages=[
        SharedMessage(
            item_id="item-user-browser",
            turn_id="turn-browser",
            role="user",
            text="请把主视觉调亮，并保留完整构图。",
            created_at=NOW,
        ),
        SharedMessage(
            item_id="item-agent-browser",
            turn_id="turn-browser",
            role="assistant",
            text="已经调整亮度和主体边缘。下面是完整图片，请先看一眼。",
            created_at=NOW + timedelta(seconds=2),
        ),
    ],
    artifacts=[
        SharedArtifact(
            artifact_id="artifact-browser",
            revision_id="revision-browser",
            family="image",
            display_name="活动主视觉_精修.png",
            mime_type="image/png",
            size_bytes=len(PNG_BYTES),
            turn_id="turn-browser",
            created_at=NOW + timedelta(seconds=2),
            preview=media,
        )
    ],
    created_at=NOW,
    expires_at=NOW + timedelta(days=1),
)
shares.stage_media(
    "account-browser",
    payload.share_id,
    media.media_id,
    content=PNG_BYTES,
    kind=media.kind,
    mime_type=media.mime_type,
    content_sha256=media.sha256,
    idempotency_key=f"{payload.share_id}:{media.media_id}",
)
published = shares.publish(
    "account-browser", payload, idempotency_key=payload.share_id
)
PUBLIC_TOKEN = urlsplit(published.public_url).path.rsplit("/", 1)[-1]

app = create_control_plane_app(
    ControlPlaneRepository(DATABASE, verifier=_Verifier()),
    authenticator=_RejectingAuthenticator(),
    share_repository=shares,
)


@app.get("/__ga/share", include_in_schema=False)
def visual_share() -> RedirectResponse:
    return RedirectResponse(f"/s/{PUBLIC_TOKEN}", status_code=307)
