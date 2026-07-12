"""Structured cloud image-edit boundary for precise retouch.

The adapter contract deliberately has no free-form ``prompt`` field.  The
Runtime supplies immutable image identities, normalized annotations and the
user's global instruction as separate typed values.  Binary source material is
repr-hidden and never copied into Runtime events or checkpoints.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import inspect
from types import MappingProxyType
from typing import Any, Awaitable, Mapping, Protocol

from ecorex.artifacts import InspectionRegion, QualityEvidence, RetouchAnnotation


def _required(value: str, label: str, *, maximum: int = 256) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{label} is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class RetouchImageAsset:
    artifact_id: str
    revision_id: str
    mime_type: str
    sha256: str
    content: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _required(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "revision_id", _required(self.revision_id, "revision_id"))
        mime_type = str(self.mime_type or "").split(";", 1)[0].strip().casefold()
        if not mime_type.startswith("image/") or mime_type == "image/svg+xml":
            raise ValueError("retouch source must be a raster image")
        object.__setattr__(self, "mime_type", mime_type)
        content = bytes(self.content)
        digest = hashlib.sha256(content).hexdigest()
        if digest != str(self.sha256 or "").casefold():
            raise ValueError("retouch source digest does not match its bytes")
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "content", content)

    def metadata(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "size_bytes": len(self.content),
        }


@dataclass(frozen=True, slots=True)
class RetouchMaskAsset:
    sha256: str
    width_px: int
    height_px: int
    covered_fraction: float
    pixel_regions: tuple[Mapping[str, int], ...]
    content: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        content = bytes(self.content)
        digest = hashlib.sha256(content).hexdigest()
        if digest != str(self.sha256 or "").casefold():
            raise ValueError("retouch mask digest does not match its bytes")
        if not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("retouch mask must be a PNG image")
        if not 1 <= self.width_px <= 16_384 or not 1 <= self.height_px <= 16_384:
            raise ValueError("retouch mask dimensions are invalid")
        fraction = float(self.covered_fraction)
        if not 0 <= fraction <= 1:
            raise ValueError("retouch mask coverage is invalid")
        regions = tuple(MappingProxyType(dict(region)) for region in self.pixel_regions)
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "covered_fraction", fraction)
        object.__setattr__(self, "pixel_regions", regions)
        object.__setattr__(self, "content", content)

    def metadata(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "coordinate_space_version": "oriented-normalized-v1",
            "width_px": self.width_px,
            "height_px": self.height_px,
            "sha256": self.sha256,
            "size_bytes": len(self.content),
            "covered_fraction": self.covered_fraction,
            "pixel_regions": [dict(region) for region in self.pixel_regions],
        }


@dataclass(frozen=True, slots=True)
class StructuredRetouchAdapterRequest:
    job_id: str
    idempotency_key: str
    model_id: str
    base: RetouchImageAsset
    selected: tuple[RetouchImageAsset, ...]
    references: tuple[RetouchImageAsset, ...]
    annotations: tuple[RetouchAnnotation, ...]
    global_instruction: str
    edit_surface: Mapping[str, Any] | None = None
    mask: RetouchMaskAsset | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _required(self.job_id, "job_id"))
        object.__setattr__(
            self,
            "idempotency_key",
            _required(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "model_id", _required(self.model_id, "model_id"))
        if not isinstance(self.base, RetouchImageAsset):
            raise ValueError("base must be RetouchImageAsset")
        selected = tuple(self.selected)
        references = tuple(self.references)
        annotations = tuple(self.annotations)
        if not selected or self.base.artifact_id not in {
            item.artifact_id for item in selected
        }:
            raise ValueError("selected images must include the base artifact")
        if len(selected) > 50 or len(references) > 50 or len(annotations) > 100:
            raise ValueError("retouch adapter request exceeds bounded inputs")
        if not all(isinstance(item, RetouchImageAsset) for item in (*selected, *references)):
            raise ValueError("retouch adapter images are invalid")
        if not all(isinstance(item, RetouchAnnotation) for item in annotations):
            raise ValueError("retouch adapter annotations are invalid")
        if len({item.artifact_id for item in selected}) != len(selected):
            raise ValueError("selected retouch artifacts must be unique")
        if len({item.artifact_id for item in references}) != len(references):
            raise ValueError("reference retouch artifacts must be unique")
        instruction = str(self.global_instruction or "").strip()
        if len(instruction) > 8000 or (not annotations and not instruction):
            raise ValueError("retouch adapter instruction is invalid")
        object.__setattr__(self, "selected", selected)
        object.__setattr__(self, "references", references)
        object.__setattr__(self, "annotations", annotations)
        object.__setattr__(self, "global_instruction", instruction)
        if self.edit_surface is not None:
            surface = dict(self.edit_surface)
            expected = {
                "base_revision_id", "raster_digest", "width_px", "height_px",
                "orientation", "color_space", "mime_type", "coordinate_space_version",
            }
            if set(surface) != expected:
                raise ValueError("retouch edit surface contract is invalid")
            if (
                surface["base_revision_id"] != self.base.revision_id
                or surface["raster_digest"] != self.base.sha256
                or surface["mime_type"] != self.base.mime_type
                or surface["coordinate_space_version"] != "oriented-normalized-v1"
            ):
                raise ValueError("retouch edit surface does not match the base image")
            object.__setattr__(self, "edit_surface", MappingProxyType(surface))
        if self.mask is not None:
            if self.edit_surface is None or not isinstance(self.mask, RetouchMaskAsset):
                raise ValueError("retouch mask requires an edit surface")
            if (
                self.mask.width_px != self.edit_surface["width_px"]
                or self.mask.height_px != self.edit_surface["height_px"]
            ):
                raise ValueError("retouch mask dimensions do not match edit surface")

    def metadata(self) -> dict[str, object]:
        """Safe structured metadata; intentionally excludes all image bytes."""

        return {
            "job_id": self.job_id,
            "idempotency_key": self.idempotency_key,
            "model_id": self.model_id,
            "base": self.base.metadata(),
            "selected": [item.metadata() for item in self.selected],
            "references": [item.metadata() for item in self.references],
            "annotations": [annotation.to_dict() for annotation in self.annotations],
            "global_instruction": self.global_instruction,
            "edit_surface": (
                dict(self.edit_surface) if self.edit_surface is not None else None
            ),
            "mask": self.mask.metadata() if self.mask is not None else None,
        }


@dataclass(frozen=True, slots=True)
class StructuredRetouchAdapterResult:
    result_id: str
    content: bytes = field(repr=False, compare=False)
    mime_type: str = "image/png"
    requested_name: str | None = None
    change_summary: str = ""
    inspection_regions: tuple[InspectionRegion, ...] = ()
    quality_evidence: QualityEvidence = field(default_factory=QualityEvidence)

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _required(self.result_id, "result_id"))
        content = bytes(self.content)
        if not content:
            raise ValueError("retouch adapter returned empty image content")
        object.__setattr__(self, "content", content)
        mime_type = str(self.mime_type or "").split(";", 1)[0].strip().casefold()
        if not mime_type.startswith("image/") or mime_type == "image/svg+xml":
            raise ValueError("retouch adapter returned a non-raster media type")
        object.__setattr__(self, "mime_type", mime_type)
        if self.requested_name is not None:
            object.__setattr__(
                self,
                "requested_name",
                _required(self.requested_name, "requested_name", maximum=255),
            )
        summary = _required(self.change_summary, "change_summary", maximum=8000)
        object.__setattr__(self, "change_summary", summary)
        regions = tuple(self.inspection_regions)
        if not all(isinstance(region, InspectionRegion) for region in regions):
            raise ValueError("adapter inspection regions are invalid")
        object.__setattr__(self, "inspection_regions", regions)
        if not isinstance(self.quality_evidence, QualityEvidence):
            raise ValueError("adapter quality evidence is invalid")


class CloudImageRetouchAdapter(Protocol):
    """Managed cloud adapter; repeated keys must not repeat image editing."""

    def edit(
        self, request: StructuredRetouchAdapterRequest
    ) -> StructuredRetouchAdapterResult | Awaitable[StructuredRetouchAdapterResult]:
        ...

    def recover(
        self, idempotency_key: str
    ) -> StructuredRetouchAdapterResult | None | Awaitable[StructuredRetouchAdapterResult | None]:
        ...


class RetouchAdapterError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(_required(code, "retouch adapter error code"))
        self.code = str(code)
        self.retryable = bool(retryable)


async def invoke_adapter(
    adapter: CloudImageRetouchAdapter,
    request: StructuredRetouchAdapterRequest,
    *,
    recovery_only: bool,
) -> StructuredRetouchAdapterResult | None:
    operation = adapter.recover if recovery_only else adapter.edit
    argument = request.idempotency_key if recovery_only else request
    if inspect.iscoroutinefunction(operation):
        result = operation(argument)  # type: ignore[arg-type]
    else:
        result = await asyncio.to_thread(operation, argument)  # type: ignore[arg-type]
    if inspect.isawaitable(result):
        result = await result
    if result is not None and not isinstance(result, StructuredRetouchAdapterResult):
        raise RetouchAdapterError("invalid_adapter_result", retryable=False)
    return result


__all__ = [
    "CloudImageRetouchAdapter",
    "RetouchAdapterError",
    "RetouchImageAsset",
    "RetouchMaskAsset",
    "StructuredRetouchAdapterRequest",
    "StructuredRetouchAdapterResult",
    "invoke_adapter",
]
