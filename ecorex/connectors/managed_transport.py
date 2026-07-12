"""Strict managed-session adapter for the EcoreX connector gateway.

Provider OAuth client secrets and API-specific behavior live behind the managed
gateway.  The local Runtime receives one opaque grant, stores it in the OS
credential vault, and keeps ConnectorService as the authority for lifecycle,
scope, idempotency, audit and replay decisions.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import re
from typing import TYPE_CHECKING, Any, Final, Mapping
from urllib.parse import urlsplit

import httpx

from .errors import ConnectorAuthError, ConnectorUnavailable
from .models import (
    AuthChallenge,
    AuthGrant,
    ConnectorAuthKind,
    ConnectorHealth,
    ConnectorHealthResult,
)

if TYPE_CHECKING:
    from ecorex.session import ManagedSessionService


CONNECTOR_GATEWAY_PATH: Final = "/api/v1/connectors"
DEFAULT_MAX_CONNECTOR_REQUEST_BYTES: Final = 8 * 1024 * 1024
DEFAULT_MAX_CONNECTOR_RESPONSE_BYTES: Final = 8 * 1024 * 1024
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")


class ManagedConnectorTransportError(RuntimeError):
    """A deliberately non-sensitive managed connector transport failure."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ManagedConnectorTransportError(
            "invalid_request", retryable=False
        ) from None


def _validated_root(endpoint: str, allowed_hosts: frozenset[str]) -> str:
    parsed = urlsplit(endpoint.rstrip("/"))
    hosts = frozenset(host.casefold().rstrip(".") for host in allowed_hosts if host)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or not hostname
        or hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path != CONNECTOR_GATEWAY_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "connector gateway must be the allowlisted HTTPS v1 connector root"
        )
    return f"https://{hostname}{CONNECTOR_GATEWAY_PATH}"


def _bounded_string(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ManagedConnectorTransportError(f"invalid_{label}", retryable=False)
    return value


def _exact_object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ManagedConnectorTransportError(f"invalid_{label}", retryable=False)
    return value


class ManagedConnectorGatewayAdapter:
    """One fixed connector contract bound to the signed managed gateway root."""

    def __init__(
        self,
        *,
        connector_id: str,
        endpoint: str,
        allowed_hosts: frozenset[str],
        session: ManagedSessionService,
        client: httpx.AsyncClient | None = None,
        max_request_bytes: int = DEFAULT_MAX_CONNECTOR_REQUEST_BYTES,
        max_response_bytes: int = DEFAULT_MAX_CONNECTOR_RESPONSE_BYTES,
    ) -> None:
        if _SAFE_ID.fullmatch(connector_id) is None:
            raise ValueError("managed connector ID is invalid")
        if session is None:
            raise ValueError("managed session authority is required")
        if not 4096 <= max_request_bytes <= 16 * 1024 * 1024:
            raise ValueError("managed connector request bound is invalid")
        if not 4096 <= max_response_bytes <= 16 * 1024 * 1024:
            raise ValueError("managed connector response bound is invalid")
        self.connector_id = connector_id
        self.root = _validated_root(endpoint, allowed_hosts)
        self.session = session
        self.max_request_bytes = max_request_bytes
        self.max_response_bytes = max_response_bytes
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=60, write=60, pool=10),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def begin_auth(
        self,
        *,
        flow_id: str,
        auth_kind: ConnectorAuthKind,
        return_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> AuthChallenge:
        try:
            raw = await self._request(
                "auth/begin",
                {
                    "flow_id": flow_id,
                    "auth_kind": auth_kind.value,
                    "return_uri": return_uri,
                    "state": state,
                    "code_challenge": code_challenge,
                    "code_challenge_method": code_challenge_method,
                },
                idempotency_key=flow_id,
            )
            value = _exact_object(
                raw,
                {
                    "flow_id",
                    "connector_id",
                    "auth_kind",
                    "expires_at",
                    "authorization_url",
                    "user_code",
                    "verification_url",
                },
                "auth_challenge",
            )
            return AuthChallenge(
                flow_id=_bounded_string(value["flow_id"], "flow_id", maximum=256),
                connector_id=_bounded_string(
                    value["connector_id"], "connector_id", maximum=128
                ),
                auth_kind=ConnectorAuthKind(str(value["auth_kind"])),
                expires_at=datetime.fromisoformat(
                    _bounded_string(value["expires_at"], "expires_at", maximum=64)
                ),
                authorization_url=self._optional_string(
                    value["authorization_url"], "authorization_url", 4096
                ),
                user_code=self._optional_string(value["user_code"], "user_code", 256),
                verification_url=self._optional_string(
                    value["verification_url"], "verification_url", 4096
                ),
            )
        except (ManagedConnectorTransportError, ValueError, TypeError):
            raise ConnectorAuthError(
                "managed connector authorization could not be started"
            ) from None

    async def complete_auth(
        self,
        *,
        flow_id: str,
        response: Mapping[str, str],
        private_state: Mapping[str, str],
    ) -> AuthGrant:
        try:
            raw = await self._request(
                "auth/complete",
                {
                    "flow_id": flow_id,
                    "response": dict(response),
                    "private_state": dict(private_state),
                },
                idempotency_key=f"complete:{flow_id}",
            )
            value = _exact_object(
                raw,
                {
                    "account_subject",
                    "account_display_name",
                    "granted_scopes",
                    "managed_grant",
                },
                "auth_grant",
            )
            scopes = value["granted_scopes"]
            if (
                not isinstance(scopes, list)
                or len(scopes) > 256
                or any(not isinstance(scope, str) for scope in scopes)
            ):
                raise ManagedConnectorTransportError("invalid_scopes", retryable=False)
            return AuthGrant(
                account_subject=_bounded_string(
                    value["account_subject"], "account_subject", maximum=512
                ),
                account_display_name=_bounded_string(
                    value["account_display_name"],
                    "account_display_name",
                    maximum=512,
                ),
                granted_scopes=frozenset(scopes),
                credential_material={
                    "managed_grant": _bounded_string(
                        value["managed_grant"], "managed_grant", maximum=16 * 1024
                    )
                },
            )
        except (ManagedConnectorTransportError, ValueError, TypeError):
            raise ConnectorAuthError(
                "managed connector authorization could not be completed"
            ) from None

    async def check_health(
        self, credentials: Mapping[str, str]
    ) -> ConnectorHealthResult:
        try:
            raw = await self._request(
                "health",
                {"managed_grant": self._managed_grant(credentials)},
            )
            value = _exact_object(raw, {"health", "error_code"}, "health")
            error_code = self._optional_string(value["error_code"], "error_code", 128)
            return ConnectorHealthResult(
                health=ConnectorHealth(str(value["health"])),
                error_code=error_code,
            )
        except (ManagedConnectorTransportError, ValueError, TypeError):
            raise ConnectorUnavailable("managed connector health is unavailable") from None

    async def invoke(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any],
        credentials: Mapping[str, str],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if _SAFE_ID.fullmatch(action_id) is None:
            raise ConnectorUnavailable("managed connector action is invalid")
        raw = await self._request(
            f"actions/{action_id}",
            {
                "managed_grant": self._managed_grant(credentials),
                "inputs": dict(inputs),
                "idempotency_key": idempotency_key,
            },
            idempotency_key=idempotency_key,
        )
        if not isinstance(raw, dict):
            raise ConnectorUnavailable("managed connector returned an invalid result")
        return raw

    async def revoke(
        self,
        *,
        credentials: Mapping[str, str],
        idempotency_key: str,
    ) -> bool:
        raw = await self._request(
            "revoke",
            {"managed_grant": self._managed_grant(credentials)},
            idempotency_key=idempotency_key,
        )
        value = _exact_object(raw, {"revoked"}, "revocation")
        return value["revoked"] is True

    async def _request(
        self,
        suffix: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        body = _canonical_json(payload)
        if len(body) > self.max_request_bytes:
            raise ManagedConnectorTransportError("request_too_large", retryable=False)
        try:
            before, token, after = await asyncio.to_thread(
                self._session_request_identity
            )
        except Exception:
            raise ManagedConnectorTransportError(
                "session_unavailable", retryable=True
            ) from None
        if (
            before.account_id != after.account_id
            or before.lease_digest != after.lease_digest
            or before.generation != after.generation
        ):
            token = ""
            raise ManagedConnectorTransportError("session_changed", retryable=True)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = _bounded_string(
                idempotency_key, "idempotency_key", maximum=512
            )
        url = f"{self.root}/{self.connector_id}/{suffix}"
        try:
            request = self.client.build_request(
                "POST", url, headers=headers, content=body
            )
            response = await self.client.send(
                request, stream=True, follow_redirects=False
            )
            try:
                return await self._consume_response(response)
            finally:
                await response.aclose()
        except ManagedConnectorTransportError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise ManagedConnectorTransportError(
                "transport_unavailable", retryable=True
            ) from None
        finally:
            token = ""

    def _session_request_identity(self):
        """Read one stable credential generation outside the network loop."""

        before = self.session.snapshot()
        token = self.session.bearer_token()
        after = self.session.snapshot()
        return before, token, after

    async def _consume_response(self, response: httpx.Response) -> Any:
        if response.is_redirect or response.history:
            raise ManagedConnectorTransportError("redirect_refused", retryable=False)
        if response.headers.get("content-encoding", "identity").casefold() != "identity":
            raise ManagedConnectorTransportError(
                "compressed_response", retryable=False
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ManagedConnectorTransportError("invalid_response", retryable=False)
        received = bytearray()
        async for chunk in response.aiter_bytes():
            received.extend(chunk)
            if len(received) > self.max_response_bytes:
                raise ManagedConnectorTransportError(
                    "response_too_large", retryable=False
                )
        status = response.status_code
        if status not in {200, 201, 202}:
            retryable = status in {401, 408, 425, 429} or 500 <= status <= 599
            raise ManagedConnectorTransportError(
                "remote_retryable" if retryable else "remote_rejected",
                retryable=retryable,
            )
        try:
            return json.loads(
                bytes(received).decode("utf-8"), object_pairs_hook=_unique_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ManagedConnectorTransportError(
                "invalid_response", retryable=False
            ) from None

    @staticmethod
    def _optional_string(value: Any, label: str, maximum: int) -> str | None:
        if value is None:
            return None
        return _bounded_string(value, label, maximum=maximum)

    @staticmethod
    def _managed_grant(credentials: Mapping[str, str]) -> str:
        if not isinstance(credentials, Mapping) or set(credentials) != {"managed_grant"}:
            raise ConnectorUnavailable("managed connector credential is invalid")
        return _bounded_string(
            credentials.get("managed_grant"), "managed_grant", maximum=16 * 1024
        )


__all__ = [
    "CONNECTOR_GATEWAY_PATH",
    "ManagedConnectorGatewayAdapter",
    "ManagedConnectorTransportError",
]
