"""Model-visible entry into the Runtime-owned external connection lifecycles."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Mapping

from agent.tools.base_tool import BaseTool, ToolResult


_TENCENT_DOCS_ENDPOINT = "https://docs.qq.com/openapi/mcp"
_FLOW_ID = re.compile(r"^[A-Za-z0-9_-]{8,192}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_runtime: "ExternalConnectionRuntime | None" = None


def bind_external_connection_runtime(runtime: "ExternalConnectionRuntime | None") -> None:
    global _runtime
    _runtime = runtime


def _public_error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    detail = getattr(error, "detail", None)
    if isinstance(detail, Mapping) and isinstance(detail.get("code"), str):
        return str(detail["code"])
    if (
        isinstance(error, ValueError)
        and error.args
        and isinstance(error.args[0], str)
        and _ERROR_CODE.fullmatch(error.args[0])
    ):
        return error.args[0]
    return "external_connection_unavailable"


def _owner_prefix(value: str) -> str:
    return "agent-connect-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] + "-"


def _request_id(owner_id: str, request_id: str) -> str:
    return _owner_prefix(owner_id) + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]


class ExternalConnectionRuntime:
    """Thin adapter; every state transition remains owned by its Cow service."""

    def __init__(
        self,
        *,
        connector_service: Any,
        connector_repository: Any,
        connector_oauth_return_uri: str,
        channel_service: Any,
        mcp_service: Any,
    ) -> None:
        self.connector_service = connector_service
        self.connector_repository = connector_repository
        self.connector_oauth_return_uri = connector_oauth_return_uri
        self.channel_service = channel_service
        self.mcp_service = mcp_service

    def execute(
        self,
        provider: str,
        action: str,
        *,
        workspace: Path,
        flow_id: str | None,
        request_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        if provider == "feishu":
            return self._feishu(
                action, flow_id=flow_id, request_id=request_id, owner_id=owner_id
            )
        if provider == "tencent-docs":
            return self._tencent_docs(action, workspace=workspace)
        if provider in {"weixin", "dingtalk"}:
            return self._channel(
                provider,
                action,
                workspace=workspace,
                flow_id=flow_id,
                request_id=request_id,
            )
        raise ValueError("external_connection_provider_invalid")

    def _feishu(
        self,
        action: str,
        *,
        flow_id: str | None,
        request_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        if action == "start":
            from ecorex.connectors.models import ConnectorAuthKind

            challenge = asyncio.run(
                self.connector_service.begin_connect(
                    "feishu",
                    auth_kind=ConnectorAuthKind.OAUTH2,
                    return_uri=self.connector_oauth_return_uri,
                    client_request_id=_request_id(owner_id, request_id),
                )
            )
            return {
                "provider": "feishu",
                "state": "awaiting_callback",
                "ready": False,
                "requires_user_handoff": True,
                **challenge.to_dict(),
            }
        if action == "poll":
            if not flow_id or not _FLOW_ID.fullmatch(flow_id):
                raise ValueError("external_connection_flow_id_invalid")
            if not self.connector_repository.lifecycle_owner_has_flow(
                _owner_prefix(owner_id), flow_id
            ):
                raise ValueError("external_connection_flow_not_owned")
            completion = self.connector_repository.auth_completion_for_flow(flow_id)
            if completion is not None:
                connector_id, instance_id = completion
                instance = self.connector_repository.get_instance(instance_id)
                connected = bool(
                    connector_id == "feishu"
                    and instance is not None
                    and instance.health.value in {"connected", "degraded"}
                )
                return {
                    "provider": "feishu",
                    "flow_id": flow_id,
                    "state": "connected" if connected else "authorization_unavailable",
                    "ready": connected,
                    "instance_id": instance_id if connected else None,
                }
            active = self.connector_repository.get_active_flow(flow_id)
            return {
                "provider": "feishu",
                "flow_id": flow_id,
                "state": (
                    "awaiting_callback"
                    if active is not None and active.connector_id == "feishu"
                    else "authorization_unavailable"
                ),
                "ready": False,
            }
        item = next(
            (
                value
                for value in self.connector_service.catalog()
                if value.definition.connector_id == "feishu"
            ),
            None,
        )
        if item is None:
            return {"provider": "feishu", "state": "unavailable", "ready": False}
        instances = [instance.to_dict() for instance in item.instances]
        connected = any(
            instance["health"] in {"connected", "degraded"} for instance in instances
        )
        return {
            "provider": "feishu",
            "state": "connected" if connected else "authorization_required",
            "ready": connected,
            "adapter_available": item.adapter_available,
            "unavailable_reason": item.unavailable_reason,
            "instances": instances,
        }

    def _tencent_docs(self, action: str, *, workspace: Path) -> dict[str, Any]:
        from ecorex.extensions.user_mcp import UserMCPServerRequest

        service = self.mcp_service.for_workspace(workspace)
        server = next(
            (item for item in service.list() if item["endpoint"] == _TENCENT_DOCS_ENDPOINT),
            None,
        )
        if action == "start" and server is None:
            server = service.create(
                UserMCPServerRequest(
                    display_name="tencent-docs",
                    endpoint=_TENCENT_DOCS_ENDPOINT,
                    auth_kind="oauth2",
                    oauth_scope="docs:read docs:write",
                    authorization_hosts=["docs.qq.com"],
                )
            )
        if server is None:
            return {
                "provider": "tencent-docs",
                "state": "unconfigured",
                "ready": False,
            }
        if action == "start":
            if server["auth_kind"] != "oauth2":
                return {
                    "provider": "tencent-docs",
                    "state": "requires_configuration",
                    "ready": False,
                    "requires_user_handoff": True,
                    "auth_kind": server["auth_kind"],
                }
            return {
                "provider": "tencent-docs",
                "ready": False,
                "requires_user_handoff": True,
                **service.begin_oauth(server["server_id"]),
            }
        return self._tencent_status(service, server)

    @staticmethod
    def _tencent_status(service: Any, server: Mapping[str, Any]) -> dict[str, Any]:
        oauth = next(
            (
                item
                for item in service.oauth_items()
                if item["service_id"] == server["server_id"]
            ),
            None,
        )
        authorized = bool(
            oauth and oauth["state"] == "authorized"
        ) or bool(server["auth_kind"] == "bearer" and server["credential_configured"])
        ready = bool(authorized and server["tool_count"])
        return {
            "provider": "tencent-docs",
            "state": (
                "connected"
                if ready
                else "starting"
                if authorized
                else "authorization_required"
            ),
            "ready": ready,
            "auth_kind": server["auth_kind"],
            "server_id": server["server_id"],
            "tool_count": server["tool_count"],
            "tool_names": server["tool_names"],
        }

    def _channel(
        self,
        provider: str,
        action: str,
        *,
        workspace: Path,
        flow_id: str | None,
        request_id: str,
    ) -> dict[str, Any]:
        item = next(
            (
                value
                for value in self.channel_service.catalog()["items"]
                if value["channel_id"] == provider
            ),
            None,
        )
        if item is None:
            return {"provider": provider, "state": "unavailable", "ready": False}
        if provider == "weixin" and action in {"start", "poll"}:
            if action == "start":
                result = self.channel_service.begin_authorization(
                    "weixin", request_id=request_id
                )
            else:
                if not flow_id or not _FLOW_ID.fullmatch(flow_id):
                    raise ValueError("external_connection_flow_id_invalid")
                result = self.channel_service.poll_authorization(
                    "weixin", flow_id, request_id=request_id
                )
            return self._weixin_result(result, workspace)
        instance = item.get("instance")
        connected = bool(instance and instance.get("state") == "connected")
        if provider == "dingtalk" and action == "start" and not connected:
            missing = list((instance or {}).get("missing_fields") or [])
            if not missing and item["actions"].get("enable"):
                instance = self.channel_service.enable("dingtalk", request_id=request_id)
                connected = bool(instance and instance.get("state") == "connected")
            if not connected:
                return {
                    "provider": provider,
                    "state": "requires_configuration",
                    "ready": False,
                    "requires_user_handoff": True,
                    "fields": [
                        {
                            key: field.get(key)
                            for key in ("key", "label", "required", "secret", "configured")
                        }
                        for field in item.get("fields", [])
                    ],
                }
        return {
            "provider": provider,
            "state": "connected" if connected else "authorization_required",
            "ready": connected,
            "instance": instance,
        }

    @staticmethod
    def _weixin_result(result: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
        payload = {key: value for key, value in result.items() if key != "qr_image_data_url"}
        qr_data = result.get("qr_image_data_url")
        if qr_data:
            try:
                header, encoded = str(qr_data).split(",", 1)
                if header != "data:image/png;base64":
                    raise ValueError
                content = base64.b64decode(encoded, validate=True)
                if not content.startswith(b"\x89PNG\r\n\x1a\n") or len(content) > 2_000_000:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("weixin_qr_artifact_invalid") from None
            root = workspace / ".ecorex" / "connection-artifacts"
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                root.resolve().relative_to(workspace.resolve())
            except ValueError:
                raise ValueError("weixin_qr_artifact_path_invalid") from None
            os.chmod(root, 0o700)
            target = root / f"weixin-{hashlib.sha256(str(result['flow_id']).encode()).hexdigest()[:24]}.png"
            temporary = root / f".{target.name}.{os.urandom(6).hex()}.tmp"
            try:
                temporary.write_bytes(content)
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            payload["qr_artifact_path"] = str(target.resolve())
        status = str(result.get("status") or "")
        state = {
            "pending": "awaiting_scan",
            "scanned": "scanned",
            "confirmed": "connected",
            "expired": "expired",
            "cancelled": "cancelled",
        }.get(status, "authorization_unavailable")
        payload.update(
            provider="weixin",
            state=state,
            ready=status == "confirmed",
            requires_user_handoff=status in {"pending", "scanned"},
        )
        return payload


class ExternalConnectionsTool(BaseTool):
    name = "external_connections"
    description = (
        "Start or check the Runtime-owned Feishu, Tencent Docs, Weixin, or DingTalk "
        "connection lifecycle. This is the only authentication entry; it never accepts secrets."
    )
    params = {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "enum": ["feishu", "tencent-docs", "weixin", "dingtalk"],
            },
            "action": {"type": "string", "enum": ["status", "start", "poll"]},
            "flow_id": {"type": "string", "description": "Owned flow id returned by start."},
        },
        "required": ["provider", "action"],
    }

    def apply_config(self, config: dict) -> None:
        super().apply_config(config)
        self.cwd = str(config.get("cwd") or self.cwd or os.getcwd())

    def execute(self, params: dict) -> ToolResult:
        provider = str(params.get("provider") or "").strip().lower()
        action = str(params.get("action") or "").strip().lower()
        if provider not in {"feishu", "tencent-docs", "weixin", "dingtalk"}:
            return ToolResult.fail({"error_code": "external_connection_provider_invalid"})
        if action not in {"status", "start", "poll"}:
            return ToolResult.fail({"error_code": "external_connection_action_invalid"})
        if _runtime is None:
            return ToolResult.fail({"error_code": "external_connection_runtime_unavailable"})
        context = getattr(self, "context", None)
        request_id = str(
            getattr(context, "_current_request_id", None)
            or getattr(self, "tool_call_id", None)
            or "agent"
        )
        owner_id = str(getattr(context, "_current_session_id", None) or request_id)
        try:
            result = _runtime.execute(
                provider,
                action,
                workspace=Path(self.cwd or os.getcwd()).expanduser().resolve(),
                flow_id=str(params.get("flow_id") or "") or None,
                request_id=request_id,
                owner_id=owner_id,
            )
        except Exception as error:
            return ToolResult.fail(
                {
                    "provider": provider,
                    "action": action,
                    "state": "unavailable",
                    "ready": False,
                    "error_code": _public_error_code(error),
                    "retryable": False,
                }
            )
        return ToolResult.success(result)
