"""CowAgent 2.1.5-compatible local ``mcp.json`` composition."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import HTMLResponse

from ecorex.capabilities import (
    ApprovalRequirement,
    CapabilityEffect,
    Exposure,
    IdempotencyClass,
    SandboxLevel,
)

from .mcp import (
    MCP_PROTOCOL_VERSION,
    LegacySSEMCPTransport,
    MCPRuntimeBinding,
    MCPStdioTransport,
    MCPToolContract,
    ManagedHTTPMCPTransport,
    discover_mcp_tools,
)
from .models import (
    EXTENSION_CONTRACT_VERSION,
    ExtensionCompatibility,
    ExtensionExport,
    ExtensionExportKind,
    ExtensionExposure,
    ExtensionKind,
    ExtensionManifest,
    ExtensionSignature,
    ExtensionSource,
    ExtensionTransport,
    ExtensionTrust,
    RuntimeBoundary,
    canonical_digest,
    verify_user_configured_mcp,
)
from .user_mcp import UserMCPServerRequest


_CACHE_SCHEMA_VERSION = 1
_HTTP_TYPES = frozenset(
    {"http", "streamable-http", "streamable_http", "streamablehttp"}
)
_ENV_PASSTHROUGH = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "TMPDIR",
    "NODE_PATH",
    "NVM_DIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMDATA",
    "TEMP",
    "TMP",
    "HOMEDRIVE",
    "HOMEPATH",
)
_SENSITIVE_ENV_NAMES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_PASSWD", "_CREDENTIAL")


@dataclass(frozen=True, slots=True)
class _ServerConfig:
    name: str
    transport: str
    payload: Mapping[str, Any]
    digest: str


class CowMCPConfigService:
    """Load cached tools immediately, discover changed servers in background."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        runtime_api_version: str,
        platform: str,
        architecture: str,
        reload_requester: Any | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.runtime_api_version = runtime_api_version
        self.platform = platform
        self.architecture = architecture
        self.reload_requester = reload_requester
        self.poll_seconds = poll_seconds
        self.config_path = _config_path(self.workspace_root)
        self.cache_path = self.workspace_root / ".ecorex" / "mcp-catalog-v1.json"
        self.errors: tuple[str, ...] = ()
        self._signature = _file_signature(self.config_path)
        self._configs = _load_configs(self.config_path)
        self._cache = _read_cache(self.cache_path)
        self._bindings, self._pending = self._compose(self._configs, self._cache)
        self._task: asyncio.Task[None] | None = None

    def bindings(self) -> tuple[MCPRuntimeBinding, ...]:
        return self._bindings

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        if self._pending:
            await self._refresh(self._configs, self._pending)
        while True:
            await asyncio.sleep(self.poll_seconds)
            path = _config_path(self.workspace_root)
            signature = _file_signature(path)
            if path == self.config_path and signature == self._signature:
                continue
            self.config_path = path
            self._signature = signature
            configs = _load_configs(path)
            by_name = {item.name: item for item in configs}
            prior = {item.name: item for item in self._configs}
            changed = tuple(
                item
                for item in configs
                if item.name not in prior or item.digest != prior[item.name].digest
            )
            removed = set(prior) - set(by_name)
            self._configs = configs
            if changed:
                await self._refresh(configs, changed)
            elif removed:
                self._cache = {
                    name: value for name, value in self._cache.items() if name in by_name
                }
                _write_cache(self.cache_path, self._cache)
                self._request_reload("mcp.json")

    async def _refresh(
        self,
        configs: tuple[_ServerConfig, ...],
        pending: tuple[_ServerConfig, ...],
    ) -> None:
        previous_cache = dict(self._cache)
        cache = dict(previous_cache)
        errors: list[str] = []
        for config in pending:
            transport = None
            try:
                transport = await _transport(config, self.workspace_root)
                tools = await discover_mcp_tools(transport)
                cache[config.name] = {
                    "config_digest": config.digest,
                    "tools": [_tool_payload(tool) for tool in tools],
                }
            except Exception as error:
                cache.pop(config.name, None)
                errors.append(f"{config.name}:{getattr(error, 'code', type(error).__name__)}")
            finally:
                if transport is not None:
                    try:
                        await transport.close()
                    except Exception:
                        pass
        names = {item.name for item in configs}
        cache = {name: value for name, value in cache.items() if name in names}
        self._cache = cache
        self.errors = tuple(errors)
        _write_cache(self.cache_path, cache)
        if cache != previous_cache:
            self._request_reload("mcp.json")

    def _request_reload(self, reason: str) -> None:
        if callable(self.reload_requester):
            try:
                self.reload_requester(f"cow-mcp:{reason}")
            except Exception:
                pass

    def _compose(
        self,
        configs: tuple[_ServerConfig, ...],
        cache: Mapping[str, Any],
    ) -> tuple[tuple[MCPRuntimeBinding, ...], tuple[_ServerConfig, ...]]:
        bindings: list[MCPRuntimeBinding] = []
        pending: list[_ServerConfig] = []
        for config in configs:
            cached = cache.get(config.name)
            if not isinstance(cached, Mapping) or cached.get("config_digest") != config.digest:
                pending.append(config)
                continue
            try:
                tools = _decode_tools(cached.get("tools"))
                bindings.append(self._binding(config, tools))
            except (TypeError, ValueError):
                pending.append(config)
        return tuple(bindings), tuple(pending)

    def _binding(
        self,
        config: _ServerConfig,
        tools: tuple[MCPToolContract, ...],
    ) -> MCPRuntimeBinding:
        extension_id = "cow.mcp." + hashlib.sha256(
            config.name.encode("utf-8")
        ).hexdigest()[:32]
        transport = (
            ExtensionTransport.STDIO
            if config.transport == "stdio"
            else ExtensionTransport.STREAMABLE_HTTP
        )
        boundary = (
            RuntimeBoundary.PROCESS
            if transport is ExtensionTransport.STDIO
            else RuntimeBoundary.MANAGED_ADAPTER
        )
        manifest = ExtensionManifest(
            schema_version=1,
            contract_version=EXTENSION_CONTRACT_VERSION,
            extension_id=extension_id,
            version="1.0.0",
            kind=ExtensionKind.MCP_SERVER,
            display_name=config.name,
            description=f"CowAgent local MCP server: {config.name}",
            artifact_sha256=config.digest,
            source=ExtensionSource.USER_CONFIGURATION,
            trust=ExtensionTrust.USER_CONFIGURED,
            runtime_boundary=boundary,
            transport=transport,
            compatibility=ExtensionCompatibility(
                runtime_api=f"={self.runtime_api_version}",
                platforms=(),
                architectures=(),
            ),
            dependencies=(),
            conflicts=(),
            exports=(
                ExtensionExport(
                    export_id=extension_id,
                    kind=ExtensionExportKind.MCP_SERVER,
                    exposure=ExtensionExposure.DIRECT,
                    permission_effects=("execute", "network", "read", "write"),
                ),
            ),
            supported_protocol_versions=(MCP_PROTOCOL_VERSION,),
            upstream_metadata=None,
            signature=ExtensionSignature(
                algorithm="user-mcp-config-sha256",
                key_id="user-mcp-config-v1",
                value=config.digest,
            ),
        )
        verified = verify_user_configured_mcp(
            manifest,
            runtime_api_version=self.runtime_api_version,
            platform=self.platform,
            architecture=self.architecture,
        )

        async def session_factory(_tenant: str) -> Any:
            return await _transport(config, self.workspace_root)

        return MCPRuntimeBinding(
            extension_id=extension_id,
            revision_id=manifest.revision_id,
            artifact_sha256=config.digest,
            transport=transport,
            tools=tools,
            verified_manifest=verified,
            session_factory=session_factory,
            request_timeout_seconds=float(config.payload.get("timeout", 120)),
        )


class CowMCPSettingsService:
    """Settings projection backed by the exact ``mcp.json`` Cow executes."""

    def __init__(self, workspace_root: str | Path, manager: Any) -> None:
        self.default_workspace_root = Path(workspace_root).expanduser().resolve()
        self.workspace_root = self.default_workspace_root
        self.path = self.workspace_root / "mcp.json"
        self.manager = manager

    def bind_workspace(self, workspace_root: str | Path | None) -> None:
        root = (
            self.default_workspace_root
            if workspace_root is None
            else Path(workspace_root).expanduser().resolve()
        )
        self.workspace_root = root
        self.path = root / "mcp.json"
        self.manager.bind_workspace(root)

    @staticmethod
    def _server_id(name: str) -> str:
        return "user.mcp." + hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]

    def _root(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"mcpServers": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(503, detail={"code": "mcp_configuration_corrupt"})
        if not isinstance(value, dict):
            raise HTTPException(503, detail={"code": "mcp_configuration_corrupt"})
        raw = value.get("mcpServers") or value.get("mcp_servers") or {}
        if not isinstance(raw, dict):
            raise HTTPException(503, detail={"code": "mcp_configuration_corrupt"})
        value["mcpServers"] = raw
        value.pop("mcp_servers", None)
        return value

    def _write(self, root: dict[str, Any]) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(root, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self.manager.refresh_mcp_if_changed()

    def _find(self, server_id: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
        root = self._root()
        for name, config in root["mcpServers"].items():
            if self._server_id(name) == server_id and isinstance(config, dict):
                return root, name, config
        raise HTTPException(404, detail={"code": "mcp_server_not_found"})

    def projection(self, name: str, config: Mapping[str, Any]) -> dict[str, Any]:
        tools = sorted(
            tool_name
            for tool_name, tool in self.manager._mcp_tool_instances.items()
            if getattr(tool, "server_name", None) == name
        )
        headers = config.get("headers")
        authorization = headers.get("Authorization") if isinstance(headers, Mapping) else None
        endpoint = str(config.get("url") or config.get("command") or "")
        return {
            "server_id": self._server_id(name),
            "display_name": str(config.get("_emate_display_name") or name),
            "endpoint": endpoint,
            "auth_kind": str(config.get("_emate_auth_kind") or ("bearer" if authorization else "none")),
            "oauth_client_id": config.get("_emate_oauth_client_id"),
            "oauth_scope": str(config.get("_emate_oauth_scope") or ""),
            "authorization_hosts": list(config.get("_emate_authorization_hosts") or []),
            "enabled": config.get("enabled", True) is not False,
            "credential_configured": bool(authorization),
            "tested_at": config.get("_emate_tested_at"),
            "tool_count": len(tools),
            "tool_names": tools,
            "revision": int(config.get("_emate_revision") or 1),
        }

    def list(self) -> list[dict[str, Any]]:
        root = self._root()
        return [
            self.projection(name, config)
            for name, config in root["mcpServers"].items()
            if isinstance(name, str) and isinstance(config, Mapping)
        ]

    def create(self, request: UserMCPServerRequest) -> dict[str, Any]:
        root = self._root()
        name = request.display_name
        if name in root["mcpServers"]:
            raise HTTPException(409, detail={"code": "mcp_server_exists"})
        config = self._request_config(request, previous=None)
        root["mcpServers"][name] = config
        self._write(root)
        return self.projection(name, config)

    def update(self, server_id: str, request: UserMCPServerRequest) -> dict[str, Any]:
        root, name, previous = self._find(server_id)
        config = self._request_config(request, previous=previous)
        config["_emate_revision"] = int(previous.get("_emate_revision") or 1) + 1
        root["mcpServers"][name] = config
        self._write(root)
        return self.projection(name, config)

    def set_enabled(self, server_id: str, enabled: bool) -> dict[str, Any]:
        root, name, config = self._find(server_id)
        config["enabled"] = enabled
        config["_emate_revision"] = int(config.get("_emate_revision") or 1) + 1
        self._write(root)
        return self.projection(name, config)

    def remove(self, server_id: str) -> None:
        from agent.tools.mcp.mcp_oauth import clear_server_record

        root, name, _config = self._find(server_id)
        root["mcpServers"].pop(name, None)
        clear_server_record(name)
        self._write(root)

    def test(self, server_id: str) -> dict[str, Any]:
        root, name, config = self._find(server_id)
        self.manager.ensure_mcp_configured_loaded(
            wait_seconds=5.0,
            poll_interval_seconds=0.05,
            server_name=name,
        )
        state = self.manager.list_mcp_status().get(name)
        if state != "ready":
            code = "mcp_oauth_authorization_required" if state == "needs_auth" else "mcp_server_test_failed"
            raise HTTPException(422, detail={"code": code})
        config["_emate_tested_at"] = int(time.time())
        config["_emate_revision"] = int(config.get("_emate_revision") or 1) + 1
        root["mcpServers"][name] = config
        self._write(root)
        return self.projection(name, config)

    def oauth_items(self) -> list[dict[str, Any]]:
        from agent.tools.mcp.mcp_oauth import load_server_record

        root = self._root()
        items = []
        for name, config in root["mcpServers"].items():
            if not isinstance(config, Mapping) or config.get("_emate_auth_kind") != "oauth2":
                continue
            record = load_server_record(name)
            expires_at = float(record.get("expires_at") or 0) or None
            authorized = bool(record.get("access_token"))
            items.append(
                {
                    "service_id": self._server_id(name),
                    "state": "authorized" if authorized else "authorization_required",
                    "expires_at": expires_at,
                    "scope": str(config.get("_emate_oauth_scope") or ""),
                }
            )
        return items

    def begin_oauth(self, server_id: str) -> dict[str, Any]:
        _root, name, _config = self._find(server_id)
        self.manager.ensure_mcp_configured_loaded(
            wait_seconds=5.0,
            poll_interval_seconds=0.05,
            server_name=name,
        )
        client = self.manager._mcp_registry.get(name) if self.manager._mcp_registry else None
        handler = getattr(client, "_oauth", None)
        url = handler.build_authorization_url() if handler is not None else None
        if not url:
            raise HTTPException(422, detail={"code": "mcp_oauth_authorization_unavailable"})
        return {
            "service_id": server_id,
            "state": "authorizing",
            "authorization_url": url,
            "expires_at": int(time.time()) + 600,
        }

    def clear_oauth(self, server_id: str) -> None:
        from agent.tools.mcp.mcp_oauth import clear_server_record

        _root, name, _config = self._find(server_id)
        clear_server_record(name)
        self.manager.reload_mcp_server(name)

    @staticmethod
    def _request_config(
        request: UserMCPServerRequest, *, previous: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        config = {
            "type": "streamable-http",
            "url": request.endpoint,
            "enabled": previous.get("enabled", True) if previous else True,
            "_emate_display_name": request.display_name,
            "_emate_auth_kind": request.auth_kind,
            "_emate_oauth_client_id": request.oauth_client_id,
            "_emate_oauth_scope": request.oauth_scope,
            "_emate_authorization_hosts": request.authorization_hosts,
            "_emate_revision": int(previous.get("_emate_revision") or 1) if previous else 1,
        }
        if request.auth_kind == "bearer":
            token = request.credential.get_secret_value() if request.credential is not None else None
            if token:
                config["headers"] = {"Authorization": f"Bearer {token}"}
            elif previous and isinstance(previous.get("headers"), Mapping):
                config["headers"] = dict(previous["headers"])
            else:
                raise HTTPException(422, detail={"code": "mcp_bearer_credential_required"})
        return config


def create_cow_mcp_router(
    service: CowMCPSettingsService,
    *,
    workspace_resolver: Any | None = None,
) -> APIRouter:
    """Keep the existing desktop API while making ``mcp.json`` authoritative."""

    router = APIRouter(tags=["mcp-servers"])

    def mutation(server: dict[str, Any]) -> dict[str, Any]:
        return {"server": server, "restart_required": True, "restart_scheduled": True}

    def select(project_id: str | None) -> None:
        service.bind_workspace(
            workspace_resolver(project_id) if workspace_resolver is not None else None
        )

    @router.get("/api/v1/mcp/servers")
    def list_servers(project_id: str | None = None) -> dict[str, Any]:
        select(project_id)
        return {"items": service.list()}

    @router.post("/api/v1/mcp/servers", status_code=status.HTTP_201_CREATED)
    def create_server(
        request: UserMCPServerRequest, project_id: str | None = None
    ) -> dict[str, Any]:
        select(project_id)
        return mutation(service.create(request))

    @router.put("/api/v1/mcp/servers/{server_id}")
    def update_server(
        server_id: str,
        request: UserMCPServerRequest,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        select(project_id)
        return mutation(service.update(server_id, request))

    @router.post("/api/v1/mcp/servers/{server_id}/test")
    def test_server(server_id: str, project_id: str | None = None) -> dict[str, Any]:
        select(project_id)
        return mutation(service.test(server_id))

    @router.post("/api/v1/mcp/servers/{server_id}/enable")
    def enable_server(server_id: str, project_id: str | None = None) -> dict[str, Any]:
        select(project_id)
        return mutation(service.set_enabled(server_id, True))

    @router.post("/api/v1/mcp/servers/{server_id}/disable")
    def disable_server(server_id: str, project_id: str | None = None) -> dict[str, Any]:
        select(project_id)
        return mutation(service.set_enabled(server_id, False))

    @router.delete("/api/v1/mcp/servers/{server_id}", status_code=204)
    def delete_server(server_id: str, project_id: str | None = None) -> Response:
        select(project_id)
        service.remove(server_id)
        return Response(status_code=204)

    @router.get("/api/v1/mcp/oauth")
    def oauth_statuses(project_id: str | None = None) -> dict[str, Any]:
        select(project_id)
        return {"items": service.oauth_items()}

    @router.post("/api/v1/mcp/oauth/{server_id}/begin")
    def begin_oauth(server_id: str, project_id: str | None = None) -> dict[str, Any]:
        select(project_id)
        return service.begin_oauth(server_id)

    @router.delete("/api/v1/mcp/oauth/{server_id}", status_code=204)
    def clear_oauth(server_id: str, project_id: str | None = None) -> Response:
        select(project_id)
        service.clear_oauth(server_id)
        return Response(status_code=204)

    def finish_oauth(code: str = "", state: str = "", error: str = "") -> HTMLResponse:
        from agent.tools.mcp.mcp_client import notify_server_authorized
        from agent.tools.mcp.mcp_oauth import pop_pending

        if error or not code or not state:
            return HTMLResponse("MCP authorization failed.", status_code=400)
        handler = pop_pending(state)
        if handler is None or not handler.finish_authorization(code):
            return HTMLResponse("MCP authorization failed.", status_code=400)
        notify_server_authorized(handler.server_name)
        return HTMLResponse("MCP authorization complete. You may close this window.")

    router.add_api_route("/mcp/oauth/callback", finish_oauth, methods=["GET"])
    router.add_api_route("/api/v1/mcp/oauth/callback", finish_oauth, methods=["GET"])
    return router


def _config_path(workspace_root: Path) -> Path:
    mcp = workspace_root / "mcp.json"
    return mcp if mcp.is_file() else workspace_root / "config.json"


def _load_configs(path: Path) -> tuple[_ServerConfig, ...]:
    if not path.is_file():
        return ()
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if not isinstance(root, Mapping):
        return ()
    raw: Any
    if path.name == "config.json":
        raw = root.get("mcp_servers", [])
    else:
        raw = root.get("mcpServers") or root.get("mcp_servers") or root
    items: list[Mapping[str, Any]] = []
    if isinstance(raw, Mapping):
        for name, value in raw.items():
            if isinstance(name, str) and isinstance(value, Mapping):
                items.append({"name": name, **dict(value)})
    elif isinstance(raw, list):
        items.extend(value for value in raw if isinstance(value, Mapping))
    configs: list[_ServerConfig] = []
    names: set[str] = set()
    for item in items:
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            continue
        if item.get("enabled", True) is False:
            continue
        transport = str(item.get("type", "stdio")).casefold()
        transport = "streamable-http" if transport in _HTTP_TYPES else transport
        if transport not in {"stdio", "sse", "streamable-http"}:
            continue
        timeout = item.get("timeout", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
            continue
        payload = dict(item)
        payload["name"] = name.strip()
        payload["type"] = transport
        payload["timeout"] = timeout
        try:
            digest = canonical_digest(payload)
        except (TypeError, ValueError):
            continue
        configs.append(
            _ServerConfig(
                name=name.strip(),
                transport=transport,
                payload=payload,
                digest=digest,
            )
        )
        names.add(name.strip())
    return tuple(configs)


async def _transport(config: _ServerConfig, workspace_root: Path) -> Any:
    payload = config.payload
    if config.transport == "stdio":
        command = payload.get("command")
        args = payload.get("args", [])
        if not isinstance(command, str) or not command or not isinstance(args, list) or any(
            not isinstance(value, str) for value in args
        ):
            raise ValueError("MCP stdio command is invalid")
        cwd_value = payload.get("cwd")
        cwd = workspace_root
        if isinstance(cwd_value, str) and cwd_value:
            candidate = Path(cwd_value).expanduser()
            cwd = (workspace_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        extra = payload.get("env", {})
        if not isinstance(extra, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, (str, int, float, bool))
            for key, value in extra.items()
        ):
            raise ValueError("MCP stdio environment is invalid")
        if payload.get("inherit_full_env") is True:
            env = {
                key: value
                for key, value in os.environ.items()
                if not any(marker in key.upper() for marker in _SENSITIVE_ENV_NAMES)
            }
        else:
            env = {key: os.environ[key] for key in _ENV_PASSTHROUGH if key in os.environ}
        env.update({key: str(value) for key, value in extra.items()})
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return MCPStdioTransport(process)

    url = payload.get("url")
    headers = payload.get("headers", {})
    if not isinstance(url, str) or not isinstance(headers, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in headers.items()
    ):
        raise ValueError("MCP HTTP configuration is invalid")
    if config.transport == "sse":
        return LegacySSEMCPTransport(url, headers=headers)
    parsed = urlsplit(url)
    if not parsed.hostname:
        raise ValueError("MCP HTTP endpoint is invalid")
    return ManagedHTTPMCPTransport(
        url,
        expected_host=parsed.hostname,
        headers=headers,
    )


def _tool_payload(tool: MCPToolContract) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": dict(tool.input_schema),
        "outputSchema": dict(tool.output_schema),
    }


def _decode_tools(value: Any) -> tuple[MCPToolContract, ...]:
    if not isinstance(value, list):
        raise ValueError("MCP cached tools are invalid")
    tools = tuple(
        MCPToolContract(
            name=item["name"],
            description=item["description"],
            input_schema=item["inputSchema"],
            output_schema=item.get("outputSchema", {"type": "object"}),
            effects=frozenset(
                {
                    CapabilityEffect.EXECUTE,
                    CapabilityEffect.NETWORK,
                    CapabilityEffect.READ,
                    CapabilityEffect.WRITE,
                }
            ),
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            approval_requirement=ApprovalRequirement.NEVER,
            required_sandbox=SandboxLevel.DANGER_FULL_ACCESS,
            exposure=Exposure.DIRECT,
        )
        for item in value
        if isinstance(item, Mapping)
    )
    if len(tools) != len(value):
        raise ValueError("MCP cached tools are invalid")
    return tuple(sorted(tools, key=lambda item: item.name))


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, Mapping) or value.get("schema_version") != _CACHE_SCHEMA_VERSION:
        return {}
    servers = value.get("servers")
    return dict(servers) if isinstance(servers, Mapping) else {}


def _write_cache(path: Path, servers: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"schema_version": _CACHE_SCHEMA_VERSION, "servers": dict(servers)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _file_signature(path: Path) -> tuple[int, int, str] | None:
    try:
        payload = path.read_bytes()
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, len(payload), hashlib.sha256(payload).hexdigest()


__all__ = [
    "CowMCPConfigService",
    "CowMCPSettingsService",
    "create_cow_mcp_router",
]
