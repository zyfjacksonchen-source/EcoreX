"""Strict, tenant-isolated FastAPI surface for cloud image jobs."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime
import asyncio
import base64
import hashlib
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cas import (
    ImageContentAddressedStore,
    ImageContentReference,
)
from .models import (
    ImageBackpressure,
    ImageIdempotencyConflict,
    ImageInputNotFound,
    ImageInputReceipt,
    ImageInvalidTransition,
    ImageJob,
    ImageJobNotFound,
    ImageJobStatus,
    ImageOperation,
    ImageOrchestratorError,
    ImageResultRejected,
    ImageSubmitRequest,
)
from .service import ImageOrchestrationService


_ACCOUNT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{2,255}$")
_JOB = r"^imgjob_[0-9a-f]{32}$"


class StrictImageApiRoute(APIRoute):
    """Do not reflect a rejected prompt or metadata value in a 422 response."""

    def get_route_handler(self) -> Callable[..., Coroutine[Any, Any, Any]]:
        original = super().get_route_handler()

        async def handler(request: Any) -> Any:
            try:
                return await original(request)
            except RequestValidationError as error:
                fields = []
                for issue in error.errors():
                    location = [str(part) for part in issue.get("loc", ())]
                    if location:
                        fields.append(".".join(location))
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": "image request validation failed",
                        "fields": sorted(set(fields))[:32],
                    },
                )

        return handler


class ImageSubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    operation: Literal["generate", "retouch"]
    model_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
    client_request_id: str = Field(
        min_length=8,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,255}$",
    )
    prompt: str = Field(min_length=1, max_length=131_072)
    width: int = Field(default=1024, ge=64, le=8192)
    height: int = Field(default=1024, ge=64, le=8192)
    count: int = Field(default=1, ge=1, le=8)
    input_sha256: list[str] = Field(default_factory=list, max_length=32)
    instruction: str | None = Field(default=None, max_length=65_536)
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int = Field(default=4, ge=1, le=10)
    deadline_seconds: int = Field(default=900, ge=30, le=86_400)
    metadata: dict[str, str | int | bool] = Field(default_factory=dict)

    @field_validator("width", "height")
    @classmethod
    def aligned_dimensions(cls, value: int) -> int:
        if value % 8:
            raise ValueError("image dimensions must be 8-aligned")
        return value

    @model_validator(mode="after")
    def validate_operation(self) -> "ImageSubmitBody":
        if self.operation == "retouch" and (not self.input_sha256 or not self.instruction):
            raise ValueError("retouch requires input_sha256 and instruction")
        return self

    def to_domain(self) -> ImageSubmitRequest:
        return ImageSubmitRequest(
            operation=ImageOperation(self.operation),
            model_id=self.model_id,
            client_request_id=self.client_request_id,
            prompt=self.prompt,
            width=self.width,
            height=self.height,
            count=self.count,
            input_sha256=tuple(self.input_sha256),
            instruction=self.instruction,
            priority=self.priority,
            max_attempts=self.max_attempts,
            deadline_seconds=self.deadline_seconds,
            metadata=self.metadata,
        )


class ImageRecoverBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    recovery_request_id: str = Field(
        min_length=8,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,255}$",
    )


class ImageResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str
    size_bytes: int
    mime_type: str


class ImageJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    operation: str
    model_id: str
    status: str
    attempt: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    deadline: datetime
    result: ImageResultResponse | None
    last_error_code: str | None


class ImageSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    created: bool
    job: ImageJobResponse


class ImageMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    queued: int
    active: int
    retry_wait: int
    completed: int
    failed: int
    cancelled: int
    dead_letter: int
    queued_weight: int
    oldest_queued_seconds: float
    usage_billed_units: int


class ImageInputResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str
    size_bytes: int
    mime_type: str


def _account_id(principal: Any) -> str:
    account_id = getattr(principal, "account_id", None)
    if not isinstance(account_id, str) or not _ACCOUNT.fullmatch(account_id):
        raise HTTPException(status_code=401, detail="image principal is invalid")
    return account_id


def _authorized_models(principal: Any, *, required: bool) -> frozenset[str] | None:
    if not required:
        return None
    allowed = getattr(principal, "allowed_model_ids", None)
    if (
        not isinstance(allowed, frozenset)
        or not allowed
        or len(allowed) > 128
        or any(
            not isinstance(model_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", model_id) is None
            for model_id in allowed
        )
    ):
        raise HTTPException(status_code=401, detail="image principal is invalid")
    return allowed


def _input_reference(account_id: str, sha256: str) -> ImageContentReference:
    # Keep tenant identities out of object-store metadata and stay inside the
    # reference-ID bound even when the authenticated account uses its maximum
    # legal length.  The domain-separated digest remains deterministic for
    # idempotent re-uploads by the same tenant.
    identity = hashlib.sha256(
        b"ecorex-image-input-reference-v1\0"
        + account_id.encode("utf-8")
        + b"\0"
        + sha256.encode("ascii")
    ).hexdigest()
    return ImageContentReference("account-input", identity)


def _job_response(job: ImageJob) -> ImageJobResponse:
    result = None
    if job.result is not None:
        result = ImageResultResponse(
            sha256=job.result.sha256,
            size_bytes=job.result.size_bytes,
            mime_type=job.result.mime_type,
        )
    return ImageJobResponse(
        job_id=job.job_id,
        operation=job.request.operation.value,
        model_id=job.request.model_id,
        status=job.status.value,
        attempt=job.attempt,
        max_attempts=job.request.max_attempts,
        created_at=job.created_at,
        updated_at=job.updated_at,
        deadline=job.deadline,
        result=result,
        last_error_code=job.last_error_code,
    )


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, ImageJobNotFound):
        return HTTPException(status_code=404, detail="image job was not found")
    if isinstance(error, ImageInputNotFound):
        return HTTPException(status_code=409, detail="image input is not registered")
    if isinstance(error, ImageBackpressure):
        return HTTPException(
            status_code=429,
            detail="image queue capacity is exhausted",
            headers={"Retry-After": "5"},
        )
    if isinstance(error, (ImageIdempotencyConflict, ImageInvalidTransition)):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, (ValueError, TypeError)):
        return HTTPException(status_code=422, detail="image request is invalid")
    if isinstance(error, ImageOrchestratorError):
        return HTTPException(status_code=503, detail="image orchestration is unavailable")
    raise error


def create_image_orchestration_router(
    service: ImageOrchestrationService,
    *,
    principal_dependency: Callable[..., Any],
    content_store: ImageContentAddressedStore | None = None,
    blob_memory_envelope_bytes: int = 512 * 1024 * 1024,
    require_model_entitlements: bool = False,
) -> APIRouter:
    """Create a router whose tenant identity is exclusively dependency-derived."""

    if content_store is not None:
        if not isinstance(content_store, ImageContentAddressedStore):
            raise TypeError("content_store does not implement image CAS")
        if (
            service.store.deployment_scope not in {"local", "shared"}
            or content_store.deployment_scope not in {"local", "shared"}
            or service.store.deployment_scope != content_store.deployment_scope
        ):
            raise ValueError(
                "shared image jobs require shared content-addressed storage"
            )
    if (
        isinstance(blob_memory_envelope_bytes, bool)
        or not isinstance(blob_memory_envelope_bytes, int)
        or not 32 * 1024 * 1024
        <= blob_memory_envelope_bytes
        <= 16 * 1024 * 1024 * 1024
    ):
        raise ValueError("image API memory envelope is invalid")
    if (
        content_store is not None
        and blob_memory_envelope_bytes < content_store.max_bytes * 2
    ):
        raise ValueError("image API memory envelope cannot hold one bounded blob")
    if not isinstance(require_model_entitlements, bool):
        raise TypeError("image model entitlement policy is invalid")

    router = APIRouter(
        prefix="/api/v1/images",
        tags=["image-orchestration"],
        route_class=StrictImageApiRoute,
    )
    # The upload path is the only async route that must consume an ASGI body.
    # Bound simultaneous in-memory bodies, and offload blocking CAS/database
    # operations below so an object-store stall cannot freeze every HTTP
    # request on the event loop.  Larger configured blobs automatically reduce
    # concurrency under the fixed process memory envelope.
    blob_slots_count = (
        1
        if content_store is None
        else max(
            1,
            min(
                32,
                blob_memory_envelope_bytes
                // max(1, content_store.max_bytes * 2),
            ),
        )
    )
    # Uploads and downloads share one process memory budget.  Separate
    # semaphores let each direction independently exhaust the nominal budget
    # during a burst and can OOM a healthy API process.
    blob_slots = asyncio.BoundedSemaphore(blob_slots_count)

    @router.put("/inputs/{sha256}", response_model=ImageInputResponse)
    async def register_image_input(
        request: Request,
        sha256: str = Path(pattern=r"^[0-9a-f]{64}$"),
        principal: Any = Depends(principal_dependency),
    ) -> ImageInputResponse:
        if content_store is None:
            raise HTTPException(status_code=503, detail="image input storage is unavailable")
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if media_type not in {"image/png", "image/jpeg", "image/webp", "image/avif"}:
            raise HTTPException(status_code=415, detail="image input media type is unsupported")
        if request.headers.get("content-encoding", "identity").casefold() != "identity":
            raise HTTPException(status_code=415, detail="encoded image inputs are unsupported")
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid Content-Length") from None
            if not 1 <= declared_size <= content_store.max_bytes:
                raise HTTPException(status_code=413, detail="image input is oversized")
        # Never let a missing or dishonest Content-Length turn this endpoint
        # into an unbounded ASGI body allocation.  Starlette's ``body()``
        # joins every frame before the application can enforce a limit, so we
        # consume frames ourselves and stop at the first byte over the CAS
        # contract.  The immutable bytes copy is made only after the bound has
        # been proven.
        try:
            async with blob_slots:
                bounded_payload = bytearray()
                async for chunk in request.stream():
                    if not isinstance(chunk, bytes):
                        raise HTTPException(
                            status_code=400,
                            detail="invalid image input body",
                        )
                    if len(bounded_payload) + len(chunk) > content_store.max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail="image input is oversized",
                        )
                    bounded_payload.extend(chunk)
                if not bounded_payload:
                    raise HTTPException(
                        status_code=413,
                        detail="image input is empty or oversized",
                    )
                if declared is not None and len(bounded_payload) != declared_size:
                    raise HTTPException(
                        status_code=400,
                        detail="Content-Length does not match body",
                    )
                payload = bytes(bounded_payload)
                del bounded_payload
                try:
                    account_id = _account_id(principal)
                    stored = await asyncio.to_thread(
                        content_store.put,
                        payload,
                        mime_type=media_type,
                        expected_sha256=sha256,
                        reference=_input_reference(account_id, sha256),
                    )
                    receipt = await asyncio.to_thread(
                        service.register_input,
                        account_id,
                        ImageInputReceipt(
                            stored.sha256,
                            stored.size_bytes,
                            stored.mime_type,
                        ),
                    )
                finally:
                    # Release the duplicate body copy before response modelling
                    # or error translation retains a traceback across an await.
                    del payload
        except ImageResultRejected as error:
            raise HTTPException(
                status_code=422,
                detail="image input failed integrity validation",
            ) from error
        except Exception as error:
            raise _http_error(error) from error
        return ImageInputResponse(
            sha256=receipt.sha256,
            size_bytes=receipt.size_bytes,
            mime_type=receipt.mime_type,
        )

    @router.post("/jobs", response_model=ImageSubmitResponse, status_code=202)
    def submit_image_job(
        body: ImageSubmitBody,
        response: Response,
        principal: Any = Depends(principal_dependency),
    ) -> ImageSubmitResponse:
        try:
            request = body.to_domain()
            authorized_models = _authorized_models(
                principal,
                required=require_model_entitlements,
            )
            if (
                authorized_models is not None
                and request.model_id not in authorized_models
            ):
                raise HTTPException(
                    status_code=403,
                    detail="image model is not authorized",
                )
            job, created = service.submit(
                _account_id(principal),
                request,
                authorized_models=authorized_models,
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _http_error(error) from error
        response.status_code = 202 if created else 200
        return ImageSubmitResponse(created=created, job=_job_response(job))

    @router.get("/jobs/{job_id}", response_model=ImageJobResponse)
    def get_image_job(
        job_id: str = Path(pattern=_JOB),
        principal: Any = Depends(principal_dependency),
    ) -> ImageJobResponse:
        try:
            return _job_response(service.get(_account_id(principal), job_id))
        except Exception as error:
            raise _http_error(error) from error

    @router.get("/jobs/{job_id}/result")
    async def download_image_result(
        job_id: str = Path(pattern=_JOB),
        if_none_match: str | None = Header(default=None, alias="If-None-Match"),
        principal: Any = Depends(principal_dependency),
    ) -> Response:
        if content_store is None:
            raise HTTPException(status_code=503, detail="image result storage is unavailable")
        try:
            job = service.get(_account_id(principal), job_id)
            if job.status is not ImageJobStatus.COMPLETED or job.result is None:
                raise ImageInvalidTransition("image result is not ready")
            expected = job.result
            etag = f'"{expected.sha256}"'
            if if_none_match is not None and if_none_match.strip() == etag:
                return Response(
                    status_code=304,
                    headers={"ETag": etag, "Cache-Control": "private, no-cache"},
                )
            await blob_slots.acquire()
            try:
                payload = await asyncio.to_thread(
                    content_store.read, expected.sha256
                )
            except BaseException:
                blob_slots.release()
                raise
            if len(payload) != expected.size_bytes:
                blob_slots.release()
                raise ImageResultRejected("image result size commitment changed")
        except Exception as error:
            raise _http_error(error) from error
        extension = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
            "image/avif": "avif",
        }[expected.mime_type]
        digest_header = base64.b64encode(bytes.fromhex(expected.sha256)).decode("ascii")
        async def stream_payload():
            try:
                # One bounded immutable chunk avoids a second join/copy inside
                # Starlette while retaining the memory permit until ASGI has
                # consumed or cancelled the response.
                yield payload
            finally:
                blob_slots.release()

        return StreamingResponse(
            stream_payload(),
            media_type=expected.mime_type,
            headers={
                "ETag": etag,
                "Digest": f"sha-256={digest_header}",
                "X-Content-SHA256": expected.sha256,
                "Content-Disposition": (
                    f'attachment; filename="image-{expected.sha256[:12]}.{extension}"'
                ),
                "Cache-Control": "private, no-cache",
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(expected.size_bytes),
            },
        )

    @router.post("/jobs/{job_id}/cancel", response_model=ImageJobResponse)
    def cancel_image_job(
        job_id: str = Path(pattern=_JOB),
        principal: Any = Depends(principal_dependency),
    ) -> ImageJobResponse:
        try:
            return _job_response(service.cancel(_account_id(principal), job_id))
        except Exception as error:
            raise _http_error(error) from error

    @router.post("/jobs/{job_id}/recover", response_model=ImageJobResponse)
    def recover_image_job(
        body: ImageRecoverBody,
        job_id: str = Path(pattern=_JOB),
        principal: Any = Depends(principal_dependency),
    ) -> ImageJobResponse:
        try:
            return _job_response(
                service.recover(
                    _account_id(principal),
                    job_id,
                    recovery_request_id=body.recovery_request_id,
                )
            )
        except Exception as error:
            raise _http_error(error) from error

    @router.get("/metrics", response_model=ImageMetricsResponse)
    def image_metrics(
        principal: Any = Depends(principal_dependency),
    ) -> ImageMetricsResponse:
        try:
            metrics = service.metrics(_account_id(principal))
            return ImageMetricsResponse(
                queued=metrics.queued,
                active=metrics.active,
                retry_wait=metrics.retry_wait,
                completed=metrics.completed,
                failed=metrics.failed,
                cancelled=metrics.cancelled,
                dead_letter=metrics.dead_letter,
                queued_weight=metrics.queued_weight,
                oldest_queued_seconds=metrics.oldest_queued_seconds,
                usage_billed_units=metrics.usage_billed_units,
            )
        except Exception as error:
            raise _http_error(error) from error

    return router


__all__ = [
    "ImageJobResponse",
    "ImageMetricsResponse",
    "ImageInputResponse",
    "ImageRecoverBody",
    "ImageSubmitBody",
    "ImageSubmitResponse",
    "StrictImageApiRoute",
    "create_image_orchestration_router",
]
