"""Public ShareSnapshot and private DiagnosticSnapshot contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("snapshot timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_optional(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


class SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ShareStatus(StrEnum):
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    REVOKING = "revoking"
    REVOKED = "revoked"
    EXPIRED = "expired"


class SharedMessage(SnapshotModel):
    item_id: str = Field(min_length=1, max_length=256)
    turn_id: str = Field(min_length=1, max_length=256)
    role: Literal["user", "assistant"]
    text: str = Field(max_length=1_000_000)
    created_at: datetime

    _created_utc = field_validator("created_at")(_utc)


class SharedMediaRendition(SnapshotModel):
    """Immutable public-media descriptor; image bytes remain outside JSON."""

    media_id: str = Field(pattern=r"^shm_[0-9a-f]{32}$")
    kind: Literal["preview", "thumbnail"]
    mime_type: str = Field(min_length=1, max_length=64)
    # Business limits are enforced by the shared Runtime/Control Plane media
    # contract so callers receive one typed, actionable error instead of an
    # input-validation response that can silently lose the image.
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("mime_type")
    @classmethod
    def _supported_mime_type(cls, value: str) -> str:
        # Public media storage and the transport signature bind the exact
        # canonical media type, not optional HTTP parameters.  Normalize at
        # the model boundary so a value such as ``image/png;charset=binary``
        # cannot pass the shared contract and then be rejected later by the
        # Control Plane's strict allowlist.
        normalized = value.split(";", 1)[0].strip().casefold()
        if (
            "/" not in normalized
            or any(ord(character) < 33 or ord(character) == 127 for character in normalized)
        ):
            raise ValueError("shared media MIME type is invalid")
        return normalized


class SharedArtifact(SnapshotModel):
    artifact_id: str = Field(min_length=1, max_length=256)
    revision_id: str = Field(min_length=1, max_length=256)
    family: Literal[
        "document",
        "spreadsheet",
        "presentation",
        "pdf",
        "image",
        "audio",
        "video",
        "data_export",
        "web_report",
        "archive",
        "cloud_link",
    ]
    display_name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    turn_id: str | None = Field(default=None, min_length=1, max_length=256)
    created_at: datetime | None = None
    preview: SharedMediaRendition | None = None

    _created_utc = field_validator("created_at")(_utc_optional)

    @field_validator("display_name")
    @classmethod
    def _safe_display_name(cls, value: str) -> str:
        # Artifact display names cross a public trust boundary.  They must be a
        # basename, not a local path or a value that can be reinterpreted as a
        # platform-specific path by a downstream renderer.
        if (
            value != value.strip(" .")
            or any(character in value for character in '<>:"/\\|?*')
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("shared artifact display name must be a safe basename")
        return value

    @field_validator("mime_type")
    @classmethod
    def _safe_mime_type(cls, value: str) -> str:
        if any(ord(character) < 33 or ord(character) == 127 for character in value):
            raise ValueError("shared artifact MIME type is invalid")
        return value


class SharePayload(SnapshotModel):
    schema_version: Literal[1, 2] = 1
    share_id: str = Field(pattern=r"^shr_[0-9a-f]{32}$")
    thread_id: str = Field(min_length=1, max_length=256)
    title: str | None = Field(default=None, max_length=200)
    source_watermark: int = Field(ge=0)
    messages: list[SharedMessage] = Field(default_factory=list, max_length=20_000)
    artifacts: list[SharedArtifact] = Field(default_factory=list, max_length=2_000)
    created_at: datetime
    expires_at: datetime

    _timestamps_utc = field_validator("created_at", "expires_at")(_utc)

    @model_validator(mode="after")
    def _time_order(self) -> "SharePayload":
        if self.expires_at <= self.created_at:
            raise ValueError("share expiry must be after creation")
        if self.schema_version == 1 and any(
            item.turn_id is not None
            or item.created_at is not None
            or item.preview is not None
            for item in self.artifacts
        ):
            raise ValueError("share schema v1 cannot contain media metadata")
        media: dict[str, SharedMediaRendition] = {}
        for artifact in self.artifacts:
            if artifact.preview is None:
                continue
            if artifact.family != "image":
                raise ValueError("only shared image artifacts may expose a preview")
            previous = media.get(artifact.preview.media_id)
            if previous is not None and previous != artifact.preview:
                raise ValueError("shared media identity has conflicting metadata")
            media[artifact.preview.media_id] = artifact.preview
        return self

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json")
        # Preserve the byte-for-byte schema-v1 contract used by already signed
        # local and cloud snapshots. Pydantic must not add new nullable fields to
        # their canonical representation merely because v2 exists.
        if self.schema_version == 1:
            for artifact in payload["artifacts"]:
                artifact.pop("turn_id", None)
                artifact.pop("created_at", None)
                artifact.pop("preview", None)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    def media_renditions(self) -> tuple[SharedMediaRendition, ...]:
        by_id: dict[str, SharedMediaRendition] = {}
        for artifact in self.artifacts:
            if artifact.preview is not None:
                by_id.setdefault(artifact.preview.media_id, artifact.preview)
        return tuple(by_id[key] for key in sorted(by_id))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class PublishedShare(SnapshotModel):
    remote_snapshot_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
    public_url: str = Field(min_length=1, max_length=4096)

    @field_validator("public_url")
    @classmethod
    def _safe_public_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("public share URL must be credential-free HTTPS")
        return value


class ShareSnapshotProjection(SnapshotModel):
    share_id: str = Field(pattern=r"^shr_[0-9a-f]{32}$")
    thread_id: str = Field(min_length=1, max_length=256)
    source_watermark: int = Field(ge=0)
    status: ShareStatus
    public_url: str | None = None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None
    error_code: str | None = None

    _timestamps_utc = field_validator("expires_at", "created_at", "updated_at")(_utc)
    _revoked_utc = field_validator("revoked_at")(_utc_optional)

    @model_validator(mode="after")
    def _consistent_projection(self) -> "ShareSnapshotProjection":
        if self.status is ShareStatus.PUBLISHED and not self.public_url:
            raise ValueError("published share must expose its public URL")
        if self.status is not ShareStatus.PUBLISHED and self.public_url:
            raise ValueError("inactive share cannot expose a public URL")
        if self.error_code is not None and self.status is not ShareStatus.FAILED:
            raise ValueError("only failed share can expose an error code")
        if self.expires_at <= self.created_at:
            raise ValueError("share expiry must be after creation")
        if self.updated_at < self.created_at:
            raise ValueError("share update time cannot precede creation")
        if self.status is ShareStatus.REVOKED and self.revoked_at is None:
            raise ValueError("revoked share must expose its revocation time")
        if self.status is not ShareStatus.REVOKED and self.revoked_at is not None:
            raise ValueError("only revoked share can expose a revocation time")
        if self.public_url is not None:
            PublishedShare(remote_snapshot_id="projection", public_url=self.public_url)
        return self


class DiagnosticEvent(SnapshotModel):
    seq: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=256)
    turn_id: str | None = Field(default=None, max_length=256)
    item_id: str | None = Field(default=None, max_length=256)
    job_id: str | None = Field(default=None, max_length=256)
    tool_call_id: str | None = Field(default=None, max_length=256)
    trace_id: str | None = Field(default=None, max_length=256)
    created_at: datetime

    _created_utc = field_validator("created_at")(_utc)


class DiagnosticPayload(SnapshotModel):
    schema_version: Literal[1] = 1
    diagnostic_id: str = Field(pattern=r"^diag_[0-9a-f]{32}$")
    thread_id: str = Field(min_length=1, max_length=256)
    source_watermark: int = Field(ge=0)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    events: list[DiagnosticEvent] = Field(default_factory=list, max_length=50_000)
    created_at: datetime

    _created_utc = field_validator("created_at")(_utc)

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class DiagnosticSnapshotProjection(SnapshotModel):
    diagnostic_id: str = Field(pattern=r"^diag_[0-9a-f]{32}$")
    thread_id: str = Field(min_length=1, max_length=256)
    source_watermark: int = Field(ge=0)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")
    created_at: datetime

    _created_utc = field_validator("created_at")(_utc)
