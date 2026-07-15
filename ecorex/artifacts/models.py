"""Public contracts for the EcoreX office artifact domain.

The objects in this module are deliberately transport agnostic.  The ASGI
layer may serialize them, but classification and visibility decisions are
already final by the time an :class:`ArtifactProjection` is produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Mapping, Sequence


class ArtifactFamily(str, Enum):
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    PDF = "pdf"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DATA_EXPORT = "data_export"
    WEB_REPORT = "web_report"
    ARCHIVE = "archive"
    CLOUD_LINK = "cloud_link"
    SOURCE_CODE = "source_code"
    SCRIPT = "script"
    DIFF = "diff"
    LOG = "log"
    TEMPORARY = "temporary"
    DIRECTORY = "directory"


class ArtifactRole(str, Enum):
    DELIVERABLE = "deliverable"
    RENDITION = "rendition"
    SOURCE = "source"
    INTERMEDIATE = "intermediate"
    DIAGNOSTIC = "diagnostic"


class ArtifactVisibility(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    INTERNAL = "internal"


class ArtifactStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class ArtifactAction(str, Enum):
    PREVIEW = "preview"
    OPEN = "open"
    DOWNLOAD = "download"
    REVEAL = "reveal"
    FEEDBACK = "feedback"
    PRECISE_RETOUCH = "precise_retouch"


class ArtifactExternalActionStatus(str, Enum):
    PREPARED = "prepared"
    LAUNCHING = "launching"
    COMPLETED = "completed"
    FAILED = "failed"


class RenditionKind(str, Enum):
    PREVIEW = "preview"
    THUMBNAIL = "thumbnail"


class FeedbackSignal(str, Enum):
    THUMBS_UP = "thumbs_up"
    THUMBS_DOWN = "thumbs_down"


class QualityStatus(str, Enum):
    NOT_CHECKED = "not_checked"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"


class RetouchJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ArtifactScope:
    """Backend ownership metadata; never inferred by the WebUI."""

    account_id: str = "local-user"
    thread_id: str | None = None
    turn_id: str | None = None
    created_by_tool_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _require_non_empty(self.account_id, "account_id"))
        for name in ("thread_id", "turn_id", "created_by_tool_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _require_non_empty(value, name))
        if self.turn_id is not None and self.thread_id is None:
            raise ValueError("artifact turn_id requires thread_id")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "account_id": self.account_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "created_by_tool_id": self.created_by_tool_id,
        }


def _require_non_empty(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _json_value(value: Any, field_name: str) -> Any:
    """Return a detached JSON-compatible value or raise a precise error."""

    return _json_value_at_depth(value, field_name, 0)


def _json_value_at_depth(value: Any, field_name: str, depth: int) -> Any:
    if depth > 16:
        raise ValueError(f"{field_name} exceeds the maximum nesting depth")

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if len(value) > 4096:
            raise ValueError(f"{field_name} contains too many fields")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} keys must be strings")
            result[key] = _json_value_at_depth(item, field_name, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 4096:
            raise ValueError(f"{field_name} contains too many items")
        return [_json_value_at_depth(item, field_name, depth + 1) for item in value]
    raise ValueError(f"{field_name} must contain only JSON-compatible values")


def _normalized_geometry(value: Mapping[str, Any]) -> dict[str, Any]:
    geometry = _json_value(value, "normalized_geometry")
    if not isinstance(geometry, dict) or not geometry:
        raise ValueError("normalized_geometry must be a non-empty object")

    coordinate_count = 0

    def validate(item: Any, depth: int = 0) -> None:
        nonlocal coordinate_count
        if depth > 8:
            raise ValueError("normalized_geometry exceeds the maximum nesting depth")
        if isinstance(item, bool):
            raise ValueError("normalized_geometry must not contain booleans")
        if isinstance(item, (int, float)):
            coordinate_count += 1
            if coordinate_count > 1024:
                raise ValueError("normalized_geometry contains too many coordinates")
            if not 0.0 <= float(item) <= 1.0:
                raise ValueError("normalized_geometry coordinates must be between 0 and 1")
            return
        if isinstance(item, dict):
            for nested in item.values():
                validate(nested, depth + 1)
            return
        if isinstance(item, list):
            for nested in item:
                validate(nested, depth + 1)
            return
        raise ValueError("normalized_geometry may contain only coordinates, lists, and objects")

    validate(geometry)
    if coordinate_count == 0:
        raise ValueError("normalized_geometry must contain at least one coordinate")
    return geometry


_ANNOTATION_KINDS = frozenset({"rectangle", "ellipse", "point", "polygon", "polyline", "brush"})
_RETOUCH_COORDINATE_SPACE = "oriented-normalized-v1"


def _retouch_edit_surface_metadata(
    value: Mapping[str, Any] | None,
    *,
    base_revision_id: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    metadata = dict(value)
    expected = {
        "base_revision_id",
        "raster_digest",
        "width_px",
        "height_px",
        "orientation",
        "color_space",
        "mime_type",
        "coordinate_space_version",
    }
    if set(metadata) != expected:
        raise ValueError("retouch edit_surface contains unsupported fields")
    if metadata["base_revision_id"] != base_revision_id:
        raise ValueError("retouch edit_surface base revision does not match request")
    digest = str(metadata["raster_digest"] or "").casefold()
    if len(digest) != 64 or any(item not in "0123456789abcdef" for item in digest):
        raise ValueError("retouch edit_surface raster_digest is invalid")
    for key in ("width_px", "height_px"):
        if isinstance(metadata[key], bool) or not isinstance(metadata[key], int) or not 1 <= metadata[key] <= 100_000:
            raise ValueError(f"retouch edit_surface {key} is invalid")
    if isinstance(metadata["orientation"], bool) or metadata["orientation"] not in range(1, 9):
        raise ValueError("retouch edit_surface orientation is invalid")
    if metadata["coordinate_space_version"] != _RETOUCH_COORDINATE_SPACE:
        raise ValueError("retouch edit_surface coordinate space is unsupported")
    metadata["raster_digest"] = digest
    metadata["color_space"] = _require_non_empty(
        str(metadata["color_space"]), "retouch edit_surface color_space"
    )
    metadata["mime_type"] = _require_non_empty(
        str(metadata["mime_type"]), "retouch edit_surface mime_type"
    )
    return metadata


def _retouch_mask_metadata(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    metadata = dict(value)
    expected = {
        "schema_version",
        "coordinate_space_version",
        "width_px",
        "height_px",
        "sha256",
        "size_bytes",
        "covered_fraction",
        "pixel_regions",
    }
    if set(metadata) != expected:
        raise ValueError("retouch mask contains unsupported fields")
    if metadata["schema_version"] != 1 or metadata["coordinate_space_version"] != _RETOUCH_COORDINATE_SPACE:
        raise ValueError("retouch mask schema or coordinate space is unsupported")
    digest = str(metadata["sha256"] or "").casefold()
    if len(digest) != 64 or any(item not in "0123456789abcdef" for item in digest):
        raise ValueError("retouch mask sha256 is invalid")
    metadata["sha256"] = digest
    for key in ("width_px", "height_px", "size_bytes"):
        if isinstance(metadata[key], bool) or not isinstance(metadata[key], int) or metadata[key] < 1:
            raise ValueError(f"retouch mask {key} is invalid")
    coverage = metadata["covered_fraction"]
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not 0 <= float(coverage) <= 1:
        raise ValueError("retouch mask covered_fraction is invalid")
    regions = metadata["pixel_regions"]
    if not isinstance(regions, list) or len(regions) > 100:
        raise ValueError("retouch mask pixel_regions is invalid")
    normalized_regions: list[dict[str, int]] = []
    for region in regions:
        if not isinstance(region, dict) or set(region) != {"x", "y", "width", "height"}:
            raise ValueError("retouch mask pixel region has an invalid shape")
        if any(isinstance(region[key], bool) or not isinstance(region[key], int) for key in region):
            raise ValueError("retouch mask pixel region values must be integers")
        if region["x"] < 0 or region["y"] < 0 or region["width"] < 1 or region["height"] < 1:
            raise ValueError("retouch mask pixel region is out of bounds")
        if region["x"] + region["width"] > metadata["width_px"] or region["y"] + region["height"] > metadata["height_px"]:
            raise ValueError("retouch mask pixel region exceeds the mask")
        normalized_regions.append(dict(region))
    metadata["covered_fraction"] = float(coverage)
    metadata["pixel_regions"] = normalized_regions
    return metadata


def _require_point(value: Any, field_name: str) -> None:
    if not isinstance(value, dict) or set(value) != {"x", "y"}:
        raise ValueError(f"{field_name} must contain exactly x and y")
    if not all(isinstance(value[key], (int, float)) and not isinstance(value[key], bool) for key in ("x", "y")):
        raise ValueError(f"{field_name} coordinates must be numbers")


def _validate_annotation_geometry(kind: str, geometry: Mapping[str, Any]) -> None:
    keys = set(geometry)
    if kind in {"rectangle", "ellipse"}:
        required = {"x", "y", "width", "height"}
        if keys != required:
            raise ValueError(f"{kind} geometry must contain exactly x, y, width, and height")
        if not all(
            isinstance(geometry[key], (int, float)) and not isinstance(geometry[key], bool)
            for key in required
        ):
            raise ValueError(f"{kind} geometry values must be numbers")
        if float(geometry["width"]) <= 0 or float(geometry["height"]) <= 0:
            raise ValueError(f"{kind} width and height must be positive")
        if float(geometry["x"]) + float(geometry["width"]) > 1:
            raise ValueError(f"{kind} exceeds the normalized horizontal bounds")
        if float(geometry["y"]) + float(geometry["height"]) > 1:
            raise ValueError(f"{kind} exceeds the normalized vertical bounds")
        return
    if kind == "point":
        _require_point(dict(geometry), "point geometry")
        return
    if kind in {"polygon", "polyline", "brush"}:
        allowed = {"points"} if kind != "brush" else {"points", "width"}
        if not keys <= allowed or "points" not in geometry:
            raise ValueError(f"{kind} geometry contains unsupported fields")
        points = geometry["points"]
        minimum = 3 if kind == "polygon" else 2
        if not isinstance(points, list) or not minimum <= len(points) <= 512:
            raise ValueError(f"{kind} geometry requires between {minimum} and 512 points")
        for index, point in enumerate(points):
            _require_point(point, f"{kind} point {index}")
        if kind == "brush" and "width" in geometry:
            if not isinstance(geometry["width"], (int, float)) or isinstance(geometry["width"], bool):
                raise ValueError("brush width must be a number")
            if float(geometry["width"]) <= 0:
                raise ValueError("brush width must be positive")
        return
    raise ValueError(f"unsupported annotation kind {kind!r}")


@dataclass(frozen=True, slots=True)
class ArtifactLineage:
    source_artifact_ids: tuple[str, ...] = ()
    supersedes_revision_id: str | None = None

    def __post_init__(self) -> None:
        sources = tuple(dict.fromkeys(_require_non_empty(item, "source_artifact_id") for item in self.source_artifact_ids))
        object.__setattr__(self, "source_artifact_ids", sources)
        if self.supersedes_revision_id is not None:
            object.__setattr__(
                self,
                "supersedes_revision_id",
                _require_non_empty(self.supersedes_revision_id, "supersedes_revision_id"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_artifact_ids": list(self.source_artifact_ids),
            "supersedes_revision_id": self.supersedes_revision_id,
        }


@dataclass(frozen=True, slots=True)
class QualityCheck:
    name: str
    status: QualityStatus
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_non_empty(self.name, "quality check name"))
        object.__setattr__(self, "status", QualityStatus(self.status))
        if self.detail is not None:
            object.__setattr__(self, "detail", str(self.detail).strip() or None)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class QualityEvidence:
    status: QualityStatus = QualityStatus.NOT_CHECKED
    checks: tuple[QualityCheck, ...] = ()
    score: float | None = None
    summary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", QualityStatus(self.status))
        object.__setattr__(self, "checks", tuple(self.checks))
        if self.score is not None:
            score = float(self.score)
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise ValueError("quality score must be between 0 and 1")
            object.__setattr__(self, "score", score)
        if self.summary is not None:
            object.__setattr__(self, "summary", str(self.summary).strip() or None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "checks": [check.to_dict() for check in self.checks],
            "score": self.score,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "QualityEvidence":
        value = value or {}
        return cls(
            status=QualityStatus(value.get("status", QualityStatus.NOT_CHECKED.value)),
            checks=tuple(
                QualityCheck(
                    name=item["name"],
                    status=QualityStatus(item["status"]),
                    detail=item.get("detail"),
                )
                for item in value.get("checks", [])
            ),
            score=value.get("score"),
            summary=value.get("summary"),
        )


@dataclass(frozen=True, slots=True)
class RenditionProjection:
    kind: RenditionKind
    mime_type: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", RenditionKind(self.kind))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class FeedbackProjection:
    feedback_id: str
    revision_id: str
    signal: FeedbackSignal
    recorded_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal", FeedbackSignal(self.signal))

    def to_dict(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "revision_id": self.revision_id,
            "signal": self.signal.value,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class ArtifactProjection:
    artifact_id: str
    revision_id: str
    family: ArtifactFamily
    role: ArtifactRole
    visibility: ArtifactVisibility
    status: ArtifactStatus
    display_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    created_at: str
    lineage: ArtifactLineage = field(default_factory=ArtifactLineage)
    renditions: tuple[RenditionProjection, ...] = ()
    actions: tuple[ArtifactAction, ...] = ()
    feedback: FeedbackProjection | None = None
    quality_evidence: QualityEvidence = field(default_factory=QualityEvidence)

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", ArtifactFamily(self.family))
        object.__setattr__(self, "role", ArtifactRole(self.role))
        object.__setattr__(self, "visibility", ArtifactVisibility(self.visibility))
        object.__setattr__(self, "status", ArtifactStatus(self.status))
        object.__setattr__(self, "renditions", tuple(self.renditions))
        object.__setattr__(self, "actions", tuple(ArtifactAction(action) for action in self.actions))

    @property
    def is_user_visible(self) -> bool:
        return self.visibility in {ArtifactVisibility.PRIMARY, ArtifactVisibility.SECONDARY}

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "family": self.family.value,
            "role": self.role.value,
            "visibility": self.visibility.value,
            "status": self.status.value,
            "display_name": self.display_name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
            "lineage": self.lineage.to_dict(),
            "renditions": [rendition.to_dict() for rendition in self.renditions],
            "actions": [action.value for action in self.actions],
            "feedback": self.feedback.to_dict() if self.feedback else None,
            "quality_evidence": self.quality_evidence.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class FeedbackRequest:
    revision_id: str
    signal: FeedbackSignal
    client_request_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision_id", _require_non_empty(self.revision_id, "revision_id"))
        object.__setattr__(self, "signal", FeedbackSignal(self.signal))
        object.__setattr__(self, "client_request_id", _require_non_empty(self.client_request_id, "client_request_id"))


@dataclass(frozen=True, slots=True)
class ArtifactExternalActionReceipt:
    """Durable at-most-once receipt for an OS-level artifact action.

    Paths and launch targets deliberately do not belong to this contract, so
    neither API responses nor Runtime events can accidentally disclose them.
    """

    artifact_id: str
    revision_id: str
    action: ArtifactAction
    client_request_id: str
    status: ArtifactExternalActionStatus
    requested_at: str
    updated_at: str
    failure_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _require_non_empty(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "revision_id", _require_non_empty(self.revision_id, "revision_id"))
        action = ArtifactAction(self.action)
        if action not in {ArtifactAction.OPEN, ArtifactAction.REVEAL}:
            raise ValueError("external artifact action must be open or reveal")
        object.__setattr__(self, "action", action)
        object.__setattr__(
            self,
            "client_request_id",
            _require_non_empty(self.client_request_id, "client_request_id"),
        )
        object.__setattr__(self, "status", ArtifactExternalActionStatus(self.status))
        if self.failure_code is not None:
            object.__setattr__(self, "failure_code", str(self.failure_code).strip() or None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "action": self.action.value,
            "client_request_id": self.client_request_id,
            "status": self.status.value,
            "requested_at": self.requested_at,
            "updated_at": self.updated_at,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    feedback_id: str
    artifact_id: str
    revision_id: str
    signal: FeedbackSignal
    client_request_id: str
    recorded_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal", FeedbackSignal(self.signal))

    def projection(self) -> FeedbackProjection:
        return FeedbackProjection(
            feedback_id=self.feedback_id,
            revision_id=self.revision_id,
            signal=self.signal,
            recorded_at=self.recorded_at,
        )


@dataclass(frozen=True, slots=True)
class RetouchAnnotation:
    kind: str
    normalized_geometry: Mapping[str, Any]
    instruction: str
    annotation_id: str | None = None

    def __post_init__(self) -> None:
        kind = _require_non_empty(self.kind, "annotation kind").casefold()
        if kind not in _ANNOTATION_KINDS:
            raise ValueError(f"unsupported annotation kind {kind!r}")
        geometry = _normalized_geometry(self.normalized_geometry)
        _validate_annotation_geometry(kind, geometry)
        instruction = _require_non_empty(self.instruction, "annotation instruction")
        if len(instruction) > 4000:
            raise ValueError("annotation instruction is too long")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "normalized_geometry", geometry)
        object.__setattr__(self, "instruction", instruction)
        if self.annotation_id is not None:
            annotation_id = _require_non_empty(self.annotation_id, "annotation_id")
            if len(annotation_id) > 128:
                raise ValueError("annotation_id is too long")
            object.__setattr__(self, "annotation_id", annotation_id)

    def to_dict(self) -> dict[str, Any]:
        projection = {
            "kind": self.kind,
            "normalized_geometry": _json_value(self.normalized_geometry, "normalized_geometry"),
            "instruction": self.instruction,
        }
        if self.annotation_id is not None:
            projection["annotation_id"] = self.annotation_id
        return projection

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RetouchAnnotation":
        return cls(
            kind=value["kind"],
            normalized_geometry=value["normalized_geometry"],
            instruction=value["instruction"],
            annotation_id=value.get("annotation_id"),
        )


@dataclass(frozen=True, slots=True)
class RetouchRequest:
    base_revision_id: str
    selected_artifact_ids: tuple[str, ...]
    agent_model_id: str | None = None
    image_model_id: str | None = None
    annotations: tuple[RetouchAnnotation, ...] = ()
    reference_artifact_ids: tuple[str, ...] = ()
    global_instruction: str = ""
    client_request_id: str = ""
    pinned_reference_revision_ids: Mapping[str, str] = field(default_factory=dict)
    edit_surface: Mapping[str, Any] | None = None
    mask: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_revision_id", _require_non_empty(self.base_revision_id, "base_revision_id"))
        selected = tuple(dict.fromkeys(_require_non_empty(item, "selected_artifact_id") for item in self.selected_artifact_ids))
        if not selected:
            raise ValueError("selected_artifact_ids must not be empty")
        if len(selected) > 50:
            raise ValueError("selected_artifact_ids must contain at most 50 items")
        object.__setattr__(self, "selected_artifact_ids", selected)
        for field_name in ("agent_model_id", "image_model_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _require_non_empty(str(value), field_name),
                )
        annotations = tuple(self.annotations)
        if len(annotations) > 100:
            raise ValueError("annotations must contain at most 100 items")
        if not all(isinstance(annotation, RetouchAnnotation) for annotation in annotations):
            raise ValueError("annotations must contain RetouchAnnotation values")
        object.__setattr__(self, "annotations", annotations)
        references = tuple(dict.fromkeys(_require_non_empty(item, "reference_artifact_id") for item in self.reference_artifact_ids))
        if len(references) > 10:
            raise ValueError("reference_artifact_ids must contain at most 10 items")
        object.__setattr__(self, "reference_artifact_ids", references)
        pinned = {
            _require_non_empty(str(artifact_id), "pinned reference artifact_id"):
            _require_non_empty(str(revision_id), "pinned reference revision_id")
            for artifact_id, revision_id in dict(self.pinned_reference_revision_ids).items()
        }
        if not set(pinned).issubset(references):
            raise ValueError(
                "pinned_reference_revision_ids may only contain reference artifacts"
            )
        object.__setattr__(self, "pinned_reference_revision_ids", pinned)
        instruction = str(self.global_instruction or "").strip()
        if len(instruction) > 8000:
            raise ValueError("global_instruction is too long")
        object.__setattr__(self, "global_instruction", instruction)
        object.__setattr__(self, "client_request_id", _require_non_empty(self.client_request_id, "client_request_id"))
        object.__setattr__(
            self,
            "edit_surface",
            _retouch_edit_surface_metadata(
                self.edit_surface,
                base_revision_id=self.base_revision_id,
            ),
        )
        object.__setattr__(self, "mask", _retouch_mask_metadata(self.mask))
        if self.mask is not None and self.edit_surface is None:
            raise ValueError("retouch mask requires edit_surface metadata")
        if not self.annotations and not instruction:
            raise ValueError("retouch requires at least one annotation or a global_instruction")

    def to_dict(self) -> dict[str, Any]:
        projection = {
            "base_revision_id": self.base_revision_id,
            "selected_artifact_ids": list(self.selected_artifact_ids),
            "agent_model_id": self.agent_model_id,
            "image_model_id": self.image_model_id,
            "annotations": [annotation.to_dict() for annotation in self.annotations],
            "reference_artifact_ids": list(self.reference_artifact_ids),
            "global_instruction": self.global_instruction,
            "client_request_id": self.client_request_id,
        }
        if self.pinned_reference_revision_ids:
            projection["pinned_reference_revision_ids"] = dict(
                self.pinned_reference_revision_ids
            )
        if self.edit_surface is not None:
            projection["edit_surface"] = dict(self.edit_surface)
        if self.mask is not None:
            projection["mask"] = dict(self.mask)
        return projection

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RetouchRequest":
        return cls(
            base_revision_id=value["base_revision_id"],
            selected_artifact_ids=tuple(value.get("selected_artifact_ids", ())),
            agent_model_id=value.get("agent_model_id"),
            image_model_id=value.get("image_model_id"),
            annotations=tuple(RetouchAnnotation.from_dict(item) for item in value.get("annotations", ())),
            reference_artifact_ids=tuple(value.get("reference_artifact_ids", ())),
            global_instruction=value.get("global_instruction", ""),
            client_request_id=value["client_request_id"],
            pinned_reference_revision_ids=value.get(
                "pinned_reference_revision_ids", {}
            ),
            edit_surface=value.get("edit_surface"),
            mask=value.get("mask"),
        )


@dataclass(frozen=True, slots=True)
class InspectionRegion:
    normalized_geometry: Mapping[str, Any]
    summary: str

    def __post_init__(self) -> None:
        geometry = _normalized_geometry(self.normalized_geometry)
        keys = set(geometry)
        if keys == {"x", "y", "width", "height"}:
            _validate_annotation_geometry("rectangle", geometry)
        elif keys == {"x", "y"}:
            _validate_annotation_geometry("point", geometry)
        elif keys == {"points"}:
            _validate_annotation_geometry("polyline", geometry)
        elif keys == {"points", "width"}:
            _validate_annotation_geometry("brush", geometry)
        else:
            raise ValueError(
                "inspection region geometry must be a rectangle, point, polygonal path, or brush path"
            )
        object.__setattr__(self, "normalized_geometry", geometry)
        summary = _require_non_empty(self.summary, "inspection region summary")
        if len(summary) > 4000:
            raise ValueError("inspection region summary is too long")
        object.__setattr__(self, "summary", summary)

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalized_geometry": _json_value(self.normalized_geometry, "normalized_geometry"),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InspectionRegion":
        return cls(normalized_geometry=value["normalized_geometry"], summary=value["summary"])


@dataclass(frozen=True, slots=True)
class RetouchStagedResult:
    """Internal CAS-backed adapter result; it never carries image bytes."""

    sha256: str
    size_bytes: int
    mime_type: str
    requested_name: str
    change_summary: str
    inspection_regions: tuple[InspectionRegion, ...] = ()
    quality_evidence: QualityEvidence = field(default_factory=QualityEvidence)
    adapter_result_id: str | None = None

    def __post_init__(self) -> None:
        digest = str(self.sha256 or "").casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("staged retouch sha256 is invalid")
        object.__setattr__(self, "sha256", digest)
        if isinstance(self.size_bytes, bool) or self.size_bytes < 1:
            raise ValueError("staged retouch size_bytes must be positive")
        mime_type = str(self.mime_type or "").split(";", 1)[0].strip().casefold()
        if not mime_type.startswith("image/") or mime_type == "image/svg+xml":
            raise ValueError("staged retouch output must be a raster image")
        object.__setattr__(self, "mime_type", mime_type)
        object.__setattr__(self, "requested_name", _require_non_empty(self.requested_name, "requested_name"))
        summary = _require_non_empty(self.change_summary, "change_summary")
        if len(summary) > 8000:
            raise ValueError("change_summary is too long")
        object.__setattr__(self, "change_summary", summary)
        object.__setattr__(self, "inspection_regions", tuple(self.inspection_regions))
        if not all(isinstance(region, InspectionRegion) for region in self.inspection_regions):
            raise ValueError("inspection_regions must contain InspectionRegion values")
        object.__setattr__(self, "quality_evidence", coerce_quality_evidence(self.quality_evidence))
        if self.adapter_result_id is not None:
            result_id = _require_non_empty(self.adapter_result_id, "adapter_result_id")
            if len(result_id) > 256:
                raise ValueError("adapter_result_id is too long")
            object.__setattr__(self, "adapter_result_id", result_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "requested_name": self.requested_name,
            "change_summary": self.change_summary,
            "inspection_regions": [region.to_dict() for region in self.inspection_regions],
            "quality_evidence": self.quality_evidence.to_dict(),
            "adapter_result_id": self.adapter_result_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RetouchStagedResult":
        return cls(
            sha256=value["sha256"],
            size_bytes=value["size_bytes"],
            mime_type=value["mime_type"],
            requested_name=value["requested_name"],
            change_summary=value["change_summary"],
            inspection_regions=tuple(
                InspectionRegion.from_dict(item)
                for item in value.get("inspection_regions", ())
            ),
            quality_evidence=QualityEvidence.from_dict(value.get("quality_evidence", {})),
            adapter_result_id=value.get("adapter_result_id"),
        )


@dataclass(frozen=True, slots=True)
class RetouchExecutionBinding:
    """Internal binding created atomically by a supervised Runtime executor."""

    durable_job_id: str
    thread_id: str
    turn_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "durable_job_id",
            _require_non_empty(self.durable_job_id, "durable_job_id"),
        )
        object.__setattr__(
            self, "thread_id", _require_non_empty(self.thread_id, "thread_id")
        )
        object.__setattr__(
            self, "turn_id", _require_non_empty(self.turn_id, "turn_id")
        )


@dataclass(frozen=True, slots=True)
class RetouchJob:
    job_id: str
    artifact_id: str
    base_revision_id: str
    request: RetouchRequest
    annotation_layer_artifact_id: str
    annotation_layer_revision_id: str
    status: RetouchJobStatus
    created_at: str
    result_revision_id: str | None = None
    change_summary: str | None = None
    inspection_regions: tuple[InspectionRegion, ...] = ()
    failure_reason: str | None = None
    durable_job_id: str | None = None
    execution_thread_id: str | None = None
    execution_turn_id: str | None = None
    external_idempotency_key: str | None = None
    staged_result: RetouchStagedResult | None = None
    input_revision_ids: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", RetouchJobStatus(self.status))
        object.__setattr__(self, "inspection_regions", tuple(self.inspection_regions))
        if self.staged_result is not None and not isinstance(
            self.staged_result, RetouchStagedResult
        ):
            raise ValueError("staged_result must be RetouchStagedResult")
        revisions = {
            _require_non_empty(artifact_id, "input artifact_id"): _require_non_empty(
                revision_id, "input revision_id"
            )
            for artifact_id, revision_id in self.input_revision_ids.items()
        }
        object.__setattr__(self, "input_revision_ids", revisions)

    def to_dict(self) -> dict[str, Any]:
        return self.public_projection().to_dict()

    def public_projection(self) -> "RetouchJobProjection":
        return RetouchJobProjection(
            job_id=self.job_id,
            artifact_id=self.artifact_id,
            base_revision_id=self.base_revision_id,
            request=self.request,
            status=self.status,
            created_at=self.created_at,
            result_revision_id=self.result_revision_id,
            change_summary=self.change_summary,
            inspection_regions=self.inspection_regions,
            failure_reason=self.failure_reason,
        )

    def to_internal_dict(self) -> dict[str, Any]:
        value = self.public_projection().to_dict()
        value["annotation_layer_artifact_id"] = self.annotation_layer_artifact_id
        value["annotation_layer_revision_id"] = self.annotation_layer_revision_id
        value["durable_job_id"] = self.durable_job_id
        value["execution_thread_id"] = self.execution_thread_id
        value["execution_turn_id"] = self.execution_turn_id
        value["external_idempotency_key"] = self.external_idempotency_key
        value["staged_result"] = (
            self.staged_result.to_dict() if self.staged_result else None
        )
        value["input_revision_ids"] = dict(self.input_revision_ids)
        return value


@dataclass(frozen=True, slots=True)
class RetouchJobProjection:
    job_id: str
    artifact_id: str
    base_revision_id: str
    request: RetouchRequest
    status: RetouchJobStatus
    created_at: str
    result_revision_id: str | None = None
    change_summary: str | None = None
    inspection_regions: tuple[InspectionRegion, ...] = ()
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", RetouchJobStatus(self.status))
        object.__setattr__(self, "inspection_regions", tuple(self.inspection_regions))

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "artifact_id": self.artifact_id,
            "base_revision_id": self.base_revision_id,
            "request": self.request.to_dict(),
            "status": self.status.value,
            "created_at": self.created_at,
            "result_revision_id": self.result_revision_id,
            "change_summary": self.change_summary,
            "inspection_regions": [region.to_dict() for region in self.inspection_regions],
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class RetouchResult:
    job: RetouchJobProjection
    artifact: ArtifactProjection

    def to_dict(self) -> dict[str, Any]:
        return {"job": self.job.to_dict(), "artifact": self.artifact.to_dict()}


def coerce_quality_evidence(value: QualityEvidence | Mapping[str, Any] | None) -> QualityEvidence:
    if value is None:
        return QualityEvidence()
    if isinstance(value, QualityEvidence):
        return value
    return QualityEvidence.from_dict(value)


def coerce_inspection_regions(
    values: Sequence[InspectionRegion | Mapping[str, Any]] | None,
) -> tuple[InspectionRegion, ...]:
    return tuple(
        item if isinstance(item, InspectionRegion) else InspectionRegion.from_dict(item)
        for item in (values or ())
    )
