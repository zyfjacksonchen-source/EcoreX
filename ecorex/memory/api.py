"""HTTP contract for learned-memory status, reset and undo."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import (
    MemoryConflict,
    MemoryContentNotFound,
    MemoryContentUnavailable,
    MemoryResetNotFound,
    MemoryUndoExpired,
)
from .service import MemoryService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StrictResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MemoryResetRequest(_StrictModel):
    confirmed: Literal[True]
    client_request_id: str = Field(min_length=8, max_length=256)


class MemoryResetProjectionResponse(_StrictResponseModel):
    reset_id: str = Field(min_length=1, max_length=256)
    status: Literal["active", "undone", "purged"]
    affected_records: int = Field(ge=0)
    affected_files: int = Field(ge=0)
    created_at: datetime = Field(strict=False)
    undo_until: datetime = Field(strict=False)
    updated_at: datetime = Field(strict=False)
    can_undo: bool

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "MemoryResetProjectionResponse":
        if (
            self.created_at.tzinfo is None
            or self.undo_until.tzinfo is None
            or self.updated_at.tzinfo is None
        ):
            raise ValueError("memory reset timestamps must be timezone-aware")
        if self.undo_until < self.created_at or self.updated_at < self.created_at:
            raise ValueError("memory reset timestamps are inconsistent")
        if self.status != "active" and self.can_undo:
            raise ValueError("only an active memory reset can be undoable")
        return self


class MemorySnapshotResponse(_StrictResponseModel):
    revision: int = Field(ge=0)
    active_learned_records: int = Field(ge=0)
    active_user_files: int = Field(ge=0)
    factory_records: int = Field(ge=0)
    tombstoned_records: int = Field(ge=0)
    tombstoned_files: int = Field(ge=0)
    resettable_count: int = Field(ge=0)
    latest_reset: MemoryResetProjectionResponse | None

    @model_validator(mode="after")
    def validate_derived_count(self) -> "MemorySnapshotResponse":
        expected = self.active_learned_records + self.active_user_files
        if self.resettable_count != expected:
            raise ValueError("memory resettable_count is inconsistent")
        return self


class MemoryMutationResponse(_StrictResponseModel):
    memory: MemorySnapshotResponse
    reset: MemoryResetProjectionResponse

    @model_validator(mode="after")
    def validate_reset_identity(self) -> "MemoryMutationResponse":
        latest = self.memory.latest_reset
        if latest is None or latest.reset_id != self.reset.reset_id:
            raise ValueError("memory mutation reset identity is inconsistent")
        return self


class MemoryContentItemResponse(_StrictResponseModel):
    item_id: str = Field(min_length=1, max_length=8192)
    name: str = Field(min_length=1, max_length=8192)
    path: str = Field(min_length=1, max_length=8192)
    kind: Literal["file", "evolution"]
    origin: Literal["factory", "learned", "imported"]
    source: str = Field(min_length=1, max_length=8192)
    size_bytes: int = Field(ge=0, le=10 * 1024 * 1024)
    updated_at: datetime | None = Field(strict=False)


class MemoryContentPageResponse(_StrictResponseModel):
    view: Literal["files", "evolution"]
    page: int = Field(ge=1)
    page_size: Literal[10]
    total: int = Field(ge=0)
    items: list[MemoryContentItemResponse] = Field(max_length=10)


class MemoryContentDocumentResponse(MemoryContentItemResponse):
    content: str = Field(max_length=10 * 1024 * 1024)


class MemoryLearningSettingsRequest(_StrictModel):
    enabled: bool


class MemoryLearningSettingsResponse(_StrictResponseModel):
    enabled: bool


def create_memory_router(service: MemoryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

    @router.get("", response_model=MemorySnapshotResponse)
    def snapshot() -> MemorySnapshotResponse:
        return MemorySnapshotResponse.model_validate(service.snapshot().to_dict())

    @router.get("/learning", response_model=MemoryLearningSettingsResponse)
    def learning_settings() -> MemoryLearningSettingsResponse:
        return MemoryLearningSettingsResponse.model_validate(
            service.learning_settings().to_dict()
        )

    @router.put("/learning", response_model=MemoryLearningSettingsResponse)
    def set_learning_settings(
        request: MemoryLearningSettingsRequest,
    ) -> MemoryLearningSettingsResponse:
        try:
            value = service.set_learning_enabled(request.enabled)
        except (OSError, ValueError):
            raise HTTPException(
                409,
                detail={
                    "code": "memory_learning_config_unavailable",
                    "message": "记忆学习设置暂时无法保存。",
                },
            ) from None
        return MemoryLearningSettingsResponse.model_validate(value.to_dict())

    @router.get("/files", response_model=MemoryContentPageResponse)
    def content_files(
        view: Literal["files", "evolution"] = "files",
        page: int = Query(default=1, ge=1, le=1_000_000),
    ) -> MemoryContentPageResponse:
        try:
            return MemoryContentPageResponse.model_validate(
                service.content_page(view=view, page=page).to_dict()
            )
        except ValueError:
            raise HTTPException(
                422,
                detail={"code": "memory_page_invalid", "message": "记忆分页参数无效。"},
            ) from None

    @router.get("/file", response_model=MemoryContentDocumentResponse)
    def content_file(
        item_id: str,
        view: Literal["files", "evolution"] = "files",
    ) -> MemoryContentDocumentResponse:
        try:
            return MemoryContentDocumentResponse.model_validate(
                service.content_document(view=view, item_id=item_id).to_dict()
            )
        except MemoryContentNotFound as error:
            raise HTTPException(
                404,
                detail={"code": error.code, "message": "没有找到这项记忆内容。"},
            ) from None
        except MemoryContentUnavailable as error:
            raise HTTPException(
                409,
                detail={"code": error.code, "message": "这项记忆内容暂时不可读取。"},
            ) from None
        except ValueError:
            raise HTTPException(
                422,
                detail={"code": "memory_content_invalid", "message": "记忆内容参数无效。"},
            ) from None

    @router.post("/reset", response_model=MemoryMutationResponse)
    def reset(request: MemoryResetRequest) -> MemoryMutationResponse:
        try:
            result = service.reset_learned(
                confirmed=request.confirmed,
                client_request_id=request.client_request_id,
            )
        except MemoryConflict as error:
            raise HTTPException(
                409,
                detail={
                    "code": error.code,
                    "message": "这次重置请求与先前操作不一致。请刷新后重试。",
                },
            ) from None
        except ValueError:
            raise HTTPException(
                422,
                detail={
                    "code": "memory_reset_confirmation_required",
                    "message": "请先确认只重置学习记忆。",
                },
            ) from None
        return MemoryMutationResponse.model_validate(
            {"memory": service.snapshot().to_dict(), "reset": result.to_dict()}
        )

    @router.post("/resets/{reset_id}/undo", response_model=MemoryMutationResponse)
    def undo(reset_id: str, request: MemoryResetRequest) -> MemoryMutationResponse:
        try:
            result = service.undo_reset(
                reset_id,
                confirmed=request.confirmed,
                client_request_id=request.client_request_id,
            )
        except MemoryResetNotFound as error:
            raise HTTPException(
                404,
                detail={"code": error.code, "message": "没有找到这次记忆重置记录。"},
            ) from None
        except MemoryUndoExpired as error:
            raise HTTPException(
                410,
                detail={
                    "code": error.code,
                    "message": "这次记忆重置已超过可撤销时间。",
                },
            ) from None
        except MemoryConflict as error:
            raise HTTPException(
                409,
                detail={
                    "code": error.code,
                    "message": "这次撤销请求与先前操作不一致。请刷新后重试。",
                },
            ) from None
        except ValueError:
            raise HTTPException(
                422,
                detail={
                    "code": "memory_undo_confirmation_required",
                    "message": "请先确认撤销这次重置。",
                },
            ) from None
        return MemoryMutationResponse.model_validate(
            {"memory": service.snapshot().to_dict(), "reset": result.to_dict()}
        )

    return router


__all__ = [
    "MemoryContentDocumentResponse",
    "MemoryContentPageResponse",
    "MemoryLearningSettingsResponse",
    "MemoryMutationResponse",
    "MemoryResetProjectionResponse",
    "MemorySnapshotResponse",
    "create_memory_router",
]
