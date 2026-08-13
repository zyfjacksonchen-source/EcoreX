"""Thin FastAPI intent surface for the backend Extension authority."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ecorex.protocol import (
    ExtensionCatalogSnapshot,
    ExtensionMutationResponse,
    ExtensionProjection,
)

from .errors import (
    ExtensionActionUnavailable,
    ExtensionError,
    ExtensionIdempotencyConflict,
    ExtensionIntegrityError,
    ExtensionNotFound,
    ExtensionRevisionConflict,
    ExtensionVerificationError,
)
from .local_bundle import MAX_LOCAL_BUNDLE_BYTES
from .service import ExtensionService


class ExtensionMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, strict=True)
    client_request_id: str = Field(
        min_length=1,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$",
    )


class LocalSkillInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extension_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{1,127}$",
    )
    bundle_base64: str = Field(
        min_length=4,
        max_length=((MAX_LOCAL_BUNDLE_BYTES + 2) // 3) * 4,
    )
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    client_request_id: str = Field(
        min_length=1,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$",
    )

    @model_validator(mode="after")
    def validate_legacy_identity_pair(self) -> "LocalSkillInstallRequest":
        if (self.extension_id is None) != (self.expected_revision is None):
            raise ValueError("extension_id and expected_revision must be supplied together")
        return self


class SkillConfigurationRequest(ExtensionMutationRequest):
    values: dict[str, str]

    @field_validator("values")
    @classmethod
    def validate_values(cls, value: dict[str, str]) -> dict[str, str]:
        if not 1 <= len(value) <= 32 or any(
            not key or len(key) > 128 or not item or len(item) > 16_384
            for key, item in value.items()
        ):
            raise ValueError("Skill configuration is invalid")
        return value


def register_extension_routes(app: FastAPI, service: ExtensionService) -> None:
    router = APIRouter(prefix="/api/v1/extensions", tags=["extensions"])

    @router.get("", response_model=ExtensionCatalogSnapshot)
    def catalog() -> ExtensionCatalogSnapshot:
        return _snapshot(service)

    @router.post("/local-skills", response_model=ExtensionMutationResponse, status_code=201)
    async def install_local_skill(request: LocalSkillInstallRequest) -> ExtensionMutationResponse:
        try:
            payload = base64.b64decode(request.bundle_base64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=422, detail="local Skill bundle is not canonical base64") from None
        if base64.b64encode(payload).decode("ascii") != request.bundle_base64:
            raise HTTPException(status_code=422, detail="local Skill bundle is not canonical base64")
        try:
            extension = service.install_local_skill_zip(
                payload,
                extension_id=request.extension_id,
                expected_revision=request.expected_revision,
                client_request_id=request.client_request_id,
            )
            if extension.status != "enabled":
                extension = await service.enable(
                    extension.extension_id,
                    expected_revision=extension.revision,
                    client_request_id="local-enable:" + hashlib.sha256(
                        request.client_request_id.encode()
                    ).hexdigest()[:32],
                )
        except ExtensionError as error:
            raise _http_error(error) from error
        return await asyncio.to_thread(_mutation, service, extension)

    @router.post("/{extension_id}/enable", response_model=ExtensionMutationResponse)
    async def enable(
        extension_id: str, request: ExtensionMutationRequest
    ) -> ExtensionMutationResponse:
        try:
            extension = await service.enable(
                extension_id,
                expected_revision=request.expected_revision,
                client_request_id=request.client_request_id,
            )
        except ExtensionError as error:
            raise _http_error(error) from error
        return await asyncio.to_thread(_mutation, service, extension)

    @router.post("/{extension_id}/disable", response_model=ExtensionMutationResponse)
    def disable(
        extension_id: str, request: ExtensionMutationRequest
    ) -> ExtensionMutationResponse:
        try:
            extension = service.disable(
                extension_id,
                expected_revision=request.expected_revision,
                client_request_id=request.client_request_id,
            )
        except ExtensionError as error:
            raise _http_error(error) from error
        return _mutation(service, extension)

    @router.post("/{extension_id}/uninstall", response_model=ExtensionMutationResponse)
    def uninstall(
        extension_id: str, request: ExtensionMutationRequest
    ) -> ExtensionMutationResponse:
        try:
            extension = service.uninstall(
                extension_id,
                expected_revision=request.expected_revision,
                client_request_id=request.client_request_id,
            )
        except ExtensionError as error:
            raise _http_error(error) from error
        return _mutation(service, extension)

    @router.post("/{extension_id}/configure", response_model=ExtensionMutationResponse)
    def configure(
        extension_id: str, request: SkillConfigurationRequest
    ) -> ExtensionMutationResponse:
        try:
            extension = service.configure_skill(
                extension_id,
                values=request.values,
                expected_revision=request.expected_revision,
                client_request_id=request.client_request_id,
            )
        except ExtensionError as error:
            raise _http_error(error) from error
        return _mutation(service, extension)

    @router.post("/{extension_id}/health", response_model=ExtensionMutationResponse)
    async def health(
        extension_id: str, request: ExtensionMutationRequest
    ) -> ExtensionMutationResponse:
        try:
            extension = await service.check_health(
                extension_id,
                expected_revision=request.expected_revision,
                client_request_id=request.client_request_id,
            )
        except ExtensionError as error:
            raise _http_error(error) from error
        return await asyncio.to_thread(_mutation, service, extension)

    @router.post("/{extension_id}/rollback", response_model=ExtensionMutationResponse)
    def rollback(
        extension_id: str, request: ExtensionMutationRequest
    ) -> ExtensionMutationResponse:
        try:
            extension = service.rollback(
                extension_id,
                expected_revision=request.expected_revision,
                client_request_id=request.client_request_id,
            )
        except ExtensionError as error:
            raise _http_error(error) from error
        return _mutation(service, extension)

    app.include_router(router)


def _snapshot(service: ExtensionService) -> ExtensionCatalogSnapshot:
    return ExtensionCatalogSnapshot.model_validate(
        service.project_snapshot().to_dict()
    )


def _projection(value) -> ExtensionProjection:
    return ExtensionProjection.model_validate(value.to_dict())


def _mutation(service: ExtensionService, extension) -> ExtensionMutationResponse:
    return ExtensionMutationResponse(
        extension=_projection(extension),
        extensions=_snapshot(service),
    )


def _http_error(error: ExtensionError) -> HTTPException:
    status = 422
    detail: dict[str, object] = {
        "code": error.code,
        "message": str(error),
    }
    if isinstance(error, ExtensionNotFound):
        status = 404
    elif isinstance(error, (ExtensionRevisionConflict, ExtensionIdempotencyConflict)):
        status = 409
    elif isinstance(error, ExtensionIntegrityError):
        status = 503
    elif isinstance(error, (ExtensionVerificationError, ExtensionActionUnavailable)):
        status = 422
    if isinstance(error, ExtensionRevisionConflict):
        detail["current_revision"] = error.current_revision
    return HTTPException(status_code=status, detail=detail)


__all__ = [
    "ExtensionMutationRequest",
    "LocalSkillInstallRequest",
    "SkillConfigurationRequest",
    "register_extension_routes",
]
