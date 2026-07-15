"""Authenticated local Runtime router for ShareSnapshot intent."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .errors import (
    ShareConflict,
    ShareMediaContractError,
    ShareNotFound,
    ShareUnavailable,
)
from .models import ShareSnapshotProjection
from .service import ShareSnapshotService


class ShareRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateShareRequest(ShareRequestModel):
    expires_in_hours: int = Field(default=24 * 7, ge=1, le=24 * 30, strict=True)
    client_request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class RevokeShareRequest(ShareRequestModel):
    client_request_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class ShareListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[ShareSnapshotProjection]
    count: int = Field(ge=0)


def create_share_router(service: ShareSnapshotService) -> APIRouter:
    router = APIRouter(tags=["shares"])

    @router.post(
        "/threads/{thread_id}/shares",
        response_model=ShareSnapshotProjection,
        status_code=201,
    )
    async def create_share(
        thread_id: str, body: CreateShareRequest
    ) -> ShareSnapshotProjection:
        try:
            return await service.create(
                thread_id,
                expires_in_hours=body.expires_in_hours,
                client_request_id=body.client_request_id,
            )
        except ShareMediaContractError as error:
            raise HTTPException(
                status_code=409,
                detail=error.public_detail(),
            ) from error
        except ShareConflict as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "share_conflict", "message": "分享请求与已有快照冲突。"},
            ) from error
        except ShareUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail={"code": "share_unavailable", "message": "暂时无法创建分享，请稍后重试。"},
            ) from error

    @router.get(
        "/shares/{share_id}", response_model=ShareSnapshotProjection
    )
    def get_share(share_id: str) -> ShareSnapshotProjection:
        try:
            return service.get(share_id)
        except ShareNotFound as error:
            raise HTTPException(status_code=404, detail="分享快照不存在。") from error
        except ShareConflict as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "share_conflict", "message": "分享快照完整性校验失败。"},
            ) from error

    @router.get(
        "/threads/{thread_id}/shares", response_model=ShareListResponse
    )
    def list_shares(
        thread_id: str,
        limit: int = Query(default=100, ge=1, le=200),
    ) -> ShareListResponse:
        try:
            items, count = service.list(thread_id, limit=limit)
            return ShareListResponse(items=items, count=count)
        except ShareConflict as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "share_conflict", "message": "分享快照完整性校验失败。"},
            ) from error

    @router.post(
        "/shares/{share_id}/revoke", response_model=ShareSnapshotProjection
    )
    async def revoke_share(
        share_id: str, body: RevokeShareRequest
    ) -> ShareSnapshotProjection:
        try:
            return await service.revoke(
                share_id, client_request_id=body.client_request_id
            )
        except ShareNotFound as error:
            raise HTTPException(status_code=404, detail="分享快照不存在。") from error
        except ShareConflict as error:
            raise HTTPException(
                status_code=409,
                detail={"code": "share_conflict", "message": "当前分享状态无法撤销。"},
            ) from error
        except ShareUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail={"code": "share_unavailable", "message": "暂时无法撤销分享，请稍后重试。"},
            ) from error

    return router
