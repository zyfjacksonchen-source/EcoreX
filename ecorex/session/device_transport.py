"""Strict HTTPS client for the EcoreX managed device-authorization broker."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .device import (
    BrokerDeviceChallenge,
    BrokerDeviceGrant,
    BrokerPollResult,
    BrokerPollStatus,
    DeviceAuthorizationUnavailable,
)
from .models import SignedManagedSessionLease


_SAFE_CLIENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_RESPONSE_BYTES = 256 * 1024


class HTTPSDeviceAuthorizationBroker:
    """Calls only two fixed same-origin endpoints and never follows redirects."""

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
            raise DeviceAuthorizationUnavailable("device broker status is invalid") from None
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
                raise DeviceAuthorizationUnavailable("device broker grant is invalid") from None
            return BrokerPollResult(status=status, grant=grant)
        _exact(body, {"schema_version", "status", "retry_after_seconds"})
        retry = body.get("retry_after_seconds")
        if retry is not None and (isinstance(retry, bool) or not isinstance(retry, int)):
            raise DeviceAuthorizationUnavailable("device broker retry interval is invalid")
        return BrokerPollResult(status=status, retry_after_seconds=retry)

    async def aclose(self) -> None:
        if self._owned:
            await self.client.aclose()

    async def _request(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        idempotency_key: str,
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
            async with self.client.stream(
                "POST",
                self.base_url + path,
                content=encoded,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                follow_redirects=False,
            ) as response:
                if response.is_redirect:
                    raise DeviceAuthorizationUnavailable("device broker redirects are forbidden")
                if response.status_code not in {200, 201}:
                    raise DeviceAuthorizationUnavailable(
                        f"device broker returned HTTP {response.status_code}"
                    )
                content_length = response.headers.get("content-length")
                if content_length and (
                    not content_length.isdigit()
                    or int(content_length) > _MAX_RESPONSE_BYTES
                ):
                    raise DeviceAuthorizationUnavailable("device broker response is oversized")
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
            raise DeviceAuthorizationUnavailable("device broker response is invalid") from None
        if not isinstance(value, Mapping):
            raise DeviceAuthorizationUnavailable("device broker response is invalid")
        return value


def _exact(value: Mapping[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise DeviceAuthorizationUnavailable("device broker response contract is invalid")


def _text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or len(item) > 128 * 1024:
        raise DeviceAuthorizationUnavailable("device broker response contains invalid text")
    return item


def _integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise DeviceAuthorizationUnavailable("device broker response contains invalid integer")
    return item


def _timestamp(value: Mapping[str, Any], key: str) -> datetime:
    raw = _text(value, key)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise DeviceAuthorizationUnavailable("device broker timestamp is invalid") from None
    if parsed.tzinfo is None:
        raise DeviceAuthorizationUnavailable("device broker timestamp is invalid")
    return parsed


__all__ = ["HTTPSDeviceAuthorizationBroker"]
