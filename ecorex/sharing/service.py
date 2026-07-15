"""Backend-authoritative construction and publication of safe snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Callable, Protocol
from urllib.parse import urlsplit
import uuid

from ecorex.artifacts import ArtifactService, sanitize_display_filename
from ecorex.protocol import ItemKind
from ecorex.runtime import RuntimeKernel

from .errors import ShareConflict, ShareMediaContractCode, ShareMediaContractError
from .media_contract import (
    MAX_SHARED_MEDIA_BYTES,
    SUPPORTED_SHARED_IMAGE_MIME_TYPES,
    shared_media_declarations,
)
from .models import (
    DiagnosticEvent,
    DiagnosticPayload,
    DiagnosticSnapshotProjection,
    PublishedShare,
    SharePayload,
    ShareSnapshotProjection,
    ShareStatus,
    SharedArtifact,
    SharedMediaRendition,
    SharedMessage,
)
from .repository import ShareRepository


class SharePublisher(Protocol):
    async def upload_media(
        self,
        share_id: str,
        media: SharedMediaRendition,
        content: bytes,
        *,
        idempotency_key: str,
    ) -> None:
        ...

    async def publish(
        self,
        payload: SharePayload,
        *,
        idempotency_key: str,
    ) -> PublishedShare:
        ...

    async def revoke(
        self,
        remote_snapshot_id: str,
        *,
        idempotency_key: str,
    ) -> None:
        ...


Clock = Callable[[], datetime]

_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ShareSnapshotService:
    def __init__(
        self,
        kernel: RuntimeKernel,
        *,
        repository: ShareRepository,
        publisher: SharePublisher,
        account_id: str,
        allowed_public_hosts: frozenset[str],
        artifacts: ArtifactService | None = None,
        clock: Clock = _utcnow,
        max_attempts: int = 3,
        operation_deadline_seconds: int = 3600,
        notify: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
            raise ValueError("share service account identity is required")
        normalized_hosts = frozenset(
            host.strip().casefold() for host in allowed_public_hosts if host.strip()
        )
        if not normalized_hosts:
            raise ValueError("share service requires an explicit public host allowlist")
        if any(
            not _HOST.fullmatch(host)
            or ".." in host
            or host.startswith(".")
            or host.endswith(".")
            for host in normalized_hosts
        ):
            raise ValueError("share service public host allowlist is invalid")
        if repository.database.path.resolve() != kernel.database.path.resolve():
            raise ValueError("share service requires one Runtime database")
        if not 1 <= max_attempts <= 10:
            raise ValueError("share operation max_attempts is invalid")
        if not 30 <= operation_deadline_seconds <= 86_400:
            raise ValueError("share operation deadline is invalid")
        self.kernel = kernel
        self.repository = repository
        self.publisher = publisher
        self.account_id = account_id
        self.allowed_public_hosts = normalized_hosts
        self.artifacts = artifacts
        self.clock = clock
        self.max_attempts = max_attempts
        self.operation_deadline_seconds = operation_deadline_seconds
        self.notify = notify or (lambda: None)

    async def create(
        self,
        thread_id: str,
        *,
        expires_in_hours: int,
        client_request_id: str,
    ) -> ShareSnapshotProjection:
        if (
            isinstance(expires_in_hours, bool)
            or not isinstance(expires_in_hours, int)
            or not 1 <= expires_in_hours <= 24 * 30
        ):
            raise ValueError("share expiry must be between one hour and 30 days")
        if not isinstance(client_request_id, str) or not _REQUEST_ID.fullmatch(
            client_request_id
        ):
            raise ValueError("share client_request_id is invalid")
        now = self.clock()
        projection = self.kernel.projection(thread_id)
        payload = self._payload(
            projection,
            share_id="shr_" + uuid.uuid4().hex,
            created_at=now,
            expires_at=now + timedelta(hours=expires_in_hours),
        )
        state, _durable_payload = self.repository.begin_create(
            account_id=self.account_id,
            client_request_id=client_request_id,
            payload=payload,
            now=now,
            max_attempts=self.max_attempts,
            deadline_seconds=self.operation_deadline_seconds,
        )
        # The immutable snapshot and exactly one Durable Job are already
        # committed.  HTTP never performs the cloud side effect directly.
        if state.status is ShareStatus.PUBLISHING:
            self.notify()
        self._validate_public_projection(state)
        return state

    async def revoke(
        self,
        share_id: str,
        *,
        client_request_id: str,
    ) -> ShareSnapshotProjection:
        if not isinstance(client_request_id, str) or not _REQUEST_ID.fullmatch(
            client_request_id
        ):
            raise ValueError("share client_request_id is invalid")
        now = self.clock()
        state, remote_snapshot_id = self.repository.begin_revoke(
            share_id,
            account_id=self.account_id,
            client_request_id=client_request_id,
            now=now,
            max_attempts=self.max_attempts,
            deadline_seconds=self.operation_deadline_seconds,
        )
        del remote_snapshot_id
        if state.status is ShareStatus.REVOKING:
            self.notify()
        self._validate_public_projection(state)
        return state

    def get(self, share_id: str) -> ShareSnapshotProjection:
        result = self.repository.get(
            share_id, account_id=self.account_id, now=self.clock()
        )
        self._validate_public_projection(result)
        return result

    def list(
        self, thread_id: str, *, limit: int = 100
    ) -> tuple[list[ShareSnapshotProjection], int]:
        # Resolve through the authoritative Runtime first so a caller cannot use
        # the share catalog as a separate Thread-existence oracle.
        self.kernel.get_thread(thread_id)
        items, count = self.repository.list_for_thread(
            thread_id,
            account_id=self.account_id,
            now=self.clock(),
            limit=limit,
        )
        for item in items:
            self._validate_public_projection(item)
        return items, count

    def _validate_public_projection(self, projection: ShareSnapshotProjection) -> None:
        if projection.public_url is None:
            return
        host = (urlsplit(projection.public_url).hostname or "").casefold()
        if host not in self.allowed_public_hosts:
            raise ShareConflict("share public host is not allowlisted")

    def _payload(
        self,
        projection,
        *,
        share_id: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> SharePayload:
        messages: list[SharedMessage] = []
        for item in projection.items:
            if item.kind is not ItemKind.MESSAGE:
                continue
            role = item.content.get("role")
            text = item.content.get("text")
            if role not in {"user", "assistant"} or not isinstance(text, str):
                continue
            messages.append(
                SharedMessage(
                    item_id=item.item_id,
                    turn_id=item.turn_id,
                    role=role,
                    text=text,
                    created_at=item.created_at,
                )
            )
        artifacts: list[SharedArtifact] = []
        if self.artifacts is not None:
            for artifact in self.artifacts.list_user_artifacts(
                account_id=self.account_id, thread_id=projection.thread.thread_id
            ):
                if artifact.visibility.value not in {"primary", "secondary"}:
                    continue
                if artifact.family.value in {
                    "source_code", "script", "diff", "log", "temporary", "directory"
                }:
                    continue
                scope = None
                get_scope = getattr(self.artifacts, "get_artifact_scope", None)
                if callable(get_scope):
                    scope = get_scope(artifact.artifact_id)
                    if scope.account_id != self.account_id or scope.thread_id != projection.thread.thread_id:
                        raise ShareConflict("shared artifact ownership does not match the thread")
                artifact_created_at = self._artifact_created_at(
                    getattr(artifact, "created_at", None)
                )
                preview = self._media_preview(artifact)
                artifacts.append(
                    SharedArtifact(
                        artifact_id=artifact.artifact_id,
                        revision_id=artifact.revision_id,
                        family=artifact.family.value,
                        display_name=sanitize_display_filename(artifact.display_name),
                        mime_type=artifact.mime_type,
                        size_bytes=artifact.size_bytes,
                        turn_id=(None if scope is None else scope.turn_id),
                        created_at=artifact_created_at,
                        preview=preview,
                    )
                )
        payload = SharePayload(
            schema_version=2,
            share_id=share_id,
            thread_id=projection.thread.thread_id,
            title=projection.thread.title,
            source_watermark=projection.watermark,
            messages=messages,
            artifacts=artifacts,
            created_at=created_at,
            expires_at=expires_at,
        )
        # This is the first authority boundary: an immutable schema-v2 image
        # snapshot is never created unless every visible image has a bounded
        # raster rendition. The Control Plane repeats the same check.
        shared_media_declarations(payload, require_publishable_schema=True)
        if len(payload.canonical_bytes()) > 8 * 1024 * 1024:
            raise ValueError("share snapshot exceeds its size limit")
        return payload

    @staticmethod
    def _artifact_created_at(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ShareConflict("shared artifact timestamp is invalid") from None
        else:
            raise ShareConflict("shared artifact timestamp is invalid")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ShareConflict("shared artifact timestamp is invalid")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _media_preview(artifact: object) -> SharedMediaRendition | None:
        if getattr(getattr(artifact, "family", None), "value", None) != "image":
            return None
        candidates: list[tuple[str, str, int, str]] = []
        renditions = tuple(getattr(artifact, "renditions", ()) or ())
        for desired in ("preview", "thumbnail"):
            for rendition in renditions:
                kind = getattr(getattr(rendition, "kind", None), "value", None)
                if kind == desired:
                    candidates.append(
                        (
                            desired,
                            str(getattr(rendition, "mime_type", "")),
                            getattr(rendition, "size_bytes", 0),
                            str(getattr(rendition, "sha256", "")),
                        )
                    )
        if not candidates:
            # The primary source blob is deliberately not promoted into a
            # public rendition. Producing or resizing previews belongs to the
            # local Artifact pipeline, never the Control Plane publisher.
            raise ShareMediaContractError(
                ShareMediaContractCode.IMAGE_PREVIEW_MISSING
            )
        saw_unsupported = False
        saw_oversized = False
        for kind, mime_type, size_bytes, digest in candidates:
            mime_type = mime_type.split(";", 1)[0].strip().casefold()
            digest = digest.strip().casefold()
            if mime_type not in SUPPORTED_SHARED_IMAGE_MIME_TYPES:
                saw_unsupported = True
                continue
            if (
                isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or size_bytes > MAX_SHARED_MEDIA_BYTES
            ):
                saw_oversized = True
                continue
            if (
                size_bytes < 1
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                continue
            identity = hashlib.sha256(
                f"ecorex-share-media-v1\n{kind}\0{mime_type}\0{size_bytes}\0{digest}".encode(
                    "ascii"
                )
            ).hexdigest()[:32]
            return SharedMediaRendition(
                media_id="shm_" + identity,
                kind=kind,
                mime_type=mime_type,
                size_bytes=size_bytes,
                sha256=digest,
            )
        if saw_oversized:
            raise ShareMediaContractError(
                ShareMediaContractCode.IMAGE_PREVIEW_TOO_LARGE
            )
        if saw_unsupported:
            raise ShareMediaContractError(
                ShareMediaContractCode.IMAGE_PREVIEW_UNSUPPORTED
            )
        raise ShareMediaContractError(ShareMediaContractCode.IMAGE_PREVIEW_INVALID)


class DiagnosticSnapshotService:
    """Creates private metadata-only snapshots; never returns a public URL."""

    def __init__(
        self,
        kernel: RuntimeKernel,
        *,
        repository: ShareRepository,
        account_id: str,
        clock: Clock = _utcnow,
    ) -> None:
        if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
            raise ValueError("diagnostic service account identity is required")
        self.kernel = kernel
        self.repository = repository
        self.account_id = account_id
        self.clock = clock

    def create(
        self,
        thread_id: str,
        *,
        reason_code: str,
        client_request_id: str,
    ) -> DiagnosticSnapshotProjection:
        if not isinstance(client_request_id, str) or not _REQUEST_ID.fullmatch(
            client_request_id
        ):
            raise ValueError("diagnostic client_request_id is invalid")
        projection = self.kernel.projection(thread_id)
        page = self.kernel.events.page(
            thread_id, after_seq=0, limit=max(1, min(projection.watermark, 50_000))
        )
        payload = DiagnosticPayload(
            diagnostic_id="diag_" + uuid.uuid4().hex,
            thread_id=thread_id,
            source_watermark=projection.watermark,
            reason_code=reason_code,
            events=[
                DiagnosticEvent(
                    seq=event.seq,
                    event_type=event.event_type,
                    turn_id=event.turn_id,
                    item_id=event.item_id,
                    job_id=event.job_id,
                    tool_call_id=event.tool_call_id,
                    trace_id=event.trace_id,
                    created_at=event.created_at,
                )
                for event in page.events
            ],
            created_at=self.clock(),
        )
        if page.has_more:
            raise ValueError("diagnostic snapshot event limit was exceeded")
        return self.repository.create_diagnostic(
            account_id=self.account_id,
            client_request_id=client_request_id,
            payload=payload,
        )
