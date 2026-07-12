"""HTTP contract for learned-memory status, reset and undo."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .errors import MemoryConflict, MemoryResetNotFound, MemoryUndoExpired
from .service import MemoryService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryResetRequest(_StrictModel):
    confirmed: Literal[True]
    client_request_id: str = Field(min_length=8, max_length=256)


class MemoryResetResponse(_StrictModel):
    memory: dict
    reset: dict


def create_memory_router(service: MemoryService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

    @router.get("")
    def snapshot() -> dict:
        return service.snapshot().to_dict()

    @router.post("/reset", response_model=MemoryResetResponse)
    def reset(request: MemoryResetRequest) -> dict:
        try:
            result = service.reset_learned(
                confirmed=request.confirmed,
                client_request_id=request.client_request_id,
            )
        except MemoryConflict as error:
            raise HTTPException(409, detail={"code": error.code, "message": "这次重置请求与先前操作不一致。请刷新后重试。"}) from None
        except ValueError:
            raise HTTPException(422, detail={"code": "memory_reset_confirmation_required", "message": "请先确认只重置学习记忆。"}) from None
        return {"memory": service.snapshot().to_dict(), "reset": result.to_dict()}

    @router.post("/resets/{reset_id}/undo", response_model=MemoryResetResponse)
    def undo(reset_id: str, request: MemoryResetRequest) -> dict:
        try:
            result = service.undo_reset(
                reset_id,
                confirmed=request.confirmed,
                client_request_id=request.client_request_id,
            )
        except MemoryResetNotFound as error:
            raise HTTPException(404, detail={"code": error.code, "message": "没有找到这次记忆重置记录。"}) from None
        except MemoryUndoExpired as error:
            raise HTTPException(410, detail={"code": error.code, "message": "这次记忆重置已超过可撤销时间。"}) from None
        except MemoryConflict as error:
            raise HTTPException(409, detail={"code": error.code, "message": "这次撤销请求与先前操作不一致。请刷新后重试。"}) from None
        except ValueError:
            raise HTTPException(422, detail={"code": "memory_undo_confirmation_required", "message": "请先确认撤销这次重置。"}) from None
        return {"memory": service.snapshot().to_dict(), "reset": result.to_dict()}

    return router


__all__ = ["create_memory_router"]
