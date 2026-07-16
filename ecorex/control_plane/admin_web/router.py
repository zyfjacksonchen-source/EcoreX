"""Mountable FastAPI router for the content-addressed administrator console."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from ecorex import __version__

from .assets import AdminWebAssetError, AdminWebAssets


_ROUTER_PREFIX = re.compile(r"^(?:/[A-Za-z0-9._-]+)+$")
_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "manifest-src 'none'",
        "worker-src 'none'",
    )
)


def _security_headers(*, cache_control: str) -> dict[str, str]:
    return {
        "Cache-Control": cache_control,
        "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-EcoreX-Product-Version": __version__,
    }


def create_admin_web_router(
    *,
    prefix: str = "/admin",
    external_asset_prefix: str | None = None,
    assets: AdminWebAssets | None = None,
) -> APIRouter:
    normalized_prefix = prefix.rstrip("/")
    if _ROUTER_PREFIX.fullmatch(normalized_prefix) is None:
        raise ValueError("administrator Web prefix is invalid")
    normalized_asset_prefix = (
        external_asset_prefix.rstrip("/")
        if external_asset_prefix is not None
        else f"{normalized_prefix}/assets"
    )
    if _ROUTER_PREFIX.fullmatch(normalized_asset_prefix) is None:
        raise ValueError("administrator external asset prefix is invalid")
    verified = assets or AdminWebAssets.load()
    rendered_index = verified.render_index(normalized_asset_prefix)
    router = APIRouter(prefix=normalized_prefix, include_in_schema=False)

    def index_response() -> HTMLResponse:
        return HTMLResponse(
            rendered_index,
            headers={
                **_security_headers(cache_control="no-store, max-age=0"),
                "Pragma": "no-cache",
                "Surrogate-Control": "no-store",
            },
        )

    router.add_api_route("", index_response, methods=["GET"])
    router.add_api_route("/", index_response, methods=["GET"])

    @router.get("/assets/{asset_name}")
    def asset_response(asset_name: str, request: Request) -> Response:
        asset = verified.get(asset_name)
        if asset is None:
            return Response(
                status_code=404,
                headers=_security_headers(cache_control="no-store, max-age=0"),
            )
        etag = f'"sha256-{asset.digest}"'
        headers = {
            **_security_headers(
                cache_control="public, max-age=31536000, immutable"
            ),
            "ETag": etag,
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(
            asset.content,
            media_type=asset.media_type,
            headers=headers,
        )

    return router


__all__ = ["AdminWebAssetError", "AdminWebAssets", "create_admin_web_router"]
