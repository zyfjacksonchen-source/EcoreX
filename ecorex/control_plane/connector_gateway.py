"""Managed Feishu gateway mounted behind the existing Control Plane boundary."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlencode
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
import httpx
from pydantic import BaseModel, ConfigDict, Field

from ecorex.observability.audit import AuditPayloadCipher
from ecorex.protocol import AuditRecordProjection
from ecorex.runtime.database import json_dumps

from .audit import CloudAuditRepository
from .models import ControlPrincipal


FEISHU_OAUTH_RETURN_URI = (
    "http://127.0.0.1:8765/api/v1/connectors/oauth/callback"
)
FEISHU_AUTHORIZATION_URL = (
    "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
)
FEISHU_ORIGIN = "https://open.feishu.cn"
FEISHU_SCOPES = (
    "docx:document",
    "docx:document:readonly",
    "drive:drive:readonly",
    "im:message",
    "im:message.send_as_user",
    "offline_access",
)

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_.:-]{1,255}$")
_SAFE_GRANT = re.compile(r"^fgrant_[A-Za-z0-9_-]{32,128}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
_ACTION_SCOPES = {
    "documents.read": frozenset({"docx:document:readonly"}),
    "documents.write": frozenset({"docx:document"}),
    "drive.search": frozenset({"drive:drive:readonly"}),
    "messages.send": frozenset({"im:message", "im:message.send_as_user"}),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ConnectorGatewayError(f"invalid_{name}", status_code=422)
    return value


def _bounded_content(value: Any, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(character) < 32 and character not in "\t\n\r" for character in value)
    ):
        raise ConnectorGatewayError(f"invalid_{name}", status_code=422)
    return value


def _stable_uuid(value: str) -> str:
    return str(uuid.UUID(bytes=hashlib.sha256(value.encode("utf-8")).digest()[:16]))


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BeginAuthRequest(_StrictModel):
    flow_id: str = Field(min_length=1, max_length=256)
    auth_kind: str = Field(min_length=1, max_length=64)
    return_uri: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=16, max_length=512)
    code_challenge: str = Field(min_length=43, max_length=128)
    code_challenge_method: str = Field(min_length=1, max_length=16)


class CompleteAuthRequest(_StrictModel):
    flow_id: str = Field(min_length=1, max_length=256)
    response: dict[str, str]
    private_state: dict[str, str]


class ManagedGrantRequest(_StrictModel):
    managed_grant: str = Field(min_length=1, max_length=16 * 1024)


class InvokeRequest(ManagedGrantRequest):
    inputs: dict[str, Any]
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=512)


class ConnectorGatewayError(RuntimeError):
    """One redacted error contract; provider response text never crosses it."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int = 503,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class FeishuProviderClient:
    """Closed HTTP client for current Feishu OAuth/OpenAPI endpoints."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=FEISHU_ORIGIN,
            timeout=httpx.Timeout(connect=10, read=60, write=60, pool=10),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json; charset=utf-8",
        }
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        try:
            response = await self.client.request(
                method,
                path,
                params=dict(params or {}),
                content=(None if body is None else _canonical(dict(body)).encode("utf-8")),
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            raise ConnectorGatewayError(
                "provider_unavailable", retryable=True
            ) from None
        if response.is_redirect or response.history:
            raise ConnectorGatewayError("provider_redirect_refused")
        if response.headers.get("content-encoding", "identity").casefold() != "identity":
            raise ConnectorGatewayError("provider_response_invalid")
        if len(response.content) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise ConnectorGatewayError("provider_response_too_large")
        try:
            value = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ConnectorGatewayError("provider_response_invalid") from None
        if not isinstance(value, dict):
            raise ConnectorGatewayError("provider_response_invalid")
        code = value.get("code")
        if response.status_code >= 500 or response.status_code in {408, 425, 429}:
            raise ConnectorGatewayError(
                "provider_unavailable", retryable=True
            )
        if response.status_code not in {200, 201, 202} or code not in {None, 0}:
            provider_code = str(code) if isinstance(code, int) else ""
            if provider_code in {"20005", "20024", "20025", "20036", "20037"}:
                raise ConnectorGatewayError(
                    "provider_authorization_expired", status_code=401
                )
            if provider_code in {"99991400", "230020", "230049"}:
                raise ConnectorGatewayError(
                    "provider_rate_limited", status_code=429, retryable=True
                )
            raise ConnectorGatewayError("provider_rejected", status_code=422)
        return value


class FeishuConnectorGateway:
    """OAuth/token/action boundary; provider credentials never leave this object."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        app_id: str,
        app_secret: str,
        encryption_key: bytes,
        audit_repository: CloudAuditRepository,
        provider: FeishuProviderClient | None = None,
        oauth_return_uri: str = FEISHU_OAUTH_RETURN_URI,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.app_id = _bounded_text(app_id, "feishu_app_id", 256)
        self.app_secret = _bounded_text(app_secret, "feishu_app_secret", 1024)
        if not isinstance(audit_repository, CloudAuditRepository):
            raise TypeError("cloud audit repository is required")
        if oauth_return_uri != FEISHU_OAUTH_RETURN_URI:
            raise ValueError("Feishu OAuth return URI is not the signed desktop URI")
        self.oauth_return_uri = oauth_return_uri
        self.cipher = AuditPayloadCipher(encryption_key)
        self.audit_repository = audit_repository
        self.provider = provider or FeishuProviderClient()
        self.clock = clock
        self._grant_locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        self.app_secret = ""
        await self.provider.aclose()

    def create_router(
        self,
        *,
        principal_dependency: Callable[..., ControlPrincipal],
    ) -> APIRouter:
        router = APIRouter(prefix="/api/v1/connectors/feishu", tags=["connectors"])

        @router.post("/auth/begin")
        async def begin_auth(
            request: BeginAuthRequest,
            idempotency_key: str = Header(..., alias="Idempotency-Key"),
            principal: ControlPrincipal = Depends(principal_dependency),
        ) -> dict[str, Any]:
            return await self._run_idempotent(
                principal,
                idempotency_key,
                "connector.auth.begin",
                request.model_dump(mode="json"),
                lambda: self._begin_auth(principal, request),
            )

        @router.post("/auth/complete")
        async def complete_auth(
            request: CompleteAuthRequest,
            idempotency_key: str = Header(..., alias="Idempotency-Key"),
            principal: ControlPrincipal = Depends(principal_dependency),
        ) -> dict[str, Any]:
            return await self._run_idempotent(
                principal,
                idempotency_key,
                "connector.auth.complete",
                request.model_dump(mode="json"),
                lambda: self._complete_auth(principal, request),
            )

        @router.post("/health")
        async def health(
            request: ManagedGrantRequest,
            principal: ControlPrincipal = Depends(principal_dependency),
        ) -> dict[str, Any]:
            source_id = "connhealth_" + uuid.uuid4().hex
            try:
                result = await self._health(principal, request.managed_grant)
                self._audit(principal, "connector.health", source_id, result)
                return result
            except ConnectorGatewayError as error:
                if error.code == "provider_authorization_expired":
                    result = {"health": "error", "error_code": "authorization_expired"}
                    self._audit(principal, "connector.health", source_id, result)
                    return result
                self._audit_failure(principal, "connector.health", source_id, error)
                raise self._http_error(error) from None

        @router.post("/actions/{action_id}")
        async def invoke(
            action_id: str,
            request: InvokeRequest,
            idempotency_key: str | None = Header(
                default=None, alias="Idempotency-Key"
            ),
            principal: ControlPrincipal = Depends(principal_dependency),
        ) -> dict[str, Any]:
            if action_id not in _ACTION_SCOPES:
                raise self._http_error(
                    ConnectorGatewayError("action_not_supported", status_code=404)
                )
            if request.idempotency_key != idempotency_key:
                raise self._http_error(
                    ConnectorGatewayError("idempotency_mismatch", status_code=422)
                )
            write = action_id in {"documents.write", "messages.send"}
            if write and idempotency_key is None:
                raise self._http_error(
                    ConnectorGatewayError("idempotency_required", status_code=422)
                )
            key = idempotency_key or ("read_" + uuid.uuid4().hex)
            return await self._run_idempotent(
                principal,
                key,
                "connector.action." + action_id,
                request.model_dump(mode="json"),
                lambda: self._invoke(principal, action_id, request, key),
            )

        @router.post("/revoke")
        async def revoke(
            request: ManagedGrantRequest,
            idempotency_key: str = Header(..., alias="Idempotency-Key"),
            principal: ControlPrincipal = Depends(principal_dependency),
        ) -> dict[str, Any]:
            return await self._run_idempotent(
                principal,
                idempotency_key,
                "connector.revoke",
                request.model_dump(mode="json"),
                lambda: self._revoke(principal, request.managed_grant),
            )

        return router

    async def _run_idempotent(
        self,
        principal: ControlPrincipal,
        key: str,
        operation: str,
        request: Mapping[str, Any],
        invoke: Callable[[], Any],
    ) -> dict[str, Any]:
        _bounded_text(key, "idempotency_key", 512)
        fingerprint = _sha(request)
        organization_id = principal.organization_id or ""
        replay = self._reserve_idempotency(
            principal.account_id, organization_id, key, operation, fingerprint
        )
        operation_created_at = self._idempotency_created_at(
            principal.account_id, organization_id, key
        )
        source_id = "connop_" + hashlib.sha256(
            (
                principal.account_id
                + "\0"
                + organization_id
                + "\0"
                + operation
                + "\0"
                + key
            ).encode("utf-8")
        ).hexdigest()
        audit_payload = {
            "connector_id": "feishu",
            "operation": operation,
            "status": "completed",
            "action_id": operation.removeprefix("connector.action.")
            if operation.startswith("connector.action.")
            else None,
        }
        if replay is not None:
            try:
                self._audit(
                    principal,
                    operation,
                    source_id,
                    audit_payload,
                    created_at=operation_created_at,
                )
            except Exception:
                raise self._http_error(
                    ConnectorGatewayError("gateway_unavailable", retryable=True)
                ) from None
            return replay
        completed_audit = False
        try:
            value = await invoke()
            if not isinstance(value, dict):
                raise ConnectorGatewayError("gateway_result_invalid")
            self._audit(
                principal,
                operation,
                source_id,
                audit_payload,
                created_at=operation_created_at,
            )
            completed_audit = True
            self._complete_idempotency(
                principal.account_id, organization_id, key, value
            )
            return value
        except ConnectorGatewayError as error:
            self._fail_idempotency(
                principal.account_id, organization_id, key, error.code
            )
            self._audit_failure(
                principal,
                operation,
                source_id,
                error,
                created_at=operation_created_at,
            )
            raise self._http_error(error) from None
        except HTTPException:
            raise
        except Exception:
            error = ConnectorGatewayError("gateway_unavailable", retryable=True)
            self._fail_idempotency(
                principal.account_id, organization_id, key, error.code
            )
            if not completed_audit:
                self._audit_failure(
                    principal,
                    operation,
                    source_id,
                    error,
                    created_at=operation_created_at,
                )
            raise self._http_error(error) from None

    async def _begin_auth(
        self, principal: ControlPrincipal, request: BeginAuthRequest
    ) -> dict[str, Any]:
        if (
            request.auth_kind != "oauth2"
            or request.return_uri != self.oauth_return_uri
            or request.code_challenge_method != "S256"
            or _SAFE_ID.fullmatch(request.flow_id) is None
            or re.fullmatch(r"[A-Za-z0-9_-]{43,128}", request.code_challenge) is None
        ):
            raise ConnectorGatewayError("authorization_request_invalid", status_code=422)
        expires = self.clock().astimezone(UTC) + timedelta(minutes=10)
        query = urlencode(
            {
                "client_id": self.app_id,
                "redirect_uri": self.oauth_return_uri,
                "response_type": "code",
                "scope": " ".join(FEISHU_SCOPES),
                "state": request.state,
                "code_challenge": request.code_challenge,
                "code_challenge_method": "S256",
            }
        )
        challenge = {
            "flow_id": request.flow_id,
            "connector_id": "feishu",
            "auth_kind": "oauth2",
            "expires_at": _iso(expires),
            "authorization_url": FEISHU_AUTHORIZATION_URL + "?" + query,
            "user_code": None,
            "verification_url": None,
        }
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT challenge_json,account_id,organization_id,return_uri,"
                "state_sha256,code_challenge,status,expires_at "
                "FROM connector_gateway_flows "
                "WHERE flow_id=?",
                (request.flow_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["account_id"]) != principal.account_id
                    or existing["organization_id"] != principal.organization_id
                    or str(existing["return_uri"]) != self.oauth_return_uri
                    or str(existing["state_sha256"])
                    != hashlib.sha256(request.state.encode("utf-8")).hexdigest()
                    or str(existing["code_challenge"]) != request.code_challenge
                    or str(existing["status"]) != "active"
                    or datetime.fromisoformat(str(existing["expires_at"]))
                    <= self.clock()
                ):
                    raise ConnectorGatewayError("authorization_flow_conflict", status_code=409)
                return json.loads(str(existing["challenge_json"]))
            connection.execute(
                "INSERT INTO connector_gateway_flows("
                "flow_id,connector_id,account_id,organization_id,return_uri,"
                "state_sha256,code_challenge,challenge_json,status,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request.flow_id,
                    "feishu",
                    principal.account_id,
                    principal.organization_id,
                    self.oauth_return_uri,
                    hashlib.sha256(request.state.encode("utf-8")).hexdigest(),
                    request.code_challenge,
                    _canonical(challenge),
                    "active",
                    _iso(expires),
                    _iso(self.clock()),
                ),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return challenge

    async def _complete_auth(
        self, principal: ControlPrincipal, request: CompleteAuthRequest
    ) -> dict[str, Any]:
        if set(request.response) != {"code", "state"} or set(
            request.private_state
        ) != {"state", "pkce_verifier", "challenge_json"}:
            raise ConnectorGatewayError("authorization_response_invalid", status_code=422)
        code = _bounded_text(request.response.get("code"), "oauth_code", 4096)
        state = _bounded_text(request.response.get("state"), "oauth_state", 512)
        verifier = _bounded_text(
            request.private_state.get("pkce_verifier"), "pkce_verifier", 128
        )
        private_state = _bounded_text(
            request.private_state.get("state"), "oauth_state", 512
        )
        private_challenge = _bounded_text(
            request.private_state.get("challenge_json"), "challenge_json", 32 * 1024
        )
        if re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) is None:
            raise ConnectorGatewayError("authorization_response_invalid", status_code=422)
        if not secrets.compare_digest(private_state, state):
            raise ConnectorGatewayError("authorization_pkce_invalid", status_code=401)
        flow = self._flow(principal, request.flow_id)
        if not secrets.compare_digest(str(flow["challenge_json"]), private_challenge):
            raise ConnectorGatewayError("authorization_flow_invalid", status_code=401)
        if not secrets.compare_digest(
            str(flow["state_sha256"]), hashlib.sha256(state.encode("utf-8")).hexdigest()
        ):
            raise ConnectorGatewayError("authorization_state_invalid", status_code=401)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        if not secrets.compare_digest(str(flow["code_challenge"]), challenge):
            raise ConnectorGatewayError("authorization_pkce_invalid", status_code=401)
        flow_result_aad = (
            "feishu-flow-result:"
            + principal.account_id
            + ":"
            + (principal.organization_id or "")
            + ":"
            + request.flow_id
        )
        if flow["status"] == "consumed":
            result_envelope = flow["result_envelope_json"]
            if not isinstance(result_envelope, str):
                raise ConnectorGatewayError("authorization_result_unavailable")
            try:
                result = json.loads(
                    self.cipher.decrypt(
                        result_envelope, associated_data=flow_result_aad
                    )
                )
            except Exception:
                raise ConnectorGatewayError(
                    "authorization_result_unavailable"
                ) from None
            if not isinstance(result, dict) or set(result) != {
                "account_subject",
                "account_display_name",
                "granted_scopes",
                "managed_grant",
            }:
                raise ConnectorGatewayError("authorization_result_unavailable")
            return result
        if datetime.fromisoformat(flow["expires_at"]) <= self.clock():
            raise ConnectorGatewayError("authorization_flow_expired", status_code=401)

        token = await self.provider.request(
            "POST",
            "/open-apis/authen/v2/oauth/token",
            body={
                "grant_type": "authorization_code",
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "code": code,
                "redirect_uri": self.oauth_return_uri,
                "code_verifier": verifier,
            },
        )
        material, access_expires, refresh_expires, scopes = self._token_response(token)
        info = await self._user_info(str(material["access_token"]))
        account_subject = _bounded_text(info.get("open_id"), "provider_subject", 512)
        display_name = _bounded_text(
            info.get("name") or account_subject, "provider_display_name", 512
        )
        managed_grant = "fgrant_" + secrets.token_urlsafe(48)
        grant_sha = hashlib.sha256(managed_grant.encode("ascii")).hexdigest()
        envelope = self.cipher.encrypt(
            _canonical(material), associated_data="feishu-grant:" + grant_sha
        )
        result = {
            "account_subject": account_subject,
            "account_display_name": display_name,
            "granted_scopes": sorted(scopes),
            "managed_grant": managed_grant,
        }
        result_envelope = self.cipher.encrypt(
            _canonical(result), associated_data=flow_result_aad
        )
        now = self.clock().astimezone(UTC)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE connector_gateway_flows SET status='consumed',consumed_at=?,"
                "result_envelope_json=? WHERE flow_id=? AND account_id=? "
                "AND organization_id IS ? AND status='active'",
                (
                    _iso(now),
                    result_envelope,
                    request.flow_id,
                    principal.account_id,
                    principal.organization_id,
                ),
            ).rowcount
            if changed != 1:
                raise ConnectorGatewayError("authorization_flow_consumed", status_code=409)
            connection.execute(
                "INSERT INTO connector_gateway_grants("
                "grant_sha256,connector_id,account_id,organization_id,account_subject,"
                "account_display_name,granted_scopes_json,token_envelope_json,"
                "access_expires_at,refresh_expires_at,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    grant_sha,
                    "feishu",
                    principal.account_id,
                    principal.organization_id,
                    account_subject,
                    display_name,
                    _canonical(sorted(scopes)),
                    envelope,
                    _iso(access_expires),
                    _iso(refresh_expires) if refresh_expires else None,
                    1,
                    _iso(now),
                    _iso(now),
                ),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return result

    async def _health(
        self, principal: ControlPrincipal, managed_grant: str
    ) -> dict[str, Any]:
        grant_sha, _row, token = await self._usable_token(principal, managed_grant)
        try:
            await self._user_info(token)
        except ConnectorGatewayError as error:
            if error.code != "provider_authorization_expired":
                raise
            _row, token = await self._refresh(principal, grant_sha, force=True)
            await self._user_info(token)
        return {"health": "connected", "error_code": None}

    async def _invoke(
        self,
        principal: ControlPrincipal,
        action_id: str,
        request: InvokeRequest,
        idempotency_key: str,
    ) -> dict[str, Any]:
        grant_sha, row, token = await self._usable_token(
            principal, request.managed_grant
        )
        try:
            return await self._invoke_with_token(
                action_id, request.inputs, idempotency_key, row, token
            )
        except ConnectorGatewayError as error:
            if error.code != "provider_authorization_expired":
                raise
            row, token = await self._refresh(principal, grant_sha, force=True)
            return await self._invoke_with_token(
                action_id, request.inputs, idempotency_key, row, token
            )

    async def _invoke_with_token(
        self,
        action_id: str,
        inputs: Mapping[str, Any],
        idempotency_key: str,
        row: sqlite3.Row,
        token: str,
    ) -> dict[str, Any]:
        scopes = frozenset(json.loads(str(row["granted_scopes_json"])))
        if not _ACTION_SCOPES[action_id] <= scopes:
            raise ConnectorGatewayError("scope_not_granted", status_code=403)
        if action_id == "documents.read":
            return await self._documents_read(token, inputs)
        if action_id == "documents.write":
            return await self._documents_write(token, inputs, idempotency_key)
        if action_id == "drive.search":
            return await self._drive_search(token, inputs)
        return await self._messages_send(token, inputs, idempotency_key)

    async def _documents_read(
        self, token: str, inputs: Mapping[str, Any]
    ) -> dict[str, Any]:
        if set(inputs) != {"document_id"}:
            raise ConnectorGatewayError("action_input_invalid", status_code=422)
        document_id = _bounded_text(inputs.get("document_id"), "document_id", 512)
        quoted = quote(document_id, safe="")
        metadata, raw = await asyncio.gather(
            self.provider.request("GET", f"/open-apis/docx/v1/documents/{quoted}", token=token),
            self.provider.request(
                "GET", f"/open-apis/docx/v1/documents/{quoted}/raw_content", token=token
            ),
        )
        document = self._data_object(metadata, "document")
        raw_data = self._data(raw)
        content = raw_data.get("content")
        if not isinstance(content, str):
            raise ConnectorGatewayError("provider_response_invalid")
        return {
            "ok": True,
            "action_id": "documents.read",
            "document_id": document_id,
            "revision_id": str(document.get("revision_id"))
            if document.get("revision_id") is not None
            else None,
            "title": str(document.get("title")) if document.get("title") is not None else None,
            "content": content,
            "url": None,
            "updated_at": None,
            "document": {
                "document_id": document_id,
                "revision_id": str(document.get("revision_id"))
                if document.get("revision_id") is not None
                else None,
                "title": str(document.get("title"))
                if document.get("title") is not None
                else None,
                "content": content,
                "url": None,
                "updated_at": None,
            },
        }

    async def _documents_write(
        self,
        token: str,
        inputs: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        allowed = {"document_id", "revision_id", "title", "content"}
        if (
            not set(inputs) <= allowed
            or ("title" in inputs) == ("content" in inputs)
        ):
            raise ConnectorGatewayError("action_input_invalid", status_code=422)
        document_id = inputs.get("document_id")
        title = inputs.get("title")
        content = inputs.get("content")
        if title is not None:
            title = _bounded_text(title, "document_title", 3200)
            if len(title) > 800:
                raise ConnectorGatewayError("action_input_invalid", status_code=422)
        if content is not None and (
            not isinstance(content, str)
            or len(content.encode("utf-8")) > 4 * 1024 * 1024
        ):
            raise ConnectorGatewayError("action_input_invalid", status_code=422)
        if content and len(self._text_chunks(content)) > 50:
            raise ConnectorGatewayError("document_content_too_large", status_code=422)
        if document_id is None:
            raise ConnectorGatewayError("action_input_invalid", status_code=422)
        document_id = _bounded_text(document_id, "document_id", 512)
        current = await self.provider.request(
            "GET",
            f"/open-apis/docx/v1/documents/{quote(document_id, safe='')}",
            token=token,
        )
        document = self._data_object(current, "document")
        expected_revision = inputs.get("revision_id")
        mutation_revision = -1
        if expected_revision is not None:
            expected_revision = _bounded_text(
                expected_revision, "document_revision", 20
            )
            if re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", expected_revision) is None:
                raise ConnectorGatewayError("action_input_invalid", status_code=422)
            mutation_revision = int(expected_revision)
            if mutation_revision > 2**63 - 1:
                raise ConnectorGatewayError("action_input_invalid", status_code=422)
        revision_matches = expected_revision is None or (
            str(document.get("revision_id")) == expected_revision
        )
        quoted = quote(document_id, safe="")
        append_content = False
        if content is not None:
            append_content = await self._document_content_is_pending(
                token,
                document_id,
                content,
                mutation_revision if revision_matches else -1,
            )
        if not revision_matches:
            if title is not None and str(document.get("title")) == title:
                pass
            elif content is not None and not append_content:
                pass
            else:
                raise ConnectorGatewayError(
                    "document_revision_conflict", status_code=409
                )
        if title is not None and str(document.get("title")) != title:
            await self.provider.request(
                "PATCH",
                f"/open-apis/docx/v1/documents/{quoted}/blocks/{quoted}",
                token=token,
                params={
                    "document_revision_id": mutation_revision,
                    "client_token": _stable_uuid(idempotency_key + ":title"),
                },
                body={
                    "update_text_elements": {
                        "elements": [{"text_run": {"content": title}}]
                    }
                },
            )
        if content is not None and append_content:
            await self._append_document_content(
                token,
                document_id,
                content,
                idempotency_key,
                mutation_revision,
            )
        final = await self.provider.request(
            "GET", f"/open-apis/docx/v1/documents/{quoted}", token=token
        )
        document = self._data_object(final, "document")
        return {
            "ok": True,
            "action_id": "documents.write",
            "document_id": document_id,
            "revision_id": str(document.get("revision_id"))
            if document.get("revision_id") is not None
            else None,
            "title": str(document.get("title")) if document.get("title") is not None else title,
            "content": None,
            "url": None,
            "updated_at": None,
        }

    async def _document_content_is_pending(
        self,
        token: str,
        document_id: str,
        content: str,
        revision_id: int,
    ) -> bool:
        quoted = quote(document_id, safe="")
        children = await self.provider.request(
            "GET",
            f"/open-apis/docx/v1/documents/{quoted}/blocks/{quoted}/children",
            token=token,
            params={"document_revision_id": revision_id, "page_size": 500},
        )
        data = self._data(children)
        items = data.get("items") or data.get("children") or []
        if not isinstance(items, list) or data.get("has_more") is True:
            raise ConnectorGatewayError("document_too_large", status_code=422)
        if not items:
            return bool(content)
        expected = self._text_chunks(content)
        existing: list[str] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("text"), dict):
                break
            elements = item["text"].get("elements")
            if not isinstance(elements, list) or not elements:
                break
            runs: list[str] = []
            for element in elements:
                if not isinstance(element, dict) or not isinstance(
                    element.get("text_run"), dict
                ):
                    break
                run = element["text_run"].get("content")
                if not isinstance(run, str):
                    break
                runs.append(run)
            else:
                existing.append("".join(runs))
                continue
            break
        if len(existing) == len(items) and existing == expected:
            return False
        raise ConnectorGatewayError(
            "document_content_replace_unsupported", status_code=422
        )

    async def _append_document_content(
        self,
        token: str,
        document_id: str,
        content: str,
        idempotency_key: str,
        revision_id: int,
    ) -> None:
        quoted = quote(document_id, safe="")
        chunks = self._text_chunks(content)
        if len(chunks) > 50:
            raise ConnectorGatewayError("document_content_too_large", status_code=422)
        for offset in range(0, len(chunks), 50):
            await self.provider.request(
                "POST",
                f"/open-apis/docx/v1/documents/{quoted}/blocks/{quoted}/children",
                token=token,
                params={
                    "document_revision_id": revision_id,
                    "client_token": _stable_uuid(f"{idempotency_key}:content:{offset // 50}"),
                },
                body={
                    "index": -1,
                    "children": [
                        {
                            "block_type": 2,
                            "text": {
                                "elements": [{"text_run": {"content": chunk}}],
                                "style": {},
                            },
                        }
                        for chunk in chunks[offset : offset + 50]
                    ],
                },
            )

    @staticmethod
    def _text_chunks(content: str) -> list[str]:
        if not content:
            return []
        chunks: list[str] = []
        remaining = content
        while remaining:
            boundary = min(len(remaining), 20_000)
            if boundary < len(remaining):
                newline = remaining.rfind("\n", 0, boundary)
                if newline > 0:
                    boundary = newline + 1
            chunks.append(remaining[:boundary])
            remaining = remaining[boundary:]
        return chunks

    async def _drive_search(
        self, token: str, inputs: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not set(inputs) <= {"query", "cursor", "limit"} or "query" not in inputs:
            raise ConnectorGatewayError("action_input_invalid", status_code=422)
        query = _bounded_text(inputs.get("query"), "drive_query", 4096)
        limit = inputs.get("limit", 50)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ConnectorGatewayError("action_input_invalid", status_code=422)
        offset = 0
        if inputs.get("cursor") is not None:
            cursor = _bounded_text(inputs["cursor"], "drive_cursor", 3)
            if re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", cursor) is None:
                raise ConnectorGatewayError("action_input_invalid", status_code=422)
            offset = int(cursor)
            if offset > 199:
                raise ConnectorGatewayError("action_input_invalid", status_code=422)
        if offset + limit >= 200:
            raise ConnectorGatewayError("action_input_invalid", status_code=422)
        body: dict[str, Any] = {
            "search_key": query,
            "count": limit,
            "offset": offset,
        }
        response = await self.provider.request(
            "POST", "/open-apis/suite/docs-api/search/object", token=token, body=body
        )
        data = self._data(response)
        raw_items = data.get("docs_entities")
        has_more = data.get("has_more")
        total = data.get("total")
        if (
            not isinstance(raw_items, list)
            or len(raw_items) > 50
            or not isinstance(has_more, bool)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
        ):
            raise ConnectorGatewayError("provider_response_invalid")
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ConnectorGatewayError("provider_response_invalid")
            try:
                file_id = _bounded_text(raw.get("docs_token"), "drive_file_id", 512)
                name = _bounded_text(raw.get("title"), "drive_file_name", 1024)
                kind = _bounded_text(raw.get("docs_type"), "drive_file_kind", 64)
                url = raw.get("url")
                if url is not None:
                    url = _bounded_text(url, "drive_file_url", 4096)
            except ConnectorGatewayError as error:
                raise ConnectorGatewayError("provider_response_invalid") from error
            items.append(
                {
                    "file_id": file_id,
                    "name": name,
                    "kind": kind,
                    "mime_type": None,
                    "url": url,
                    "modified_at": None,
                }
            )
        next_cursor = None
        if has_more:
            next_cursor = str(offset + limit)
        return {
            "ok": True,
            "action_id": "drive.search",
            "title": None,
            "items": items,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    async def _messages_send(
        self,
        token: str,
        inputs: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not set(inputs) <= {"conversation_id", "recipient_id", "text"}:
            raise ConnectorGatewayError("action_input_invalid", status_code=422)
        text = _bounded_content(inputs.get("text"), "message_text", 128 * 1024)
        conversation = inputs.get("conversation_id")
        recipient = inputs.get("recipient_id")
        if (conversation is None) == (recipient is None):
            raise ConnectorGatewayError("action_input_invalid", status_code=422)
        receive_id_type = "chat_id" if conversation is not None else "open_id"
        receive_id = _bounded_text(
            conversation if conversation is not None else recipient,
            "message_recipient",
            512,
        )
        response = await self.provider.request(
            "POST",
            "/open-apis/im/v1/messages",
            token=token,
            params={
                "receive_id_type": receive_id_type,
            },
            body={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": _canonical({"text": text}),
                "uuid": _stable_uuid(idempotency_key),
            },
        )
        data = self._data(response)
        message_id = _bounded_text(data.get("message_id"), "message_id", 512)
        created = data.get("create_time")
        sent_at = None
        if isinstance(created, str) and created.isdigit():
            sent_at = datetime.fromtimestamp(int(created) / 1000, UTC).isoformat()
        return {
            "ok": True,
            "action_id": "messages.send",
            "title": None,
            "message_id": message_id,
            "conversation_id": str(data.get("chat_id") or conversation)
            if data.get("chat_id") or conversation
            else None,
            "sent_at": sent_at,
            "url": None,
        }

    async def _revoke(
        self, principal: ControlPrincipal, managed_grant: str
    ) -> dict[str, Any]:
        grant_sha = self._grant_sha(managed_grant)
        now = self.clock().astimezone(UTC)
        destroyed = self.cipher.encrypt(
            _canonical({"revoked": True}), associated_data="feishu-grant:" + grant_sha
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT account_id,organization_id,revoked FROM connector_gateway_grants "
                "WHERE grant_sha256=?",
                (grant_sha,),
            ).fetchone()
            if (
                row is None
                or str(row["account_id"]) != principal.account_id
                or row["organization_id"] != principal.organization_id
            ):
                raise ConnectorGatewayError("managed_grant_invalid", status_code=401)
            if not bool(row["revoked"]):
                connection.execute(
                    "UPDATE connector_gateway_grants SET revoked=1,revoked_at=?,"
                    "updated_at=?,revision=revision+1,token_envelope_json=?,"
                    "granted_scopes_json='[]',access_expires_at=? "
                    "WHERE grant_sha256=?",
                    (_iso(now), _iso(now), destroyed, _iso(now), grant_sha),
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return {"revoked": True}

    async def _usable_token(
        self, principal: ControlPrincipal, managed_grant: str
    ) -> tuple[str, sqlite3.Row, str]:
        grant_sha = self._grant_sha(managed_grant)
        row, material = self._grant(principal, grant_sha)
        if datetime.fromisoformat(str(row["access_expires_at"])) > self.clock() + timedelta(seconds=60):
            return grant_sha, row, _bounded_text(
                material.get("access_token"), "provider_access_token", 8192
            )
        row, token = await self._refresh(principal, grant_sha)
        return grant_sha, row, token

    async def _refresh(
        self,
        principal: ControlPrincipal,
        grant_sha: str,
        *,
        force: bool = False,
    ) -> tuple[sqlite3.Row, str]:
        lock = self._grant_locks.setdefault(grant_sha, asyncio.Lock())
        async with lock:
            row, material = self._grant(principal, grant_sha)
            if not force and datetime.fromisoformat(str(row["access_expires_at"])) > self.clock() + timedelta(seconds=60):
                return row, _bounded_text(
                    material.get("access_token"), "provider_access_token", 8192
                )
            refresh = _bounded_text(
                material.get("refresh_token"), "provider_refresh_token", 8192
            )
            expires = row["refresh_expires_at"]
            if expires is None or datetime.fromisoformat(str(expires)) <= self.clock():
                raise ConnectorGatewayError(
                    "provider_authorization_expired", status_code=401
                )
            response = await self.provider.request(
                "POST",
                "/open-apis/authen/v2/oauth/token",
                body={
                    "grant_type": "refresh_token",
                    "client_id": self.app_id,
                    "client_secret": self.app_secret,
                    "refresh_token": refresh,
                },
            )
            updated, access_expires, refresh_expires, scopes = self._token_response(response)
            envelope = self.cipher.encrypt(
                _canonical(updated), associated_data="feishu-grant:" + grant_sha
            )
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                changed = connection.execute(
                    "UPDATE connector_gateway_grants SET token_envelope_json=?,"
                    "access_expires_at=?,refresh_expires_at=?,granted_scopes_json=?,"
                    "revision=revision+1,updated_at=? WHERE grant_sha256=? AND "
                    "account_id=? AND revision=? AND revoked=0",
                    (
                        envelope,
                        _iso(access_expires),
                        _iso(refresh_expires) if refresh_expires else None,
                        _canonical(sorted(scopes)),
                        _iso(self.clock()),
                        grant_sha,
                        principal.account_id,
                        int(row["revision"]),
                    ),
                ).rowcount
                if changed != 1:
                    raise ConnectorGatewayError("grant_changed", status_code=409, retryable=True)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                connection.close()
            row, material = self._grant(principal, grant_sha)
            return row, _bounded_text(
                material.get("access_token"), "provider_access_token", 8192
            )

    def _token_response(
        self, response: Mapping[str, Any]
    ) -> tuple[dict[str, str], datetime, datetime | None, frozenset[str]]:
        access = _bounded_text(response.get("access_token"), "provider_access_token", 8192)
        refresh = _bounded_text(response.get("refresh_token"), "provider_refresh_token", 8192)
        expires = response.get("expires_in")
        refresh_expires = response.get("refresh_token_expires_in")
        if (
            isinstance(expires, bool)
            or not isinstance(expires, int)
            or not 60 <= expires <= 31_536_000
            or isinstance(refresh_expires, bool)
            or not isinstance(refresh_expires, int)
            or not 60 <= refresh_expires <= 366 * 24 * 60 * 60
        ):
            raise ConnectorGatewayError("provider_response_invalid")
        raw_scope = response.get("scope")
        if not isinstance(raw_scope, str):
            raise ConnectorGatewayError("provider_response_invalid")
        scopes = frozenset(raw_scope.split())
        if not scopes or len(scopes) > 256 or any(len(scope) > 256 for scope in scopes):
            raise ConnectorGatewayError("provider_response_invalid")
        now = self.clock().astimezone(UTC)
        return (
            {"access_token": access, "refresh_token": refresh},
            now + timedelta(seconds=expires),
            now + timedelta(seconds=refresh_expires),
            scopes,
        )

    async def _user_info(self, access_token: str) -> dict[str, Any]:
        response = await self.provider.request(
            "GET", "/open-apis/authen/v1/user_info", token=access_token
        )
        return self._data(response)

    @staticmethod
    def _data(value: Mapping[str, Any]) -> dict[str, Any]:
        data = value.get("data")
        if not isinstance(data, dict):
            raise ConnectorGatewayError("provider_response_invalid")
        return data

    @classmethod
    def _data_object(cls, value: Mapping[str, Any], name: str) -> dict[str, Any]:
        data = cls._data(value)
        result = data.get(name)
        if not isinstance(result, dict):
            raise ConnectorGatewayError("provider_response_invalid")
        return result

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _flow(self, principal: ControlPrincipal, flow_id: str) -> sqlite3.Row:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM connector_gateway_flows WHERE flow_id=? AND account_id=? "
                "AND organization_id IS ?",
                (flow_id, principal.account_id, principal.organization_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ConnectorGatewayError("authorization_flow_invalid", status_code=401)
        return row

    @staticmethod
    def _grant_sha(managed_grant: str) -> str:
        if not isinstance(managed_grant, str) or _SAFE_GRANT.fullmatch(managed_grant) is None:
            raise ConnectorGatewayError("managed_grant_invalid", status_code=401)
        return hashlib.sha256(managed_grant.encode("ascii")).hexdigest()

    def _grant(
        self, principal: ControlPrincipal, grant_sha: str
    ) -> tuple[sqlite3.Row, dict[str, Any]]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM connector_gateway_grants WHERE grant_sha256=? "
                "AND account_id=? AND organization_id IS ?",
                (grant_sha, principal.account_id, principal.organization_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None or bool(row["revoked"]):
            raise ConnectorGatewayError("managed_grant_invalid", status_code=401)
        try:
            material = json.loads(
                self.cipher.decrypt(
                    str(row["token_envelope_json"]),
                    associated_data="feishu-grant:" + grant_sha,
                )
            )
        except Exception:
            raise ConnectorGatewayError("managed_grant_unavailable") from None
        if not isinstance(material, dict):
            raise ConnectorGatewayError("managed_grant_unavailable")
        return row, material

    def _reserve_idempotency(
        self,
        account_id: str,
        organization_id: str,
        key: str,
        operation: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        if _HEX_64.fullmatch(fingerprint) is None:
            raise ConnectorGatewayError("idempotency_invalid", status_code=422)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM connector_gateway_idempotency WHERE account_id=? "
                "AND organization_id=? AND idempotency_key=?",
                (account_id, organization_id, key),
            ).fetchone()
            now = _iso(self.clock())
            if row is None:
                connection.execute(
                    "INSERT INTO connector_gateway_idempotency("
                    "account_id,organization_id,idempotency_key,operation,request_sha256,"
                    "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        account_id,
                        organization_id,
                        key,
                        operation,
                        fingerprint,
                        "active",
                        now,
                        now,
                    ),
                )
                connection.commit()
                return None
            if str(row["operation"]) != operation or str(row["request_sha256"]) != fingerprint:
                raise ConnectorGatewayError("idempotency_conflict", status_code=409)
            if row["status"] == "completed":
                envelope = row["response_envelope_json"]
                if not isinstance(envelope, str):
                    raise ConnectorGatewayError("idempotency_unavailable")
                value = json.loads(
                    self.cipher.decrypt(
                        envelope,
                        associated_data=(
                            f"feishu-idempotency:{account_id}:{organization_id}:{key}"
                        ),
                    )
                )
                if not isinstance(value, dict):
                    raise ConnectorGatewayError("idempotency_unavailable")
                connection.commit()
                return value
            if row["status"] == "active" and (
                datetime.fromisoformat(str(row["updated_at"]))
                > self.clock().astimezone(UTC) - timedelta(minutes=5)
            ):
                raise ConnectorGatewayError(
                    "operation_in_progress", status_code=409, retryable=True
                )
            connection.execute(
                "UPDATE connector_gateway_idempotency SET status='active',"
                "error_code=NULL,updated_at=? WHERE account_id=? AND "
                "organization_id=? AND idempotency_key=?",
                (now, account_id, organization_id, key),
            )
            connection.commit()
            return None
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _complete_idempotency(
        self,
        account_id: str,
        organization_id: str,
        key: str,
        response: Mapping[str, Any],
    ) -> None:
        envelope = self.cipher.encrypt(
            _canonical(dict(response)),
            associated_data=f"feishu-idempotency:{account_id}:{organization_id}:{key}",
        )
        connection = self._connect()
        try:
            changed = connection.execute(
                "UPDATE connector_gateway_idempotency SET status='completed',"
                "response_envelope_json=?,error_code=NULL,updated_at=? "
                "WHERE account_id=? AND organization_id=? AND idempotency_key=? "
                "AND status='active'",
                (
                    envelope,
                    _iso(self.clock()),
                    account_id,
                    organization_id,
                    key,
                ),
            ).rowcount
            if changed != 1:
                raise ConnectorGatewayError("idempotency_changed", retryable=True)
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _idempotency_created_at(
        self, account_id: str, organization_id: str, key: str
    ) -> datetime:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT created_at FROM connector_gateway_idempotency WHERE "
                "account_id=? AND organization_id=? AND idempotency_key=?",
                (account_id, organization_id, key),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ConnectorGatewayError("idempotency_unavailable")
        return datetime.fromisoformat(str(row["created_at"]))

    def _fail_idempotency(
        self, account_id: str, organization_id: str, key: str, error_code: str
    ) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE connector_gateway_idempotency SET status='failed',"
                "error_code=?,updated_at=? WHERE account_id=? AND organization_id=? "
                "AND idempotency_key=? AND status='active'",
                (
                    error_code[:128],
                    _iso(self.clock()),
                    account_id,
                    organization_id,
                    key,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _audit(
        self,
        principal: ControlPrincipal,
        event_type: str,
        source_event_id: str,
        payload: Mapping[str, Any],
        *,
        created_at: datetime | None = None,
    ) -> None:
        safe = {
            "connector_id": "feishu",
            "organization_id": principal.organization_id,
            **dict(payload),
        }
        encoded = json_dumps(safe).encode("utf-8")
        audit_id = "audit_" + hashlib.sha256(
            (event_type + "\0" + source_event_id + "\0" + principal.account_id).encode("utf-8")
        ).hexdigest()
        created = (created_at or self.clock()).astimezone(UTC)
        record = AuditRecordProjection(
            audit_id=audit_id,
            source_event_id=source_event_id,
            category="connector",
            event_type=event_type,
            account_id=principal.account_id,
            payload=safe,
            payload_sha256=hashlib.sha256(encoded).hexdigest(),
            binary_included=False,
            delivery_status="published",
            attempts=1,
            created_at=created,
            published_at=created,
        )
        self.audit_repository.ingest(principal, record, idempotency_key=audit_id)

    def _audit_failure(
        self,
        principal: ControlPrincipal,
        operation: str,
        source_id: str,
        error: ConnectorGatewayError,
        *,
        created_at: datetime | None = None,
    ) -> None:
        failure_source = source_id + ".failed." + hashlib.sha256(
            error.code.encode("utf-8")
        ).hexdigest()[:16]
        self._audit(
            principal,
            operation,
            failure_source,
            {
                "connector_id": "feishu",
                "operation": operation,
                "status": "failed",
                "error_code": error.code,
                "retryable": error.retryable,
            },
            created_at=created_at,
        )

    @staticmethod
    def _http_error(error: ConnectorGatewayError) -> HTTPException:
        return HTTPException(
            status_code=error.status_code,
            detail={
                "code": error.code,
                "message": "managed connector operation failed",
                "retryable": error.retryable,
            },
        )


__all__ = [
    "FEISHU_OAUTH_RETURN_URI",
    "FEISHU_SCOPES",
    "ConnectorGatewayError",
    "FeishuConnectorGateway",
    "FeishuProviderClient",
]
