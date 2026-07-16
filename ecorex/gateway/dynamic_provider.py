"""Hot-swappable managed chat provider driven by tested active revisions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Mapping, Protocol

from ecorex.control_plane.management_models import ActiveModelConfiguration

from .models import GatewayEvent, ModelGatewayRequest, ecorex_chat_gateway_policy
from .server import GatewayPrincipal
from .responses_provider import (
    ManagedHTTPSResponsesProvider,
    ResponsesProviderUnavailable,
)
from .chat_completions_provider import ManagedHTTPSChatCompletionsProvider
from .handoff import ChatHandoffAuthority, ChatModelRevision


class ActiveModelSource(Protocol):
    def active_model(
        self, *, local_model_id: str | None = None, modality: str | None = None
    ) -> ActiveModelConfiguration: ...

    def active_public_catalog(self) -> list[dict[str, object]]: ...


@dataclass(slots=True)
class _ProviderEntry:
    provider: ManagedHTTPSResponsesProvider | ManagedHTTPSChatCompletionsProvider
    local_model_id: str
    active_count: int = 0
    retired: bool = False


class DynamicManagedResponsesProvider:
    """Freeze one tested config per request while new requests hot-swap revisions."""

    def __init__(
        self,
        source: ActiveModelSource,
        *,
        origins: Mapping[str, str],
        handoff_authority: ChatHandoffAuthority,
        chat_handoff_ttl_seconds: int = 3600,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 30.0,
        total_timeout_seconds: float = 240.0,
        max_concurrency: int = 64,
        max_connections: int = 128,
    ) -> None:
        self.source = source
        self.origins = dict(origins)
        self.handoff_authority = handoff_authority
        self.chat_handoff_ttl_seconds = chat_handoff_ttl_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds
        self.max_concurrency = max_concurrency
        self.max_connections = max_connections
        self._entries: dict[tuple[str, int], _ProviderEntry] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def stream(
        self, request: ModelGatewayRequest, principal: GatewayPrincipal
    ) -> AsyncIterator[GatewayEvent]:
        configuration = await asyncio.to_thread(
            self.source.active_model, local_model_id=request.model_id
        )
        if configuration.modality != "chat":
            raise ResponsesProviderUnavailable("managed chat model is unavailable")
        key, entry, policy = await self._acquire(configuration)
        authoritative = request.model_copy(update={"model_policy": policy})
        try:
            async for event in entry.provider.stream(authoritative, principal):
                yield event
        finally:
            await self._release(key, entry)

    async def health(self) -> None:
        catalog = await asyncio.to_thread(self.source.active_public_catalog)
        configurations = [
            await asyncio.to_thread(
                self.source.active_model,
                local_model_id=str(item["local_model_id"]),
            )
            for item in catalog
            if item.get("modality") == "chat"
        ]
        if not configurations:
            raise ResponsesProviderUnavailable("managed chat model is unavailable")
        for configuration in configurations:
            key, entry, _policy = await self._acquire(configuration)
            try:
                await entry.provider.health()
            finally:
                await self._release(key, entry)

    async def public_catalog(self) -> list[dict[str, object]]:
        return await asyncio.to_thread(self.source.active_public_catalog)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            entries = list(self._entries.values())
            self._entries.clear()
        await asyncio.gather(
            *(entry.provider.aclose() for entry in entries), return_exceptions=True
        )

    async def _acquire(self, configuration: ActiveModelConfiguration):
        key = (configuration.config_id, configuration.revision)
        close_after: list[
            ManagedHTTPSResponsesProvider | ManagedHTTPSChatCompletionsProvider
        ] = []
        async with self._lock:
            if self._closed:
                raise ResponsesProviderUnavailable("managed provider is closed")
            entry = self._entries.get(key)
            if entry is None:
                origin = self.origins.get(configuration.provider_origin_preset)
                if origin is None:
                    raise ResponsesProviderUnavailable(
                        "managed provider origin is unavailable"
                    )
                base_policy = ecorex_chat_gateway_policy(configuration.local_model_id)
                policy = base_policy.model_copy(
                    update={"upstream_model_id": configuration.upstream_model_id}
                )
                common = {
                    "origin": origin,
                    "allowed_origins": frozenset(self.origins.values()),
                    "model_mapping": {
                        configuration.local_model_id: configuration.upstream_model_id
                    },
                    "model_policies": {configuration.local_model_id: policy},
                    "bearer_token": lambda value=configuration.api_key: value,
                    "connect_timeout_seconds": self.connect_timeout_seconds,
                    "read_timeout_seconds": self.read_timeout_seconds,
                    "total_timeout_seconds": self.total_timeout_seconds,
                    "max_concurrency": self.max_concurrency,
                    "max_connections": self.max_connections,
                }
                if configuration.provider_preset == "responses":
                    provider = ManagedHTTPSResponsesProvider(
                        **common,
                        allow_dynamic_mapping=True,
                    )
                elif configuration.provider_preset == "openai_compatible_chat":
                    provider = ManagedHTTPSChatCompletionsProvider(
                        **common,
                        handoff_authority=self.handoff_authority,
                        model_revision=ChatModelRevision(
                            config_id=configuration.config_id,
                            revision=configuration.revision,
                            local_model_id=configuration.local_model_id,
                            upstream_model_id=configuration.upstream_model_id,
                            provider_protocol=configuration.provider_preset,
                            provider_origin_preset=(
                                configuration.provider_origin_preset
                            ),
                        ),
                        handoff_ttl_seconds=self.chat_handoff_ttl_seconds,
                    )
                else:
                    raise ResponsesProviderUnavailable(
                        "managed chat provider protocol is unavailable"
                    )
                entry = _ProviderEntry(
                    provider=provider,
                    local_model_id=configuration.local_model_id,
                )
                self._entries[key] = entry
                for old_key, old in list(self._entries.items()):
                    if old_key == key or old.local_model_id != entry.local_model_id:
                        continue
                    old.retired = True
                    if old.active_count == 0:
                        self._entries.pop(old_key, None)
                        close_after.append(old.provider)
            policy = entry.provider.model_policies[configuration.local_model_id]
            entry.active_count += 1
        if close_after:
            await asyncio.gather(
                *(provider.aclose() for provider in close_after),
                return_exceptions=True,
            )
        return key, entry, policy

    async def _release(
        self, key: tuple[str, int], entry: _ProviderEntry
    ) -> None:
        close = False
        async with self._lock:
            if entry.active_count <= 0:
                raise RuntimeError("dynamic provider reference count is unbalanced")
            entry.active_count -= 1
            if entry.retired and entry.active_count == 0:
                if self._entries.get(key) is entry:
                    self._entries.pop(key, None)
                close = True
        if close:
            await entry.provider.aclose()


__all__ = ["ActiveModelSource", "DynamicManagedResponsesProvider"]
