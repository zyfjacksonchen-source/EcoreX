"""Tenant-scoped application service for image orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from .models import (
    ImageInputReceipt,
    ImageJob,
    ImageJobStatus,
    ImageMetrics,
    ImageSubmitRequest,
)
from .store import ImageJobStore


class ImageOrchestrationService:
    """Keeps HTTP/account concerns outside the durable scheduler.

    The caller supplies the authenticated account.  No public method accepts an
    account embedded in an untrusted request body.
    """

    def __init__(
        self,
        store: ImageJobStore,
        *,
        wake_workers: Callable[[], None] | None = None,
        allowed_models: frozenset[str] | None = None,
        max_output_count: int = 8,
        model_configuration_resolver: "ImageModelConfigurationResolver | None" = None,
    ) -> None:
        if not isinstance(store, ImageJobStore):
            raise TypeError("store does not implement ImageJobStore")
        self.store = store
        self._wake_workers = wake_workers or (lambda: None)
        if allowed_models is not None and (
            not isinstance(allowed_models, frozenset)
            or not allowed_models
            or any(not isinstance(model, str) or not model for model in allowed_models)
        ):
            raise ValueError("image model allowlist is invalid")
        self._allowed_models = allowed_models
        if (
            isinstance(max_output_count, bool)
            or not isinstance(max_output_count, int)
            or not 1 <= max_output_count <= 8
        ):
            raise ValueError("image output count policy is invalid")
        self._max_output_count = max_output_count
        self._model_configuration_resolver = model_configuration_resolver

    def submit(
        self,
        account_id: str,
        request: ImageSubmitRequest,
        *,
        authorized_models: frozenset[str] | None = None,
    ) -> tuple[ImageJob, bool]:
        if authorized_models is not None and (
            not isinstance(authorized_models, frozenset)
            or not authorized_models
            or any(not isinstance(model, str) or not model for model in authorized_models)
        ):
            raise ValueError("image account model entitlement is invalid")
        if authorized_models is not None and request.model_id not in authorized_models:
            raise ValueError("image model is not authorized for this account")
        if (
            self._allowed_models is not None
            and request.model_id not in self._allowed_models
        ):
            # Model availability is deployment policy, not a client hint.  Keep
            # this check in the application service so every transport shares
            # the same fail-closed contract.
            raise ValueError("image model is not available")
        if request.count > self._max_output_count:
            # The durable v1 result contract commits one CAS identity.  Do not
            # accept a multi-output request and silently discard all but one
            # provider result.  Callers can submit independently idempotent
            # jobs, which also gives each output its own retry/cancel fence.
            raise ValueError("image output count exceeds deployment policy")
        if self._model_configuration_resolver is not None:
            snapshot = self._model_configuration_resolver.resolve(
                model_id=request.model_id,
                operation=request.operation.value,
            )
            request = replace(
                request,
                model_config_id=snapshot.config_id,
                model_config_revision=snapshot.revision,
                provider_model_id=snapshot.provider_model_id,
            )
        # A digest is not authority. Every retouch input must first be bound to
        # this authenticated account through the private input CAS endpoint.
        for sha256 in request.input_sha256:
            self.store.get_input(account_id, sha256)
        job, created = self.store.submit(account_id, request)
        if created:
            self._wake_workers()
        return job, created

    def register_input(
        self,
        account_id: str,
        receipt: ImageInputReceipt,
    ) -> ImageInputReceipt:
        return self.store.register_input(account_id, receipt)

    def get(self, account_id: str, job_id: str) -> ImageJob:
        return self.store.get(job_id, account_id=account_id)

    def cancel(self, account_id: str, job_id: str) -> ImageJob:
        return self.store.cancel(job_id, account_id=account_id)

    def recover(
        self,
        account_id: str,
        job_id: str,
        *,
        recovery_request_id: str,
    ) -> ImageJob:
        # Reclaim is account-scoped, so a tenant cannot perturb another
        # tenant's leases through this operator endpoint.
        self.store.reclaim_expired(account_id=account_id)
        current = self.store.get(job_id, account_id=account_id)
        if current.status is ImageJobStatus.DEAD_LETTER:
            current = self.store.requeue_dead_letter(
                job_id,
                account_id=account_id,
                recovery_request_id=recovery_request_id,
            )
            self._wake_workers()
        return current

    def metrics(self, account_id: str) -> ImageMetrics:
        return self.store.metrics(account_id=account_id)


@dataclass(frozen=True, slots=True)
class ImageModelConfigurationSnapshot:
    config_id: str
    revision: int
    provider_model_id: str


class ImageModelConfigurationResolver(Protocol):
    def resolve(
        self, *, model_id: str, operation: str
    ) -> ImageModelConfigurationSnapshot: ...


__all__ = [
    "ImageModelConfigurationResolver",
    "ImageModelConfigurationSnapshot",
    "ImageOrchestrationService",
]
