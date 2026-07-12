"""Thin `/api/v1/output` adapter; host paths never enter its schema."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .errors import (
    OutputArtifactNotEligible,
    OutputError,
    OutputIdempotencyConflict,
    OutputIntegrityError,
    OutputLocationUnavailable,
    OutputMaterializationFailed,
    OutputPolicyBindingMissing,
    OutputPolicyNotFound,
    OutputRevisionConflict,
    OutputRootChanged,
    OutputRootUnsafe,
    OutputValidationError,
)
from .service import OutputService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OutputPreferenceRequest(_StrictModel):
    location_alias: Literal["documents", "downloads", "workspace"]
    expected_revision: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=256)


class MaterializeArtifactRequest(_StrictModel):
    revision_id: str = Field(min_length=1, max_length=256)
    client_request_id: str = Field(min_length=1, max_length=256)


def _http_error(error: OutputError) -> HTTPException:
    if isinstance(error, OutputArtifactNotEligible):
        status, message = 404, "这个产物不可用于导出。"
    elif isinstance(error, (OutputRevisionConflict, OutputIdempotencyConflict)):
        status, message = 409, "输出设置已变更，请刷新后重试。"
    elif isinstance(error, (OutputPolicyBindingMissing, OutputPolicyNotFound)):
        status, message = 409, "无法验证这个任务的输出位置。"
    elif isinstance(error, (OutputRootChanged, OutputRootUnsafe)):
        status, message = 409, "输出位置已变更或不安全，请重新选择。"
    elif isinstance(error, OutputLocationUnavailable):
        status, message = 503, "当前无法使用这个输出位置。"
    elif isinstance(error, OutputValidationError):
        status, message = 422, "输出请求格式不正确。"
    elif isinstance(error, (OutputIntegrityError, OutputMaterializationFailed)):
        status, message = 500, "产物导出未完成，可以使用原请求重试。"
    else:
        status, message = 500, "产物导出未完成。"
    return HTTPException(status, detail={"code": error.code, "message": message})


def create_output_router(service: OutputService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/output", tags=["output"])

    @router.get("/locations")
    def locations() -> dict:
        return {"items": [item.to_dict() for item in service.location_catalog()]}

    @router.get("/preference")
    def preference() -> dict:
        try:
            return service.project_preference().to_dict()
        except OutputError as error:
            raise _http_error(error) from None

    @router.put("/preference")
    def update_preference(request: OutputPreferenceRequest) -> dict:
        try:
            return service.set_preference(
                request.location_alias,
                expected_revision=request.expected_revision,
                client_request_id=request.client_request_id,
            ).to_dict()
        except OutputError as error:
            raise _http_error(error) from None

    @router.post("/artifacts/{artifact_id}/materialize")
    async def materialize(artifact_id: str, request: MaterializeArtifactRequest) -> dict:
        try:
            result = await run_in_threadpool(
                service.materialize_artifact_revision,
                artifact_id,
                request.revision_id,
                client_request_id=request.client_request_id,
            )
            return result.to_dict()
        except OutputError as error:
            raise _http_error(error) from None

    @router.get("/materializations/{materialization_id}")
    def materialization(materialization_id: str) -> dict:
        try:
            return service.get_materialization(materialization_id).to_dict()
        except OutputError as error:
            raise _http_error(error) from None

    return router


__all__ = ["create_output_router"]
