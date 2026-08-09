"""User-owned HTTPS MCP registration built on the existing MCP authority."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import threading
import time
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit
import uuid

import httpcore
import httpx
from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ecorex import __version__
from ecorex.capabilities import (
    ApprovalRequirement,
    CapabilityEffect,
    IdempotencyClass,
    SandboxLevel,
)
from ecorex.connectors.vault import CredentialVault

from .mcp import (
    MAX_MCP_MESSAGE_BYTES,
    MAX_MCP_TOOL_PAGES,
    MAX_MCP_TOOLS,
    MCP_PROTOCOL_VERSION,
    MCPProtocolError,
    MCPRuntimeBinding,
    MCPToolContract,
    ManagedHTTPMCPTransport,
    _validate_mcp_initialize_result,
)
from .mcp_oauth import MCPOAuthError, MCPOAuthRegistration, MCPOAuthService
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


_SAFE_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$")
_SAFE_HOST = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_TOOL_FIELDS = frozenset({"name", "description", "inputSchema", "outputSchema"})


class UserMCPError(RuntimeError):
    def __init__(self, code: str, http_status: int = 422) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code)


class UserMCPServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=9, max_length=2048)
    auth_kind: Literal["none", "bearer", "oauth2"] = "none"
    credential: SecretStr | None = None
    oauth_client_id: str | None = Field(default=None, min_length=1, max_length=512)
    oauth_scope: str = Field(default="", max_length=2048)
    authorization_hosts: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("display_name")
    @classmethod
    def safe_display_name(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("display_name contains unsafe control characters")
        return value

    @field_validator("authorization_hosts")
    @classmethod
    def safe_authorization_hosts(cls, value: list[str]) -> list[str]:
        normalized = [_validated_public_host(item) for item in value]
        if normalized != sorted(set(normalized)):
            raise ValueError("authorization_hosts must be unique and sorted")
        return normalized

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value: str) -> str:
        try:
            _validated_https_endpoint(value)
        except (UserMCPError, ValueError):
            raise ValueError("endpoint must be one explicit public HTTPS URL") from None
        return value


@dataclass(frozen=True, slots=True)
class UserMCPServer:
    server_id: str
    display_name: str
    endpoint: str
    expected_host: str
    auth_kind: str
    oauth_client_id: str | None
    oauth_scope: str
    authorization_hosts: tuple[str, ...]
    enabled: bool
    tools: tuple[MCPToolContract, ...]
    credential_ref: str | None
    tested_at: int | None
    revision: int

    def projection(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "display_name": self.display_name,
            "endpoint": self.endpoint,
            "auth_kind": self.auth_kind,
            "oauth_client_id": self.oauth_client_id,
            "oauth_scope": self.oauth_scope,
            "authorization_hosts": list(self.authorization_hosts),
            "enabled": self.enabled,
            "credential_configured": self.credential_ref is not None,
            "tested_at": self.tested_at,
            "tool_count": len(self.tools),
            "tool_names": [tool.name for tool in self.tools],
            "revision": self.revision,
        }


class _VaultBearerProvider:
    def __init__(self, vault: CredentialVault, reference: str) -> None:
        self.vault = vault
        self.reference = reference

    async def access_token(self) -> str | None:
        try:
            material = await asyncio.to_thread(self.vault.get, self.reference)
        except (KeyError, RuntimeError):
            return None
        token = material.get("bearer_token")
        return token if isinstance(token, str) and token else None

    async def refresh_after_unauthorized(self) -> str | None:
        return None


class _PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, reject mixed/private answers, then connect to that IP."""

    def __init__(self, resolver: Any, backend: Any | None = None) -> None:
        self.resolver = resolver
        self.backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> Any:
        try:
            addresses = tuple(
                ipaddress.ip_address(value) for value in self.resolver(host)
            )
        except Exception:
            raise httpcore.ConnectError("MCP endpoint resolution failed") from None
        if not addresses or any(not address.is_global for address in addresses):
            raise httpcore.ConnectError("MCP endpoint is not public")
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self.backend.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as error:
                last_error = error
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise httpcore.UnsupportedProtocol("Unix sockets are forbidden for user MCP")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class _PublicHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, resolver: Any) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=2,
            max_keepalive_connections=1,
            http2=False,
            retries=0,
            network_backend=_PublicNetworkBackend(resolver),
        )


class UserMCPService:
    """Account+organization scoped MCP config; secrets never enter SQLite."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        account_id: str,
        organization_id: str | None,
        vault: CredentialVault,
        runtime_api_version: str,
        platform: str,
        architecture: str,
        reload_requester: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
        host_resolver: Any | None = None,
        initialize: bool = True,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.account_id = _safe_scope(account_id, "account")
        self.organization_id = _safe_scope(
            organization_id or "personal", "organization"
        )
        self.tenant_namespace = mcp_tenant_namespace(
            self.account_id, organization_id
        )
        self.vault = vault
        self.runtime_api_version = runtime_api_version
        self.platform = platform
        self.architecture = architecture
        self.reload_requester = reload_requester
        self.http_client = http_client
        self.host_resolver = host_resolver or _resolve_host_addresses
        self._lock = threading.RLock()
        self._initialized = bool(initialize)
        if initialize:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def list(self) -> tuple[UserMCPServer, ...]:
        if not self._initialized and not self.database_path.exists():
            return ()
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM user_mcp_servers WHERE account_id=? AND organization_id=? "
                    "ORDER BY display_name COLLATE NOCASE,server_id",
                    (self.account_id, self.organization_id),
                ).fetchall()
        except sqlite3.OperationalError:
            if not self._initialized:
                return ()
            raise
        return tuple(self._record(row) for row in rows)

    def get(self, server_id: str) -> UserMCPServer:
        server_id = _safe_server_id(server_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_mcp_servers WHERE account_id=? AND organization_id=? "
                "AND server_id=?",
                (self.account_id, self.organization_id, server_id),
            ).fetchone()
        if row is None:
            raise UserMCPError("mcp_server_not_found", 404)
        return self._record(row)

    def create(self, request: UserMCPServerRequest) -> UserMCPServer:
        server_id = "user.mcp." + uuid.uuid4().hex
        return self._save(None, server_id, request)

    def update(
        self, server_id: str, request: UserMCPServerRequest
    ) -> UserMCPServer:
        return self._save(self.get(server_id), server_id, request)

    def set_enabled(self, server_id: str, enabled: bool) -> UserMCPServer:
        current = self.get(server_id)
        if enabled and not current.tools:
            raise UserMCPError("mcp_server_test_required", 409)
        if current.enabled == enabled:
            return current
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE user_mcp_servers SET enabled=?,revision=revision+1,updated_at=? "
                "WHERE account_id=? AND organization_id=? AND server_id=?",
                (
                    int(enabled),
                    int(time.time()),
                    self.account_id,
                    self.organization_id,
                    current.server_id,
                ),
            )
        return self.get(server_id)

    async def remove(
        self, server_id: str, *, oauth_service: MCPOAuthService | None = None
    ) -> None:
        current = self.get(server_id)
        if (
            current.auth_kind == "oauth2"
            and oauth_service is not None
            and current.server_id in oauth_service.registrations
        ):
            try:
                await oauth_service.clear(current.server_id, self.tenant_namespace)
            except MCPOAuthError as error:
                raise UserMCPError(error.code, 503) from None
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM user_mcp_servers WHERE account_id=? AND organization_id=? "
                "AND server_id=?",
                (self.account_id, self.organization_id, current.server_id),
            )
        if current.credential_ref is not None:
            try:
                await asyncio.to_thread(self.vault.delete, current.credential_ref)
            except RuntimeError:
                raise UserMCPError("mcp_vault_unavailable", 503) from None

    async def test(
        self, server_id: str, *, oauth_service: MCPOAuthService | None
    ) -> UserMCPServer:
        current = self.get(server_id)
        if current.auth_kind == "oauth2" and (
            oauth_service is None
            or current.server_id not in oauth_service.registrations
        ):
            raise UserMCPError("mcp_server_restart_required", 409)
        transport = await self._resolved_transport(
            current, oauth_service=oauth_service
        )
        try:
            tools = await _discover_tools(transport)
        except UserMCPError:
            raise
        except Exception as error:
            code = getattr(error, "code", "mcp_server_test_failed")
            http_status = (
                409
                if code
                in {
                    "mcp_oauth_authorization_required",
                    "mcp_oauth_service_not_found",
                }
                else 503
            )
            raise UserMCPError(str(code), http_status) from None
        finally:
            await transport.close()
        encoded = _encode_tools(tools)
        now = int(time.time())
        with self._lock, self._connect() as connection:
            updated = connection.execute(
                "UPDATE user_mcp_servers SET tool_catalog_json=?,tested_at=?,"
                "revision=revision+1,updated_at=? WHERE account_id=? AND "
                "organization_id=? AND server_id=? AND revision=?",
                (
                    encoded,
                    now,
                    now,
                    self.account_id,
                    self.organization_id,
                    current.server_id,
                    current.revision,
                ),
            )
            if updated.rowcount != 1:
                raise UserMCPError("mcp_server_changed_during_test", 409)
        return self.get(server_id)

    def oauth_registrations(self) -> tuple[MCPOAuthRegistration, ...]:
        return tuple(
            self._oauth_registration(item)
            for item in self.list()
            if item.auth_kind == "oauth2"
        )

    def runtime_bindings(self) -> tuple[MCPRuntimeBinding, ...]:
        return tuple(
            self._binding(item)
            for item in self.list()
            if item.enabled and item.tools
        )

    def request_restart(self, server_id: str, operation: str) -> bool:
        if not callable(self.reload_requester):
            return False
        try:
            return bool(self.reload_requester(f"user-mcp:{operation}:{server_id}"))
        except Exception:
            return False

    def _save(
        self,
        current: UserMCPServer | None,
        server_id: str,
        request: UserMCPServerRequest,
    ) -> UserMCPServer:
        endpoint, expected_host = _validated_https_endpoint(request.endpoint)
        credential = (
            request.credential.get_secret_value()
            if request.credential is not None
            else None
        )
        if credential is not None and (
            not 1 <= len(credential) <= 16_384 or "\x00" in credential
        ):
            raise UserMCPError("mcp_credential_invalid")
        if request.auth_kind != "bearer" and credential is not None:
            raise UserMCPError("mcp_credential_auth_mismatch")
        if request.auth_kind != "oauth2" and (
            request.oauth_client_id is not None
            or request.oauth_scope
            or request.authorization_hosts
        ):
            raise UserMCPError("mcp_oauth_configuration_invalid")
        if request.auth_kind == "bearer" and credential is None and (
            current is None or current.auth_kind != "bearer" or current.credential_ref is None
        ):
            raise UserMCPError("mcp_bearer_credential_required")
        credential_ref = (
            self._credential_reference(server_id)
            if request.auth_kind == "bearer"
            else None
        )
        credential_written = False
        if credential is not None and credential_ref is not None:
            try:
                self.vault.put(credential_ref, {"bearer_token": credential})
                credential_written = True
            except RuntimeError:
                raise UserMCPError("mcp_vault_unavailable", 503) from None
        previous_ref = current.credential_ref if current is not None else None
        connection_changed = current is None or any(
            (
                current.endpoint != endpoint,
                current.auth_kind != request.auth_kind,
                current.oauth_client_id != request.oauth_client_id,
                current.oauth_scope != request.oauth_scope,
                current.authorization_hosts != tuple(request.authorization_hosts),
            )
        )
        now = int(time.time())
        try:
            with self._lock, self._connect() as connection:
                if current is None:
                    connection.execute(
                        "INSERT INTO user_mcp_servers(account_id,organization_id,server_id,"
                        "display_name,endpoint,expected_host,auth_kind,oauth_client_id,"
                        "oauth_scope,authorization_hosts_json,credential_ref,enabled,"
                        "tool_catalog_json,tested_at,revision,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                        (
                            self.account_id,
                            self.organization_id,
                            server_id,
                            request.display_name,
                            endpoint,
                            expected_host,
                            request.auth_kind,
                            request.oauth_client_id,
                            request.oauth_scope,
                            json.dumps(request.authorization_hosts, separators=(",", ":")),
                            credential_ref,
                            0,
                            "[]",
                            None,
                            now,
                            now,
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE user_mcp_servers SET display_name=?,endpoint=?,expected_host=?,"
                        "auth_kind=?,oauth_client_id=?,oauth_scope=?,authorization_hosts_json=?,"
                        "credential_ref=?,enabled=?,tool_catalog_json=?,tested_at=?,"
                        "revision=revision+1,updated_at=? WHERE account_id=? AND "
                        "organization_id=? AND server_id=?",
                        (
                            request.display_name,
                            endpoint,
                            expected_host,
                            request.auth_kind,
                            request.oauth_client_id,
                            request.oauth_scope,
                            json.dumps(request.authorization_hosts, separators=(",", ":")),
                            credential_ref,
                            0 if connection_changed else int(current.enabled),
                            "[]" if connection_changed else _encode_tools(current.tools),
                            None if connection_changed else current.tested_at,
                            now,
                            self.account_id,
                            self.organization_id,
                            server_id,
                        ),
                    )
        except Exception:
            if credential_written and current is None and credential_ref is not None:
                try:
                    self.vault.delete(credential_ref)
                except RuntimeError:
                    pass
            raise
        if previous_ref is not None and previous_ref != credential_ref:
            try:
                self.vault.delete(previous_ref)
            except RuntimeError:
                raise UserMCPError("mcp_vault_unavailable", 503) from None
        return self.get(server_id)

    def _binding(self, item: UserMCPServer) -> MCPRuntimeBinding:
        payload = {
            "server_id": item.server_id,
            "endpoint": item.endpoint,
            "auth_kind": item.auth_kind,
            "oauth_client_id": item.oauth_client_id,
            "oauth_scope": item.oauth_scope,
            "authorization_hosts": list(item.authorization_hosts),
            "tools": [_tool_payload(tool) for tool in item.tools],
        }
        digest = canonical_digest(payload)
        manifest = ExtensionManifest(
            schema_version=1,
            contract_version=EXTENSION_CONTRACT_VERSION,
            extension_id=item.server_id,
            version="1.0.0",
            kind=ExtensionKind.MCP_SERVER,
            display_name=item.display_name,
            description=f"User-authorized HTTPS MCP provider: {item.display_name}",
            artifact_sha256=digest,
            source=ExtensionSource.USER_CONFIGURATION,
            trust=ExtensionTrust.USER_CONFIGURED,
            runtime_boundary=RuntimeBoundary.MANAGED_ADAPTER,
            transport=ExtensionTransport.STREAMABLE_HTTP,
            compatibility=ExtensionCompatibility(
                runtime_api=f"={self.runtime_api_version}",
                platforms=(),
                architectures=(),
            ),
            dependencies=(),
            conflicts=(),
            exports=(
                ExtensionExport(
                    export_id=item.server_id,
                    kind=ExtensionExportKind.MCP_SERVER,
                    exposure=ExtensionExposure.DEFERRED,
                    permission_effects=("network", "read", "write"),
                ),
            ),
            supported_protocol_versions=(MCP_PROTOCOL_VERSION,),
            upstream_metadata=None,
            signature=ExtensionSignature(
                algorithm="user-mcp-config-sha256",
                key_id="user-mcp-config-v1",
                value=digest,
            ),
        )
        verified = verify_user_configured_mcp(
            manifest,
            runtime_api_version=self.runtime_api_version,
            platform=self.platform,
            architecture=self.architecture,
        )
        return MCPRuntimeBinding(
            extension_id=item.server_id,
            revision_id=manifest.revision_id,
            artifact_sha256=digest,
            transport=ExtensionTransport.STREAMABLE_HTTP,
            tools=item.tools,
            verified_manifest=verified,
            session_factory=lambda _tenant, record=item: self._resolved_transport(record),
            oauth_registration=(
                self._oauth_registration(item) if item.auth_kind == "oauth2" else None
            ),
        )

    def _transport(
        self,
        item: UserMCPServer,
        *,
        oauth_service: MCPOAuthService | None = None,
    ) -> ManagedHTTPMCPTransport:
        client = self.http_client
        own_client = False
        if client is None:
            client = httpx.AsyncClient(
                transport=_PublicHTTPTransport(self.host_resolver),
                timeout=httpx.Timeout(connect=10, read=60, write=30, pool=10),
                follow_redirects=False,
                trust_env=False,
            )
            own_client = True
        transport = ManagedHTTPMCPTransport(
            item.endpoint,
            expected_host=item.expected_host,
            client=client,
            own_client=own_client,
        )
        if item.auth_kind == "bearer":
            if item.credential_ref is None:
                raise UserMCPError("mcp_bearer_credential_required", 409)
            transport.bind_oauth(_VaultBearerProvider(self.vault, item.credential_ref))
        elif item.auth_kind == "oauth2" and oauth_service is not None:
            transport.bind_oauth(
                oauth_service.provider(self.tenant_namespace, item.server_id)
            )
        elif item.auth_kind == "oauth2":
            # Runtime execution binds the same provider inside MCPClientSupervisor.
            pass
        return transport

    async def _resolved_transport(
        self,
        item: UserMCPServer,
        *,
        oauth_service: MCPOAuthService | None = None,
    ) -> ManagedHTTPMCPTransport:
        try:
            addresses = await asyncio.to_thread(
                self.host_resolver, item.expected_host
            )
            parsed = tuple(ipaddress.ip_address(value) for value in addresses)
        except Exception:
            raise UserMCPError("mcp_endpoint_resolution_failed", 503) from None
        if not parsed or any(not address.is_global for address in parsed):
            raise UserMCPError("mcp_endpoint_not_public")
        return self._transport(item, oauth_service=oauth_service)

    @staticmethod
    def _oauth_registration(item: UserMCPServer) -> MCPOAuthRegistration:
        return MCPOAuthRegistration(
            service_id=item.server_id,
            resource_url=item.endpoint,
            expected_host=item.expected_host,
            client_id=item.oauth_client_id,
            scope=item.oauth_scope,
            authorization_hosts=frozenset(item.authorization_hosts),
        )

    def _credential_reference(self, server_id: str) -> str:
        digest = hashlib.sha256(
            f"{self.account_id}\0{self.organization_id}\0{server_id}".encode("utf-8")
        ).hexdigest()
        return f"ecorex/user-mcp/{digest}"

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS user_mcp_servers("
                "account_id TEXT NOT NULL,organization_id TEXT NOT NULL,server_id TEXT NOT NULL,"
                "display_name TEXT NOT NULL,endpoint TEXT NOT NULL,expected_host TEXT NOT NULL,"
                "auth_kind TEXT NOT NULL CHECK(auth_kind IN ('none','bearer','oauth2')),"
                "oauth_client_id TEXT,oauth_scope TEXT NOT NULL,authorization_hosts_json TEXT NOT NULL,"
                "credential_ref TEXT,enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),"
                "tool_catalog_json TEXT NOT NULL,tested_at INTEGER,revision INTEGER NOT NULL,"
                "created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,"
                "PRIMARY KEY(account_id,organization_id,server_id))"
            )
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> UserMCPServer:
        try:
            hosts_raw = json.loads(str(row["authorization_hosts_json"]))
            if not isinstance(hosts_raw, list):
                raise ValueError
            hosts = tuple(_validated_public_host(item) for item in hosts_raw)
            if hosts != tuple(sorted(set(hosts))):
                raise ValueError
            tools = _decode_tools(str(row["tool_catalog_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise UserMCPError("mcp_configuration_corrupt", 503) from None
        endpoint, expected_host = _validated_https_endpoint(str(row["endpoint"]))
        if expected_host != str(row["expected_host"]):
            raise UserMCPError("mcp_configuration_corrupt", 503)
        return UserMCPServer(
            server_id=_safe_server_id(str(row["server_id"])),
            display_name=str(row["display_name"]),
            endpoint=endpoint,
            expected_host=expected_host,
            auth_kind=str(row["auth_kind"]),
            oauth_client_id=(
                str(row["oauth_client_id"])
                if row["oauth_client_id"] is not None
                else None
            ),
            oauth_scope=str(row["oauth_scope"]),
            authorization_hosts=hosts,
            enabled=bool(row["enabled"]),
            tools=tools,
            credential_ref=(
                str(row["credential_ref"])
                if row["credential_ref"] is not None
                else None
            ),
            tested_at=(int(row["tested_at"]) if row["tested_at"] is not None else None),
            revision=int(row["revision"]),
        )


def create_user_mcp_router(
    service: UserMCPService,
    *,
    oauth_service: MCPOAuthService | None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/mcp/servers", tags=["mcp-servers"])

    @router.get("")
    def list_servers() -> Mapping[str, Any]:
        return {"items": [item.projection() for item in service.list()]}

    @router.post("", status_code=status.HTTP_201_CREATED)
    def create_server(request: UserMCPServerRequest) -> Mapping[str, Any]:
        try:
            item = service.create(request)
        except UserMCPError as error:
            raise _http_error(error) from error
        return _mutation(service, item, "create")

    @router.put("/{server_id}")
    async def update_server(
        server_id: str, request: UserMCPServerRequest
    ) -> Mapping[str, Any]:
        try:
            previous = service.get(server_id)
            item = service.update(server_id, request)
            oauth_changed = previous.auth_kind == "oauth2" and any(
                (
                    item.auth_kind != "oauth2",
                    item.endpoint != previous.endpoint,
                    item.oauth_client_id != previous.oauth_client_id,
                    item.oauth_scope != previous.oauth_scope,
                    item.authorization_hosts != previous.authorization_hosts,
                )
            )
            if (
                oauth_changed
                and oauth_service is not None
                and previous.server_id in oauth_service.registrations
            ):
                try:
                    await oauth_service.clear(
                        previous.server_id, service.tenant_namespace
                    )
                except MCPOAuthError as error:
                    service.request_restart(previous.server_id, "update")
                    raise UserMCPError(error.code, 503) from None
        except UserMCPError as error:
            raise _http_error(error) from error
        return _mutation(service, item, "update")

    @router.post("/{server_id}/test")
    async def test_server(server_id: str) -> Mapping[str, Any]:
        try:
            item = await service.test(server_id, oauth_service=oauth_service)
        except UserMCPError as error:
            raise _http_error(error) from error
        return _mutation(service, item, "test")

    @router.post("/{server_id}/enable")
    def enable_server(server_id: str) -> Mapping[str, Any]:
        try:
            item = service.set_enabled(server_id, True)
        except UserMCPError as error:
            raise _http_error(error) from error
        return _mutation(service, item, "enable")

    @router.post("/{server_id}/disable")
    def disable_server(server_id: str) -> Mapping[str, Any]:
        try:
            item = service.set_enabled(server_id, False)
        except UserMCPError as error:
            raise _http_error(error) from error
        return _mutation(service, item, "disable")

    @router.delete(
        "/{server_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_server(server_id: str) -> Response:
        try:
            await service.remove(server_id, oauth_service=oauth_service)
        except UserMCPError as error:
            raise _http_error(error) from error
        service.request_restart(server_id, "delete")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


async def _discover_tools(
    transport: ManagedHTTPMCPTransport,
) -> tuple[MCPToolContract, ...]:
    prefix = uuid.uuid4().hex
    initialize = await transport.exchange(
        {
            "jsonrpc": "2.0",
            "id": f"{prefix}:1",
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "e-Mate", "version": __version__},
            },
        },
        timeout_seconds=30,
        max_response_bytes=MAX_MCP_MESSAGE_BYTES,
    )
    _validate_mcp_initialize_result(initialize.get("result"))
    await transport.notify(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        timeout_seconds=30,
    )
    raw_tools: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    for page in range(MAX_MCP_TOOL_PAGES):
        response = await transport.exchange(
            {
                "jsonrpc": "2.0",
                "id": f"{prefix}:{page + 2}",
                "method": "tools/list",
                "params": ({"cursor": cursor} if cursor is not None else {}),
            },
            timeout_seconds=30,
            max_response_bytes=MAX_MCP_MESSAGE_BYTES,
        )
        result = response.get("result")
        if not isinstance(result, Mapping) or set(result) - {"tools", "nextCursor"}:
            raise MCPProtocolError("mcp_tool_catalog_shape_invalid")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise MCPProtocolError("mcp_tool_catalog_shape_invalid")
        raw_tools.extend(tools)
        if not 1 <= len(raw_tools) <= MAX_MCP_TOOLS:
            raise MCPProtocolError("mcp_tool_catalog_size_invalid")
        next_cursor = result.get("nextCursor")
        if next_cursor is None:
            break
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or len(next_cursor.encode("utf-8")) > 256
            or next_cursor in seen
            or any(ord(character) < 32 or ord(character) == 127 for character in next_cursor)
        ):
            raise MCPProtocolError("mcp_tool_catalog_cursor_invalid")
        seen.add(next_cursor)
        cursor = next_cursor
    else:
        raise MCPProtocolError("mcp_tool_catalog_page_limit")
    contracts: list[MCPToolContract] = []
    for raw in raw_tools:
        if (
            not isinstance(raw, Mapping)
            or set(raw) - _TOOL_FIELDS
            or not {"name", "description", "inputSchema"} <= set(raw)
        ):
            raise MCPProtocolError("mcp_tool_descriptor_invalid")
        try:
            contracts.append(
                MCPToolContract(
                    name=raw["name"],
                    description=raw["description"],
                    input_schema=raw["inputSchema"],
                    output_schema=raw.get("outputSchema", {"type": "object"}),
                    effects=frozenset(
                        {
                            CapabilityEffect.READ,
                            CapabilityEffect.WRITE,
                            CapabilityEffect.NETWORK,
                        }
                    ),
                    idempotency=IdempotencyClass.NON_IDEMPOTENT,
                    approval_requirement=ApprovalRequirement.ALWAYS,
                    required_sandbox=SandboxLevel.READ_ONLY,
                )
            )
        except (TypeError, ValueError):
            raise MCPProtocolError("mcp_tool_descriptor_invalid") from None
    names = [tool.name for tool in contracts]
    if len(set(names)) != len(names) or len({name.casefold() for name in names}) != len(names):
        raise MCPProtocolError("mcp_tool_name_invalid")
    return tuple(sorted(contracts, key=lambda item: item.name))


def _tool_payload(tool: MCPToolContract) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": dict(tool.input_schema),
        "outputSchema": dict(tool.output_schema),
    }


def _encode_tools(tools: tuple[MCPToolContract, ...]) -> str:
    return json.dumps(
        [_tool_payload(tool) for tool in tools],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_tools(payload: str) -> tuple[MCPToolContract, ...]:
    value = json.loads(payload)
    if not isinstance(value, list) or len(value) > MAX_MCP_TOOLS:
        raise ValueError("invalid MCP tool catalog")
    tools = tuple(
        MCPToolContract(
            name=item["name"],
            description=item["description"],
            input_schema=item["inputSchema"],
            output_schema=item["outputSchema"],
            effects=frozenset(
                {
                    CapabilityEffect.READ,
                    CapabilityEffect.WRITE,
                    CapabilityEffect.NETWORK,
                }
            ),
            idempotency=IdempotencyClass.NON_IDEMPOTENT,
            approval_requirement=ApprovalRequirement.ALWAYS,
            required_sandbox=SandboxLevel.READ_ONLY,
        )
        for item in value
        if isinstance(item, Mapping) and set(item) == _TOOL_FIELDS
    )
    if len(tools) != len(value):
        raise ValueError("invalid MCP tool catalog")
    names = tuple(tool.name for tool in tools)
    if names != tuple(sorted(set(names))) or len({name.casefold() for name in names}) != len(names):
        raise ValueError("invalid MCP tool catalog")
    return tools


def _validated_https_endpoint(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise UserMCPError("mcp_endpoint_invalid") from None
    host = _validated_public_host(parsed.hostname or "")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or parsed.hostname != host
        or (parsed.path and not parsed.path.startswith("/"))
    ):
        raise UserMCPError("mcp_endpoint_invalid")
    return value, host


def _validated_public_host(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("host is invalid")
    host = value.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("host is not public")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if _SAFE_HOST.fullmatch(host) is None:
            raise ValueError("host is invalid") from None
    else:
        if not address.is_global:
            raise ValueError("host is not public")
        host = address.compressed
    return host


def _resolve_host_addresses(host: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(sockaddr[0])
                for _family, _type, _protocol, _canonname, sockaddr in socket.getaddrinfo(
                    host, 443, type=socket.SOCK_STREAM
                )
            }
        )
    )


def _safe_scope(value: str, label: str) -> str:
    if not isinstance(value, str) or _SAFE_SCOPE.fullmatch(value) is None:
        raise ValueError(f"MCP {label} scope is invalid")
    return value


def _safe_server_id(value: str) -> str:
    if re.fullmatch(r"user\.mcp\.[0-9a-f]{32}", value) is None:
        raise UserMCPError("mcp_server_not_found", 404)
    return value


def mcp_tenant_namespace(account_id: str, organization_id: str | None) -> str:
    account = _safe_scope(account_id, "account")
    organization = _safe_scope(organization_id or "personal", "organization")
    digest = hashlib.sha256(
        json.dumps(
            [account, organization], separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    return "tenant_" + digest


def _mutation(
    service: UserMCPService, item: UserMCPServer, operation: str
) -> Mapping[str, Any]:
    return {
        "server": item.projection(),
        "restart_required": True,
        "restart_scheduled": service.request_restart(item.server_id, operation),
    }


def _http_error(error: UserMCPError) -> HTTPException:
    return HTTPException(
        status_code=error.http_status,
        detail={"code": error.code},
    )


__all__ = [
    "UserMCPError",
    "UserMCPServer",
    "UserMCPServerRequest",
    "UserMCPService",
    "create_user_mcp_router",
    "mcp_tenant_namespace",
]
