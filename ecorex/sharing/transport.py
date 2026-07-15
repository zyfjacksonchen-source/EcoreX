"""Strict HTTPS client for publishing sanitized snapshots to Control Plane."""

from __future__ import annotations

import hashlib
import re
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .models import PublishedShare, SharePayload, SharedMediaRendition
from .media_contract import (
    MAX_SHARED_MEDIA_BYTES,
    shared_media_declarations,
    validate_shared_media_rendition,
)


_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class ShareCredentialProvider(Protocol):
    def bearer_token(self) -> str:
        ...


class HTTPSSharePublisher:
    def __init__(
        self,
        endpoint: str,
        *,
        credentials: ShareCredentialProvider,
        allowed_hosts: frozenset[str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        normalized_hosts = frozenset(
            host.strip().casefold() for host in allowed_hosts if host.strip()
        )
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in {None, 443}
            or parsed.hostname.casefold() not in normalized_hosts
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("share endpoint must be allowlisted credential-free HTTPS")
        self.endpoint = endpoint.rstrip("/")
        self.credentials = credentials
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(connect=10, read=30, write=30, pool=10),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self.aclose()

    def _headers(
        self, idempotency_key: str, *, content_type: str = "application/json"
    ) -> dict[str, str]:
        try:
            token = self.credentials.bearer_token()
        except Exception:
            raise ShareTransportError(
                "Control Plane session is unavailable",
                code="share_auth_unavailable",
            ) from None
        if (
            not isinstance(token, str)
            or not 24 <= len(token) <= 8192
            or any(not 33 <= ord(character) <= 126 for character in token)
        ):
            raise ShareTransportError(
                "Control Plane session is unavailable",
                code="share_auth_invalid",
                retryable=False,
            )
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(
            idempotency_key
        ):
            raise ValueError("share idempotency key is invalid")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "Accept": "application/json",
            "Idempotency-Key": idempotency_key,
        }

    async def upload_media(
        self,
        share_id: str,
        media: SharedMediaRendition,
        content: bytes,
        *,
        idempotency_key: str,
    ) -> None:
        if not isinstance(media, SharedMediaRendition):
            raise ValueError("shared media descriptor is invalid")
        validate_shared_media_rendition(media)
        if not re.fullmatch(r"shr_[0-9a-f]{32}", share_id):
            raise ValueError("share identity is invalid")
        body = bytes(content)
        if len(body) != media.size_bytes or len(body) > MAX_SHARED_MEDIA_BYTES:
            raise ShareTransportError(
                "shared media size does not match its descriptor",
                code="share_media_size_invalid",
                retryable=False,
            )
        if hashlib.sha256(body).hexdigest() != media.sha256:
            raise ShareTransportError(
                "shared media digest does not match its descriptor",
                code="share_media_digest_invalid",
                retryable=False,
            )
        headers = self._headers(idempotency_key, content_type=media.mime_type)
        headers.update(
            {
                "Content-Length": str(len(body)),
                "X-Content-SHA256": media.sha256,
                "X-Share-Media-Kind": media.kind,
            }
        )
        await self._request(
            "PUT",
            f"{self.endpoint}/{share_id}/media/{media.media_id}",
            headers=headers,
            content=body,
            expected_status=204,
        )

    async def publish(
        self, payload: SharePayload, *, idempotency_key: str
    ) -> PublishedShare:
        shared_media_declarations(payload, require_publishable_schema=True)
        content = payload.canonical_bytes()
        if len(content) > _MAX_REQUEST_BYTES:
            raise ShareTransportError(
                "share snapshot exceeds its size limit",
                code="share_payload_too_large",
                retryable=False,
            )
        response = await self._request(
            "POST",
            self.endpoint,
            headers=self._headers(idempotency_key),
            content=content,
        )
        try:
            return PublishedShare.model_validate_json(response)
        except ValueError:
            raise ShareTransportError(
                "Control Plane share response is invalid",
                code="share_protocol_invalid",
                retryable=False,
            ) from None

    async def revoke(
        self, remote_snapshot_id: str, *, idempotency_key: str
    ) -> None:
        try:
            PublishedShare(
                remote_snapshot_id=remote_snapshot_id,
                public_url="https://validation.invalid/share",
            )
        except ValueError:
            raise ValueError("remote share identity is invalid")
        await self._request(
            "POST",
            f"{self.endpoint}/{remote_snapshot_id}/revoke",
            headers=self._headers(idempotency_key),
            content=b"{}",
            expected_status=204,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes,
        expected_status: int = 200,
    ) -> bytes:
        request = self.client.build_request(
            method, url, headers=headers, content=content
        )
        try:
            response = await self.client.send(
                request, stream=True, follow_redirects=False
            )
        except httpx.TimeoutException:
            raise ShareTransportError(
                "Control Plane share request timed out", code="share_timeout"
            ) from None
        except httpx.TransportError:
            raise ShareTransportError(
                "Control Plane share transport failed", code="share_transport"
            ) from None
        try:
            if response.is_redirect:
                raise ShareTransportError(
                    "Control Plane share redirect was rejected",
                    code="share_redirect_rejected",
                    retryable=False,
                )
            if response.status_code != expected_status:
                retryable = response.status_code == 429 or response.status_code >= 500
                raise ShareTransportError(
                    f"Control Plane share request failed ({response.status_code})",
                    code=(
                        "share_remote_unavailable"
                        if retryable
                        else "share_remote_rejected"
                    ),
                    retryable=retryable,
                )
            if response.headers.get("content-encoding", "identity").casefold() not in {
                "",
                "identity",
            }:
                raise ShareTransportError(
                    "Control Plane share response encoding is invalid",
                    code="share_protocol_encoding",
                    retryable=False,
                )
            declared = response.headers.get("content-length")
            if declared:
                if (
                    len(declared) > 20
                    or not declared.isascii()
                    or not declared.isdecimal()
                ):
                    raise ShareTransportError(
                        "Control Plane share Content-Length is invalid",
                        code="share_protocol_length",
                        retryable=False,
                    )
                if int(declared) > _MAX_RESPONSE_BYTES:
                    raise ShareTransportError(
                        "Control Plane share response is too large",
                        code="share_response_too_large",
                        retryable=False,
                    )
            body = bytearray()
            if response.is_stream_consumed:
                # Mock/custom transports may legally return an already-buffered
                # response. The default network transport reaches the streaming
                # branch below and enforces the cap before buffering completes.
                body.extend(response.content)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ShareTransportError(
                        "Control Plane share response is too large",
                        code="share_response_too_large",
                        retryable=False,
                    )
            else:
                async for chunk in response.aiter_raw():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ShareTransportError(
                            "Control Plane share response is too large",
                            code="share_response_too_large",
                            retryable=False,
                        )
            if expected_status == 204:
                if body:
                    raise ShareTransportError(
                        "share revoke response must be empty",
                        code="share_protocol_revoke_body",
                        retryable=False,
                    )
                return b""
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if media_type != "application/json":
                raise ShareTransportError(
                    "Control Plane share response type is invalid",
                    code="share_protocol_content_type",
                    retryable=False,
                )
            return bytes(body)
        finally:
            await response.aclose()


class ShareTransportError(RuntimeError):
    """Sanitized transport failure with an explicit retry contract."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "share_transport_error",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
