"""Authenticated local Runtime bridge for the global e-Mate Skill Hub."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import re
from typing import Any, Protocol

from fastapi import APIRouter, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from ecorex.protocol import (
    ExtensionCatalogSnapshot,
    ExtensionMutationResponse,
    ExtensionProjection,
    SkillHubCardProjection,
    SkillHubDetailProjection,
    SkillHubListResponse,
)

from .errors import ExtensionError
from .service import ExtensionService
from .skill_migration import EXCLUDED_SKILL_SLUGS, SKILL_ALIASES


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,95}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SkillHubCloudClient(Protocol):
    async def list_skills(
        self, *, query: str, category: str | None, tag: str | None,
        source: str | None, cursor: str | None, limit: int
    ) -> Mapping[str, Any]: ...

    async def skill_detail(self, *, slug: str) -> Mapping[str, Any]: ...

    async def download_package(self, *, slug: str, version: str) -> tuple[bytes, str]: ...

    async def upload_skill(
        self,
        *,
        slug: str,
        category: str,
        bundle_base64: str,
        client_request_id: str,
    ) -> Mapping[str, Any]: ...

    async def create_install_intent(
        self, *, slug: str, version: str, package_sha256: str, client_request_id: str
    ) -> Mapping[str, Any]: ...

    async def consume_install_intent(self, *, install_intent: str) -> Mapping[str, Any]: ...

    async def complete_install_intent(
        self, *, completion_receipt: str, status: str
    ) -> None: ...


class SkillHubInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=5, max_length=64)
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_request_id: str = Field(min_length=8, max_length=192)
    install_intent: str | None = Field(default=None, min_length=64, max_length=4096)


class SkillHubUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,95}$")
    category: str = Field(pattern=r"^(third_party|content_creation|office_productivity)$")
    bundle_base64: str = Field(min_length=4, max_length=96 * 1024 * 1024)
    client_request_id: str = Field(min_length=8, max_length=192)


def register_skill_hub_runtime_routes(
    app: FastAPI,
    *,
    client: SkillHubCloudClient,
    extensions: ExtensionService,
) -> None:
    router = APIRouter(prefix="/api/v1/skill-hub", tags=["skill-hub"])

    @router.get("/skills", response_model=SkillHubListResponse)
    async def list_skills(
        query: str = Query(default="", max_length=128),
        category: str | None = Query(default=None, max_length=32),
        tag: str | None = Query(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$"),
        source: str | None = Query(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$"),
        cursor: str | None = Query(default=None, max_length=96),
        limit: int = Query(default=24, ge=1, le=100),
    ) -> SkillHubListResponse:
        try:
            cloud = SkillHubListResponse.model_validate(
                await client.list_skills(
                    query=query, category=category, tag=tag, source=source,
                    cursor=cursor, limit=limit
                )
            )
        except Exception as error:
            raise HTTPException(status_code=503, detail="Skill Hub is unavailable") from error
        installed = {item.extension_id: item for item in extensions.catalog()}
        items: list[SkillHubCardProjection] = []
        for card in cloud.items:
            items.append(_with_local_state(extensions, installed, card))
        return SkillHubListResponse(items=items, next_cursor=cloud.next_cursor)

    @router.get("/skills/{slug}", response_model=SkillHubDetailProjection)
    async def skill_detail(slug: str) -> SkillHubDetailProjection:
        if not _SLUG.fullmatch(slug):
            raise HTTPException(status_code=404, detail="Skill was not found")
        try:
            detail = SkillHubDetailProjection.model_validate(
                await client.skill_detail(slug=slug)
            )
        except Exception as error:
            raise HTTPException(status_code=503, detail="Skill Hub is unavailable") from error
        installed = {item.extension_id: item for item in extensions.catalog()}
        return detail.model_copy(
            update={
                "skill": _with_local_state(extensions, installed, detail.skill),
                "versions": [
                    _with_local_state(extensions, installed, card)
                    for card in detail.versions
                ],
            }
        )

    @router.post("/skills/{slug}/install", response_model=ExtensionMutationResponse)
    async def install(slug: str, request: SkillHubInstallRequest) -> ExtensionMutationResponse:
        if not _SLUG.fullmatch(slug):
            raise HTTPException(status_code=404, detail="Skill was not found")
        if slug in EXCLUDED_SKILL_SLUGS:
            raise HTTPException(status_code=404, detail="Skill was not found")
        completion_receipt: str | None = None
        try:
            intent = request.install_intent
            if intent is None:
                created_intent = await client.create_install_intent(
                    slug=slug,
                    version=request.version,
                    package_sha256=request.package_sha256,
                    client_request_id=request.client_request_id,
                )
                intent = str(created_intent.get("install_intent", ""))
            claimed = await client.consume_install_intent(install_intent=intent)
            if any(
                claimed.get(key) != expected
                for key, expected in (
                    ("slug", slug),
                    ("version", request.version),
                    ("package_sha256", request.package_sha256),
                )
            ):
                raise ValueError("Skill Hub install intent identity changed")
            completion_receipt = str(claimed.get("completion_receipt", ""))
            if len(completion_receipt) < 64:
                raise ValueError("Skill Hub install completion authority is invalid")
            extension_id = _extension_id(extensions, slug)
            existing_state = extensions.repository.state(extension_id)
            existing = extensions.projection(extension_id) if existing_state else None
            if extension_id.startswith("skill.") and existing is not None:
                if existing.status == "uninstalled":
                    raise ValueError("Canonical Skill provider is uninstalled")
                if existing.status == "enabled":
                    await client.complete_install_intent(
                        completion_receipt=completion_receipt, status="installed"
                    )
                    return _mutation(extensions, existing)
                enabled = await extensions.enable(
                    extension_id,
                    expected_revision=existing.revision,
                    client_request_id=f"hub-enable:{hashlib.sha256(request.client_request_id.encode()).hexdigest()[:32]}",
                )
                await client.complete_install_intent(
                    completion_receipt=completion_receipt, status="installed"
                )
                return _mutation(extensions, enabled)
            if (
                existing is not None
                and existing.active_version == request.version
                and existing.active_digest == request.package_sha256
            ):
                if existing.status == "enabled":
                    await client.complete_install_intent(
                        completion_receipt=completion_receipt, status="installed"
                    )
                    return _mutation(extensions, existing)
                enabled = await extensions.enable(
                    extension_id,
                    expected_revision=existing.revision,
                    client_request_id=f"hub-enable:{hashlib.sha256(request.client_request_id.encode()).hexdigest()[:32]}",
                )
                await client.complete_install_intent(
                    completion_receipt=completion_receipt, status="installed"
                )
                return _mutation(extensions, enabled)
            payload, expected_digest = await client.download_package(
                slug=slug, version=request.version
            )
            if not hmac.compare_digest(expected_digest, request.package_sha256):
                raise ValueError("Skill Hub package identity changed")
            if extensions.local_bundle_store is None:
                raise ValueError("Skill storage is unavailable")
            normalized = extensions.local_bundle_store.ingest_zip(payload)
            if not hmac.compare_digest(normalized.artifact_sha256, request.package_sha256):
                raise ValueError("Skill package digest is invalid")
            if normalized.metadata.version != request.version:
                raise ValueError("Skill package version does not match the requested version")
            state = extensions.repository.state(extension_id)
            request_digest = hashlib.sha256(request.client_request_id.encode()).hexdigest()[:32]
            staged = extensions.install_local_skill_zip(
                payload,
                extension_id=extension_id,
                expected_revision=state.revision if state else 0,
                client_request_id=f"hub-install:{request_digest}:stage",
            )
            enabled = await extensions.enable(
                extension_id,
                expected_revision=staged.revision,
                client_request_id=f"hub-install:{request_digest}:enable",
            )
            if enabled.status != "enabled":
                raise ValueError("Skill did not become ready")
            await client.complete_install_intent(
                completion_receipt=completion_receipt, status="installed"
            )
        except ExtensionError as error:
            if completion_receipt:
                await _complete_failed(client, completion_receipt)
            raise HTTPException(status_code=422, detail={"code": error.code, "message": str(error)}) from error
        except Exception as error:
            if completion_receipt:
                await _complete_failed(client, completion_receipt)
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _mutation(extensions, enabled)

    @router.get("/skills/{slug}/versions/{version}/package")
    async def download(slug: str, version: str) -> Response:
        if not _SLUG.fullmatch(slug) or not _VERSION.fullmatch(version):
            raise HTTPException(status_code=404, detail="Skill package was not found")
        try:
            catalog = SkillHubDetailProjection.model_validate(
                await client.skill_detail(slug=slug)
            )
            card = next(
                item
                for item in catalog.versions
                if item.slug == slug and item.version == version
            )
            payload, digest = await client.download_package(slug=slug, version=version)
            if not hmac.compare_digest(digest, card.package_sha256):
                raise ValueError("Skill Hub package identity changed")
        except (StopIteration, ValueError):
            raise HTTPException(status_code=404, detail="Skill package was not found") from None
        except Exception as error:
            raise HTTPException(status_code=503, detail="Skill Hub is unavailable") from error
        return Response(
            payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{slug}-{version}.zip"',
                "X-Skill-Content-SHA256": digest,
                "Cache-Control": "no-store",
            },
        )

    @router.post("/skills", response_model=SkillHubCardProjection, status_code=201)
    async def upload(request: SkillHubUploadRequest) -> SkillHubCardProjection:
        try:
            return SkillHubCardProjection.model_validate(
                await client.upload_skill(**request.model_dump())
            )
        except Exception as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    app.include_router(router)


def _mutation(service: ExtensionService, extension) -> ExtensionMutationResponse:
    return ExtensionMutationResponse(
        extension=ExtensionProjection.model_validate(extension.to_dict()),
        extensions=ExtensionCatalogSnapshot.model_validate(
            service.project_snapshot().to_dict()
        ),
    )


def _extension_id(service: ExtensionService, slug: str) -> str:
    canonical = SKILL_ALIASES.get(slug, slug)
    native = f"skill.{canonical}"
    return native if service.repository.state(native) is not None else f"hub.{canonical}"


def _with_local_state(
    service: ExtensionService,
    installed: Mapping[str, Any],
    card: SkillHubCardProjection,
) -> SkillHubCardProjection:
    local = installed.get(_extension_id(service, card.slug))
    if local is None:
        return card
    status = (
        "installed_enabled" if local.status == "enabled"
        else "uninstalled" if local.status == "uninstalled"
        else "installed_disabled"
    )
    return card.model_copy(
        update={"installation_status": status, "readiness": local.readiness}
    )


async def _complete_failed(client: SkillHubCloudClient, receipt: str) -> None:
    try:
        await client.complete_install_intent(
            completion_receipt=receipt, status="failed"
        )
    except Exception:
        pass


__all__ = ["SkillHubCloudClient", "register_skill_hub_runtime_routes"]
