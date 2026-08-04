"""Strict HTTPS client for the EcoreX managed device-authorization broker."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .device import (
    BrokerDeviceChallenge,
    BrokerDeviceGrant,
    BrokerPasswordChangeReceipt,
    BrokerPollResult,
    BrokerPollStatus,
    BrokerRevocationReceipt,
    DeviceAuthorizationUnauthorized,
    DeviceAuthorizationConflict,
    DeviceAuthorizationUnavailable,
)
from .models import SignedManagedSessionLease


_SAFE_CLIENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_SKILL_PACKAGE_BYTES = 64 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DeviceRefreshInvalidGrant(DeviceAuthorizationUnavailable):
    """The refresh credential is terminal and device login is required."""


class HTTPSDeviceAuthorizationBroker:
    """Calls fixed same-origin Control Plane endpoints and never follows redirects."""

    def __init__(
        self,
        base_url: str,
        *,
        client_id: str,
        allowed_hosts: frozenset[str],
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        normalized = str(base_url).rstrip("/")
        parsed = urlsplit(normalized)
        hosts = frozenset(host.casefold() for host in allowed_hosts)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.casefold() not in hosts
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("device broker must be an allowlisted HTTPS origin")
        if not _SAFE_CLIENT.fullmatch(client_id):
            raise ValueError("device broker client_id is invalid")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("device broker timeout is invalid")
        self.base_url = normalized
        self.client_id = client_id
        self._owned = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    async def begin(self, *, idempotency_key: str) -> BrokerDeviceChallenge:
        body = await self._request(
            "/v1/device/authorize",
            {"schema_version": 1, "client_id": self.client_id},
            idempotency_key=idempotency_key,
        )
        _exact(
            body,
            {
                "schema_version",
                "provider_flow_id",
                "device_code",
                "user_code",
                "verification_url",
                "expires_at",
                "poll_interval_seconds",
            },
        )
        if body.get("schema_version") != 1:
            raise DeviceAuthorizationUnavailable("device broker schema is unsupported")
        return BrokerDeviceChallenge(
            provider_flow_id=_text(body, "provider_flow_id"),
            device_code=_text(body, "device_code"),
            user_code=_text(body, "user_code"),
            verification_url=_text(body, "verification_url"),
            expires_at=_timestamp(body, "expires_at"),
            poll_interval_seconds=_integer(body, "poll_interval_seconds"),
        )

    async def poll(
        self,
        *,
        provider_flow_id: str,
        device_code: str,
        idempotency_key: str,
    ) -> BrokerPollResult:
        body = await self._request(
            "/v1/device/token",
            {
                "schema_version": 1,
                "client_id": self.client_id,
                "provider_flow_id": provider_flow_id,
                "device_code": device_code,
            },
            idempotency_key=idempotency_key,
        )
        if body.get("schema_version") != 1:
            raise DeviceAuthorizationUnavailable("device broker schema is unsupported")
        try:
            status = BrokerPollStatus(_text(body, "status"))
        except ValueError:
            raise DeviceAuthorizationUnavailable(
                "device broker status is invalid"
            ) from None
        if status is BrokerPollStatus.AUTHORIZED:
            _exact(
                body,
                {"schema_version", "status", "lease", "access_token", "refresh_token"},
            )
            raw_lease = body.get("lease")
            if not isinstance(raw_lease, Mapping):
                raise DeviceAuthorizationUnavailable("device broker lease is invalid")
            try:
                lease = SignedManagedSessionLease.from_dict(raw_lease)
                grant = BrokerDeviceGrant(
                    lease=lease,
                    access_token=_text(body, "access_token"),
                    refresh_token=_text(body, "refresh_token"),
                )
            except (TypeError, ValueError):
                raise DeviceAuthorizationUnavailable(
                    "device broker grant is invalid"
                ) from None
            return BrokerPollResult(status=status, grant=grant)
        _exact(body, {"schema_version", "status", "retry_after_seconds"})
        retry = body.get("retry_after_seconds")
        if retry is not None and (
            isinstance(retry, bool) or not isinstance(retry, int)
        ):
            raise DeviceAuthorizationUnavailable(
                "device broker retry interval is invalid"
            )
        return BrokerPollResult(status=status, retry_after_seconds=retry)

    async def refresh(
        self,
        *,
        lease_id: str,
        refresh_token: str,
        idempotency_key: str,
    ) -> BrokerDeviceGrant:
        body = await self._request(
            "/v1/device/token",
            {
                "schema_version": 1,
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "lease_id": lease_id,
                "refresh_token": refresh_token,
            },
            idempotency_key=idempotency_key,
            allow_invalid_grant=True,
        )
        _exact(
            body,
            {"schema_version", "status", "lease", "access_token", "refresh_token"},
        )
        if body.get("schema_version") != 1 or body.get("status") != "authorized":
            raise DeviceAuthorizationUnavailable("device refresh response is invalid")
        raw_lease = body.get("lease")
        if not isinstance(raw_lease, Mapping):
            raise DeviceAuthorizationUnavailable("device refresh lease is invalid")
        try:
            return BrokerDeviceGrant(
                lease=SignedManagedSessionLease.from_dict(raw_lease),
                access_token=_text(body, "access_token"),
                refresh_token=_text(body, "refresh_token"),
            )
        except (TypeError, ValueError):
            raise DeviceAuthorizationUnavailable(
                "device refresh grant is invalid"
            ) from None

    async def login(
        self,
        *,
        identifier: str,
        password: str,
        idempotency_key: str,
    ) -> BrokerDeviceGrant:
        body = await self._request(
            "/v1/session/login",
            {
                "schema_version": 1,
                "client_id": self.client_id,
                "identifier": identifier,
                "password": password,
            },
            idempotency_key=idempotency_key,
            allow_login_failure=True,
        )
        _exact(
            body,
            {"schema_version", "status", "lease", "access_token", "refresh_token"},
        )
        if body.get("schema_version") != 1 or body.get("status") != "authorized":
            raise DeviceAuthorizationUnavailable("password login response is invalid")
        raw_lease = body.get("lease")
        if not isinstance(raw_lease, Mapping):
            raise DeviceAuthorizationUnavailable("password login lease is invalid")
        try:
            return BrokerDeviceGrant(
                lease=SignedManagedSessionLease.from_dict(raw_lease),
                access_token=_text(body, "access_token"),
                refresh_token=_text(body, "refresh_token"),
            )
        except (TypeError, ValueError):
            raise DeviceAuthorizationUnavailable(
                "password login grant is invalid"
            ) from None

    async def revoke(
        self,
        *,
        lease_id: str,
        account_id: str,
        refresh_token: str,
        idempotency_key: str,
    ) -> BrokerRevocationReceipt:
        body = await self._request(
            "/v1/device/revoke",
            {
                "schema_version": 1,
                "client_id": self.client_id,
                "lease_id": lease_id,
                "account_id": account_id,
                "refresh_token": refresh_token,
            },
            idempotency_key=idempotency_key,
            allow_invalid_grant=True,
        )
        _exact(
            body,
            {
                "schema_version",
                "status",
                "lease_id",
                "account_id",
                "already_revoked",
            },
        )
        if (
            body.get("schema_version") != 1
            or body.get("status") != "revoked"
            or not isinstance(body.get("already_revoked"), bool)
        ):
            raise DeviceAuthorizationUnavailable(
                "device revoke response is invalid"
            )
        return BrokerRevocationReceipt(
            lease_id=_text(body, "lease_id"),
            account_id=_text(body, "account_id"),
            already_revoked=bool(body["already_revoked"]),
        )

    async def change_password(
        self,
        *,
        current_password: str,
        new_password: str,
        access_token: str,
        client_request_id: str,
        idempotency_key: str,
    ) -> BrokerPasswordChangeReceipt:
        body = await self._request(
            "/v1/account/password",
            {
                "schema_version": 1,
                "current_password": current_password,
                "new_password": new_password,
                "client_request_id": client_request_id,
            },
            idempotency_key=idempotency_key,
            bearer_token=access_token,
            allow_password_change_failure=True,
        )
        _exact(body, {"schema_version", "status", "reauthentication_required"})
        if (
            body.get("schema_version") != 1
            or body.get("status") != "changed"
            or body.get("reauthentication_required") is not True
        ):
            raise DeviceAuthorizationUnavailable("password change response is invalid")
        return BrokerPasswordChangeReceipt()

    async def skill_hub_list(
        self,
        *,
        access_token: str,
        query: str,
        category: str | None,
        tag: str | None,
        source: str | None,
        cursor: str | None,
        limit: int,
    ) -> Mapping[str, Any]:
        parameters: dict[str, str | int] = {"query": query, "limit": limit}
        if category is not None:
            parameters["category"] = category
        if tag is not None:
            parameters["tag"] = tag
        if source is not None:
            parameters["source"] = source
        if cursor is not None:
            parameters["cursor"] = cursor
        response = await self._authorized_get(
            "/ecorex-agent/client/skill-hub/v1/skills",
            access_token=access_token,
            parameters=parameters,
            maximum_bytes=_MAX_RESPONSE_BYTES,
        )
        try:
            value = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeviceAuthorizationUnavailable("Skill Hub catalog is invalid") from None
        if not isinstance(value, Mapping):
            raise DeviceAuthorizationUnavailable("Skill Hub catalog is invalid")
        return value

    async def skill_hub_detail(
        self, *, access_token: str, slug: str
    ) -> Mapping[str, Any]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,95}", slug):
            raise DeviceAuthorizationUnavailable("Skill Hub identity is invalid")
        response = await self._authorized_get(
            f"/ecorex-agent/client/skill-hub/v1/skills/{slug}",
            access_token=access_token,
            parameters=None,
            maximum_bytes=_MAX_RESPONSE_BYTES,
        )
        try:
            value = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeviceAuthorizationUnavailable("Skill Hub detail is invalid") from None
        if not isinstance(value, Mapping):
            raise DeviceAuthorizationUnavailable("Skill Hub detail is invalid")
        return value

    async def skill_hub_download(
        self,
        *,
        access_token: str,
        slug: str,
        version: str,
    ) -> tuple[bytes, str]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,95}", slug) or not version:
            raise DeviceAuthorizationUnavailable("Skill Hub package identity is invalid")
        content, headers = await self._authorized_get(
            f"/ecorex-agent/client/skill-hub/v1/skills/{slug}/versions/{version}/package",
            access_token=access_token,
            parameters=None,
            maximum_bytes=_MAX_SKILL_PACKAGE_BYTES,
            include_headers=True,
        )
        digest = headers.get("x-skill-content-sha256", "")
        if not _SHA256.fullmatch(digest):
            raise DeviceAuthorizationUnavailable("Skill Hub package digest is invalid")
        return content, digest

    async def skill_hub_upload(
        self,
        *,
        access_token: str,
        slug: str,
        category: str,
        bundle_base64: str,
        client_request_id: str,
    ) -> Mapping[str, Any]:
        return await self._request(
            "/ecorex-agent/client/skill-hub/v1/skills",
            {
                "slug": slug,
                "category": category,
                "bundle_base64": bundle_base64,
                "client_request_id": client_request_id,
            },
            idempotency_key=client_request_id,
            bearer_token=access_token,
        )

    async def skill_hub_create_install_intent(
        self,
        *,
        access_token: str,
        slug: str,
        version: str,
        package_sha256: str,
        client_request_id: str,
    ) -> Mapping[str, Any]:
        return await self._request(
            f"/ecorex-agent/client/skill-hub/v1/skills/{slug}/versions/{version}/install-intent",
            {
                "package_sha256": package_sha256,
                "client_request_id": client_request_id,
            },
            idempotency_key=client_request_id,
            bearer_token=access_token,
        )

    async def skill_hub_consume_install_intent(
        self, *, access_token: str, install_intent: str
    ) -> Mapping[str, Any]:
        digest = hashlib.sha256(install_intent.encode()).hexdigest()[:32]
        return await self._request(
            "/ecorex-agent/client/skill-hub/v1/install-intents/consume",
            {"install_intent": install_intent},
            idempotency_key=f"skill-intent-consume-{digest}",
            bearer_token=access_token,
        )

    async def skill_hub_complete_install_intent(
        self, *, access_token: str, completion_receipt: str, status: str
    ) -> None:
        digest = hashlib.sha256(completion_receipt.encode()).hexdigest()[:32]
        value = await self._request(
            "/ecorex-agent/client/skill-hub/v1/install-intents/complete",
            {"completion_receipt": completion_receipt, "status": status},
            idempotency_key=f"skill-intent-complete-{digest}",
            bearer_token=access_token,
        )
        if value != {"schema_version": 1, "status": status}:
            raise DeviceAuthorizationUnavailable("Skill Hub completion response is invalid")

    async def _authorized_get(
        self,
        path: str,
        *,
        access_token: str,
        parameters: Mapping[str, str | int] | None,
        maximum_bytes: int,
        include_headers: bool = False,
    ):
        if not 128 <= len(access_token) <= 4096 or any(character.isspace() for character in access_token):
            raise DeviceAuthorizationUnavailable("Skill Hub authorization is invalid")
        try:
            async with self.client.stream(
                "GET",
                self.base_url + path,
                params=parameters,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json, application/zip"},
                follow_redirects=False,
            ) as response:
                if response.status_code != 200 or response.is_redirect:
                    raise DeviceAuthorizationUnavailable(
                        f"Skill Hub returned HTTP {response.status_code}"
                    )
                length = response.headers.get("content-length")
                if length and (not length.isdigit() or int(length) > maximum_bytes):
                    raise DeviceAuthorizationUnavailable("Skill Hub response is oversized")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > maximum_bytes:
                        raise DeviceAuthorizationUnavailable("Skill Hub response is oversized")
                content = bytes(payload)
                headers = {key.casefold(): value for key, value in response.headers.items()}
        except DeviceAuthorizationUnavailable:
            raise
        except Exception as error:
            raise DeviceAuthorizationUnavailable(
                f"Skill Hub request failed: {type(error).__name__}"
            ) from None
        return (content, headers) if include_headers else content

    async def aclose(self) -> None:
        if self._owned:
            await self.client.aclose()

    async def _request(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str,
        allow_invalid_grant: bool = False,
        allow_login_failure: bool = False,
        allow_password_change_failure: bool = False,
        bearer_token: str | None = None,
    ) -> Mapping[str, Any]:
        if not _SAFE_CLIENT.fullmatch(idempotency_key.replace(":", "-")):
            raise DeviceAuthorizationUnavailable("device request identity is invalid")
        try:
            encoded = json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Idempotency-Key": idempotency_key,
            }
            if bearer_token is not None:
                if (
                    not 128 <= len(bearer_token) <= 4096
                    or any(character.isspace() for character in bearer_token)
                ):
                    raise DeviceAuthorizationUnavailable(
                        "broker authorization is invalid"
                    )
                headers["Authorization"] = f"Bearer {bearer_token}"
            async with self.client.stream(
                "POST",
                self.base_url + path,
                content=encoded,
                headers=headers,
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise DeviceAuthorizationUnavailable(
                        "device broker redirects are forbidden"
                    )
                response_status = response.status_code
                if response_status not in {200, 201} and not (
                    (allow_invalid_grant and response_status == 401)
                    or (allow_login_failure and response_status in {401, 429})
                    or (allow_password_change_failure and response_status in {401, 409})
                ):
                    raise DeviceAuthorizationUnavailable(
                        f"device broker returned HTTP {response.status_code}"
                    )
                content_length = response.headers.get("content-length")
                if content_length and (
                    not content_length.isdigit()
                    or int(content_length) > _MAX_RESPONSE_BYTES
                ):
                    raise DeviceAuthorizationUnavailable(
                        "device broker response is oversized"
                    )
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    payload.extend(chunk)
                    if len(payload) > _MAX_RESPONSE_BYTES:
                        raise DeviceAuthorizationUnavailable(
                            "device broker response is oversized"
                        )
        except DeviceAuthorizationUnavailable:
            raise
        except Exception as exc:
            raise DeviceAuthorizationUnavailable(
                f"device broker request failed: {type(exc).__name__}"
            ) from None
        try:
            value = json.loads(bytes(payload).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DeviceAuthorizationUnavailable(
                "device broker response is invalid"
            ) from None
        if not isinstance(value, Mapping):
            raise DeviceAuthorizationUnavailable("device broker response is invalid")
        if allow_login_failure and response_status in {401, 429}:
            detail = value.get("detail")
            if (
                isinstance(detail, Mapping)
                and detail.get("code") == "invalid_credentials"
                and set(detail) == {"code", "message"}
                and set(value) == {"detail"}
            ):
                raise DeviceAuthorizationUnauthorized("password login failed")
            raise DeviceAuthorizationUnavailable(
                "password login authentication response is invalid"
            )
        if allow_password_change_failure and response_status in {401, 409}:
            detail = value.get("detail")
            if not isinstance(detail, Mapping) or set(value) != {"detail"}:
                raise DeviceAuthorizationUnavailable(
                    "password change authentication response is invalid"
                )
            if response_status == 401 and detail.get("code") == "invalid_current_password":
                raise DeviceAuthorizationUnauthorized("current password is invalid")
            if response_status == 409 and detail.get("code") == "password_change_conflict":
                raise DeviceAuthorizationConflict("password change conflicted")
            raise DeviceAuthorizationUnavailable(
                "password change authentication response is invalid"
            )
        if response_status == 401:
            detail = value.get("detail")
            if (
                isinstance(detail, Mapping)
                and detail.get("code") == "invalid_grant"
                and set(detail) == {"code", "message"}
                and set(value) == {"detail"}
            ):
                raise DeviceRefreshInvalidGrant(
                    "managed session requires reauthorization"
                )
            raise DeviceAuthorizationUnavailable("device refresh authentication failed")
        return value


def _exact(value: Mapping[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise DeviceAuthorizationUnavailable(
            "device broker response contract is invalid"
        )


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item) > 128 * 1024:
        raise DeviceAuthorizationUnavailable(
            "device broker response contains invalid text"
        )
    return item


def _integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise DeviceAuthorizationUnavailable(
            "device broker response contains invalid integer"
        )
    return item


def _timestamp(value: Mapping[str, Any], key: str) -> datetime:
    raw = _text(value, key)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise DeviceAuthorizationUnavailable(
            "device broker timestamp is invalid"
        ) from None
    if parsed.tzinfo is None:
        raise DeviceAuthorizationUnavailable("device broker timestamp is invalid")
    return parsed


__all__ = ["DeviceRefreshInvalidGrant", "HTTPSDeviceAuthorizationBroker"]
