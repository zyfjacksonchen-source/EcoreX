"""Strict public wire contracts for Artifact and precise-retouch responses.

The domain dataclasses deliberately stay transport agnostic.  These Pydantic
models are the fail-closed ASGI boundary: only user-visible Artifact families
can cross it, every fixed object rejects extra fields, and related Artifact,
revision, workspace and Job identities must agree before React sees a byte.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .classification import PUBLIC_ARTIFACT_FAMILIES, PUBLIC_ARTIFACT_VISIBILITIES
from .models import (
    ArtifactAction,
    ArtifactFamily,
    ArtifactRole,
    ArtifactStatus,
    ArtifactVisibility,
    FeedbackSignal,
    InspectionRegion,
    QualityStatus,
    RenditionKind,
    RetouchAnnotation,
    RetouchJobStatus,
    RetouchRequest,
)
from .retouch_workspace import RetouchWorkspaceStatus


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_FAMILIES = frozenset(PUBLIC_ARTIFACT_FAMILIES)
_PUBLIC_VISIBILITIES = frozenset(PUBLIC_ARTIFACT_VISIBILITIES)
_ANNOTATION_KINDS = frozenset(
    {"rectangle", "ellipse", "point", "polygon", "polyline", "brush"}
)


class ArtifactWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


def _require_sha256(value: str) -> str:
    normalized = str(value).casefold()
    if not _SHA256.fullmatch(normalized):
        raise ValueError("value must be a lowercase SHA-256 digest")
    return normalized


class ArtifactLineageResponse(ArtifactWireModel):
    source_artifact_ids: list[str] = Field(default_factory=list, max_length=256)
    supersedes_revision_id: str | None = Field(default=None, min_length=1, max_length=128)


class QualityCheckResponse(ArtifactWireModel):
    name: str = Field(min_length=1, max_length=256)
    status: QualityStatus
    detail: str | None = Field(default=None, max_length=4000)


class QualityEvidenceResponse(ArtifactWireModel):
    status: QualityStatus
    checks: list[QualityCheckResponse] = Field(default_factory=list, max_length=256)
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    summary: str | None = Field(default=None, max_length=8000)


class RenditionProjectionResponse(ArtifactWireModel):
    kind: RenditionKind
    mime_type: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(ge=0)
    sha256: str

    _validate_sha256 = field_validator("sha256")(_require_sha256)


class FeedbackProjectionResponse(ArtifactWireModel):
    feedback_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    signal: FeedbackSignal
    recorded_at: datetime


class ArtifactProjectionResponse(ArtifactWireModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    family: ArtifactFamily
    role: ArtifactRole
    visibility: ArtifactVisibility
    status: ArtifactStatus
    display_name: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(min_length=1, max_length=256)
    size_bytes: int = Field(ge=0)
    sha256: str
    created_at: datetime
    lineage: ArtifactLineageResponse
    renditions: list[RenditionProjectionResponse] = Field(default_factory=list, max_length=256)
    actions: list[ArtifactAction] = Field(default_factory=list, max_length=32)
    feedback: FeedbackProjectionResponse | None = None
    quality_evidence: QualityEvidenceResponse

    _validate_sha256 = field_validator("sha256")(_require_sha256)

    @model_validator(mode="after")
    def _public_only(self) -> "ArtifactProjectionResponse":
        if self.family not in _PUBLIC_FAMILIES:
            raise ValueError("internal Artifact family cannot cross the public response")
        if self.visibility not in _PUBLIC_VISIBILITIES:
            raise ValueError("internal Artifact visibility cannot cross the public response")
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("Artifact actions must be unique")
        if self.feedback is not None and self.feedback.revision_id != self.revision_id:
            raise ValueError("Artifact feedback revision does not match the projection")
        return self


class ArtifactListResponse(ArtifactWireModel):
    items: list[ArtifactProjectionResponse] = Field(default_factory=list, max_length=10_000)
    count: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def _count_matches(self) -> "ArtifactListResponse":
        if self.count != len(self.items):
            raise ValueError("Artifact count does not match items")
        identities = {(item.artifact_id, item.revision_id) for item in self.items}
        if len(identities) != len(self.items):
            raise ValueError("Artifact list contains duplicate revision identities")
        return self


class ArtifactExternalActionResponse(ArtifactWireModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    action: Literal["open", "reveal"]
    client_request_id: str = Field(min_length=1, max_length=256)
    status: Literal["completed"]
    requested_at: datetime
    updated_at: datetime
    failure_code: None

    @model_validator(mode="after")
    def _ordered(self) -> "ArtifactExternalActionResponse":
        if self.updated_at < self.requested_at:
            raise ValueError("Artifact action update precedes its request")
        return self


class RetouchAnnotationResponse(ArtifactWireModel):
    kind: Literal["rectangle", "ellipse", "point", "polygon", "polyline", "brush"]
    normalized_geometry: dict[str, Any]
    instruction: str = Field(min_length=1, max_length=4000)
    annotation_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _valid_domain_geometry(self) -> "RetouchAnnotationResponse":
        RetouchAnnotation(
            kind=self.kind,
            normalized_geometry=self.normalized_geometry,
            instruction=self.instruction,
            annotation_id=self.annotation_id,
        )
        return self


class RetouchInspectionRegionResponse(ArtifactWireModel):
    normalized_geometry: dict[str, Any]
    summary: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _valid_domain_geometry(self) -> "RetouchInspectionRegionResponse":
        InspectionRegion(
            normalized_geometry=self.normalized_geometry,
            summary=self.summary,
        )
        return self


class RetouchEditSurfaceResponse(ArtifactWireModel):
    base_revision_id: str = Field(min_length=1, max_length=128)
    raster_digest: str
    width_px: int = Field(ge=1, le=100_000)
    height_px: int = Field(ge=1, le=100_000)
    orientation: int = Field(ge=1, le=8)
    color_space: str = Field(min_length=1, max_length=128)
    mime_type: str = Field(min_length=1, max_length=256)
    coordinate_space_version: Literal["oriented-normalized-v1"]

    _validate_digest = field_validator("raster_digest")(_require_sha256)


class RetouchPixelRegionResponse(ArtifactWireModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)


class RetouchMaskResponse(ArtifactWireModel):
    schema_version: Literal[1]
    coordinate_space_version: Literal["oriented-normalized-v1"]
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    sha256: str
    size_bytes: int = Field(ge=1)
    covered_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)
    pixel_regions: list[RetouchPixelRegionResponse] = Field(default_factory=list, max_length=100)

    _validate_sha256 = field_validator("sha256")(_require_sha256)

    @model_validator(mode="after")
    def _regions_fit(self) -> "RetouchMaskResponse":
        for region in self.pixel_regions:
            if region.x + region.width > self.width_px:
                raise ValueError("retouch mask region exceeds its width")
            if region.y + region.height > self.height_px:
                raise ValueError("retouch mask region exceeds its height")
        return self


class RetouchReferenceResponse(ArtifactWireModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(min_length=1, max_length=256)
    sha256: str
    preview_url: str = Field(min_length=1, max_length=1024)

    _validate_sha256 = field_validator("sha256")(_require_sha256)


class RetouchViewStateResponse(ArtifactWireModel):
    zoom: float = Field(default=1, gt=0, le=64, allow_inf_nan=False)
    pan_x: float = Field(default=0, ge=-16, le=16, allow_inf_nan=False)
    pan_y: float = Field(default=0, ge=-16, le=16, allow_inf_nan=False)
    selected_annotation_id: str | None = Field(default=None, min_length=1, max_length=128)
    tool: Literal[
        "rectangle",
        "ellipse",
        "point",
        "polygon",
        "polyline",
        "brush",
        "select",
        "pan",
    ] = "select"


class RetouchRequestResponse(ArtifactWireModel):
    base_revision_id: str = Field(min_length=1, max_length=128)
    selected_artifact_ids: list[str] = Field(min_length=1, max_length=50)
    agent_model_id: str | None = Field(default=None, min_length=1, max_length=256)
    image_model_id: str | None = Field(default=None, min_length=1, max_length=256)
    annotations: list[RetouchAnnotationResponse] = Field(default_factory=list, max_length=100)
    reference_artifact_ids: list[str] = Field(default_factory=list, max_length=10)
    global_instruction: str = Field(default="", max_length=8000)
    client_request_id: str = Field(min_length=1, max_length=256)
    pinned_reference_revision_ids: dict[str, str] = Field(default_factory=dict)
    edit_surface: RetouchEditSurfaceResponse | None = None
    mask: RetouchMaskResponse | None = None

    @model_validator(mode="after")
    def _domain_invariants(self) -> "RetouchRequestResponse":
        if len(set(self.selected_artifact_ids)) != len(self.selected_artifact_ids):
            raise ValueError("selected Artifact identities must be unique")
        if len(set(self.reference_artifact_ids)) != len(self.reference_artifact_ids):
            raise ValueError("reference Artifact identities must be unique")
        if not set(self.pinned_reference_revision_ids).issubset(
            self.reference_artifact_ids
        ):
            raise ValueError("pinned reference revisions are not selected references")
        if self.edit_surface is not None:
            if self.edit_surface.base_revision_id != self.base_revision_id:
                raise ValueError("retouch edit surface revision does not match request")
        if self.mask is not None and self.edit_surface is None:
            raise ValueError("retouch mask requires an edit surface")
        RetouchRequest(
            base_revision_id=self.base_revision_id,
            selected_artifact_ids=tuple(self.selected_artifact_ids),
            agent_model_id=self.agent_model_id,
            image_model_id=self.image_model_id,
            annotations=tuple(
                RetouchAnnotation(
                    kind=item.kind,
                    normalized_geometry=item.normalized_geometry,
                    instruction=item.instruction,
                    annotation_id=item.annotation_id,
                )
                for item in self.annotations
            ),
            reference_artifact_ids=tuple(self.reference_artifact_ids),
            global_instruction=self.global_instruction,
            client_request_id=self.client_request_id,
            pinned_reference_revision_ids=self.pinned_reference_revision_ids,
            edit_surface=(
                self.edit_surface.model_dump(mode="json")
                if self.edit_surface is not None
                else None
            ),
            mask=self.mask.model_dump(mode="json") if self.mask is not None else None,
        )
        return self


class RetouchJobResponse(ArtifactWireModel):
    job_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=128)
    base_revision_id: str = Field(min_length=1, max_length=128)
    request: RetouchRequestResponse
    status: RetouchJobStatus
    created_at: datetime
    result_revision_id: str | None = Field(default=None, min_length=1, max_length=128)
    change_summary: str | None = Field(default=None, min_length=1, max_length=8000)
    inspection_regions: list[RetouchInspectionRegionResponse] = Field(
        default_factory=list, max_length=100
    )
    failure_reason: str | None = Field(default=None, min_length=1, max_length=8000)

    @model_validator(mode="after")
    def _identity_and_lifecycle(self) -> "RetouchJobResponse":
        if self.request.base_revision_id != self.base_revision_id:
            raise ValueError("retouch Job base revision does not match its request")
        if self.artifact_id not in self.request.selected_artifact_ids:
            raise ValueError("retouch Job target is absent from selected Artifacts")
        if self.status is RetouchJobStatus.COMPLETED:
            if self.result_revision_id is None or self.change_summary is None:
                raise ValueError("completed retouch Job has no result")
            if self.failure_reason is not None:
                raise ValueError("completed retouch Job contains a failure")
        elif self.status is RetouchJobStatus.FAILED:
            if self.failure_reason is None:
                raise ValueError("failed retouch Job has no failure reason")
            if self.result_revision_id is not None:
                raise ValueError("failed retouch Job contains a result revision")
        elif self.result_revision_id is not None:
            raise ValueError("unfinished retouch Job contains a result revision")
        return self


class RetouchWorkspaceResponse(ArtifactWireModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    status: RetouchWorkspaceStatus
    edit_surface: RetouchEditSurfaceResponse
    annotations: list[RetouchAnnotationResponse] = Field(default_factory=list, max_length=100)
    references: list[RetouchReferenceResponse] = Field(default_factory=list, max_length=10)
    global_instruction: str = Field(default="", max_length=8000)
    view_state: RetouchViewStateResponse
    mask: RetouchMaskResponse | None = None
    submitted_job_id: str | None = Field(default=None, min_length=1, max_length=128)
    job: RetouchJobResponse | None = None
    result: ArtifactProjectionResponse | None = None
    result_surface: RetouchEditSurfaceResponse | None = None
    surface_url: str = Field(min_length=1, max_length=1024)
    result_url: str | None = Field(default=None, min_length=1, max_length=1024)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _identity_and_lifecycle(self) -> "RetouchWorkspaceResponse":
        expected_surface = f"/api/v1/retouch-workspaces/{self.workspace_id}/surface"
        if self.surface_url != expected_surface:
            raise ValueError("retouch workspace surface URL does not match its identity")
        if self.updated_at < self.created_at:
            raise ValueError("retouch workspace update precedes creation")
        if len({item.artifact_id for item in self.references}) != len(self.references):
            raise ValueError("retouch workspace contains duplicate references")
        for reference in self.references:
            expected_preview = (
                f"/api/v1/retouch-workspaces/{self.workspace_id}/references/"
                f"{reference.artifact_id}/preview"
            )
            if reference.preview_url != expected_preview:
                raise ValueError("retouch reference preview URL has identity drift")
        annotation_ids = {
            item.annotation_id for item in self.annotations if item.annotation_id is not None
        }
        if len(annotation_ids) != sum(
            item.annotation_id is not None for item in self.annotations
        ):
            raise ValueError("retouch workspace contains duplicate annotation identities")
        selected = self.view_state.selected_annotation_id
        if selected is not None and selected not in annotation_ids:
            raise ValueError("retouch view selects an unknown annotation")
        if self.status is RetouchWorkspaceStatus.SUBMITTED:
            if self.submitted_job_id is None:
                raise ValueError("submitted retouch workspace has no Job")
        elif self.job is not None or self.result is not None:
            raise ValueError("unsubmitted retouch workspace exposes execution state")
        if self.job is not None:
            if self.job.job_id != self.submitted_job_id:
                raise ValueError("retouch workspace Job identity drift")
            if self.job.artifact_id != self.artifact_id:
                raise ValueError("retouch workspace Job targets another Artifact")
            if self.job.base_revision_id != self.edit_surface.base_revision_id:
                raise ValueError("retouch workspace Job uses another base revision")
        if self.result is None:
            if self.result_surface is not None or self.result_url is not None:
                raise ValueError("retouch workspace exposes result metadata without a result")
        else:
            if self.job is None or self.job.status is not RetouchJobStatus.COMPLETED:
                raise ValueError("retouch workspace result has no completed Job")
            if self.result.artifact_id != self.artifact_id:
                raise ValueError("retouch result belongs to another Artifact")
            if self.result.revision_id != self.job.result_revision_id:
                raise ValueError("retouch result revision does not match its Job")
            expected_result = f"/api/v1/retouch-workspaces/{self.workspace_id}/result"
            if self.result_url != expected_result:
                raise ValueError("retouch workspace result URL has identity drift")
            if (
                self.result_surface is not None
                and self.result_surface.base_revision_id != self.result.revision_id
            ):
                raise ValueError("retouch result surface revision has identity drift")
        return self


__all__ = [
    "ArtifactExternalActionResponse",
    "ArtifactListResponse",
    "ArtifactProjectionResponse",
    "ArtifactWireModel",
    "FeedbackProjectionResponse",
    "RetouchJobResponse",
    "RetouchWorkspaceResponse",
]
