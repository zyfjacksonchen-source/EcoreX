"""OAuth 2.1 + PKCE authority for verified remote MCP bindings."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from ecorex.connectors.vault import CredentialVault
from ecorex.json_boundary import JSONComplexityError, validate_json_complexity


_SAFE_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_MAX_DOCUMENT_BYTES = 64 * 1024
_PENDING_SECONDS = 600


class MCPOAuthError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class MCPOAuthRegistration:
    service_id: str
    resource_url: str
    expected_host: str
    client_id: str | None = None
    scope: str = ""
    authorization_hosts: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if _SAFE_ID.fullmatch(self.service_id) is None:
            raise ValueError("MCP OAuth service ID is invalid")
        resource = _validated_https_url(
            self.resource_url,
            frozenset({self.expected_host}),
            allow_query=False,
        )
        if resource != self.resource_url:
            raise ValueError("MCP OAuth resource URL must be canonical")
        if self.client_id is not None and not 1 <= len(self.client_id) <= 512:
            raise ValueError("MCP OAuth client ID is invalid")
        if len(self.scope.encode("utf-8")) > 2048:
            raise ValueError("MCP OAuth scope is too large")
        hosts = frozenset(
            host.casefold().rstrip(".")
            for host in ({self.expected_host} | set(self.authorization_hosts))
            if host
        )
        if not hosts:
            raise ValueError("MCP OAuth authorization hosts are required")
        object.__setattr__(self, "authorization_hosts", hosts)


@dataclass(frozen=True, slots=True)
class MCPOAuthStatus:
    service_id: str
    state: str
    expires_at: int | None
    scope: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "state": self.state,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class _PendingAuthorization:
    service_id: str
    tenant_id: str
    verifier: str
    created_at: float
    metadata: Mapping[str, str]
    client_id: str
    client_secret: str | None
    scope: str
    credential_generation: int


class _MCPOAuthTokenProvider:
    def __init__(
        self, service: "MCPOAuthService", tenant_id: str, service_id: str
    ) -> None:
        self._service = service
        self._tenant_id = tenant_id
        self._service_id = service_id

    async def access_token(self) -> str | None:
        return await self._service.access_token(self._service_id, self._tenant_id)

    async def refresh_after_unauthorized(self) -> str | None:
        return await self._service.refresh(self._service_id, self._tenant_id)


class MCPOAuthService:
    """Tenant-isolated OAuth state; credential material stays in the OS vault."""

    def __init__(
        self,
        registrations: tuple[MCPOAuthRegistration, ...],
        *,
        redirect_uri: str,
        vault: CredentialVault,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        by_id = {item.service_id: item for item in registrations}
        if len(by_id) != len(registrations):
            raise ValueError("MCP OAuth registrations must be unique")
        parsed_redirect = urlsplit(redirect_uri)
        if (
            parsed_redirect.scheme != "http"
            or parsed_redirect.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_redirect.username is not None
            or parsed_redirect.password is not None
            or parsed_redirect.query
            or parsed_redirect.fragment
        ):
            raise ValueError("MCP OAuth redirect must be an exact loopback HTTP URI")
        self.registrations = by_id
        self.redirect_uri = redirect_uri
        self.vault = vault
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None
        self._pending: dict[str, _PendingAuthorization] = {}
        self._lock = asyncio.Lock()
        self._credential_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._credential_generations: dict[tuple[str, str], int] = {}

    def provider(self, tenant_id: str, service_id: str) -> _MCPOAuthTokenProvider:
        self._registration(service_id)
        return _MCPOAuthTokenProvider(self, _safe_tenant(tenant_id), service_id)

    async def statuses(self, tenant_id: str) -> tuple[MCPOAuthStatus, ...]:
        return tuple(
            [
                await self.status(service_id, tenant_id)
                for service_id in sorted(self.registrations)
            ]
        )

    async def status(self, service_id: str, tenant_id: str) -> MCPOAuthStatus:
        registration = self._registration(service_id)
        tenant_id = _safe_tenant(tenant_id)
        record = await self._load_record(registration, tenant_id)
        now = int(time.time())
        if record.get("access_token") and (
            not record.get("expires_at") or int(record["expires_at"]) > now
        ):
            state = "authorized"
        elif record.get("refresh_token"):
            state = "reauthorization_required"
        else:
            async with self._lock:
                self._prune_pending()
                state = (
                    "authorizing"
                    if any(
                        item.service_id == service_id and item.tenant_id == tenant_id
                        for item in self._pending.values()
                    )
                    else "authorization_required"
                )
        expires_at = int(record["expires_at"]) if record.get("expires_at") else None
        return MCPOAuthStatus(
            service_id=service_id,
            state=state,
            expires_at=expires_at,
            scope=record.get("scope", registration.scope),
        )

    async def begin(self, service_id: str, tenant_id: str) -> Mapping[str, Any]:
        registration = self._registration(service_id)
        tenant_id = _safe_tenant(tenant_id)
        credential_key = (service_id, tenant_id)
        async with self._credential_lock(credential_key):
            stored = await self._load_record(registration, tenant_id)
            metadata = await self._metadata(registration, stored)
            client_id = registration.client_id or stored.get("client_id")
            client_secret = stored.get("client_secret")
            scope = (
                registration.scope or stored.get("scope") or metadata.get("scope", "")
            )
            if not client_id:
                registration_endpoint = metadata.get("registration_endpoint")
                if not registration_endpoint:
                    raise MCPOAuthError("mcp_oauth_client_registration_required")
                registered = await self._request_json(
                    "POST",
                    registration_endpoint,
                    registration.authorization_hosts,
                    json_body={
                        "client_name": "e-Mate",
                        "redirect_uris": [self.redirect_uri],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "token_endpoint_auth_method": "none",
                    },
                    expected_statuses={200, 201},
                )
                client_id = _bounded_secret(registered.get("client_id"), "client_id")
                client_secret = _optional_secret(
                    registered.get("client_secret"), "client_secret"
                )
            verifier = _b64url(secrets.token_bytes(48))
            challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
            state = secrets.token_urlsafe(32)
            await self._save_record(
                registration,
                tenant_id,
                {
                    **stored,
                    **metadata,
                    "client_id": client_id,
                    **({"client_secret": client_secret} if client_secret else {}),
                    **({"scope": scope} if scope else {}),
                },
            )
            generation = self._advance_credential_generation(credential_key)
            pending = _PendingAuthorization(
                service_id=service_id,
                tenant_id=tenant_id,
                verifier=verifier,
                created_at=time.time(),
                metadata=metadata,
                client_id=client_id,
                client_secret=client_secret,
                scope=scope,
                credential_generation=generation,
            )
            async with self._lock:
                self._prune_pending()
                self._pending = {
                    key: item
                    for key, item in self._pending.items()
                    if not (
                        item.service_id == service_id and item.tenant_id == tenant_id
                    )
                }
                self._pending[state] = pending
        parameters = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": registration.resource_url,
        }
        if scope:
            parameters["scope"] = scope
        authorization_url = (
            metadata["authorization_endpoint"] + "?" + urlencode(parameters)
        )
        return {
            "service_id": service_id,
            "state": "authorizing",
            "authorization_url": authorization_url,
            "expires_at": int(pending.created_at + _PENDING_SECONDS),
        }

    async def complete(
        self,
        *,
        state: str,
        code: str | None,
        error: str | None = None,
    ) -> MCPOAuthStatus:
        if not state or len(state) > 512:
            raise MCPOAuthError("mcp_oauth_state_invalid")
        async with self._lock:
            self._prune_pending()
            pending = self._pending.pop(state, None)
        if pending is None:
            raise MCPOAuthError("mcp_oauth_state_invalid")
        if error:
            raise MCPOAuthError("mcp_oauth_authorization_denied")
        if not code or len(code) > 8192:
            raise MCPOAuthError("mcp_oauth_code_invalid")
        registration = self._registration(pending.service_id)
        credential_key = (pending.service_id, pending.tenant_id)
        async with self._credential_lock(credential_key):
            if (
                self._credential_generation(credential_key)
                != pending.credential_generation
            ):
                raise MCPOAuthError("mcp_oauth_state_invalid")
            fields = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": pending.client_id,
                "code_verifier": pending.verifier,
                "resource": registration.resource_url,
            }
            if pending.client_secret:
                fields["client_secret"] = pending.client_secret
            token = await self._request_json(
                "POST",
                pending.metadata["token_endpoint"],
                registration.authorization_hosts,
                data=fields,
                expected_statuses={200},
            )
            record = await self._load_record(registration, pending.tenant_id)
            await self._save_record(
                registration,
                pending.tenant_id,
                _token_record(record, token, pending.scope),
            )
            self._advance_credential_generation(credential_key)
        return await self.status(pending.service_id, pending.tenant_id)

    async def access_token(self, service_id: str, tenant_id: str) -> str | None:
        registration = self._registration(service_id)
        tenant_id = _safe_tenant(tenant_id)
        credential_key = (service_id, tenant_id)
        observed_generation = self._credential_generation(credential_key)
        record = await self._load_record(registration, tenant_id)
        if not record.get("access_token"):
            return None
        expires_at = int(record.get("expires_at", "0") or 0)
        if expires_at and expires_at <= int(time.time()) + 60:
            return await self._refresh(
                registration,
                tenant_id,
                credential_key,
                observed_generation,
            )
        return record["access_token"]

    async def refresh(self, service_id: str, tenant_id: str) -> str | None:
        registration = self._registration(service_id)
        tenant_id = _safe_tenant(tenant_id)
        credential_key = (service_id, tenant_id)
        return await self._refresh(
            registration,
            tenant_id,
            credential_key,
            self._credential_generation(credential_key),
        )

    async def _refresh(
        self,
        registration: MCPOAuthRegistration,
        tenant_id: str,
        credential_key: tuple[str, str],
        observed_generation: int,
    ) -> str | None:
        async with self._credential_lock(credential_key):
            record = await self._load_record(registration, tenant_id)
            if self._credential_generation(credential_key) != observed_generation:
                return record.get("access_token")
            if not record.get("refresh_token") or not record.get("token_endpoint"):
                return None
            fields = {
                "grant_type": "refresh_token",
                "refresh_token": record["refresh_token"],
                "client_id": record.get("client_id", ""),
                "resource": registration.resource_url,
            }
            if record.get("client_secret"):
                fields["client_secret"] = record["client_secret"]
            try:
                token = await self._request_json(
                    "POST",
                    record["token_endpoint"],
                    registration.authorization_hosts,
                    data=fields,
                    expected_statuses={200},
                )
                updated = _token_record(record, token, record.get("scope", ""))
            except MCPOAuthError:
                await self._save_record(
                    registration,
                    tenant_id,
                    {
                        key: value
                        for key, value in record.items()
                        if key != "access_token"
                    },
                )
                self._advance_credential_generation(credential_key)
                return None
            await self._save_record(registration, tenant_id, updated)
            self._advance_credential_generation(credential_key)
            return updated["access_token"]

    async def clear(self, service_id: str, tenant_id: str) -> None:
        registration = self._registration(service_id)
        tenant_id = _safe_tenant(tenant_id)
        credential_key = (service_id, tenant_id)
        async with self._credential_lock(credential_key):
            record = await self._load_record(registration, tenant_id)
            token = record.get("refresh_token") or record.get("access_token")
            if token and record.get("revocation_endpoint"):
                fields = {"token": token, "client_id": record.get("client_id", "")}
                if record.get("client_secret"):
                    fields["client_secret"] = record["client_secret"]
                try:
                    await self._request_json(
                        "POST",
                        record["revocation_endpoint"],
                        registration.authorization_hosts,
                        data=fields,
                        expected_statuses={200, 204},
                        allow_empty=True,
                    )
                except MCPOAuthError:
                    pass
            try:
                await asyncio.to_thread(
                    self.vault.delete, self._reference(registration, tenant_id)
                )
            except RuntimeError:
                raise MCPOAuthError("mcp_oauth_vault_unavailable") from None
            self._advance_credential_generation(credential_key)
        async with self._lock:
            self._pending = {
                key: item
                for key, item in self._pending.items()
                if not (item.service_id == service_id and item.tenant_id == tenant_id)
            }

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self.close()

    async def _metadata(
        self,
        registration: MCPOAuthRegistration,
        stored: Mapping[str, str],
    ) -> dict[str, str]:
        if stored.get("authorization_endpoint") and stored.get("token_endpoint"):
            cached: dict[str, str] = {}
            for key, value in stored.items():
                if key.endswith("_endpoint"):
                    cached[key] = _validated_https_url(
                        value,
                        registration.authorization_hosts,
                        allow_query=False,
                    )
                elif key == "scope":
                    cached[key] = value[:2048]
            return cached
        resource = urlsplit(registration.resource_url)
        resource_origin = f"{resource.scheme}://{resource.netloc}"
        protected = await self._request_json(
            "GET",
            resource_origin + "/.well-known/oauth-protected-resource",
            frozenset({registration.expected_host.casefold().rstrip(".")}),
            expected_statuses={200},
        )
        advertised = protected.get("authorization_servers")
        auth_server = (
            advertised[0]
            if isinstance(advertised, list)
            and advertised
            and isinstance(advertised[0], str)
            else resource_origin
        )
        auth_server = _validated_https_url(
            auth_server,
            registration.authorization_hosts,
            allow_query=False,
        ).rstrip("/")
        metadata = await self._request_json(
            "GET",
            auth_server + "/.well-known/oauth-authorization-server",
            registration.authorization_hosts,
            expected_statuses={200},
        )
        issuer = metadata.get("issuer", auth_server)
        if not isinstance(issuer, str) or issuer.rstrip("/") != auth_server:
            raise MCPOAuthError("mcp_oauth_metadata_invalid")
        normalized: dict[str, str] = {}
        for key in (
            "authorization_endpoint",
            "token_endpoint",
            "registration_endpoint",
            "revocation_endpoint",
        ):
            value = metadata.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise MCPOAuthError("mcp_oauth_metadata_invalid")
            normalized[key] = _validated_https_url(
                value,
                registration.authorization_hosts,
                allow_query=False,
            )
        if not {"authorization_endpoint", "token_endpoint"} <= set(normalized):
            raise MCPOAuthError("mcp_oauth_metadata_invalid")
        protected_scopes = protected.get("required_scopes") or protected.get(
            "scopes_supported"
        )
        if not registration.scope and isinstance(protected_scopes, list):
            scope = " ".join(item for item in protected_scopes if isinstance(item, str))
            if scope:
                normalized["scope"] = scope[:2048]
        return normalized

    async def _request_json(
        self,
        method: str,
        url: str,
        allowed_hosts: frozenset[str],
        *,
        expected_statuses: set[int],
        json_body: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
        allow_empty: bool = False,
    ) -> Mapping[str, Any]:
        target = _validated_https_url(url, allowed_hosts, allow_query=False)
        try:
            response = await self.client.request(
                method,
                target,
                json=dict(json_body) if json_body is not None else None,
                data=dict(data) if data is not None else None,
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            raise MCPOAuthError("mcp_oauth_transport_failed") from None
        if response.status_code in {301, 302, 303, 307, 308}:
            raise MCPOAuthError("mcp_oauth_redirect_forbidden")
        if response.status_code not in expected_statuses:
            raise MCPOAuthError("mcp_oauth_http_failed")
        if len(response.content) > _MAX_DOCUMENT_BYTES:
            raise MCPOAuthError("mcp_oauth_response_too_large")
        if not response.content and allow_empty:
            return {}
        if (
            response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
            != "application/json"
        ):
            raise MCPOAuthError("mcp_oauth_response_invalid")
        try:
            payload = response.json()
        except (json.JSONDecodeError, RecursionError, UnicodeDecodeError):
            raise MCPOAuthError("mcp_oauth_response_invalid") from None
        if not isinstance(payload, dict):
            raise MCPOAuthError("mcp_oauth_response_invalid")
        try:
            validate_json_complexity(payload)
        except JSONComplexityError:
            raise MCPOAuthError("mcp_oauth_response_invalid") from None
        return payload

    async def _load_record(
        self,
        registration: MCPOAuthRegistration,
        tenant_id: str,
    ) -> dict[str, str]:
        try:
            return dict(
                await asyncio.to_thread(
                    self.vault.get, self._reference(registration, tenant_id)
                )
            )
        except KeyError:
            return {}
        except RuntimeError:
            raise MCPOAuthError("mcp_oauth_vault_unavailable") from None

    async def _save_record(
        self,
        registration: MCPOAuthRegistration,
        tenant_id: str,
        record: Mapping[str, str],
    ) -> None:
        material = {str(key): str(value) for key, value in record.items() if value}
        if not material:
            raise MCPOAuthError("mcp_oauth_record_invalid")
        try:
            await asyncio.to_thread(
                self.vault.put,
                self._reference(registration, tenant_id),
                material,
            )
        except RuntimeError:
            raise MCPOAuthError("mcp_oauth_vault_unavailable") from None

    @staticmethod
    def _reference(registration: MCPOAuthRegistration, tenant_id: str) -> str:
        registration_generation = json.dumps(
            {
                "authorization_hosts": sorted(registration.authorization_hosts),
                "client_id": registration.client_id,
                "expected_host": registration.expected_host,
                "resource_url": registration.resource_url,
                "scope": registration.scope,
                "service_id": registration.service_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            f"{tenant_id}\0{registration_generation}".encode("utf-8")
        ).hexdigest()
        return f"ecorex/mcp-oauth/{digest}"

    def _credential_lock(self, key: tuple[str, str]) -> asyncio.Lock:
        return self._credential_locks.setdefault(key, asyncio.Lock())

    def _credential_generation(self, key: tuple[str, str]) -> int:
        return self._credential_generations.get(key, 0)

    def _advance_credential_generation(self, key: tuple[str, str]) -> int:
        generation = self._credential_generation(key) + 1
        self._credential_generations[key] = generation
        return generation

    def _registration(self, service_id: str) -> MCPOAuthRegistration:
        registration = self.registrations.get(service_id)
        if registration is None:
            raise MCPOAuthError("mcp_oauth_service_not_found")
        return registration

    def _prune_pending(self) -> None:
        cutoff = time.time() - _PENDING_SECONDS
        self._pending = {
            state: item
            for state, item in self._pending.items()
            if item.created_at >= cutoff
        }


def register_mcp_oauth_routes(
    app: FastAPI,
    service: MCPOAuthService,
    *,
    tenant_id: str,
) -> None:
    router = APIRouter(prefix="/api/v1/mcp/oauth", tags=["mcp-oauth"])

    @router.get("")
    async def oauth_statuses() -> Mapping[str, Any]:
        try:
            items = await service.statuses(tenant_id)
        except MCPOAuthError as error:
            raise _http_error(error) from error
        return {"items": [item.to_dict() for item in items]}

    @router.post("/{service_id}/begin")
    async def begin(service_id: str) -> Mapping[str, Any]:
        try:
            return await service.begin(service_id, tenant_id)
        except MCPOAuthError as error:
            raise _http_error(error) from error

    @router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def clear(service_id: str) -> None:
        try:
            await service.clear(service_id, tenant_id)
        except MCPOAuthError as error:
            raise _http_error(error) from error

    @router.get("/callback")
    async def callback(request: Request) -> HTMLResponse:
        if len(str(request.url.query).encode("utf-8")) > 16_384:
            return _callback_html(None, MCPOAuthError("mcp_oauth_callback_invalid"))
        state_value = request.query_params.get("state", "")
        try:
            projection = await service.complete(
                state=state_value,
                code=request.query_params.get("code"),
                error=request.query_params.get("error"),
            )
            return _callback_html(projection, None)
        except MCPOAuthError as error:
            return _callback_html(None, error)

    app.include_router(router)


def _callback_html(
    projection: MCPOAuthStatus | None,
    error: MCPOAuthError | None,
) -> HTMLResponse:
    succeeded = projection is not None and error is None
    payload = {
        "source": "ecorex.mcp.oauth",
        "status": "completed" if succeeded else "failed",
        **(
            {"service_id": projection.service_id}
            if projection is not None
            else {"error_code": error.code if error else "mcp_oauth_failed"}
        ),
    }
    payload_json = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    nonce = secrets.token_urlsafe(24)
    title = "授权已完成" if succeeded else "授权未完成"
    message = "可以关闭此窗口并返回 e-Mate。" if succeeded else "请返回 e-Mate 后重试。"
    content = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style nonce="{nonce}">:root{{color-scheme:light dark;font-family:system-ui,sans-serif}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:Canvas;color:CanvasText}}main{{padding:2rem;text-align:center}}h1{{font-size:1.25rem}}p{{opacity:.75}}</style></head><body><main><h1>{title}</h1><p>{message}</p></main><script nonce="{nonce}">try{{if(window.opener&&!window.opener.closed)window.opener.postMessage({payload_json},window.location.origin)}}finally{{setTimeout(()=>window.close(),80)}}</script></body></html>"""
    return HTMLResponse(
        content,
        status_code=200 if succeeded else 400,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _http_error(error: MCPOAuthError) -> HTTPException:
    code = 404 if error.code == "mcp_oauth_service_not_found" else 422
    if error.code in {"mcp_oauth_transport_failed", "mcp_oauth_vault_unavailable"}:
        code = 503
    if error.code in {"mcp_oauth_state_invalid", "mcp_oauth_authorization_denied"}:
        code = 409
    return HTTPException(status_code=code, detail={"code": error.code})


def _safe_tenant(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}", value
    ):
        raise MCPOAuthError("mcp_oauth_tenant_invalid")
    return value


def _validated_https_url(
    value: str, hosts: frozenset[str], *, allow_query: bool
) -> str:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or (parsed.query and not allow_query)
        or parsed.fragment
    ):
        raise MCPOAuthError("mcp_oauth_endpoint_invalid")
    return value


def _b64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _bounded_secret(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192 or "\x00" in value:
        raise MCPOAuthError(f"mcp_oauth_{label}_invalid")
    return value


def _optional_secret(value: Any, label: str) -> str | None:
    return None if value is None else _bounded_secret(value, label)


def _token_record(
    current: Mapping[str, str],
    response: Mapping[str, Any],
    scope: str,
) -> dict[str, str]:
    token_type = str(response.get("token_type", "Bearer"))
    if token_type.casefold() != "bearer":
        raise MCPOAuthError("mcp_oauth_token_type_invalid")
    access_token = _bounded_secret(response.get("access_token"), "access_token")
    refresh_token = _optional_secret(response.get("refresh_token"), "refresh_token")
    try:
        expires_in = int(response.get("expires_in", 3600))
    except (TypeError, ValueError):
        raise MCPOAuthError("mcp_oauth_token_expiry_invalid") from None
    if not 1 <= expires_in <= 366 * 24 * 60 * 60:
        raise MCPOAuthError("mcp_oauth_token_expiry_invalid")
    resolved_scope = str(response.get("scope") or scope)
    if len(resolved_scope.encode("utf-8")) > 2048:
        raise MCPOAuthError("mcp_oauth_scope_invalid")
    return {
        **dict(current),
        "access_token": access_token,
        **({"refresh_token": refresh_token} if refresh_token else {}),
        "expires_at": str(int(time.time()) + expires_in),
        **({"scope": resolved_scope} if resolved_scope else {}),
    }


__all__ = [
    "MCPOAuthError",
    "MCPOAuthRegistration",
    "MCPOAuthService",
    "MCPOAuthStatus",
    "register_mcp_oauth_routes",
]
