"""Connector definition/adapter registry used to generate the WebUI catalog."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from typing import Any, Protocol, runtime_checkable

from .errors import ConnectorNotFound
from .models import (
    AuthChallenge,
    AuthGrant,
    ConnectorAuthKind,
    ConnectorDefinition,
    ConnectorHealthResult,
)


@runtime_checkable
class ConnectorAdapter(Protocol):
    async def begin_auth(
        self,
        *,
        flow_id: str,
        auth_kind: ConnectorAuthKind,
        return_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> AuthChallenge:
        ...

    async def complete_auth(
        self,
        *,
        flow_id: str,
        response: Mapping[str, str],
        private_state: Mapping[str, str],
    ) -> AuthGrant:
        ...

    async def check_health(self, credentials: Mapping[str, str]) -> ConnectorHealthResult:
        ...

    def invoke(
        self,
        *,
        action_id: str,
        inputs: Mapping[str, Any],
        credentials: Mapping[str, str],
        idempotency_key: str | None,
    ) -> Any | Awaitable[Any]:
        ...


@runtime_checkable
class RevocableConnectorAdapter(Protocol):
    """Optional capability for idempotent provider-side grant revocation."""

    def revoke(
        self,
        *,
        credentials: Mapping[str, str],
        idempotency_key: str,
    ) -> Any | Awaitable[Any]:
        ...


class ConnectorRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ConnectorDefinition] = {}
        self._adapters: dict[str, ConnectorAdapter] = {}
        self._sealed = False

    def register(
        self,
        definition: ConnectorDefinition,
        adapter: ConnectorAdapter | None = None,
    ) -> None:
        if self._sealed:
            raise RuntimeError("connector registry is sealed")
        if len(self._definitions) >= 256:
            raise ValueError("connector registry exceeds the product limit")
        if definition.connector_id in self._definitions:
            raise ValueError(f"duplicate connector_id: {definition.connector_id}")
        if adapter is not None and not isinstance(adapter, ConnectorAdapter):
            raise TypeError("connector adapter does not implement the v1 contract")
        self._definitions[definition.connector_id] = definition
        if adapter is not None:
            self._adapters[definition.connector_id] = adapter

    def seal(self) -> None:
        self._sealed = True

    def definition(self, connector_id: str) -> ConnectorDefinition:
        try:
            return self._definitions[connector_id]
        except KeyError as exc:
            raise ConnectorNotFound(f"unknown connector: {connector_id!r}") from exc

    def adapter(self, connector_id: str) -> ConnectorAdapter:
        self.definition(connector_id)
        try:
            return self._adapters[connector_id]
        except KeyError as exc:
            raise ConnectorNotFound(
                f"connector adapter is not installed: {connector_id!r}"
            ) from exc

    def has_adapter(self, connector_id: str) -> bool:
        return connector_id in self._adapters

    def definitions(self) -> tuple[ConnectorDefinition, ...]:
        return tuple(
            sorted(
                self._definitions.values(),
                key=lambda item: (item.tier.value != "stable", item.display_name),
            )
        )
