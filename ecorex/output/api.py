"""Thin `/api/v1/output` adapter; host paths never enter its schema."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
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


class _StrictResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


OutputLocationLiteral = Literal["documents", "downloads", "workspace"]


class OutputLocationOptionResponse(_StrictResponseModel):
    alias: OutputLocationLiteral
    available: bool


class OutputLocationCatalogResponse(_StrictResponseModel):
    items: list[OutputLocationOptionResponse] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_complete_catalog(self) -> "OutputLocationCatalogResponse":
        aliases = [item.alias for item in self.items]
        if len(set(aliases)) != len(aliases) or set(aliases) != {
            "documents",
            "downloads",
            "workspace",
        }:
            raise ValueError("output location catalog is incomplete or duplicated")
        return self


class OutputPreferenceResponse(_StrictResponseModel):
    account_id: str = Field(min_length=1, max_length=256)
    location_alias: OutputLocationLiteral
    revision: int = Field(ge=1)
    output_policy_snapshot_id: str = Field(pattern=r"^outpol_[0-9a-f]{64}$")
    updated_at: datetime = Field(strict=False)


class OutputMaterializationResponse(_StrictResponseModel):
    materialization_id: str = Field(pattern=r"^mat_[0-9a-f]{64}$")
    artifact_id: str = Field(min_length=1, max_length=256)
    revision_id: str = Field(min_length=1, max_length=256)
    output_policy_snapshot_id: str = Field(pattern=r"^outpol_[0-9a-f]{64}$")
    location_alias: OutputLocationLiteral
    display_name: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    status: Literal["preparing", "published", "completed"]
    reused_existing: bool
    created_at: datetime = Field(strict=False)
    completed_at: datetime | None = Field(strict=False)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "OutputMaterializationResponse":
        if self.created_at.tzinfo is None:
            raise ValueError("output materialization created_at must be timezone-aware")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError(
                "output materialization completed_at must be timezone-aware"
            )
        if (self.status == "completed") != (self.completed_at is not None):
            raise ValueError("output materialization completion state is inconsistent")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("output materialization timestamps are inconsistent")
        return self


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

    @router.get("/locations", response_model=OutputLocationCatalogResponse)
    def locations() -> OutputLocationCatalogResponse:
        return OutputLocationCatalogResponse.model_validate(
            {"items": [item.to_dict() for item in service.location_catalog()]}
        )

    @router.get("/preference", response_model=OutputPreferenceResponse)
    def preference() -> OutputPreferenceResponse:
        try:
            return OutputPreferenceResponse.model_validate(
                service.project_preference().to_dict()
            )
        except OutputError as error:
            raise _http_error(error) from None

    @router.put("/preference", response_model=OutputPreferenceResponse)
    def update_preference(request: OutputPreferenceRequest) -> OutputPreferenceResponse:
        try:
            return OutputPreferenceResponse.model_validate(
                service.set_preference(
                    request.location_alias,
                    expected_revision=request.expected_revision,
                    client_request_id=request.client_request_id,
                ).to_dict()
            )
        except OutputError as error:
            raise _http_error(error) from None

    @router.post(
        "/artifacts/{artifact_id}/materialize",
        response_model=OutputMaterializationResponse,
    )
    async def materialize(
        artifact_id: str,
        request: MaterializeArtifactRequest,
    ) -> OutputMaterializationResponse:
        try:
            result = await run_in_threadpool(
                service.materialize_artifact_revision,
                artifact_id,
                request.revision_id,
                client_request_id=request.client_request_id,
            )
            return OutputMaterializationResponse.model_validate(result.to_dict())
        except OutputError as error:
            raise _http_error(error) from None

    @router.get(
        "/materializations/{materialization_id}",
        response_model=OutputMaterializationResponse,
    )
    def materialization(materialization_id: str) -> OutputMaterializationResponse:
        try:
            return OutputMaterializationResponse.model_validate(
                service.get_materialization(materialization_id).to_dict()
            )
        except OutputError as error:
            raise _http_error(error) from None

    return router


__all__ = [
    "OutputLocationCatalogResponse",
    "OutputLocationOptionResponse",
    "OutputMaterializationResponse",
    "OutputPreferenceResponse",
    "create_output_router",
]
