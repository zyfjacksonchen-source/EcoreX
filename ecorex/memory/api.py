"""HTTP contract for learned-memory status, reset and undo."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import MemoryConflict, MemoryResetNotFound, MemoryUndoExpired
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


def create_memory_router(service: MemoryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

    @router.get("", response_model=MemorySnapshotResponse)
    def snapshot() -> MemorySnapshotResponse:
        return MemorySnapshotResponse.model_validate(service.snapshot().to_dict())

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
    "MemoryMutationResponse",
    "MemoryResetProjectionResponse",
    "MemorySnapshotResponse",
    "create_memory_router",
]
