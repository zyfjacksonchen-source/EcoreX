"""Authenticated Runtime API for deleting the encrypted legacy-key backup."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .errors import QuarantineStateError
from .quarantine import MigrationQuarantineService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeleteQuarantineRequest(_StrictModel):
    confirmed: Literal[True]
    client_request_id: str = Field(min_length=8, max_length=256)


def create_migration_quarantine_router(
    service: MigrationQuarantineService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/migration/quarantine", tags=["migration"])

    @router.get("")
    def status() -> dict:
        try:
            return service.status().to_dict()
        except QuarantineStateError:
            raise HTTPException(
                409,
                detail={
                    "code": "migration_quarantine_invalid",
                    "message": "旧版凭证备份状态异常，已停止操作。",
                },
            ) from None

    @router.post("/delete")
    def delete(request: DeleteQuarantineRequest) -> dict:
        try:
            return service.delete(
                confirmed=request.confirmed,
                client_request_id=request.client_request_id,
            ).to_dict()
        except QuarantineStateError:
            raise HTTPException(
                409,
                detail={
                    "code": "migration_quarantine_invalid",
                    "message": "旧版凭证备份状态异常，未执行删除。",
                },
            ) from None
        except ValueError:
            raise HTTPException(
                422,
                detail={
                    "code": "migration_quarantine_confirmation_required",
                    "message": "请确认删除旧版凭证备份。",
                },
            ) from None

    return router


__all__ = ["DeleteQuarantineRequest", "create_migration_quarantine_router"]
