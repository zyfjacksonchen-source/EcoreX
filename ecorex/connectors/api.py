"""Mountable `/api/v1/connectors` router for the product Runtime.

Authentication, Origin, and CSRF enforcement intentionally remain owned by the
parent Runtime middleware. This router never accepts credential material or an
OAuth callback URI from the WebUI.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import json
import secrets
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from .errors import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorIdempotencyConflict,
    ConnectorIdempotencyRequired,
    ConnectorInputInvalid,
    ConnectorInvocationUncertain,
    ConnectorNotFound,
    ConnectorPermissionDenied,
    ConnectorUnavailable,
)
from .models import ConnectorAuthKind, ConnectorInstance
from .service import ConnectorService


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BeginConnectorAuthRequest(_StrictModel):
    auth_kind: ConnectorAuthKind


class ReauthorizeConnectorRequest(_StrictModel):
    auth_kind: ConnectorAuthKind


class CompleteConnectorAuthRequest(_StrictModel):
    response: dict[str, str] = Field(min_length=1, max_length=32)


class InvokeConnectorActionRequest(_StrictModel):
    inputs: dict[str, Any]
    idempotency_key: str | None = Field(default=None, max_length=512)


class ResolveConnectorOperationRequest(_StrictModel):
    resolution: Literal["manually_reconciled", "confirmed_not_executed"]


HardDenyProvider = Callable[[str, str], frozenset[str]]


def create_connector_router(
    service: ConnectorService,
    *,
    oauth_return_uri: str,
    hard_deny_provider: HardDenyProvider | None = None,
    disconnect_drain_timeout: float = 30.0,
) -> APIRouter:
    """Create a thin router whose decisions remain backend-authoritative."""

    if oauth_return_uri not in service.allowed_return_uris:
        raise ValueError("connector router callback URI is not service-allowlisted")
    if disconnect_drain_timeout <= 0:
        raise ValueError("disconnect_drain_timeout must be positive")
    policy = hard_deny_provider or (lambda _instance_id, _action_id: frozenset())
    router = APIRouter(prefix="/connectors", tags=["connectors"])

    @router.get("")
    async def catalog() -> dict[str, Any]:
        items = await asyncio.to_thread(service.catalog)
        return {
            "contract_version": "1.0",
            "items": [item.to_dict() for item in items],
        }

    @router.post("/{connector_id}/auth/begin")
    async def begin_auth(
        connector_id: str,
        request: BeginConnectorAuthRequest,
        client_request_id: str | None = Header(
            default=None,
            alias="X-EcoreX-Client-Request-ID",
        ),
    ) -> dict[str, Any]:
        try:
            challenge = await service.begin_connect(
                connector_id,
                auth_kind=request.auth_kind,
                return_uri=oauth_return_uri,
                client_request_id=client_request_id,
            )
        except ConnectorError as exc:
            _raise_connector_http(exc)
        return challenge.to_dict()

    @router.post("/auth/{flow_id}/complete")
    async def complete_auth(
        flow_id: str,
        request: CompleteConnectorAuthRequest,
    ) -> dict[str, Any]:
        try:
            instance = await service.complete_connect(flow_id, request.response)
        except ConnectorError as exc:
            _raise_connector_http(exc)
        return _instance_projection(service, instance)

    @router.get("/oauth/callback")
    async def oauth_callback(request: Request) -> Any:
        wants_html = "text/html" in request.headers.get("accept", "").casefold()
        try:
            if len(request.query_params) > 32:
                raise ConnectorAuthError("OAuth callback is too large")
            callback_values: dict[str, str] = {}
            for key, value in request.query_params.multi_items():
                if key in callback_values or len(key) > 128 or len(value) > 4096:
                    raise ConnectorAuthError("invalid OAuth callback")
                callback_values[key] = value
            state_value = callback_values.get("state")
            if not state_value:
                raise ConnectorAuthError("OAuth state is missing")
            flow_id = await asyncio.to_thread(
                service.repository.flow_id_for_oauth_state, state_value
            )
            if flow_id is None:
                raise ConnectorAuthError("OAuth state is invalid or expired")
            instance = await service.complete_connect(flow_id, callback_values)
            projection = _instance_projection(service, instance)
        except ConnectorError as exc:
            if wants_html:
                return _oauth_callback_html(
                    oauth_return_uri=oauth_return_uri,
                    error=exc,
                )
            _raise_connector_http(exc)
        if wants_html:
            return _oauth_callback_html(
                oauth_return_uri=oauth_return_uri,
                projection=projection,
            )
        return projection

    @router.post("/instances/{instance_id}/reauthorize")
    async def reauthorize(
        instance_id: str,
        request: ReauthorizeConnectorRequest,
        client_request_id: str | None = Header(
            default=None,
            alias="X-EcoreX-Client-Request-ID",
        ),
    ) -> dict[str, Any]:
        try:
            challenge = await service.begin_reauthorize(
                instance_id,
                auth_kind=request.auth_kind,
                return_uri=oauth_return_uri,
                client_request_id=client_request_id,
            )
        except ConnectorError as exc:
            _raise_connector_http(exc)
        return challenge.to_dict()

    @router.post("/instances/{instance_id}/health")
    async def refresh_health(
        instance_id: str,
        client_request_id: str | None = Header(
            default=None,
            alias="X-EcoreX-Client-Request-ID",
        ),
    ) -> dict[str, Any]:
        try:
            instance = await service.refresh_health(
                instance_id,
                client_request_id=client_request_id,
            )
        except ConnectorError as exc:
            _raise_connector_http(exc)
        return _instance_projection(service, instance)

    @router.delete(
        "/instances/{instance_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def disconnect(
        instance_id: str,
        client_request_id: str | None = Header(
            default=None,
            alias="X-EcoreX-Client-Request-ID",
        ),
    ) -> Response:
        try:
            await service.disconnect(
                instance_id,
                drain_timeout=disconnect_drain_timeout,
                client_request_id=client_request_id,
            )
        except ConnectorError as exc:
            _raise_connector_http(exc)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/instances/{instance_id}/actions/{action_id}")
    async def invoke(
        instance_id: str,
        action_id: str,
        request: InvokeConnectorActionRequest,
    ) -> dict[str, Any]:
        try:
            admin_hard_denies = await asyncio.to_thread(
                policy, instance_id, action_id
            )
            result = await service.invoke(
                instance_id,
                action_id,
                request.inputs,
                idempotency_key=request.idempotency_key,
                admin_hard_denies=admin_hard_denies,
                admin_hard_denies_provider=lambda: policy(
                    instance_id, action_id
                ),
            )
        except ConnectorError as exc:
            _raise_connector_http(exc)
        if not isinstance(result, dict):
            # Public built-in action contracts all have object roots.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": "connector_invalid_result", "message": "连接器返回无效"},
            )
        return result

    @router.get("/instances/{instance_id}/uncertain-operations")
    async def uncertain_operations(instance_id: str) -> dict[str, Any]:
        state = await asyncio.to_thread(
            service.repository.get_instance_state, instance_id
        )
        if state is None:
            _raise_connector_http(
                ConnectorNotFound(f"unknown connector instance: {instance_id!r}")
            )
        return {
            "instance_id": instance_id,
            "operation_ids": list(
                await asyncio.to_thread(
                    service.repository.uncertain_operation_ids, instance_id
                )
            ),
        }

    @router.post(
        "/instances/{instance_id}/uncertain-operations/{operation_id}/resolve",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def resolve_uncertain_operation(
        instance_id: str,
        operation_id: str,
        request: ResolveConnectorOperationRequest,
    ) -> Response:
        try:
            await asyncio.to_thread(
                service.repository.resolve_uncertain_operation,
                instance_id,
                operation_id,
                resolution=request.resolution,
            )
        except KeyError:
            _raise_connector_http(
                ConnectorNotFound("unknown uncertain connector operation")
            )
        await service.publish_pending_best_effort()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def _instance_projection(
    service: ConnectorService,
    instance: ConnectorInstance,
) -> dict[str, Any]:
    definition = service.registry.definition(instance.connector_id)
    return instance.to_projection(definition).to_dict()


def _raise_connector_http(error: ConnectorError) -> None:
    http_status = _connector_http_status(error)
    raise HTTPException(
        status_code=http_status,
        detail={
            "code": error.code,
            "message": _connector_public_message(error.code),
        },
    ) from None


def _connector_http_status(error: ConnectorError) -> int:
    if isinstance(error, ConnectorNotFound):
        return status.HTTP_404_NOT_FOUND
    elif isinstance(error, ConnectorPermissionDenied):
        return status.HTTP_403_FORBIDDEN
    elif isinstance(error, (ConnectorIdempotencyConflict, ConnectorInvocationUncertain)):
        return status.HTTP_409_CONFLICT
    elif isinstance(error, ConnectorInputInvalid):
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    elif isinstance(error, (ConnectorAuthError, ConnectorIdempotencyRequired)):
        return status.HTTP_400_BAD_REQUEST
    elif isinstance(error, ConnectorUnavailable):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _connector_public_message(error_code: str) -> str:
    messages = {
        "connector_not_found": "连接器或实例不存在",
        "connector_permission_denied": "连接器操作被权限策略阻止",
        "connector_idempotency_conflict": "请求幂等键冲突",
        "connector_invocation_uncertain": "连接器操作结果不确定，需要人工确认",
        "connector_auth_error": "连接器授权失败或已失效",
        "connector_idempotency_required": "写操作需要幂等键",
        "connector_input_invalid": "连接器操作参数不符合要求",
        "connector_unavailable": "连接器暂不可用",
    }
    return messages.get(error_code, "连接器请求失败")


def _oauth_callback_html(
    *,
    oauth_return_uri: str,
    projection: Mapping[str, Any] | None = None,
    error: ConnectorError | None = None,
) -> HTMLResponse:
    succeeded = projection is not None and error is None
    parsed = urlsplit(oauth_return_uri)
    exact_origin = f"{parsed.scheme}://{parsed.netloc}"
    nonce = secrets.token_urlsafe(24)
    if succeeded:
        machine_payload = {
            "source": "ecorex.connector.oauth",
            "status": "completed",
            "connector_id": str(projection.get("connector_id", "")),
            "instance_id": str(projection.get("instance_id", "")),
        }
        result = "completed"
        result_code = "ok"
        title = "连接已完成"
        message = "e-Mate 已安全接收授权结果，正在关闭此窗口。"
        http_status = status.HTTP_200_OK
    else:
        assert error is not None
        machine_payload = {
            "source": "ecorex.connector.oauth",
            "status": "failed",
            "error_code": error.code,
        }
        result = "failed"
        result_code = error.code
        title = "连接未完成"
        message = _connector_public_message(error.code) + "，请返回 EcoreX 重试。"
        http_status = _connector_http_status(error)

    payload_json = json.dumps(
        machine_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    origin_json = json.dumps(exact_origin)
    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <style nonce="{nonce}">
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText; }}
    main {{ max-width: 32rem; padding: 2rem; text-align: center; }}
    h1 {{ font-size: 1.25rem; margin: 0 0 .75rem; }}
    p {{ line-height: 1.6; margin: 0; opacity: .76; }}
  </style>
</head>
<body>
  <main><h1>{title}</h1><p>{message}</p></main>
  <script nonce="{nonce}">
    (() => {{
      try {{
        if (window.opener && !window.opener.closed) {{
          window.opener.postMessage({payload_json}, {origin_json});
        }}
      }} finally {{
        window.setTimeout(() => window.close(), 80);
      }}
    }})();
  </script>
</body>
</html>"""
    return HTMLResponse(
        content=content,
        status_code=http_status,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Content-Security-Policy": (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-EcoreX-Connector-Result": result,
            "X-EcoreX-Connector-Code": result_code,
        },
    )


__all__ = [
    "BeginConnectorAuthRequest",
    "CompleteConnectorAuthRequest",
    "HardDenyProvider",
    "InvokeConnectorActionRequest",
    "ReauthorizeConnectorRequest",
    "ResolveConnectorOperationRequest",
    "create_connector_router",
]
