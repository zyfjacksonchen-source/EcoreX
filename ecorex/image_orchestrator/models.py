"""Contracts for the cloud-authoritative image job orchestration domain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping


class ImageOperation(StrEnum):
    GENERATE = "generate"
    RETOUCH = "retouch"


class ImageJobStatus(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMMITTING = "committing"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


ACTIVE_STATUSES = frozenset(
    {
        ImageJobStatus.LEASED,
        ImageJobStatus.RUNNING,
        ImageJobStatus.VERIFYING,
        ImageJobStatus.COMMITTING,
    }
)
SCHEDULABLE_STATUSES = frozenset(
    {ImageJobStatus.QUEUED, ImageJobStatus.RETRY_WAIT}
)
TERMINAL_STATUSES = frozenset(
    {
        ImageJobStatus.COMPLETED,
        ImageJobStatus.CANCELLED,
        ImageJobStatus.FAILED,
        ImageJobStatus.DEAD_LETTER,
    }
)


class ImageOrchestratorError(RuntimeError):
    code = "image_orchestrator_error"


class ImageIdempotencyConflict(ImageOrchestratorError):
    code = "image_idempotency_conflict"


class ImageBackpressure(ImageOrchestratorError):
    code = "image_backpressure"


class ImageJobNotFound(ImageOrchestratorError):
    code = "image_job_not_found"


class ImageInputNotFound(ImageOrchestratorError):
    code = "image_input_not_found"


class ImageLeaseLost(ImageOrchestratorError):
    code = "image_lease_lost"


class ImageInvalidTransition(ImageOrchestratorError):
    code = "image_invalid_transition"


class ImageResultRejected(ImageOrchestratorError):
    code = "image_result_rejected"


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{7,255}$")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MIME = re.compile(r"^image/(?:png|jpeg|webp|avif)$")


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        raise ValueError("value cannot be canonically encoded") from None


@dataclass(frozen=True, slots=True)
class ImageSubmitRequest:
    operation: ImageOperation
    model_id: str
    client_request_id: str
    prompt: str
    width: int = 1024
    height: int = 1024
    count: int = 1
    input_sha256: tuple[str, ...] = ()
    instruction: str | None = None
    priority: int = 0
    max_attempts: int = 4
    deadline_seconds: int = 900
    metadata: Mapping[str, str | int | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            operation = (
                self.operation
                if isinstance(self.operation, ImageOperation)
                else ImageOperation(self.operation)
            )
        except (TypeError, ValueError):
            raise ValueError("image operation is unsupported") from None
        object.__setattr__(self, "operation", operation)
        if not isinstance(self.model_id, str) or not _MODEL.fullmatch(self.model_id):
            raise ValueError("model_id is invalid")
        if not isinstance(self.client_request_id, str) or not _ID.fullmatch(
            self.client_request_id
        ):
            raise ValueError("client_request_id is invalid")
        if (
            not isinstance(self.prompt, str)
            or not self.prompt.strip()
            or len(self.prompt.encode("utf-8")) > 128 * 1024
            or "\x00" in self.prompt
        ):
            raise ValueError("prompt is invalid")
        object.__setattr__(self, "prompt", self.prompt.strip())
        for label, value in (("width", self.width), ("height", self.height)):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 64
                or value > 8192
                or value % 8
            ):
                raise ValueError(f"{label} must be an 8-aligned value from 64 to 8192")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or not 1 <= self.count <= 8:
            raise ValueError("image count must be between one and eight")
        if not -100 <= self.priority <= 100:
            raise ValueError("priority must be between -100 and 100")
        if not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be between one and ten")
        if not 30 <= self.deadline_seconds <= 86_400:
            raise ValueError("deadline_seconds must be between 30 and 86400")
        inputs = tuple(self.input_sha256)
        if len(inputs) > 32 or any(not _SHA.fullmatch(value) for value in inputs):
            raise ValueError("input_sha256 contains an invalid content identity")
        if len(inputs) != len(set(inputs)):
            raise ValueError("input_sha256 must be unique")
        if operation is ImageOperation.RETOUCH and not inputs:
            raise ValueError("retouch requires at least one content-addressed input")
        object.__setattr__(self, "input_sha256", inputs)
        if self.instruction is not None and (
            not isinstance(self.instruction, str)
            or not self.instruction.strip()
            or len(self.instruction.encode("utf-8")) > 64 * 1024
            or "\x00" in self.instruction
        ):
            raise ValueError("retouch instruction is invalid")
        if operation is ImageOperation.RETOUCH and not self.instruction:
            raise ValueError("retouch requires an instruction")
        if self.instruction is not None:
            object.__setattr__(self, "instruction", self.instruction.strip())
        metadata = dict(self.metadata)
        if len(metadata) > 32:
            raise ValueError("metadata is too large")
        safe_metadata: dict[str, str | int | bool] = {}
        for key, value in metadata.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", key):
                raise ValueError("metadata key is invalid")
            if re.search(r"token|secret|password|path|key", key, re.IGNORECASE):
                raise ValueError("metadata cannot contain credential or path fields")
            if not isinstance(value, (str, int, bool)) or (
                isinstance(value, str) and len(value.encode("utf-8")) > 1024
            ):
                raise ValueError("metadata value is invalid")
            safe_metadata[key] = value
        object.__setattr__(self, "metadata", MappingProxyType(safe_metadata))

    @property
    def size_class(self) -> str:
        pixels = self.width * self.height
        if pixels <= 1024 * 1024:
            return "small"
        if pixels <= 4 * 1024 * 1024:
            return "medium"
        return "large"

    def scheduling_weight(self, model_weights: Mapping[str, float] | None = None) -> int:
        megapixels = max(1, math.ceil((self.width * self.height) / 1_048_576))
        operation_weight = 2 if self.operation is ImageOperation.RETOUCH else 1
        model_weight = float((model_weights or {}).get(self.model_id, 1.0))
        if not 0.25 <= model_weight <= 16:
            raise ValueError("configured model weight is invalid")
        return max(1, math.ceil(megapixels * self.count * operation_weight * model_weight))

    def provider_payload(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "model_id": self.model_id,
            "prompt": self.prompt,
            "width": self.width,
            "height": self.height,
            "count": self.count,
            "input_sha256": list(self.input_sha256),
            "instruction": self.instruction,
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        material = {
            **self.provider_payload(),
            "priority": self.priority,
            "max_attempts": self.max_attempts,
            "deadline_seconds": self.deadline_seconds,
        }
        return hashlib.sha256(
            b"ecorex-image-submit-v1\0" + canonical_json(material).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ImageResult:
    sha256: str
    size_bytes: int
    mime_type: str

    def __post_init__(self) -> None:
        if not _SHA.fullmatch(self.sha256):
            raise ValueError("result SHA-256 is invalid")
        if not 1 <= self.size_bytes <= 256 * 1024 * 1024:
            raise ValueError("result size is invalid")
        if not _MIME.fullmatch(self.mime_type):
            raise ValueError("result MIME type is unsupported")


@dataclass(frozen=True, slots=True)
class ImageInputReceipt:
    sha256: str
    size_bytes: int
    mime_type: str

    def __post_init__(self) -> None:
        # Input and output image bytes share the same validated CAS contract.
        ImageResult(self.sha256, self.size_bytes, self.mime_type)


@dataclass(frozen=True, slots=True)
class ImageUsage:
    provider: str
    model_id: str
    input_units: int = 0
    output_units: int = 1
    billed_units: int = 0

    def __post_init__(self) -> None:
        if not _MODEL.fullmatch(self.provider) or not _MODEL.fullmatch(self.model_id):
            raise ValueError("usage identity is invalid")
        for value in (self.input_units, self.output_units, self.billed_units):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**15:
                raise ValueError("usage value is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "input_units": self.input_units,
            "output_units": self.output_units,
            "billed_units": self.billed_units,
        }


@dataclass(frozen=True, slots=True)
class ImageJob:
    job_id: str
    account_id: str
    request: ImageSubmitRequest
    status: ImageJobStatus
    weight: int
    attempt: int
    fair_finish: float
    available_at: datetime
    deadline: datetime
    created_at: datetime
    updated_at: datetime
    provider_idempotency_key: str
    lease_owner: str | None = None
    lease_token: str | None = field(default=None, repr=False)
    lease_generation: int = 0
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    provider_request_id: str | None = None
    checkpoint: Mapping[str, Any] = field(default_factory=dict, repr=False)
    cancellation_requested: bool = False
    last_error_code: str | None = None
    result: ImageResult | None = None
    usage: ImageUsage | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class ImageJobProjection:
    job_id: str
    operation: str
    model_id: str
    status: str
    attempt: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    deadline: datetime
    result: ImageResult | None
    last_error_code: str | None

    @classmethod
    def from_job(cls, job: ImageJob) -> "ImageJobProjection":
        return cls(
            job_id=job.job_id,
            operation=job.request.operation.value,
            model_id=job.request.model_id,
            status=job.status.value,
            attempt=job.attempt,
            max_attempts=job.request.max_attempts,
            created_at=job.created_at,
            updated_at=job.updated_at,
            deadline=job.deadline,
            result=job.result,
            last_error_code=job.last_error_code,
        )


@dataclass(frozen=True, slots=True)
class ImageLimits:
    max_queued_jobs: int = 10_000
    max_queued_weight: int = 100_000
    max_account_queued_jobs: int = 1_000
    max_account_queued_weight: int = 20_000
    max_running_jobs: int = 128
    max_account_running: int = 8
    max_model_running: int = 64
    max_operation_running: int = 96

    def __post_init__(self) -> None:
        for value in (
            self.max_queued_jobs,
            self.max_queued_weight,
            self.max_account_queued_jobs,
            self.max_account_queued_weight,
            self.max_running_jobs,
            self.max_account_running,
            self.max_model_running,
            self.max_operation_running,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("image concurrency limits must be positive integers")


@dataclass(frozen=True, slots=True)
class ImageMetrics:
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


def default_deadline(request: ImageSubmitRequest, now: datetime) -> datetime:
    return require_utc(now, "now") + timedelta(seconds=request.deadline_seconds)


__all__ = [
    "ACTIVE_STATUSES",
    "ImageBackpressure",
    "ImageIdempotencyConflict",
    "ImageInputReceipt",
    "ImageInputNotFound",
    "ImageInvalidTransition",
    "ImageJob",
    "ImageJobNotFound",
    "ImageJobProjection",
    "ImageJobStatus",
    "ImageLeaseLost",
    "ImageLimits",
    "ImageMetrics",
    "ImageOperation",
    "ImageOrchestratorError",
    "ImageResult",
    "ImageResultRejected",
    "ImageSubmitRequest",
    "ImageUsage",
    "SCHEDULABLE_STATUSES",
    "TERMINAL_STATUSES",
    "canonical_json",
    "default_deadline",
    "require_utc",
    "utc_now",
]
