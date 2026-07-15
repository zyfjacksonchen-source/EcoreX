"""Durable hot-swappable image provider using frozen tested revisions."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Mapping, Protocol

from ecorex.control_plane.management_models import ActiveModelConfiguration

from .managed_provider import ManagedHTTPSImageProvider
from .models import ImageJob, ImageOperation, ImageUsage
from .provider import ProviderResult, ProviderUnavailable
from .service import ImageModelConfigurationSnapshot


class ImageModelSource(Protocol):
    def active_model(
        self, *, local_model_id: str | None = None, modality: str | None = None
    ) -> ActiveModelConfiguration: ...

    def model_revision(
        self, config_id: str, revision: int
    ) -> ActiveModelConfiguration: ...


class AdminImageModelConfigurationResolver:
    def __init__(self, source: ImageModelSource) -> None:
        self.source = source

    def resolve(
        self, *, model_id: str, operation: str
    ) -> ImageModelConfigurationSnapshot:
        if operation == ImageOperation.RETOUCH.value:
            configuration = self.source.active_model(modality="image_edit")
            expected = "image_edit"
        elif operation == ImageOperation.GENERATE.value:
            configuration = self.source.active_model(local_model_id=model_id)
            expected = "image_generation"
        else:
            raise ValueError("image operation is unsupported")
        if configuration.modality != expected:
            raise ValueError("active image model modality is invalid")
        return ImageModelConfigurationSnapshot(
            config_id=configuration.config_id,
            revision=configuration.revision,
            provider_model_id=configuration.upstream_model_id,
        )


@dataclass(slots=True)
class _Entry:
    provider: ManagedHTTPSImageProvider
    active_count: int = 0


class DynamicManagedImageProvider:
    """Keep a job on its persisted revision across retries and process restarts."""

    def __init__(
        self,
        source: ImageModelSource,
        *,
        provider_id: str,
        origins: Mapping[str, str],
        timeout_seconds: float,
        connect_timeout_seconds: float,
        max_image_bytes: int,
        max_connections: int,
        max_concurrency: int,
        max_cached_revisions: int = 32,
        provider_factory: Callable[
            [ActiveModelConfiguration, str], ManagedHTTPSImageProvider
        ] | None = None,
    ) -> None:
        self.source = source
        self.provider_id = provider_id
        self.origins = dict(origins)
        self.timeout_seconds = timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.max_image_bytes = max_image_bytes
        self.max_connections = max_connections
        self.max_concurrency = max_concurrency
        if not 2 <= max_cached_revisions <= 256:
            raise ValueError("dynamic image provider cache limit is invalid")
        self.max_cached_revisions = max_cached_revisions
        self._provider_factory = provider_factory
        self._entries: OrderedDict[tuple[str, int], _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._closed = False

    async def submit(self, job: ImageJob, *, idempotency_key: str) -> ProviderResult:
        return await self._call("submit", job, idempotency_key=idempotency_key)

    async def recover(
        self,
        job: ImageJob,
        *,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> ProviderResult:
        return await self._call(
            "recover",
            job,
            idempotency_key=idempotency_key,
            provider_request_id=provider_request_id,
        )

    async def cancel(
        self,
        job: ImageJob,
        *,
        idempotency_key: str,
        provider_request_id: str | None,
    ) -> None:
        key, entry, upstream_job = await self._acquire(job)
        try:
            await entry.provider.cancel(
                upstream_job,
                idempotency_key=idempotency_key,
                provider_request_id=provider_request_id,
            )
        finally:
            await self._release(key, entry)

    async def health(self) -> None:
        configurations = [
            await asyncio.to_thread(self.source.active_model, modality=modality)
            for modality in ("image_generation", "image_edit")
        ]
        for configuration in configurations:
            key, entry = await self._acquire_configuration(configuration)
            try:
                await entry.provider.health()
            finally:
                await self._release(key, entry)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            entries = list(self._entries.values())
            self._entries.clear()
        await asyncio.gather(
            *(entry.provider.aclose() for entry in entries), return_exceptions=True
        )

    async def _call(self, method: str, job: ImageJob, **kwargs) -> ProviderResult:
        key, entry, upstream_job = await self._acquire(job)
        try:
            result = await getattr(entry.provider, method)(upstream_job, **kwargs)
            if result.usage is None:
                return result
            usage = result.usage
            return ProviderResult(
                state=result.state,
                provider_request_id=result.provider_request_id,
                payload=result.payload,
                mime_type=result.mime_type,
                sha256=result.sha256,
                usage=ImageUsage(
                    provider=self.provider_id,
                    model_id=job.request.model_id,
                    input_units=usage.input_units,
                    output_units=usage.output_units,
                    billed_units=usage.billed_units,
                ),
                error_code=result.error_code,
            )
        finally:
            await self._release(key, entry)

    async def _acquire(self, job: ImageJob):
        request = job.request
        if (
            request.model_config_id is None
            or request.model_config_revision is None
            or request.provider_model_id is None
        ):
            raise ProviderUnavailable("image model snapshot is unavailable")
        configuration = await asyncio.to_thread(
            self.source.model_revision,
            request.model_config_id,
            request.model_config_revision,
        )
        expected = (
            "image_edit"
            if request.operation is ImageOperation.RETOUCH
            else "image_generation"
        )
        if (
            configuration.modality != expected
            or configuration.upstream_model_id != request.provider_model_id
        ):
            raise ProviderUnavailable("image model snapshot is inconsistent")
        key, entry = await self._acquire_configuration(configuration)
        upstream_job = replace(
            job,
            request=replace(job.request, model_id=configuration.upstream_model_id),
        )
        return key, entry, upstream_job

    async def _acquire_configuration(
        self, configuration: ActiveModelConfiguration
    ) -> tuple[tuple[str, int], _Entry]:
        key = (configuration.config_id, configuration.revision)
        async with self._lock:
            if self._closed:
                raise ProviderUnavailable("managed image provider is closed")
            entry = self._entries.get(key)
            if entry is None:
                origin = self.origins.get(configuration.provider_preset)
                if origin is None:
                    raise ProviderUnavailable("managed image origin is unavailable")
                entry = _Entry(
                    self._create_provider(configuration, origin)
                )
                self._entries[key] = entry
            self._entries.move_to_end(key)
            entry.active_count += 1
            return key, entry

    async def _release(self, key: tuple[str, int], entry: _Entry) -> None:
        retired: list[ManagedHTTPSImageProvider]
        async with self._lock:
            if entry.active_count <= 0 or self._entries.get(key) is not entry:
                raise RuntimeError("dynamic image provider reference count is unbalanced")
            entry.active_count -= 1
            retired = self._retire_idle_locked()
        if retired:
            await asyncio.gather(
                *(provider.aclose() for provider in retired),
                return_exceptions=True,
            )

    def _create_provider(
        self,
        configuration: ActiveModelConfiguration,
        origin: str,
    ) -> ManagedHTTPSImageProvider:
        if self._provider_factory is not None:
            return self._provider_factory(configuration, origin)
        return ManagedHTTPSImageProvider(
            provider_id=self.provider_id,
            origin=origin,
            allowed_origins=frozenset(self.origins.values()),
            allowed_models=frozenset({configuration.upstream_model_id}),
            bearer_token=lambda value=configuration.api_key: value,
            timeout_seconds=self.timeout_seconds,
            connect_timeout_seconds=self.connect_timeout_seconds,
            max_image_bytes=self.max_image_bytes,
            max_connections=self.max_connections,
            max_concurrency=self.max_concurrency,
        )

    def _retire_idle_locked(self) -> list[ManagedHTTPSImageProvider]:
        retired: list[ManagedHTTPSImageProvider] = []
        if len(self._entries) <= self.max_cached_revisions:
            return retired
        for key, entry in tuple(self._entries.items()):
            if len(self._entries) <= self.max_cached_revisions:
                break
            if entry.active_count != 0:
                continue
            del self._entries[key]
            retired.append(entry.provider)
        return retired


__all__ = [
    "AdminImageModelConfigurationResolver",
    "DynamicManagedImageProvider",
    "ImageModelSource",
]
