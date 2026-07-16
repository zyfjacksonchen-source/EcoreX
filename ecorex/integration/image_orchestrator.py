"""Product adapters for the unified managed image orchestration service."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from ecorex.artifacts import InspectionRegion, QualityEvidence
from ecorex.image_orchestrator import ImageOperation, ImageSubmitRequest

from .managed_image import (
    ManagedImageClientError,
    ManagedImageDownloadedResult,
    ManagedImageInputAsset,
    ManagedImageOrchestrationClient,
)
from .retouch_adapter import (
    RetouchAdapterError,
    StructuredRetouchAdapterRequest,
    StructuredRetouchAdapterResult,
)


class ManagedImageRetouchAdapter:
    """Maps one local structured RetouchJob to one cloud image Job."""

    def __init__(
        self,
        client: ManagedImageOrchestrationClient,
    ) -> None:
        if not isinstance(client, ManagedImageOrchestrationClient):
            raise TypeError("retouch adapter requires ManagedImageOrchestrationClient")
        self.client = client

    async def aclose(self) -> None:
        # The Product composition owns and closes the shared managed client.
        return None

    async def edit(
        self,
        request: StructuredRetouchAdapterRequest,
    ) -> StructuredRetouchAdapterResult:
        command, inputs = self._command(request)
        try:
            downloaded = await self.client.execute(command, inputs=inputs)
        except ManagedImageClientError as error:
            raise RetouchAdapterError(error.code, retryable=error.retryable) from error
        return self._result(
            downloaded,
            summary="已按结构化标注与全局说明完成图片修改。",
            regions=tuple(
                InspectionRegion(
                    normalized_geometry=annotation.normalized_geometry,
                    summary=annotation.instruction,
                )
                for annotation in request.annotations
            ),
        )

    async def recover(
        self,
        idempotency_key: str,
    ) -> StructuredRetouchAdapterResult | None:
        try:
            downloaded = await self.client.recover_result(idempotency_key)
        except ManagedImageClientError as error:
            raise RetouchAdapterError(error.code, retryable=error.retryable) from error
        if downloaded is None:
            return None
        return self._result(
            downloaded,
            summary="已恢复此前完成的云端修图结果，未重复执行图片修改。",
            regions=(),
        )

    def _command(
        self,
        request: StructuredRetouchAdapterRequest,
    ) -> tuple[ImageSubmitRequest, tuple[ManagedImageInputAsset, ...]]:
        ordered_assets = []
        seen: set[str] = set()
        for asset in (request.base, *request.selected, *request.references):
            if asset.sha256 in seen:
                continue
            seen.add(asset.sha256)
            ordered_assets.append(asset)
        structured = {
            "schema_version": 1,
            "base": {
                "artifact_id": request.base.artifact_id,
                "revision_id": request.base.revision_id,
                "sha256": request.base.sha256,
            },
            "selected": [
                {
                    "artifact_id": asset.artifact_id,
                    "revision_id": asset.revision_id,
                    "sha256": asset.sha256,
                }
                for asset in request.selected
            ],
            "references": [
                {
                    "artifact_id": asset.artifact_id,
                    "revision_id": asset.revision_id,
                    "sha256": asset.sha256,
                }
                for asset in request.references
            ],
            "annotations": [annotation.to_dict() for annotation in request.annotations],
            "global_instruction": request.global_instruction,
            "edit_surface": (
                dict(request.edit_surface) if request.edit_surface is not None else None
            ),
            "mask": request.mask.metadata() if request.mask is not None else None,
        }
        instruction = json.dumps(
            structured,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        dimensions = (
            {
                "width": int(request.edit_surface["width_px"]),
                "height": int(request.edit_surface["height_px"]),
            }
            if request.edit_surface is not None
            else {}
        )
        try:
            command = ImageSubmitRequest(
                operation=ImageOperation.RETOUCH,
                model_id=request.model_id,
                client_request_id=request.idempotency_key,
                prompt="Apply the signed structured retouch command.",
                **dimensions,
                input_sha256=tuple(
                    [asset.sha256 for asset in ordered_assets]
                    + ([request.mask.sha256] if request.mask is not None else [])
                ),
                instruction=instruction,
                metadata={"request_kind": "structured_retouch", "schema_version": 1},
            )
        except ValueError as error:
            raise RetouchAdapterError("invalid_structured_retouch", retryable=False) from error
        inputs = tuple(
            ManagedImageInputAsset(
                sha256=asset.sha256,
                mime_type=asset.mime_type,
                content=asset.content,
            )
            for asset in ordered_assets
        ) + (
            (
                ManagedImageInputAsset(
                    sha256=request.mask.sha256,
                    mime_type="image/png",
                    content=request.mask.content,
                ),
            )
            if request.mask is not None
            else ()
        )
        return command, inputs

    @staticmethod
    def _result(
        downloaded: ManagedImageDownloadedResult,
        *,
        summary: str,
        regions: tuple[InspectionRegion, ...],
    ) -> StructuredRetouchAdapterResult:
        descriptor = downloaded.job.result
        assert descriptor is not None
        extension = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/avif": ".avif",
        }[descriptor.mime_type]
        requested_name = PurePosixPath(
            f"retouched-{downloaded.job.job_id[-12:]}{extension}"
        ).name
        return StructuredRetouchAdapterResult(
            result_id=downloaded.job.job_id,
            content=downloaded.content,
            mime_type=descriptor.mime_type,
            requested_name=requested_name,
            change_summary=summary,
            inspection_regions=regions,
            quality_evidence=QualityEvidence(
                summary="结果已通过云端 digest、MIME、长度和 ETag 完整性校验。"
            ),
        )


__all__ = ["ManagedImageRetouchAdapter"]
