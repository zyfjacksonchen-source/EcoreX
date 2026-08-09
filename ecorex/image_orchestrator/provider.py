"""Provider protocol and normalized failure taxonomy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
import re
from typing import Protocol, runtime_checkable

from .models import ImageJob, ImageUsage


class ProviderState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class ProviderError(RuntimeError):
    code = "provider_error"
    retryable = False


class ProviderUncertain(ProviderError):
    code = "provider_uncertain"
    retryable = True


class ProviderRateLimited(ProviderError):
    code = "provider_rate_limited"
    retryable = True

    def __init__(
        self,
        message: str = "provider is rate limited",
        *,
        retry_after_seconds: float | int | None = None,
        recovery_required: bool = False,
    ) -> None:
        super().__init__(message)
        if not isinstance(recovery_required, bool):
            raise TypeError("provider rate-limit recovery policy is invalid")
        self.retry_after_seconds = normalize_retry_after_seconds(
            retry_after_seconds
        )
        self.recovery_required = recovery_required


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"
    retryable = True


class ProviderOutOfMemory(ProviderError):
    code = "provider_out_of_memory"
    retryable = True


class ProviderRejected(ProviderError):
    code = "provider_rejected"
    retryable = False


class ProviderModelUnavailable(ProviderRejected):
    """A definite pre-execution rejection that permits a model fallback."""

    code = "provider_model_unavailable"


def normalize_retry_after_seconds(value: object) -> float | None:
    """Return a safe provider backoff hint or ``None`` for malformed input.

    A provider cannot force an unbounded queue stall or a hot retry.  The
    worker's exponential backoff remains the fallback when this returns None.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if not math.isfinite(seconds):
        return None
    return min(3600.0, max(1.0, seconds))


@dataclass(frozen=True, slots=True)
class ProviderResult:
    state: ProviderState
    provider_request_id: str | None = None
    payload: bytes | None = None
    mime_type: str | None = None
    sha256: str | None = None
    usage: ImageUsage | None = None
    error_code: str | None = None
    actual_model_id: str | None = None
    fallback_from_model_id: str | None = None
    fallback_used: bool | None = None

    def __post_init__(self) -> None:
        if self.provider_request_id is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,511}", self.provider_request_id
        ):
            raise ValueError("provider request identity is invalid")
        for value in (self.actual_model_id, self.fallback_from_model_id):
            if value is not None and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value
            ):
                raise ValueError("provider model provenance is invalid")
        if (
            (self.fallback_used is not None and not isinstance(self.fallback_used, bool))
            or (self.fallback_from_model_id is not None and self.fallback_used is not True)
            or (self.fallback_used is not None and self.actual_model_id is None)
            or (
                self.fallback_used is True
                and (
                    self.fallback_from_model_id is None
                    or self.fallback_from_model_id == self.actual_model_id
                )
            )
            or (self.fallback_used is False and self.fallback_from_model_id is not None)
        ):
            raise ValueError("provider fallback provenance is invalid")
        if self.state is ProviderState.COMPLETED:
            if not isinstance(self.payload, bytes) or not self.payload:
                raise ValueError("completed provider result requires bytes")
            if self.mime_type is None or self.usage is None:
                raise ValueError("completed provider result requires MIME and usage")
        elif (
            self.payload is not None
            or self.mime_type is not None
            or self.usage is not None
            or self.actual_model_id is not None
            or self.fallback_from_model_id is not None
            or self.fallback_used is not None
        ):
            raise ValueError("non-completed provider result cannot carry image facts")


@runtime_checkable
class ImageProvider(Protocol):
    provider_id: str

    async def submit(
        self,
        job: ImageJob,
        *,
        idempotency_key: str,
    ) -> ProviderResult: ...

    async def recover(
        self,
        job: ImageJob,
        *,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderResult: ...

    async def cancel(
        self,
        job: ImageJob,
        *,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> None: ...


__all__ = [
    "ImageProvider",
    "ProviderError",
    "ProviderOutOfMemory",
    "ProviderRateLimited",
    "ProviderModelUnavailable",
    "ProviderRejected",
    "ProviderResult",
    "ProviderState",
    "ProviderUncertain",
    "ProviderUnavailable",
    "normalize_retry_after_seconds",
]
