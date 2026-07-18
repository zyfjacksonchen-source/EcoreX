"""Explicit, bounded activation probes for administrator-managed models.

Catalog visibility is useful diagnostics, but it is not proof that a model can
serve the operation EcoreX will use.  This module therefore performs a real,
preset-specific inference only from the explicit ``test-and-activate`` admin
operation.  It is deliberately not wired into readiness or background health
checks: image probes can be billable and an uncertain POST must never be
retried automatically.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
import ssl
import struct
from typing import Any, Mapping, Protocol, runtime_checkable
import zlib

import httpx

from ecorex.gateway.responses_provider import (
    ResponsesProviderConfigurationError,
    normalize_https_origin,
)
from ecorex.gateway.models import ecorex_chat_gateway_policy

from .management_models import ActiveModelConfiguration, MANAGED_MODEL_ORIGIN_PRESETS


_CATALOG_LIMIT = 2 * 1024 * 1024
_TEXT_RESULT_LIMIT = 2 * 1024 * 1024
_IMAGE_RESULT_LIMIT = 64 * 1024 * 1024
_ACTIVATION_MARKER = "ECOREX_ACTIVATION_OK"
_IMAGE_EDGE = 1024


@dataclass(frozen=True, slots=True)
class ModelConnectionTestResult:
    passed: bool
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.passed and self.error_code is not None:
            raise ValueError("successful model test cannot include an error")
        if not self.passed and self.error_code not in {
            "provider_test_unconfigured",
            "provider_test_timeout",
            "provider_test_unavailable",
            "provider_test_uncertain",
            "provider_test_rate_limited",
            "provider_key_rejected",
            "provider_test_rejected",
            "provider_inference_rejected",
            "provider_model_unavailable",
            "provider_test_protocol",
            "provider_test_invalid",
            "provider_test_cancelled",
        }:
            raise ValueError("failed model test requires a safe error code")


@runtime_checkable
class ModelConnectionTester(Protocol):
    async def test(
        self, configuration: ActiveModelConfiguration
    ) -> ModelConnectionTestResult: ...


class RejectingModelConnectionTester:
    async def test(
        self, configuration: ActiveModelConfiguration
    ) -> ModelConnectionTestResult:
        del configuration
        return ModelConnectionTestResult(
            passed=False, error_code="provider_test_unconfigured"
        )


class _ProbeFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HTTPSModelConnectionTester:
    """Run one explicit catalog check followed by one real inference probe.

    Origins are configuration-owned public HTTPS origins.  Provider bodies and
    generated image bytes are inspected in memory only and never returned or
    persisted.  The deterministic idempotency key is scoped to one immutable
    configuration revision so a human retry can reconcile at providers that
    implement idempotency without this client issuing an automatic second POST.
    """

    def __init__(
        self,
        origins: Mapping[str, str],
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 180.0,
        max_concurrency: int = 4,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        normalized: dict[str, str] = {}
        for preset, origin in origins.items():
            if preset not in set(MANAGED_MODEL_ORIGIN_PRESETS.values()) | {
                # Read-only compatibility for a v1 database while its
                # deployment origins are being migrated.
                "responses",
                "openai_compatible_chat",
                "openai_compatible_image",
            }:
                raise ValueError("unknown model provider preset")
            try:
                normalized[preset] = normalize_https_origin(origin)
            except ResponsesProviderConfigurationError:
                raise ValueError("model provider origin is invalid") from None
        if (
            not normalized
            or not 1.0 <= timeout_seconds <= 600.0
            or not 1 <= max_concurrency <= 16
        ):
            raise ValueError("model connection tester configuration is invalid")
        self.origins = normalized
        self.timeout_seconds = timeout_seconds
        self._slots = asyncio.BoundedSemaphore(max_concurrency)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=min(10.0, timeout_seconds),
                read=timeout_seconds,
                write=timeout_seconds,
                pool=min(10.0, timeout_seconds),
            ),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            follow_redirects=False,
            trust_env=False,
            http2=False,
            verify=ssl_context if ssl_context is not None else True,
        )
        if not isinstance(self._client, httpx.AsyncClient):
            raise ValueError("model connection tester client is invalid")

    async def test(
        self, configuration: ActiveModelConfiguration
    ) -> ModelConnectionTestResult:
        origin = self.origins.get(configuration.provider_origin_preset)
        # Transitional compatibility is intentionally read-only: existing
        # single-origin deployments can test an old revision before their
        # deployment config is migrated, while all new slot configs persist a
        # dedicated origin preset.
        if origin is None:
            origin = self.origins.get(configuration.provider_preset)
        if origin is None:
            return ModelConnectionTestResult(
                passed=False, error_code="provider_test_unconfigured"
            )
        try:
            async with self._slots:
                await self._catalog_probe(origin, configuration)
                await self._inference_probe(origin, configuration)
        except _ProbeFailure as error:
            return ModelConnectionTestResult(passed=False, error_code=error.code)
        return ModelConnectionTestResult(passed=True)

    async def _catalog_probe(
        self, origin: str, configuration: ActiveModelConfiguration
    ) -> None:
        response = await self._request(
            "GET",
            origin + "/v1/models",
            configuration=configuration,
            maximum=_CATALOG_LIMIT,
            submission=False,
        )
        value = self._json_response(response)
        data = value.get("data")
        if not isinstance(data, list) or len(data) > 20_000:
            raise _ProbeFailure("provider_test_protocol")
        visible = {
            item.get("id")
            for item in data
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        if configuration.upstream_model_id not in visible:
            raise _ProbeFailure("provider_model_unavailable")

    async def _inference_probe(
        self, origin: str, configuration: ActiveModelConfiguration
    ) -> None:
        if configuration.provider_preset == "responses":
            try:
                policy = ecorex_chat_gateway_policy(configuration.local_model_id)
            except ValueError:
                raise _ProbeFailure("provider_test_unconfigured") from None
            response = await self._request(
                "POST",
                origin + "/v1/responses",
                configuration=configuration,
                maximum=_TEXT_RESULT_LIMIT,
                submission=True,
                json_body={
                    "model": configuration.upstream_model_id,
                    "instructions": (
                        "Return exactly ECOREX_ACTIVATION_OK and no other text."
                    ),
                    "input": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "EcoreX administrator activation probe.",
                                }
                            ],
                        }
                    ],
                    "max_output_tokens": 512,
                    "store": False,
                    "reasoning": {"effort": policy.reasoning_effort},
                    "context_management": [
                        {
                            "type": policy.context_management.type,
                            "compact_threshold": (
                                policy.context_management.compact_threshold_tokens
                            ),
                        }
                    ],
                },
            )
            value = self._json_response(response)
            self._validate_returned_model(value, configuration.upstream_model_id)
            text = self._responses_text(value)
            if text.strip() != _ACTIVATION_MARKER:
                raise _ProbeFailure("provider_test_protocol")
            return
        if configuration.provider_preset == "openai_compatible_chat":
            response = await self._request(
                "POST",
                origin + "/v1/chat/completions",
                configuration=configuration,
                maximum=_TEXT_RESULT_LIMIT,
                submission=True,
                json_body={
                    "model": configuration.upstream_model_id,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Return exactly ECOREX_ACTIVATION_OK and no other text."
                            ),
                        },
                        {
                            "role": "user",
                            "content": "EcoreX administrator activation probe.",
                        },
                    ],
                    "max_tokens": 64,
                    "stream": False,
                },
            )
            value = self._json_response(response)
            self._validate_returned_model(value, configuration.upstream_model_id)
            if self._chat_text(value).strip() != _ACTIVATION_MARKER:
                raise _ProbeFailure("provider_test_protocol")
            return
        if configuration.provider_preset != "openai_compatible_image":
            raise _ProbeFailure("provider_test_unconfigured")

        if configuration.modality == "image_generation":
            response = await self._request(
                "POST",
                origin + "/v1/images/generations",
                configuration=configuration,
                maximum=_IMAGE_RESULT_LIMIT,
                submission=True,
                json_body={
                    "model": configuration.upstream_model_id,
                    "prompt": (
                        "A plain centered orange circle on a clean white background."
                    ),
                    "n": 1,
                    "size": "1024x1024",
                    "quality": "low",
                    "output_format": "png",
                    "response_format": "b64_json",
                },
            )
        elif configuration.modality == "image_edit":
            response = await self._request(
                "POST",
                origin + "/v1/images/edits",
                configuration=configuration,
                maximum=_IMAGE_RESULT_LIMIT,
                submission=True,
                form={
                    "model": configuration.upstream_model_id,
                    "prompt": (
                        "Keep the white background and add one centered orange circle."
                    ),
                    "size": "1024x1024",
                    "quality": "low",
                    "output_format": "png",
                    "response_format": "b64_json",
                },
                files={
                    "image": (
                        "ecorex-activation.png",
                        _activation_png(),
                        "image/png",
                    )
                },
            )
        else:
            raise _ProbeFailure("provider_test_unconfigured")
        value = self._json_response(response)
        self._validate_image(value, configuration)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        configuration: ActiveModelConfiguration,
        maximum: int,
        submission: bool,
        json_body: Mapping[str, Any] | None = None,
        form: Mapping[str, str] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
    ) -> tuple[int, Mapping[str, str], bytes]:
        headers = {
            "Authorization": f"Bearer {configuration.api_key}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "EcoreX-Control-Plane/1.0",
        }
        if submission:
            headers["Idempotency-Key"] = self._idempotency_key(configuration)
        request = self._client.build_request(
            method,
            url,
            headers=headers,
            json=json_body,
            data=form,
            files=files,
        )
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await self._client.send(
                    request, stream=True, follow_redirects=False
                )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > maximum:
                        raise _ProbeFailure("provider_test_protocol")
        except _ProbeFailure:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise _ProbeFailure(
                "provider_test_uncertain" if submission else "provider_test_timeout"
            ) from None
        except httpx.TransportError:
            raise _ProbeFailure(
                "provider_test_uncertain" if submission else "provider_test_unavailable"
            ) from None
        finally:
            if response is not None:
                await response.aclose()

        status = response.status_code
        if status in {401, 403}:
            raise _ProbeFailure("provider_key_rejected")
        if status == 429:
            raise _ProbeFailure("provider_test_rate_limited")
        if status in {408, 425} or status >= 500:
            raise _ProbeFailure(
                "provider_test_uncertain" if submission else "provider_test_unavailable"
            )
        if status != 200:
            raise _ProbeFailure(
                "provider_inference_rejected" if submission else "provider_test_rejected"
            )
        return status, response.headers, bytes(body)

    @staticmethod
    def _json_response(
        response: tuple[int, Mapping[str, str], bytes]
    ) -> dict[str, Any]:
        _status, headers, body = response
        content_type = headers.get("content-type", "").split(";", 1)[0]
        if content_type.strip().casefold() != "application/json" or not body:
            raise _ProbeFailure("provider_test_protocol")
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise _ProbeFailure("provider_test_protocol") from None
        if not isinstance(value, dict):
            raise _ProbeFailure("provider_test_protocol")
        return value

    @staticmethod
    def _validate_returned_model(value: Mapping[str, Any], expected: str) -> None:
        returned = value.get("model")
        if not isinstance(returned, str) or not (
            returned == expected or returned.startswith(expected + "-")
        ):
            raise _ProbeFailure("provider_test_protocol")

    @staticmethod
    def _responses_text(value: Mapping[str, Any]) -> str:
        output = value.get("output")
        if not isinstance(output, list) or len(output) > 128:
            raise _ProbeFailure("provider_test_protocol")
        parts: list[str] = []
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list) or len(content) > 128:
                raise _ProbeFailure("provider_test_protocol")
            for part in content:
                if (
                    isinstance(part, Mapping)
                    and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str)
                ):
                    parts.append(part["text"])
        if not parts:
            raise _ProbeFailure("provider_test_protocol")
        return "".join(parts)

    @staticmethod
    def _chat_text(value: Mapping[str, Any]) -> str:
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise _ProbeFailure("provider_test_protocol")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            raise _ProbeFailure("provider_test_protocol")
        return content

    @staticmethod
    def _validate_image(
        value: Mapping[str, Any],
        configuration: ActiveModelConfiguration,
    ) -> None:
        data = value.get("data")
        if not isinstance(data, list) or len(data) != 1:
            raise _ProbeFailure("provider_test_protocol")
        item = data[0]
        encoded = item.get("b64_json") if isinstance(item, Mapping) else None
        if not isinstance(encoded, str) or not encoded or len(encoded) > 48 * 1024 * 1024:
            raise _ProbeFailure("provider_test_protocol")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise _ProbeFailure("provider_test_protocol") from None
        if (
            len(payload) < 24
            or payload[:8] != b"\x89PNG\r\n\x1a\n"
            or payload[12:16] != b"IHDR"
        ):
            raise _ProbeFailure("provider_test_protocol")
        width, height = struct.unpack(">II", payload[16:24])
        exact = (width, height) == (_IMAGE_EDGE, _IMAGE_EDGE)
        bounded_native_square = (
            configuration.local_model_id in {"gpt-image-2", "gpt-image-2-edit"}
            and width == height
            and _IMAGE_EDGE // 2 <= width <= _IMAGE_EDGE * 2
        )
        if not exact and not bounded_native_square:
            raise _ProbeFailure("provider_test_protocol")

    @staticmethod
    def _idempotency_key(configuration: ActiveModelConfiguration) -> str:
        material = (
            f"{configuration.config_id}\0{configuration.revision}\0"
            f"{configuration.provider_preset}\0{configuration.upstream_model_id}"
        ).encode("utf-8")
        return "ecorex-model-activation-" + hashlib.sha256(material).hexdigest()[:32]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _activation_png() -> bytes:
    """Create one deterministic 1024px RGBA PNG without optional dependencies."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    scanline = b"\x00" + (b"\xff\xff\xff\xff" * _IMAGE_EDGE)
    pixels = zlib.compress(scanline * _IMAGE_EDGE, level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", _IMAGE_EDGE, _IMAGE_EDGE, 8, 6, 0, 0, 0),
        )
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


__all__ = [
    "HTTPSModelConnectionTester",
    "ModelConnectionTester",
    "ModelConnectionTestResult",
    "RejectingModelConnectionTester",
]
