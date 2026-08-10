"""Bounded HTTPS adapter for the managed EcoreX image provider.

The adapter deliberately implements one fixed wire contract.  It never accepts
an arbitrary endpoint per job, never follows redirects, never uses environment
proxy settings and never renders credentials or upstream response bodies in an
exception.  A rotating workload-identity/secret source is called immediately
before each request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
import ssl
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ecorex.json_boundary import validate_json_complexity

from .cas import validate_image_payload
from .models import ImageJob, ImageUsage, canonical_json
from .provider import (
    ProviderRateLimited,
    ProviderRejected,
    ProviderResult,
    ProviderState,
    ProviderUncertain,
    ProviderUnavailable,
    normalize_retry_after_seconds,
)


_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/avif"})
_JSON_RESPONSE_BYTES = 128 * 1024
_RETRY_AFTER_HEADER_BYTES = 128


class ManagedImageProviderConfigurationError(RuntimeError):
    """The provider trust or resource envelope is missing/unsafe."""


def normalize_https_origin(value: str) -> str:
    """Return one canonical HTTPS origin and reject URL-shaped surprises."""

    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ManagedImageProviderConfigurationError("managed image origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ManagedImageProviderConfigurationError("managed image origin is invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ManagedImageProviderConfigurationError("managed image origin is invalid")
    hostname = parsed.hostname.casefold().rstrip(".")
    if not hostname or any(ord(character) < 33 for character in hostname):
        raise ManagedImageProviderConfigurationError("managed image origin is invalid")
    return urlunsplit(("https", hostname, "", "", ""))


class ManagedHTTPSImageProvider:
    """Production provider adapter with uncertainty-safe submit/recover/cancel."""

    def __init__(
        self,
        *,
        provider_id: str,
        origin: str,
        allowed_origins: frozenset[str],
        allowed_models: frozenset[str],
        bearer_token: Callable[[], str],
        timeout_seconds: float = 60.0,
        connect_timeout_seconds: float = 5.0,
        max_image_bytes: int = 64 * 1024 * 1024,
        max_connections: int = 32,
        max_concurrency: int = 16,
        ssl_context: ssl.SSLContext | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(provider_id, str) or _PROVIDER_ID.fullmatch(provider_id) is None:
            raise ManagedImageProviderConfigurationError("managed image provider identity is invalid")
        normalized_origin = normalize_https_origin(origin)
        if not isinstance(allowed_origins, frozenset) or not allowed_origins:
            raise ManagedImageProviderConfigurationError("managed image origin allowlist is missing")
        normalized_allowlist = frozenset(normalize_https_origin(item) for item in allowed_origins)
        if normalized_origin not in normalized_allowlist:
            raise ManagedImageProviderConfigurationError("managed image origin is not allowlisted")
        if (
            not isinstance(allowed_models, frozenset)
            or not allowed_models
            or any(
                not isinstance(model, str)
                or _PROVIDER_ID.fullmatch(model) is None
                for model in allowed_models
            )
        ):
            raise ManagedImageProviderConfigurationError("managed image model allowlist is invalid")
        if not callable(bearer_token):
            raise ManagedImageProviderConfigurationError("managed image credential source is unavailable")
        if not 1.0 <= timeout_seconds <= 600.0 or not 0.1 <= connect_timeout_seconds <= min(60.0, timeout_seconds):
            raise ManagedImageProviderConfigurationError("managed image timeout is invalid")
        if not 1024 <= max_image_bytes <= 256 * 1024 * 1024:
            raise ManagedImageProviderConfigurationError("managed image byte bound is invalid")
        if not 1 <= max_concurrency <= max_connections <= 256:
            raise ManagedImageProviderConfigurationError("managed image concurrency is invalid")

        self.provider_id = provider_id
        self.origin = normalized_origin
        self.allowed_origins = normalized_allowlist
        self.allowed_models = allowed_models
        self._bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.max_image_bytes = max_image_bytes
        self._slots = asyncio.BoundedSemaphore(max_concurrency)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=connect_timeout_seconds,
                read=timeout_seconds,
                write=timeout_seconds,
                pool=connect_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
                keepalive_expiry=15.0,
            ),
            follow_redirects=False,
            trust_env=False,
            http2=False,
            verify=ssl_context if ssl_context is not None else True,
        )
        if not isinstance(self._client, httpx.AsyncClient):
            raise ManagedImageProviderConfigurationError("managed image HTTP client is invalid")

    async def submit(self, job: ImageJob, *, idempotency_key: str) -> ProviderResult:
        self._validate_job(job)
        payload = {
            "schema_version": 1,
            "account_id": job.account_id,
            "job_id": job.job_id,
            "idempotency_key": idempotency_key,
            "request": job.request.provider_payload(),
        }
        try:
            value = await self._json_request("/v1/image/jobs", payload)
        except (ProviderRateLimited, ProviderRejected):
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            # A submit request may have crossed the provider boundary even if
            # no response arrived.  Only recover may decide whether resubmit is
            # safe; the worker persists that uncertainty bit.
            raise ProviderUncertain("managed image submit result is uncertain") from None
        return await self._result(job, value)

    async def recover(
        self,
        job: ImageJob,
        *,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderResult:
        self._validate_job(job)
        self._validate_optional_request_id(provider_request_id)
        payload = {
            "schema_version": 1,
            "account_id": job.account_id,
            "job_id": job.job_id,
            "idempotency_key": idempotency_key,
            "provider_request_id": provider_request_id,
        }
        try:
            value = await self._json_request("/v1/image/jobs/recover", payload)
            return await self._result(job, value)
        except (ProviderRateLimited, ProviderRejected):
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProviderUnavailable("managed image recovery is unavailable") from None

    async def cancel(
        self,
        job: ImageJob,
        *,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> None:
        self._validate_job(job)
        self._validate_optional_request_id(provider_request_id)
        payload = {
            "schema_version": 1,
            "account_id": job.account_id,
            "job_id": job.job_id,
            "idempotency_key": idempotency_key,
            "provider_request_id": provider_request_id,
        }
        try:
            value = await self._json_request(
                "/v1/image/jobs/cancel", payload, not_found_ok=True
            )
        except (ProviderRateLimited, ProviderRejected):
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProviderUnavailable("managed image cancellation is unavailable") from None
        if value and value != {"schema_version": 1, "cancelled": True}:
            raise ProviderRejected("managed image cancellation response is invalid")

    async def health(self) -> None:
        """Authenticated live dependency probe with a bounded response."""

        value = await self._json_request("/v1/image/health", None, method="GET")
        if value != {"schema_version": 1, "status": "ready"}:
            raise ProviderUnavailable("managed image provider is not ready")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _validate_job(self, job: ImageJob) -> None:
        if not isinstance(job, ImageJob) or job.request.model_id not in self.allowed_models:
            raise ProviderRejected("managed image model is not available")

    @staticmethod
    def _validate_optional_request_id(value: str | None) -> None:
        if value is not None and (
            not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None
        ):
            raise ProviderRejected("managed image request identity is invalid")

    async def _json_request(
        self,
        path: str,
        payload: Mapping[str, Any] | None,
        *,
        method: str = "POST",
        not_found_ok: bool = False,
    ) -> dict[str, Any]:
        headers = self._headers()
        content = None
        if payload is not None:
            content = canonical_json(dict(payload)).encode("utf-8")
            if len(content) > 256 * 1024:
                raise ProviderRejected("managed image request is oversized")
            headers["Content-Type"] = "application/json"
            idempotency_key = payload.get("idempotency_key")
            if isinstance(idempotency_key, str):
                headers["Idempotency-Key"] = idempotency_key
        request = self._client.build_request(
            method,
            self.origin + path,
            headers=headers,
            content=content,
        )
        async with self._slots:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await self._client.send(request, stream=True)
                    try:
                        status = response.status_code
                        if status == 429:
                            raise ProviderRateLimited(
                                "managed image provider is rate limited",
                                retry_after_seconds=_parse_retry_after(
                                    response.headers.get("retry-after")
                                ),
                            )
                        if status == 404 and not_found_ok:
                            return {}
                        if 500 <= status <= 599 or status in {408, 425}:
                            raise ProviderUnavailable("managed image provider is unavailable")
                        if status < 200 or status >= 300:
                            raise ProviderRejected("managed image provider rejected the request")
                        body = await self._bounded_body(response, _JSON_RESPONSE_BYTES)
                    finally:
                        await response.aclose()
            except (ProviderRateLimited, ProviderRejected, ProviderUnavailable):
                raise
            except asyncio.CancelledError:
                raise
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
                raise ProviderUnavailable("managed image provider is unavailable") from None
        return self._decode_object(body)

    async def _result(self, job: ImageJob, value: Mapping[str, Any]) -> ProviderResult:
        required = {
            "schema_version",
            "state",
            "account_id",
            "job_id",
            "provider_request_id",
            "result",
            "usage",
            "error_code",
        }
        if set(value) != required or value.get("schema_version") != 1:
            raise ProviderRejected("managed image response contract is invalid")
        if value.get("account_id") != job.account_id or value.get("job_id") != job.job_id:
            raise ProviderRejected("managed image response identity is invalid")
        try:
            state = ProviderState(value.get("state"))
        except (TypeError, ValueError):
            raise ProviderRejected("managed image response state is invalid") from None
        request_id = value.get("provider_request_id")
        self._validate_optional_request_id(request_id)
        error_code = value.get("error_code")
        if error_code is not None and (
            not isinstance(error_code, str) or _ERROR_CODE.fullmatch(error_code) is None
        ):
            raise ProviderRejected("managed image error code is invalid")

        if state is ProviderState.COMPLETED:
            if request_id is None or error_code is not None:
                raise ProviderRejected("managed image completion is invalid")
            result = value.get("result")
            usage_value = value.get("usage")
            if not isinstance(result, Mapping) or set(result) != {
                "sha256",
                "size_bytes",
                "mime_type",
            }:
                raise ProviderRejected("managed image result contract is invalid")
            sha256 = result.get("sha256")
            size_bytes = result.get("size_bytes")
            mime_type = result.get("mime_type")
            if (
                not isinstance(sha256, str)
                or _SHA256.fullmatch(sha256) is None
                or isinstance(size_bytes, bool)
                or not isinstance(size_bytes, int)
                or not 1 <= size_bytes <= self.max_image_bytes
                or mime_type not in _MIME_TYPES
            ):
                raise ProviderRejected("managed image result commitment is invalid")
            usage = self._usage(job, usage_value)
            payload = await self._download_result(
                request_id,
                sha256=sha256,
                size_bytes=size_bytes,
                mime_type=str(mime_type),
            )
            return ProviderResult(
                state,
                provider_request_id=request_id,
                payload=payload,
                mime_type=str(mime_type),
                sha256=sha256,
                usage=usage,
            )

        if value.get("result") is not None or value.get("usage") is not None:
            raise ProviderRejected("managed image non-terminal response carries a result")
        if state is ProviderState.FAILED:
            if error_code is None:
                raise ProviderRejected("managed image failure omitted its error code")
        elif error_code is not None:
            raise ProviderRejected("managed image response error is inconsistent")
        return ProviderResult(
            state,
            provider_request_id=request_id,
            error_code=error_code,
        )

    def _usage(self, job: ImageJob, value: Any) -> ImageUsage:
        if not isinstance(value, Mapping) or set(value) != {
            "provider",
            "model_id",
            "input_units",
            "output_units",
            "billed_units",
        }:
            raise ProviderRejected("managed image usage contract is invalid")
        try:
            usage = ImageUsage(**dict(value))
        except (TypeError, ValueError):
            raise ProviderRejected("managed image usage contract is invalid") from None
        if usage.provider != self.provider_id or usage.model_id != job.request.model_id:
            raise ProviderRejected("managed image usage identity is invalid")
        return usage

    async def _download_result(
        self,
        provider_request_id: str,
        *,
        sha256: str,
        size_bytes: int,
        mime_type: str,
    ) -> bytes:
        # The URL is constructed from the already-allowlisted origin.  Upstream
        # response data can never select a host, scheme, port or path prefix.
        url = self.origin + "/v1/image/results/" + quote(provider_request_id, safe="")
        headers = self._headers()
        headers["Accept"] = mime_type
        request = self._client.build_request("GET", url, headers=headers)
        async with self._slots:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await self._client.send(request, stream=True)
                    try:
                        if response.status_code == 429:
                            raise ProviderRateLimited(
                                "managed image provider is rate limited",
                                retry_after_seconds=_parse_retry_after(
                                    response.headers.get("retry-after")
                                ),
                                recovery_required=True,
                            )
                        if 500 <= response.status_code <= 599:
                            raise ProviderUnavailable("managed image result is unavailable")
                        if response.status_code < 200 or response.status_code >= 300:
                            raise ProviderRejected("managed image result was rejected")
                        declared = response.headers.get("content-length")
                        if declared is not None and (
                            not declared.isdigit()
                            or int(declared) != size_bytes
                            or int(declared) > self.max_image_bytes
                        ):
                            raise ProviderRejected("managed image result size changed")
                        response_type = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
                        if response_type != mime_type:
                            raise ProviderRejected("managed image result MIME changed")
                        body = await self._bounded_body(response, self.max_image_bytes)
                    finally:
                        await response.aclose()
            except (ProviderRateLimited, ProviderRejected, ProviderUnavailable):
                raise
            except asyncio.CancelledError:
                raise
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
                raise ProviderUnavailable("managed image result is unavailable") from None
        if len(body) != size_bytes or hashlib.sha256(body).hexdigest() != sha256:
            raise ProviderRejected("managed image result integrity changed")
        try:
            validate_image_payload(
                body,
                mime_type=mime_type,
                max_bytes=self.max_image_bytes,
                expected_sha256=sha256,
            )
        except Exception:
            raise ProviderRejected("managed image result content is invalid") from None
        return body
    def _headers(self) -> dict[str, str]:
        try:
            token = self._bearer_token()
        except Exception:
            raise ProviderUnavailable("managed image credential is unavailable") from None
        if (
            not isinstance(token, str)
            or not 24 <= len(token) <= 8192
            or any(character.isspace() or ord(character) < 33 for character in token)
        ):
            raise ProviderUnavailable("managed image credential is unavailable")
        return {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": "EcoreX-Image-Orchestrator/1.0",
        }

    @staticmethod
    async def _bounded_body(response: httpx.Response, maximum: int) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None and (
            not declared.isdigit() or int(declared) > maximum
        ):
            raise ProviderRejected("managed image response is oversized")
        body = bytearray()
        async for chunk in response.aiter_bytes(64 * 1024):
            if len(body) + len(chunk) > maximum:
                raise ProviderRejected("managed image response is oversized")
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    def _decode_object(payload: bytes) -> dict[str, Any]:
        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON member")
                value[key] = item
            return value

        try:
            value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
            validate_json_complexity(value)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise ProviderRejected("managed image response is invalid") from None
        if not isinstance(value, dict):
            raise ProviderRejected("managed image response is invalid")
        return value


def _parse_retry_after(
    value: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse RFC Retry-After without allowing a provider-controlled stall."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > _RETRY_AFTER_HEADER_BYTES
    ):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.isascii() and candidate.isdigit():
        try:
            return normalize_retry_after_seconds(int(candidate, 10))
        except (ValueError, OverflowError):
            return None
    try:
        parsed = parsedate_to_datetime(candidate)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None or reference.utcoffset() is None:
        return None
    return normalize_retry_after_seconds(
        (parsed.astimezone(UTC) - reference.astimezone(UTC)).total_seconds()
    )


__all__ = [
    "ManagedHTTPSImageProvider",
    "ManagedImageProviderConfigurationError",
    "normalize_https_origin",
]
