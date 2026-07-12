"""Horizontal storage contract for image orchestration workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from .models import (
    ImageJob,
    ImageInputReceipt,
    ImageMetrics,
    ImageResult,
    ImageSubmitRequest,
    ImageUsage,
)


@dataclass(frozen=True, slots=True)
class ProviderCircuitDecision:
    """One durable provider-circuit admission decision.

    ``half_open`` is true only for the single probe which atomically replaced
    an expired cooldown with a bounded probe lease.  Other workers observe the
    probe lease as ``retry_at`` and therefore cannot create a recovery stampede.
    """

    admitted: bool
    half_open: bool = False
    retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.admitted and self.retry_at is not None:
            raise ValueError("an admitted provider call cannot carry retry_at")
        if not self.admitted and self.retry_at is None:
            raise ValueError("a rejected provider call requires retry_at")
        if self.half_open and not self.admitted:
            raise ValueError("only an admitted provider call can be half-open")


@runtime_checkable
class ImageJobStore(Protocol):
    deployment_scope: str

    def register_input(
        self,
        account_id: str,
        receipt: ImageInputReceipt,
    ) -> ImageInputReceipt: ...

    def get_input(self, account_id: str, sha256: str) -> ImageInputReceipt: ...

    def submit(self, account_id: str, request: ImageSubmitRequest) -> tuple[ImageJob, bool]: ...

    def get(self, job_id: str, *, account_id: str | None = None) -> ImageJob: ...

    def lease_next(self, worker_id: str, *, lease_seconds: int = 30) -> ImageJob | None: ...

    def heartbeat(self, job_id: str, lease_token: str, *, lease_seconds: int = 30) -> ImageJob: ...

    def transition(
        self,
        job_id: str,
        lease_token: str,
        *,
        expected: tuple[str, ...],
        target: str,
        checkpoint: Mapping[str, Any] | None = None,
        provider_request_id: str | None = None,
    ) -> ImageJob: ...

    def schedule_retry(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_code: str,
        available_at: datetime,
        checkpoint: Mapping[str, Any],
    ) -> ImageJob: ...

    def fail(self, job_id: str, lease_token: str, *, error_code: str) -> ImageJob: ...

    def complete(
        self,
        job_id: str,
        lease_token: str,
        *,
        result: ImageResult,
        usage: ImageUsage,
    ) -> ImageJob: ...

    def cancel(self, job_id: str, *, account_id: str) -> ImageJob: ...

    def reclaim_expired(self, *, account_id: str | None = None) -> int: ...

    def requeue_dead_letter(
        self,
        job_id: str,
        *,
        account_id: str,
        recovery_request_id: str,
    ) -> ImageJob: ...

    def metrics(self, *, account_id: str | None = None) -> ImageMetrics: ...

    def events(self, job_id: str) -> tuple[Mapping[str, Any], ...]: ...

    def breaker_open_until(self, scope: str) -> datetime | None: ...

    def admit_provider_call(
        self,
        scope: str,
        *,
        probe_seconds: int,
    ) -> ProviderCircuitDecision: ...

    def record_provider_failure(
        self,
        scope: str,
        *,
        threshold: int,
        cooldown_seconds: int,
    ) -> datetime | None: ...

    def record_provider_rate_limit(
        self,
        scope: str,
        *,
        retry_at: datetime,
        cooldown_seconds: int,
    ) -> datetime: ...

    def record_provider_success(self, scope: str) -> None: ...


__all__ = ["ImageJobStore", "ProviderCircuitDecision"]
