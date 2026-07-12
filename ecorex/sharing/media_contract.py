"""One fail-closed media contract shared by Runtime and Control Plane."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .errors import ShareMediaContractCode, ShareMediaContractError

if TYPE_CHECKING:
    from .models import SharePayload, SharedMediaRendition


SUPPORTED_SHARED_IMAGE_MIME_TYPES = frozenset(
    {"image/avif", "image/gif", "image/jpeg", "image/png", "image/webp"}
)
MAX_SHARED_MEDIA_BYTES = 16 * 1024 * 1024
MAX_SHARED_MEDIA_TOTAL_BYTES = 64 * 1024 * 1024

_MEDIA_ID = re.compile(r"^shm_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_shared_media_rendition(
    media: object,
) -> tuple[str, str, int, str]:
    """Return canonical metadata or a path-free typed contract error."""

    media_id = getattr(media, "media_id", None)
    kind = getattr(media, "kind", None)
    mime_type = getattr(media, "mime_type", None)
    size_bytes = getattr(media, "size_bytes", None)
    sha256 = getattr(media, "sha256", None)
    if not isinstance(mime_type, str):
        raise ShareMediaContractError(
            ShareMediaContractCode.IMAGE_PREVIEW_UNSUPPORTED
        )
    normalized_mime = mime_type.split(";", 1)[0].strip().casefold()
    if normalized_mime not in SUPPORTED_SHARED_IMAGE_MIME_TYPES:
        raise ShareMediaContractError(
            ShareMediaContractCode.IMAGE_PREVIEW_UNSUPPORTED
        )
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes > MAX_SHARED_MEDIA_BYTES
    ):
        raise ShareMediaContractError(
            ShareMediaContractCode.IMAGE_PREVIEW_TOO_LARGE
        )
    if (
        size_bytes < 1
        or not isinstance(media_id, str)
        or _MEDIA_ID.fullmatch(media_id) is None
        or kind not in {"preview", "thumbnail"}
        or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
    ):
        raise ShareMediaContractError(ShareMediaContractCode.IMAGE_PREVIEW_INVALID)
    return kind, normalized_mime, size_bytes, sha256


def shared_media_declarations(
    payload: "SharePayload",
    *,
    require_publishable_schema: bool,
) -> dict[str, tuple[str, str, int, str]]:
    """Validate every v2 image and return its immutable media declarations.

    Schema v1 is accepted only by historical read paths. New Runtime and Cloud
    publication paths set ``require_publishable_schema=True``.
    """

    schema_version = getattr(payload, "schema_version", None)
    if schema_version == 1:
        if require_publishable_schema:
            raise ShareMediaContractError(
                ShareMediaContractCode.SCHEMA_UPGRADE_REQUIRED
            )
        return {}
    if schema_version != 2:
        raise ShareMediaContractError(ShareMediaContractCode.IMAGE_PREVIEW_INVALID)

    declarations: dict[str, tuple[str, str, int, str]] = {}
    total_size = 0
    for artifact in tuple(getattr(payload, "artifacts", ()) or ()):
        family = getattr(artifact, "family", None)
        preview = getattr(artifact, "preview", None)
        if family == "image":
            if preview is None:
                raise ShareMediaContractError(
                    ShareMediaContractCode.IMAGE_PREVIEW_MISSING
                )
        elif preview is not None:
            raise ShareMediaContractError(
                ShareMediaContractCode.IMAGE_PREVIEW_INVALID
            )
        else:
            continue

        declaration = validate_shared_media_rendition(preview)
        media_id = getattr(preview, "media_id")
        prior = declarations.get(media_id)
        if prior is not None and prior != declaration:
            raise ShareMediaContractError(
                ShareMediaContractCode.IMAGE_PREVIEW_INVALID
            )
        if prior is None:
            declarations[media_id] = declaration
            total_size += declaration[2]
            if total_size > MAX_SHARED_MEDIA_TOTAL_BYTES:
                raise ShareMediaContractError(
                    ShareMediaContractCode.MEDIA_TOTAL_TOO_LARGE
                )
    return declarations


__all__ = [
    "MAX_SHARED_MEDIA_BYTES",
    "MAX_SHARED_MEDIA_TOTAL_BYTES",
    "SUPPORTED_SHARED_IMAGE_MIME_TYPES",
    "shared_media_declarations",
    "validate_shared_media_rendition",
]
