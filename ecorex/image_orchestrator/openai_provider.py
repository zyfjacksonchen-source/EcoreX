"""Direct, bounded OpenAI-compatible Images API adapter.

This adapter runs only in the cloud Image Orchestrator.  Administrator-managed
credentials therefore never cross into the local Runtime.  A submit is never
retried inside this adapter: after a timeout or 5xx response the upstream may
already have billed the request, so the durable worker moves into recovery
instead of creating a second image implicitly.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
import json
import re
from typing import Any

import httpx

from .cas import ImageContentAddressedStore, validate_image_payload
from .managed_provider import (
    ManagedImageProviderConfigurationError,
    normalize_https_origin,
)
from .models import ImageJob, ImageOperation, ImageUsage, canonical_json
from .provider import (
    ProviderRateLimited,
    ProviderRejected,
    ProviderResult,
    ProviderState,
    ProviderUncertain,
    ProviderUnavailable,
    normalize_retry_after_seconds,
)


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_JSON_OVERHEAD_BYTES = 64 * 1024
_HEALTH_BYTES = 2 * 1024 * 1024
_MAX_EDIT_IMAGES = 16
_MAX_RETRY_AFTER_BYTES = 128
_UPSTREAM_INPUT_BYTES = 50 * 1024 * 1024
_GPT_IMAGE_2_MAX_EDGE = 3840
_GPT_IMAGE_2_MIN_PIXELS = 655_360
_GPT_IMAGE_2_MAX_PIXELS = 8_294_400


class OpenAICompatibleImageProvider:
    """Call fixed generation/edit routes with one frozen model revision."""

    def __init__(
        self,
        *,
        provider_id: str,
        origin: str,
        allowed_origins: frozenset[str],
        allowed_models: frozenset[str],
        bearer_token: Callable[[], str],
        input_store: ImageContentAddressedStore | None,
        timeout_seconds: float = 120.0,
        connect_timeout_seconds: float = 5.0,
        max_image_bytes: int = 64 * 1024 * 1024,
        max_connections: int = 32,
        max_concurrency: int = 16,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(provider_id, str) or _IDENTITY.fullmatch(provider_id) is None:
            raise ManagedImageProviderConfigurationError(
                "image provider identity is invalid"
            )
        normalized_origin = normalize_https_origin(origin)
        if not isinstance(allowed_origins, frozenset) or not allowed_origins:
            raise ManagedImageProviderConfigurationError(
                "image provider origin allowlist is missing"
            )
        normalized_allowlist = frozenset(
            normalize_https_origin(item) for item in allowed_origins
        )
        if normalized_origin not in normalized_allowlist:
            raise ManagedImageProviderConfigurationError(
                "image provider origin is not allowlisted"
            )
        if (
            not isinstance(allowed_models, frozenset)
            or not allowed_models
            or any(
                not isinstance(model, str) or _IDENTITY.fullmatch(model) is None
                for model in allowed_models
            )
        ):
            raise ManagedImageProviderConfigurationError(
                "image provider model allowlist is invalid"
            )
        if not callable(bearer_token):
            raise ManagedImageProviderConfigurationError(
                "image provider credential source is unavailable"
            )
        if input_store is not None and not isinstance(
            input_store, ImageContentAddressedStore
        ):
            raise ManagedImageProviderConfigurationError(
                "image provider input store is invalid"
            )
        if not 1.0 <= timeout_seconds <= 600.0 or not (
            0.1 <= connect_timeout_seconds <= min(60.0, timeout_seconds)
        ):
            raise ManagedImageProviderConfigurationError(
                "image provider timeout is invalid"
            )
        if not 1024 <= max_image_bytes <= 256 * 1024 * 1024:
            raise ManagedImageProviderConfigurationError(
                "image provider byte bound is invalid"
            )
        if not 1 <= max_concurrency <= max_connections <= 256:
            raise ManagedImageProviderConfigurationError(
                "image provider concurrency is invalid"
            )

        self.provider_id = provider_id
        self.origin = normalized_origin
        self.allowed_origins = normalized_allowlist
        self.allowed_models = allowed_models
        self.input_store = input_store
        self.timeout_seconds = timeout_seconds
        self.max_image_bytes = max_image_bytes
        self._bearer_token = bearer_token
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
        )
        if not isinstance(self._client, httpx.AsyncClient):
            raise ManagedImageProviderConfigurationError(
                "image provider HTTP client is invalid"
            )

    async def submit(self, job: ImageJob, *, idempotency_key: str) -> ProviderResult:
        self._validate_job(job)
        self._validate_idempotency_key(idempotency_key)
        if job.request.operation is ImageOperation.GENERATE:
            body, headers = await self._request(
                "POST",
                "/v1/images/generations",
                content=canonical_json(self._generation_fields(job)).encode("utf-8"),
                content_type="application/json",
                idempotency_key=idempotency_key,
                submission=True,
                maximum=self._image_json_limit,
            )
        else:
            fields, files = await self._retouch_fields(job)
            body, headers = await self._request(
                "POST",
                "/v1/images/edits",
                form=fields,
                files=files,
                idempotency_key=idempotency_key,
                submission=True,
                maximum=self._image_json_limit,
            )
        return self._completed(job, body, headers)

    async def recover(
        self,
        job: ImageJob,
        *,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderResult:
        self._validate_job(job)
        self._validate_idempotency_key(idempotency_key)
        if provider_request_id is not None and (
            not isinstance(provider_request_id, str)
            or _REQUEST_ID.fullmatch(provider_request_id) is None
        ):
            raise ProviderRejected("image provider request identity is invalid")
        # The synchronous Images API has no authoritative lookup endpoint.
        # Resubmitting here could double bill and create duplicate artifacts.
        raise ProviderUncertain("image provider result requires reconciliation")

    async def cancel(
        self,
        job: ImageJob,
        *,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> None:
        self._validate_job(job)
        self._validate_idempotency_key(idempotency_key)
        if provider_request_id is not None and (
            not isinstance(provider_request_id, str)
            or _REQUEST_ID.fullmatch(provider_request_id) is None
        ):
            raise ProviderRejected("image provider request identity is invalid")
        # A completed synchronous request cannot be cancelled upstream.

    async def health(self) -> None:
        self._pillow()
        body, _headers = await self._request(
            "GET",
            "/v1/models",
            submission=False,
            maximum=_HEALTH_BYTES,
        )
        value = self._decode_object(body)
        data = value.get("data")
        if not isinstance(data, list):
            raise ProviderUnavailable("image provider model catalog is unavailable")
        available = {
            item.get("id")
            for item in data
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        if not self.allowed_models.issubset(available):
            raise ProviderUnavailable("image provider model is unavailable")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def _image_json_limit(self) -> int:
        return ((self.max_image_bytes + 2) // 3) * 4 + _JSON_OVERHEAD_BYTES

    def _generation_fields(self, job: ImageJob) -> dict[str, Any]:
        request = job.request
        return {
            "model": request.model_id,
            "prompt": request.prompt,
            "n": 1,
            "size": self._provider_size(
                request.model_id, request.width, request.height
            ),
            "quality": self._quality(request.metadata.get("quality")),
            "output_format": "png",
        }

    async def _retouch_fields(
        self, job: ImageJob
    ) -> tuple[dict[str, str], list[tuple[str, tuple[str, bytes, str]]]]:
        if self.input_store is None:
            raise ProviderUnavailable("image retouch input store is unavailable")
        request = job.request
        instruction, image_digests, mask_digest = self._retouch_command(job)
        if not image_digests or len(image_digests) > _MAX_EDIT_IMAGES:
            raise ProviderRejected("image retouch input count is invalid")
        loaded: list[tuple[str, bytes, str]] = []
        total_input_bytes = 0
        for index, digest in enumerate(image_digests):
            payload, mime_type = await self._load_input(digest)
            if len(payload) >= _UPSTREAM_INPUT_BYTES:
                raise ProviderRejected("image retouch input exceeds provider limit")
            total_input_bytes += len(payload)
            if total_input_bytes > self.max_image_bytes:
                raise ProviderRejected("image retouch inputs are oversized")
            loaded.append((f"image_{index}{self._extension(mime_type)}", payload, mime_type))
        mask: tuple[str, bytes, str] | None = None
        if mask_digest is not None:
            payload, mime_type = await self._load_input(mask_digest)
            if mime_type != "image/png":
                raise ProviderRejected("image retouch mask must be PNG")
            loaded[0], payload = await asyncio.to_thread(
                self._prepare_mask,
                loaded[0],
                payload,
                request.width,
                request.height,
            )
            total_input_bytes = sum(len(item[1]) for item in loaded) + len(payload)
            if (
                len(payload) >= _UPSTREAM_INPUT_BYTES
                or total_input_bytes > self.max_image_bytes
            ):
                raise ProviderRejected("image retouch inputs are oversized")
            mask = ("mask.png", payload, mime_type)
        field_name = "image" if len(loaded) == 1 else "image[]"
        files = [(field_name, item) for item in loaded]
        if mask is not None:
            files.append(("mask", mask))
        fields = {
            "model": request.model_id,
            "prompt": instruction,
            "size": self._provider_size(
                request.model_id, request.width, request.height
            ),
            "quality": self._quality(request.metadata.get("quality")),
            "output_format": "png",
        }
        return fields, files

    def _retouch_command(
        self, job: ImageJob
    ) -> tuple[str, tuple[str, ...], str | None]:
        request = job.request
        assert request.instruction is not None
        inputs = set(request.input_sha256)
        try:
            structured = json.loads(request.instruction)
        except json.JSONDecodeError:
            structured = None
        except RecursionError:
            raise ProviderRejected("image retouch instruction is invalid") from None
        if not isinstance(structured, Mapping):
            return request.instruction, request.input_sha256, None
        if structured.get("schema_version") != 1:
            raise ProviderRejected("image retouch command schema is invalid")
        ordered: list[str] = []

        def add_digest(value: Any) -> None:
            if (
                isinstance(value, str)
                and _DIGEST.fullmatch(value)
                and value in inputs
                and value not in ordered
            ):
                ordered.append(value)

        base = structured.get("base")
        if not isinstance(base, Mapping):
            raise ProviderRejected("image retouch base is invalid")
        add_digest(base.get("sha256"))
        if not ordered:
            raise ProviderRejected("image retouch base is unavailable")
        for collection_name in ("selected", "references"):
            collection = structured.get(collection_name, [])
            if not isinstance(collection, list):
                raise ProviderRejected("image retouch references are invalid")
            for item in collection:
                if isinstance(item, Mapping):
                    add_digest(item.get("sha256"))
        mask_value = structured.get("mask")
        mask_digest: str | None = None
        if mask_value is not None:
            if not isinstance(mask_value, Mapping):
                raise ProviderRejected("image retouch mask is invalid")
            candidate = mask_value.get("sha256")
            if (
                not isinstance(candidate, str)
                or _DIGEST.fullmatch(candidate) is None
                or candidate not in inputs
            ):
                raise ProviderRejected("image retouch mask is unavailable")
            mask_digest = candidate
        uncovered = inputs.difference(ordered)
        if mask_digest is not None:
            uncovered.discard(mask_digest)
        if uncovered:
            raise ProviderRejected("image retouch inputs are inconsistent")

        instructions: list[str] = []
        global_instruction = structured.get("global_instruction")
        if isinstance(global_instruction, str) and global_instruction.strip():
            instructions.append(global_instruction.strip())
        annotations = structured.get("annotations", [])
        if not isinstance(annotations, list):
            raise ProviderRejected("image retouch annotations are invalid")
        for annotation in annotations:
            if not isinstance(annotation, Mapping):
                raise ProviderRejected("image retouch annotation is invalid")
            value = annotation.get("instruction")
            if isinstance(value, str) and value.strip():
                instructions.append(value.strip())
        if not instructions:
            instructions.append(request.prompt)
        scope = (
            "Apply the requested changes only inside the supplied mask. "
            "Preserve every unmasked region, composition, identity, text and style."
            if mask_digest is not None
            else "Use the first image as the base and preserve unrelated content."
        )
        prompt = scope + "\n\nRequested changes:\n" + "\n".join(
            f"- {value}" for value in instructions
        )
        if len(prompt.encode("utf-8")) > 32 * 1024:
            raise ProviderRejected("image retouch instruction is oversized")
        return prompt, tuple(ordered), mask_digest

    async def _load_input(self, digest: str) -> tuple[bytes, str]:
        assert self.input_store is not None
        try:
            payload = await asyncio.to_thread(self.input_store.read, digest)
        except Exception:
            raise ProviderUnavailable("image retouch input is unavailable") from None
        mime_type = self._mime_type(payload)
        try:
            validate_image_payload(
                payload,
                mime_type=mime_type,
                max_bytes=self.max_image_bytes,
                expected_sha256=digest,
            )
        except Exception:
            raise ProviderRejected("image retouch input is invalid") from None
        return payload, mime_type

    async def _request(
        self,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        content_type: str | None = None,
        form: Mapping[str, str] | None = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        idempotency_key: str | None = None,
        submission: bool,
        maximum: int,
    ) -> tuple[bytes, Mapping[str, str]]:
        headers = self._headers()
        if content_type is not None:
            headers["Content-Type"] = content_type
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = self._client.build_request(
            method,
            self.origin + path,
            headers=headers,
            content=content,
            data=form,
            files=files,
        )
        async with self._slots:
            response: httpx.Response | None = None
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await self._client.send(request, stream=True)
                    if response.status_code == 429:
                        raise ProviderRateLimited(
                            "image provider is rate limited",
                            retry_after_seconds=self._retry_after(
                                response.headers.get("retry-after")
                            ),
                            recovery_required=False,
                        )
                    if response.status_code in {408, 425} or 500 <= response.status_code <= 599:
                        if submission:
                            raise ProviderUncertain(
                                "image provider submit result is uncertain"
                            )
                        raise ProviderUnavailable("image provider is unavailable")
                    if response.status_code < 200 or response.status_code >= 300:
                        raise ProviderRejected("image provider rejected the request")
                    body = await self._bounded_body(response, maximum)
                    return body, dict(response.headers)
            except (
                ProviderRateLimited,
                ProviderRejected,
                ProviderUncertain,
                ProviderUnavailable,
            ):
                raise
            except asyncio.CancelledError:
                raise
            except (TimeoutError, httpx.TimeoutException, httpx.TransportError):
                error = ProviderUncertain if submission else ProviderUnavailable
                raise error("image provider request result is unavailable") from None
            finally:
                if response is not None:
                    await response.aclose()

    def _completed(
        self,
        job: ImageJob,
        payload: bytes,
        headers: Mapping[str, str],
    ) -> ProviderResult:
        value = self._decode_object(payload)
        data = value.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
            raise ProviderRejected("image provider response is invalid")
        item = data[0]
        encoded = item.get("b64_json")
        if not isinstance(encoded, str) or "url" in item:
            raise ProviderRejected("image provider did not return inline image bytes")
        if len(encoded) > ((self.max_image_bytes + 2) // 3) * 4:
            raise ProviderRejected("image provider response is oversized")
        try:
            image = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ProviderRejected("image provider response is invalid") from None
        mime_type = self._mime_type(image)
        if mime_type != "image/png":
            raise ProviderRejected("image provider output format changed")
        try:
            result = validate_image_payload(
                image,
                mime_type=mime_type,
                max_bytes=self.max_image_bytes,
            )
        except Exception:
            raise ProviderRejected("image provider result is invalid") from None
        self._verify_provider_png(image, job)
        request_id = headers.get("x-request-id")
        if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
            request_id = job.job_id if _REQUEST_ID.fullmatch(job.job_id) else None
        return ProviderResult(
            ProviderState.COMPLETED,
            provider_request_id=request_id,
            payload=image,
            mime_type=mime_type,
            sha256=result.sha256,
            usage=self._usage(job, value.get("usage")),
        )

    def _usage(self, job: ImageJob, value: Any) -> ImageUsage:
        raw = value if isinstance(value, Mapping) else {}

        def count(name: str, fallback: int) -> int:
            candidate = raw.get(name)
            return (
                candidate
                if isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and 0 <= candidate <= 10**15
                else fallback
            )

        return ImageUsage(
            provider=self.provider_id,
            model_id=job.request.model_id,
            input_units=count("input_tokens", 0),
            output_units=count("output_tokens", 1),
            billed_units=1,
        )

    def _headers(self) -> dict[str, str]:
        try:
            token = self._bearer_token()
        except Exception:
            raise ProviderUnavailable("image provider credential is unavailable") from None
        if (
            not isinstance(token, str)
            or not 24 <= len(token) <= 8192
            or any(character.isspace() or ord(character) < 33 for character in token)
        ):
            raise ProviderUnavailable("image provider credential is unavailable")
        return {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": "EcoreX-Image-Orchestrator/1.0",
        }

    def _validate_job(self, job: ImageJob) -> None:
        if not isinstance(job, ImageJob) or job.request.model_id not in self.allowed_models:
            raise ProviderRejected("image provider model is unavailable")
        request = job.request
        if request.count != 1:
            raise ProviderRejected("image provider output count is unsupported")
        if request.width * request.height * 4 > self.max_image_bytes:
            raise ProviderRejected("image provider dimensions exceed the memory envelope")
        self._provider_size(request.model_id, request.width, request.height)

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
            raise ProviderRejected("image provider idempotency identity is invalid")

    @staticmethod
    def _provider_size(model_id: str, width: int, height: int) -> str:
        candidate = f"{width}x{height}"
        if model_id == "gpt-image-2" or model_id.startswith("gpt-image-2-"):
            pixels = width * height
            shorter, longer = sorted((width, height))
            if (
                longer > _GPT_IMAGE_2_MAX_EDGE
                or width % 16
                or height % 16
                or longer > shorter * 3
                or pixels < _GPT_IMAGE_2_MIN_PIXELS
                or pixels > _GPT_IMAGE_2_MAX_PIXELS
            ):
                raise ProviderRejected("image provider dimensions are unsupported")
            return candidate
        return (
            candidate
            if candidate in {"1024x1024", "1536x1024", "1024x1536"}
            else "auto"
        )

    @staticmethod
    def _quality(value: Any) -> str:
        return value if value in {"low", "medium", "high", "auto"} else "medium"

    @staticmethod
    def _mime_type(payload: bytes) -> str:
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if payload.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
            return "image/webp"
        if len(payload) >= 16 and payload[4:8] == b"ftyp":
            box_size = int.from_bytes(payload[:4], "big")
            if 16 <= box_size <= len(payload):
                brands = {payload[8:12]}
                brands.update(
                    payload[offset : offset + 4]
                    for offset in range(16, box_size - 3, 4)
                )
                if brands & {b"avif", b"avis"}:
                    return "image/avif"
        raise ProviderRejected("image provider content type is invalid")

    @staticmethod
    def _extension(mime_type: str) -> str:
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/avif": ".avif",
        }[mime_type]

    def _prepare_mask(
        self,
        base: tuple[str, bytes, str],
        mask_payload: bytes,
        expected_width: int,
        expected_height: int,
    ) -> tuple[tuple[str, bytes, str], bytes]:
        Image = self._pillow()
        from PIL import ImageOps

        try:
            with Image.open(BytesIO(base[1])) as source_image:
                source_width, source_height = source_image.size
                if (
                    source_width < 1
                    or source_height < 1
                    or source_width > 8192
                    or source_height > 8192
                    or source_width * source_height * 4 > self.max_image_bytes
                ):
                    raise ProviderRejected("image retouch dimensions are oversized")
                if source_image.format == "PNG":
                    width, height = source_image.size
                    source_image.verify()
                else:
                    oriented = ImageOps.exif_transpose(source_image)
                    try:
                        width, height = oriented.size
                        converted = oriented.convert("RGBA")
                        output = BytesIO()
                        converted.save(output, format="PNG", compress_level=6)
                        converted.close()
                        base_payload = output.getvalue()
                        base = ("image_0.png", base_payload, "image/png")
                    finally:
                        if oriented is not source_image:
                            oriented.close()
                if (width, height) != (expected_width, expected_height):
                    raise ProviderRejected(
                        "image retouch surface dimensions are inconsistent"
                    )
            with Image.open(BytesIO(mask_payload)) as source_mask:
                if source_mask.format != "PNG":
                    raise ProviderRejected("image retouch mask must be PNG")
                mask_width, mask_height = source_mask.size
                if (
                    mask_width < 1
                    or mask_height < 1
                    or mask_width * mask_height > 4_194_304
                ):
                    raise ProviderRejected("image retouch mask dimensions are invalid")
                selection = source_mask.convert("L")
                if selection.size != (width, height):
                    resized = selection.resize((width, height), Image.Resampling.NEAREST)
                    selection.close()
                    selection = resized
                # EcoreX mask pixels are 255 inside the selected edit region.
                # Images API alpha semantics are the inverse: transparent
                # pixels are replaced and opaque pixels are preserved.
                alpha = selection.point(lambda value: 255 - value)
                selection.close()
                provider_mask = Image.new("RGBA", (width, height), (255, 255, 255, 255))
                provider_mask.putalpha(alpha)
                alpha.close()
                output = BytesIO()
                provider_mask.save(output, format="PNG", compress_level=9)
                provider_mask.close()
                prepared_mask = output.getvalue()
        except ProviderRejected:
            raise
        except MemoryError:
            raise
        except Exception:
            raise ProviderRejected("image retouch surface is invalid") from None
        if len(base[1]) >= _UPSTREAM_INPUT_BYTES:
            raise ProviderRejected("image retouch input exceeds provider limit")
        return base, prepared_mask

    def _verify_provider_png(self, payload: bytes, job: ImageJob) -> None:
        Image = self._pillow()
        try:
            with Image.open(BytesIO(payload)) as image:
                width, height = image.size
                if (
                    image.format != "PNG"
                    or width < 1
                    or height < 1
                    or width > 8192
                    or height > 8192
                    or width * height * 4 > self.max_image_bytes
                ):
                    raise ProviderRejected("image provider dimensions are invalid")
                requested = self._provider_size(
                    job.request.model_id,
                    job.request.width,
                    job.request.height,
                )
                if requested != "auto" and (width, height) != (
                    job.request.width,
                    job.request.height,
                ):
                    raise ProviderRejected("image provider dimensions changed")
                image.verify()
        except ProviderRejected:
            raise
        except MemoryError:
            raise
        except Exception:
            raise ProviderRejected("image provider PNG is invalid") from None

    @staticmethod
    def _pillow():
        try:
            from PIL import Image
        except ImportError:
            raise ProviderUnavailable("image codec dependency is unavailable") from None
        return Image

    @staticmethod
    async def _bounded_body(response: httpx.Response, maximum: int) -> bytes:
        declared = response.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > maximum):
            raise ProviderRejected("image provider response is oversized")
        body = bytearray()
        async for chunk in response.aiter_bytes(64 * 1024):
            if len(body) + len(chunk) > maximum:
                raise ProviderRejected("image provider response is oversized")
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
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ProviderRejected("image provider response is invalid") from None
        if not isinstance(value, dict):
            raise ProviderRejected("image provider response is invalid")
        return value

    @staticmethod
    def _retry_after(
        value: str | None,
        *,
        now: datetime | None = None,
    ) -> float | None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > _MAX_RETRY_AFTER_BYTES
        ):
            return None
        candidate = value.strip()
        if candidate.isascii() and candidate.isdigit():
            try:
                return normalize_retry_after_seconds(int(candidate))
            except (ValueError, OverflowError):
                return None
        try:
            parsed = parsedate_to_datetime(candidate)
        except (TypeError, ValueError, OverflowError, IndexError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        reference = now or datetime.now(UTC)
        return normalize_retry_after_seconds((parsed - reference).total_seconds())


__all__ = ["OpenAICompatibleImageProvider"]
