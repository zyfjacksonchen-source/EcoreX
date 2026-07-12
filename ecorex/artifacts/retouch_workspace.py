"""Backend-authoritative precise-retouch draft projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .models import RetouchAnnotation


MAX_RETOUCH_REFERENCES = 10
MAX_RETOUCH_ANNOTATIONS = 100
COORDINATE_SPACE_VERSION = "oriented-normalized-v1"


class RetouchWorkspaceStatus(str, Enum):
    EDITING = "editing"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"


@dataclass(frozen=True, slots=True)
class RetouchEditSurface:
    base_revision_id: str
    raster_digest: str
    width_px: int
    height_px: int
    orientation: int
    color_space: str
    mime_type: str
    coordinate_space_version: str = COORDINATE_SPACE_VERSION

    def __post_init__(self) -> None:
        if not self.base_revision_id.strip():
            raise ValueError("edit surface base_revision_id must not be empty")
        digest = self.raster_digest.casefold()
        if len(digest) != 64 or any(item not in "0123456789abcdef" for item in digest):
            raise ValueError("edit surface raster_digest is invalid")
        object.__setattr__(self, "raster_digest", digest)
        if self.width_px < 1 or self.height_px < 1:
            raise ValueError("edit surface dimensions must be positive")
        if self.orientation not in range(1, 9):
            raise ValueError("edit surface orientation must be between 1 and 8")
        if self.coordinate_space_version != COORDINATE_SPACE_VERSION:
            raise ValueError("unsupported edit surface coordinate space")

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_revision_id": self.base_revision_id,
            "raster_digest": self.raster_digest,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "orientation": self.orientation,
            "color_space": self.color_space,
            "mime_type": self.mime_type,
            "coordinate_space_version": self.coordinate_space_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RetouchEditSurface":
        return cls(
            base_revision_id=str(value["base_revision_id"]),
            raster_digest=str(value["raster_digest"]),
            width_px=int(value["width_px"]),
            height_px=int(value["height_px"]),
            orientation=int(value["orientation"]),
            color_space=str(value["color_space"]),
            mime_type=str(value["mime_type"]),
            coordinate_space_version=str(value["coordinate_space_version"]),
        )


@dataclass(frozen=True, slots=True)
class RetouchReference:
    artifact_id: str
    revision_id: str
    display_name: str
    mime_type: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "display_name": self.display_name,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RetouchReference":
        return cls(
            artifact_id=str(value["artifact_id"]),
            revision_id=str(value["revision_id"]),
            display_name=str(value["display_name"]),
            mime_type=str(value["mime_type"]),
            sha256=str(value["sha256"]),
        )


@dataclass(frozen=True, slots=True)
class RetouchWorkspaceProjection:
    workspace_id: str
    artifact_id: str
    version: int
    status: RetouchWorkspaceStatus
    edit_surface: RetouchEditSurface
    annotations: tuple[RetouchAnnotation, ...] = ()
    references: tuple[RetouchReference, ...] = ()
    global_instruction: str = ""
    view_state: Mapping[str, Any] = field(default_factory=dict)
    mask: Mapping[str, Any] | None = None
    submitted_job_id: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", RetouchWorkspaceStatus(self.status))
        object.__setattr__(self, "annotations", tuple(self.annotations))
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "view_state", dict(self.view_state))
        if self.mask is not None:
            object.__setattr__(self, "mask", dict(self.mask))
        if self.version < 1:
            raise ValueError("retouch workspace version must be positive")
        if len(self.annotations) > MAX_RETOUCH_ANNOTATIONS:
            raise ValueError("retouch workspace has too many annotations")
        if len(self.references) > MAX_RETOUCH_REFERENCES:
            raise ValueError("retouch workspace has too many references")

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "artifact_id": self.artifact_id,
            "version": self.version,
            "status": self.status.value,
            "edit_surface": self.edit_surface.to_dict(),
            "annotations": [item.to_dict() for item in self.annotations],
            "references": [item.to_dict() for item in self.references],
            "global_instruction": self.global_instruction,
            "view_state": dict(self.view_state),
            "mask": dict(self.mask) if self.mask is not None else None,
            "submitted_job_id": self.submitted_job_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


__all__ = [
    "COORDINATE_SPACE_VERSION",
    "MAX_RETOUCH_ANNOTATIONS",
    "MAX_RETOUCH_REFERENCES",
    "RetouchEditSurface",
    "RetouchReference",
    "RetouchWorkspaceProjection",
    "RetouchWorkspaceStatus",
]
