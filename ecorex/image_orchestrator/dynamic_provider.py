"""Durable hot-swappable image provider using frozen tested revisions."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
import ssl
from typing import Mapping, Protocol

from ecorex.control_plane.management_models import ActiveModelConfiguration

from .cas import ImageContentAddressedStore
from .models import ImageJob, ImageOperation, ImageUsage
from .openai_provider import OpenAICompatibleImageProvider
from .provider import ImageProvider, ProviderResult, ProviderUnavailable
from .service import ImageModelConfigurationSnapshot


class ImageModelSource(Protocol):
    def active_model(
        self, *, local_model_id: str | None = None, modality: str | None = None
    ) -> ActiveModelConfiguration: ...

    def model_revision(
        self, config_id: str, revision: int
    ) -> ActiveModelConfiguration: ...


class _ManagedImageProvider(ImageProvider, Protocol):
    async def health(self) -> None: ...

    async def aclose(self) -> None: ...


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
    provider: _ManagedImageProvider
    active_count: int = 0
    retired: bool = False
    close_started: bool = False


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
        ssl_context: ssl.SSLContext | None = None,
        input_store: ImageContentAddressedStore | None = None,
        max_cached_revisions: int = 32,
        provider_factory: Callable[
            [ActiveModelConfiguration, str], _ManagedImageProvider
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
        self.ssl_context = ssl_context
        self.input_store = input_store
        if not 2 <= max_cached_revisions <= 256:
            raise ValueError("dynamic image provider cache limit is invalid")
        self.max_cached_revisions = max_cached_revisions
        self._provider_factory = provider_factory
        self._entries: OrderedDict[tuple[str, int], _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._closed = False
        self._close_complete = False
        self._close_waiter: asyncio.Future[None] | None = None
        self._closing_count = 0
        self._close_tasks: set[asyncio.Task[None]] = set()

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
            if self._close_complete:
                return
            self._closed = True
            if self._close_waiter is None:
                self._close_waiter = asyncio.get_running_loop().create_future()
            waiter = self._close_waiter
            close_now: list[_Entry] = []
            for key, entry in tuple(self._entries.items()):
                entry.retired = True
                if entry.active_count != 0:
                    continue
                del self._entries[key]
                self._begin_close_locked(entry)
                close_now.append(entry)
            self._finish_close_locked()
        self._schedule_closes(close_now)
        await asyncio.shield(waiter)

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
                origin = self.origins.get(configuration.provider_origin_preset)
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
        close_now: list[_Entry] = []
        async with self._lock:
            if entry.active_count <= 0:
                raise RuntimeError("dynamic image provider reference count is unbalanced")
            entry.active_count -= 1
            if entry.retired and entry.active_count == 0:
                if self._entries.get(key) is entry:
                    del self._entries[key]
                self._begin_close_locked(entry)
                close_now.append(entry)
            close_now.extend(self._retire_idle_locked())
            self._finish_close_locked()
        await self._await_closes(close_now)

    def _create_provider(
        self,
        configuration: ActiveModelConfiguration,
        origin: str,
    ) -> _ManagedImageProvider:
        if self._provider_factory is not None:
            return self._provider_factory(configuration, origin)
        return OpenAICompatibleImageProvider(
            provider_id=self.provider_id,
            origin=origin,
            allowed_origins=frozenset(self.origins.values()),
            allowed_models=frozenset({configuration.upstream_model_id}),
            bearer_token=lambda value=configuration.api_key: value,
            input_store=self.input_store,
            timeout_seconds=self.timeout_seconds,
            connect_timeout_seconds=self.connect_timeout_seconds,
            max_image_bytes=self.max_image_bytes,
            max_connections=self.max_connections,
            max_concurrency=self.max_concurrency,
            ssl_context=self.ssl_context,
        )

    def _retire_idle_locked(self) -> list[_Entry]:
        retired: list[_Entry] = []
        if len(self._entries) <= self.max_cached_revisions:
            return retired
        for key, entry in tuple(self._entries.items()):
            if len(self._entries) <= self.max_cached_revisions:
                break
            if entry.active_count != 0:
                continue
            del self._entries[key]
            entry.retired = True
            self._begin_close_locked(entry)
            retired.append(entry)
        return retired

    def _begin_close_locked(self, entry: _Entry) -> None:
        if entry.close_started:
            return
        entry.close_started = True
        self._closing_count += 1

    def _schedule_closes(self, entries: list[_Entry]) -> list[asyncio.Task[None]]:
        tasks: list[asyncio.Task[None]] = []
        for entry in entries:
            task = asyncio.create_task(self._close_entry(entry))
            self._close_tasks.add(task)
            task.add_done_callback(self._close_tasks.discard)
            tasks.append(task)
        return tasks

    async def _await_closes(self, entries: list[_Entry]) -> None:
        tasks = self._schedule_closes(entries)
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))

    async def _close_entry(self, entry: _Entry) -> None:
        try:
            await entry.provider.aclose()
        except (Exception, asyncio.CancelledError):
            # Shutdown and cache retirement are best-effort, matching the provider
            # contract. Lifecycle accounting must still complete exactly once.
            pass
        finally:
            async with self._lock:
                self._closing_count -= 1
                if self._closing_count < 0:
                    raise RuntimeError(
                        "dynamic image provider close count is unbalanced"
                    )
                self._finish_close_locked()

    def _finish_close_locked(self) -> None:
        if (
            not self._closed
            or self._entries
            or self._closing_count != 0
            or self._close_complete
        ):
            return
        self._close_complete = True
        if self._close_waiter is not None and not self._close_waiter.done():
            self._close_waiter.set_result(None)


__all__ = [
    "AdminImageModelConfigurationResolver",
    "DynamicManagedImageProvider",
    "ImageModelSource",
]
