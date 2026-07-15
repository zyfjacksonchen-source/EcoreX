"""Durable, account-scoped user-input attachment boundary.

Attachments are intentionally stored as internal source Artifacts.  They are
not office deliverables and can only be reached through an opaque attachment
identity that Runtime later binds into a Turn's immutable metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import mimetypes
from typing import Any, Iterable, Mapping

from ecorex.artifacts import (
    ArtifactRole,
    ArtifactScope,
    ArtifactService,
    ArtifactVisibility,
)
from ecorex.protocol import InputAttachmentProjection

try:  # avoid a runtime import cycle in lightweight Artifact-only tests
    from ecorex.capabilities import CapabilityDeniedError, ToolArgumentsValidationError
except ImportError:  # pragma: no cover
    CapabilityDeniedError = ValueError
    ToolArgumentsValidationError = ValueError


MAX_INPUT_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_INPUT_ATTACHMENTS_PER_TURN = 20
_ALLOWED_MIME_PREFIXES = ("image/", "text/")
_ALLOWED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/json",
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/msword",
    }
)


class InputAttachmentError(ValueError):
    code = "input_attachment_invalid"


class InputAttachmentUnavailable(InputAttachmentError):
    code = "input_attachment_unavailable"


class InputAttachmentConflict(InputAttachmentError):
    code = "input_attachment_conflict"


INPUT_ATTACHMENT_READ_MAX_CHARS = 32 * 1024


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _media_kind(mime_type: str) -> str:
    if mime_type.startswith("image/"):
        return "image"
    if mime_type in _ALLOWED_MIME_TYPES or mime_type.startswith("text/"):
        return "document"
    return "file"


def _normalized_mime(filename: str, declared: str | None) -> str:
    value = str(declared or "").split(";", 1)[0].strip().casefold()
    guessed, _ = mimetypes.guess_type(filename)
    candidate = value if value and value != "application/octet-stream" else guessed
    normalized = str(candidate or "application/octet-stream").casefold()
    if normalized.startswith(_ALLOWED_MIME_PREFIXES) or normalized in _ALLOWED_MIME_TYPES:
        return normalized
    raise InputAttachmentError("this file type is not supported for chat input")


@dataclass(frozen=True, slots=True)
class InputAttachmentService:
    artifacts: ArtifactService
    account_id: str

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise ValueError("input attachment account identity is required")

    def upload(
        self,
        content: bytes,
        *,
        filename: str,
        mime_type: str | None,
        client_request_id: str,
    ) -> InputAttachmentProjection:
        request_id = str(client_request_id or "").strip()
        name = str(filename or "").strip()
        if not request_id or len(request_id) > 256:
            raise InputAttachmentError("attachment request identity is invalid")
        if not name or len(name) > 512:
            raise InputAttachmentError("attachment filename is invalid")
        if not isinstance(content, bytes) or not content:
            raise InputAttachmentError("attachment content is empty")
        if len(content) > MAX_INPUT_ATTACHMENT_BYTES:
            raise InputAttachmentError("attachment exceeds the 64 MiB limit")
        normalized_mime = _normalized_mime(name, mime_type)
        digest = hashlib.sha256(
            _json(
                {
                    "filename": name,
                    "mime_type": normalized_mime,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
            ).encode("utf-8")
        ).hexdigest()
        prepared = self.artifacts.prepare_artifact(
            content,
            requested_name=name,
            mime_type=normalized_mime,
            role=ArtifactRole.SOURCE,
            requested_visibility=ArtifactVisibility.INTERNAL,
            scope=ArtifactScope(
                account_id=self.account_id,
                created_by_tool_id="input_attachment",
            ),
        )
        with self.artifacts.repository.database.transaction() as connection:
            existing = connection.execute(
                "SELECT request_digest, artifact_id, original_name FROM input_attachment_uploads "
                "WHERE client_request_id = ? AND account_id = ?",
                (request_id, self.account_id),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise InputAttachmentConflict(
                        "attachment request identity was reused with different content"
                    )
                projection = self.artifacts.get_internal_artifact(existing["artifact_id"])
                return self._projection(projection, display_name=existing["original_name"])
            projection = self.artifacts.create_artifact_in_transaction(connection, prepared)
            connection.execute(
                "INSERT INTO input_attachment_uploads("
                "client_request_id, account_id, request_digest, original_name, artifact_id, revision_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    self.account_id,
                    digest,
                    name,
                    projection.artifact_id,
                    projection.revision_id,
                    str(projection.created_at),
                ),
            )
        return self._projection(projection, display_name=name)

    def resolve(self, attachment_ids: Iterable[str]) -> tuple[InputAttachmentProjection, ...]:
        ids = tuple(str(value or "").strip() for value in attachment_ids)
        if len(ids) > MAX_INPUT_ATTACHMENTS_PER_TURN or len(set(ids)) != len(ids):
            raise InputAttachmentError("attachment selection is invalid")
        if any(not value or len(value) > 128 for value in ids):
            raise InputAttachmentError("attachment identity is invalid")
        result: list[InputAttachmentProjection] = []
        with self.artifacts.repository.database.reader() as connection:
            for attachment_id in ids:
                row = connection.execute(
                    "SELECT artifact_id, original_name FROM input_attachment_uploads "
                    "WHERE artifact_id = ? AND account_id = ?",
                    (attachment_id, self.account_id),
                ).fetchone()
                if row is None:
                    raise InputAttachmentUnavailable("selected attachment is unavailable")
                projection = self.artifacts.get_internal_artifact(attachment_id)
                scope = self.artifacts.get_artifact_scope(attachment_id)
                if (
                    scope.account_id != self.account_id
                    or scope.created_by_tool_id != "input_attachment"
                ):
                    raise InputAttachmentUnavailable("selected attachment is unavailable")
                result.append(self._projection(projection, display_name=row["original_name"]))
        return tuple(result)

    def read(self, attachment_id: str) -> tuple[InputAttachmentProjection, bytes]:
        projection = self.resolve((attachment_id,))[0]
        return projection, self.artifacts.read_internal_revision_content(projection.revision_id)

    @staticmethod
    def _projection(artifact, *, display_name: str | None = None) -> InputAttachmentProjection:
        return InputAttachmentProjection(
            attachment_id=artifact.artifact_id,
            revision_id=artifact.revision_id,
            display_name=display_name or artifact.display_name,
            mime_type=artifact.mime_type,
            size_bytes=artifact.size_bytes,
            media_kind=_media_kind(artifact.mime_type),
            sha256=artifact.sha256,
            created_at=artifact.created_at,
        )


class InputAttachmentReadRuntime:
    """Read a Turn-bound text attachment without exposing storage paths."""

    def __init__(self, attachments: InputAttachmentService) -> None:
        self.attachments = attachments

    def read(self, arguments: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
        scope = getattr(context, "execution_scope", None)
        if getattr(context, "tool_id", None) != "input_attachment_read" or scope is None:
            raise CapabilityDeniedError("input attachment read requires Runtime execution scope")
        attachment_id = arguments.get("attachment_id")
        offset = arguments.get("offset_chars", 0)
        maximum = arguments.get("max_chars", INPUT_ATTACHMENT_READ_MAX_CHARS)
        if (
            not isinstance(attachment_id, str)
            or not attachment_id
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
            or isinstance(maximum, bool)
            or not isinstance(maximum, int)
            or not 1 <= maximum <= INPUT_ATTACHMENT_READ_MAX_CHARS
        ):
            raise ToolArgumentsValidationError("input attachment read request is invalid")
        with self.attachments.artifacts.repository.database.reader() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM turns WHERE turn_id = ? AND thread_id = ?",
                (scope.turn_id, scope.thread_id),
            ).fetchone()
        try:
            metadata = json.loads(str(row["metadata_json"])) if row is not None else {}
        except (TypeError, ValueError, KeyError):
            metadata = {}
        bound = metadata.get("input_attachments") if isinstance(metadata, dict) else None
        bound_ids = {
            item.get("attachment_id")
            for item in bound
            if isinstance(item, dict) and isinstance(item.get("attachment_id"), str)
        } if isinstance(bound, list) else set()
        if attachment_id not in bound_ids:
            raise CapabilityDeniedError("attachment is not bound to this Turn")
        projection, content = self.attachments.read(attachment_id)
        if projection.media_kind == "image":
            return {
                "schema_version": 1,
                "kind": "image",
                "attachment_id": projection.attachment_id,
                "revision_id": projection.revision_id,
                "mime_type": projection.mime_type,
                "size_bytes": projection.size_bytes,
                "sha256": projection.sha256,
                "content": None,
                "next_offset_chars": 0,
                "eof": True,
            }
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ToolArgumentsValidationError("attachment is not UTF-8 text") from None
        if offset > len(text):
            raise ToolArgumentsValidationError("attachment read offset exceeds content")
        next_offset = min(len(text), offset + maximum)
        return {
            "schema_version": 1,
            "kind": "text",
            "attachment_id": projection.attachment_id,
            "revision_id": projection.revision_id,
            "mime_type": projection.mime_type,
            "size_bytes": projection.size_bytes,
            "sha256": projection.sha256,
            "content": text[offset:next_offset],
            "next_offset_chars": next_offset,
            "eof": next_offset == len(text),
        }


__all__ = [
    "InputAttachmentConflict",
    "InputAttachmentError",
    "InputAttachmentService",
    "InputAttachmentReadRuntime",
    "InputAttachmentUnavailable",
    "MAX_INPUT_ATTACHMENT_BYTES",
]
