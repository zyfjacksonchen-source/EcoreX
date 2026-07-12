"""Strict HTTPS NDJSON client for the managed EcoreX Model Gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .errors import (
    GatewayAuthenticationError,
    GatewayProtocolError,
    GatewayRejected,
    GatewayUnavailable,
)
from .models import GatewayEvent, GatewayEventType, ModelGatewayRequest


class GatewayCredentialProvider(Protocol):
    def bearer_token(self) -> str:
        ...


class ModelGateway(Protocol):
    def stream(self, request: ModelGatewayRequest) -> AsyncIterator[GatewayEvent]:
        ...


class RejectingGatewayCredentialProvider:
    def bearer_token(self) -> str:
        raise GatewayAuthenticationError("managed gateway session is unavailable")


class RejectingModelGateway:
    async def stream(self, request: ModelGatewayRequest) -> AsyncIterator[GatewayEvent]:
        del request
        raise GatewayUnavailable("managed Model Gateway is not configured")
        yield  # pragma: no cover


class ManagedModelGatewayClient:
    def __init__(
        self,
        endpoint: str,
        *,
        credentials: GatewayCredentialProvider,
        allowed_hosts: frozenset[str],
        client: httpx.AsyncClient | None = None,
        max_request_bytes: int = 4 * 1024 * 1024,
        max_event_bytes: int = 1024 * 1024,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
            or parsed.path not in {"/v1/responses", "/api/v1/model/stream"}
        ):
            raise ValueError(
                "Model Gateway endpoint must be an absolute credential-free HTTPS URL"
            )
        normalized_hosts = frozenset(host.casefold() for host in allowed_hosts if host)
        if not normalized_hosts or parsed.hostname.casefold() not in normalized_hosts:
            raise ValueError("Model Gateway host is not explicitly allowlisted")
        if not 1024 <= max_request_bytes <= 16 * 1024 * 1024:
            raise ValueError("gateway request limit is invalid")
        if not 1024 <= max_event_bytes <= 4 * 1024 * 1024:
            raise ValueError("gateway event limit is invalid")
        if not max_event_bytes <= max_response_bytes <= 64 * 1024 * 1024:
            raise ValueError("gateway response limit is invalid")
        self.endpoint = endpoint
        self.credentials = credentials
        self.max_request_bytes = max_request_bytes
        self.max_event_bytes = max_event_bytes
        self.max_response_bytes = max_response_bytes
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def stream(self, request: ModelGatewayRequest) -> AsyncIterator[GatewayEvent]:
        try:
            token = self.credentials.bearer_token()
        except Exception:
            raise GatewayAuthenticationError(
                "managed gateway session is unavailable"
            ) from None
        if (
            not isinstance(token, str)
            or not 24 <= len(token) <= 4096
            or any(not 33 <= ord(ch) <= 126 for ch in token)
        ):
            raise GatewayAuthenticationError("managed gateway session token is invalid")
        encoded_request = request.model_dump_json().encode("utf-8")
        if len(encoded_request) > self.max_request_bytes:
            raise GatewayProtocolError("managed gateway request exceeded its size limit")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/x-ndjson",
            "Content-Type": "application/json",
            "Accept-Encoding": "identity",
            "X-EcoreX-Protocol": "1",
        }
        try:
            async with self.client.stream(
                "POST",
                self.endpoint,
                headers=headers,
                content=encoded_request,
                follow_redirects=False,
            ) as response:
                if response.status_code in {401, 403}:
                    raise GatewayAuthenticationError("managed gateway authentication was rejected")
                if response.status_code == 429 or response.status_code >= 500:
                    raise GatewayUnavailable(
                        f"managed gateway is temporarily unavailable ({response.status_code})"
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise GatewayRejected(
                        f"managed gateway rejected the request ({response.status_code})"
                    )
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                if media_type not in {"application/x-ndjson", "application/json-seq"}:
                    raise GatewayProtocolError(
                        "managed gateway returned an unsupported stream type"
                    )
                if response.headers.get("content-encoding", "identity").casefold() != "identity":
                    raise GatewayProtocolError("managed gateway stream encoding is unsupported")
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        parsed_length = int(declared_length)
                    except ValueError as error:
                        raise GatewayProtocolError(
                            "managed gateway returned an invalid Content-Length"
                        ) from error
                    if parsed_length < 0 or parsed_length > self.max_response_bytes:
                        raise GatewayProtocolError(
                            "managed gateway response exceeded its size limit"
                        )
                expected_seq = 1
                total = 0
                response_id: str | None = None
                terminal_event: GatewayEvent | None = None
                buffered = bytearray()

                def decode_line(encoded: bytes) -> GatewayEvent:
                    if len(encoded) > self.max_event_bytes:
                        raise GatewayProtocolError(
                            "managed gateway response exceeded its size limit"
                        )
                    try:
                        raw = json.loads(encoded)
                        return GatewayEvent.model_validate(raw)
                    except (
                        json.JSONDecodeError,
                        UnicodeDecodeError,
                        RecursionError,
                        ValueError,
                        TypeError,
                    ) as error:
                        raise GatewayProtocolError(
                            "managed gateway emitted an invalid event"
                        ) from error

                def accept_line(encoded: bytes) -> GatewayEvent | None:
                    nonlocal expected_seq, response_id, terminal_event
                    if terminal_event is not None:
                        raise GatewayProtocolError(
                            "managed gateway emitted data after a terminal event"
                        )
                    event = decode_line(encoded)
                    if event.seq != expected_seq:
                        raise GatewayProtocolError(
                            "managed gateway event sequence is not contiguous"
                        )
                    if response_id is not None and event.response_id != response_id:
                        raise GatewayProtocolError(
                            "managed gateway response identity changed"
                        )
                    response_id = event.response_id
                    expected_seq += 1
                    if event.event_type in {
                        GatewayEventType.TOOL_CALL_REQUESTED,
                        GatewayEventType.RESPONSE_COMPLETED,
                        GatewayEventType.RESPONSE_FAILED,
                    }:
                        # Do not expose a terminal fact until EOF proves the
                        # server did not append contradictory post-terminal data.
                        terminal_event = event
                        return None
                    return event

                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.max_response_bytes:
                        raise GatewayProtocolError(
                            "managed gateway response exceeded its size limit"
                        )
                    buffered.extend(chunk)
                    while True:
                        newline = buffered.find(b"\n")
                        if newline < 0:
                            if len(buffered) > self.max_event_bytes:
                                raise GatewayProtocolError(
                                    "managed gateway response exceeded its size limit"
                                )
                            break
                        encoded = bytes(buffered[:newline])
                        del buffered[: newline + 1]
                        if encoded.endswith(b"\r"):
                            encoded = encoded[:-1]
                        if not encoded:
                            continue
                        event = accept_line(encoded)
                        if event is not None:
                            yield event
                if buffered:
                    encoded = bytes(buffered[:-1] if buffered.endswith(b"\r") else buffered)
                    event = accept_line(encoded)
                    if event is not None:
                        yield event
                if terminal_event is None:
                    raise GatewayUnavailable(
                        "managed gateway stream ended before a terminal event"
                    )
                yield terminal_event
        except httpx.TimeoutException as error:
            raise GatewayUnavailable("managed gateway timed out") from error
        except httpx.TransportError as error:
            raise GatewayUnavailable("managed gateway transport failed") from error
