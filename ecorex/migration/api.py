"""Authenticated Runtime API for deleting the encrypted legacy-key backup."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import QuarantineStateError
from .quarantine import MigrationQuarantineService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StrictResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DeleteQuarantineRequest(_StrictModel):
    confirmed: Literal[True]
    client_request_id: str = Field(min_length=8, max_length=256)


class MigrationQuarantineItemResponse(_StrictResponseModel):
    kind: Literal[
        "api_key",
        "refresh_token",
        "access_token",
        "password",
        "cryptographic_key",
        "client_secret",
        "credential",
    ]
    origin: Literal[
        "product_configuration",
        "mcp_configuration",
        "skill_configuration",
        "permission_configuration",
    ]
    count: int = Field(ge=1)


class MigrationQuarantineResponse(_StrictResponseModel):
    status: Literal["absent", "available", "deleted"]
    entry_count: int = Field(ge=0)
    can_delete: bool
    deleted_at: datetime | None = Field(strict=False)
    items: list[MigrationQuarantineItemResponse] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "MigrationQuarantineResponse":
        identities = {(item.kind, item.origin) for item in self.items}
        if len(identities) != len(self.items):
            raise ValueError("migration quarantine categories must be unique")
        item_count = sum(item.count for item in self.items)
        if self.status == "absent":
            valid = (
                self.entry_count == 0
                and not self.can_delete
                and self.deleted_at is None
                and not self.items
            )
        elif self.status == "available":
            valid = (
                self.entry_count > 0
                and self.can_delete
                and self.deleted_at is None
                and item_count == self.entry_count
            )
        else:
            valid = (
                self.entry_count > 0
                and not self.can_delete
                and self.deleted_at is not None
                and item_count == self.entry_count
            )
        if not valid:
            raise ValueError("migration quarantine lifecycle is inconsistent")
        if self.deleted_at is not None and self.deleted_at.tzinfo is None:
            raise ValueError("migration quarantine deleted_at must be timezone-aware")
        return self


def create_migration_quarantine_router(
    service: MigrationQuarantineService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/migration/quarantine", tags=["migration"])

    @router.get("", response_model=MigrationQuarantineResponse)
    def status() -> MigrationQuarantineResponse:
        try:
            return MigrationQuarantineResponse.model_validate(
                service.status().to_dict()
            )
        except QuarantineStateError:
            raise HTTPException(
                409,
                detail={
                    "code": "migration_quarantine_invalid",
                    "message": "旧版凭证备份状态异常，已停止操作。",
                },
            ) from None

    @router.post("/delete", response_model=MigrationQuarantineResponse)
    def delete(request: DeleteQuarantineRequest) -> MigrationQuarantineResponse:
        try:
            return MigrationQuarantineResponse.model_validate(
                service.delete(
                    confirmed=request.confirmed,
                    client_request_id=request.client_request_id,
                ).to_dict()
            )
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


__all__ = [
    "DeleteQuarantineRequest",
    "MigrationQuarantineItemResponse",
    "MigrationQuarantineResponse",
    "create_migration_quarantine_router",
]
