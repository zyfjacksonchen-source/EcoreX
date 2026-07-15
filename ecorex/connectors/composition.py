"""Production composition boundary mountable by the parent Runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from fastapi import APIRouter
from ecorex.runtime.invariant_guard import RuntimeExecutionGate

from .api import HardDenyProvider, create_connector_router
from .builtin import builtin_connector_registry
from .maintenance import (
    ConnectorMaintenanceSupervisor,
    MaintenanceAllowed,
    MaintenanceErrorSink,
)
from .models import ConnectorInvocationRecord
from .registry import ConnectorAdapter
from .repository import ConnectorOutboxEvent, SQLiteConnectorRepository
from .service import ConnectorService
from .vault import CredentialVault, RejectingCredentialVault, production_credential_vault


@runtime_checkable
class ConnectorEventSink(Protocol):
    """At-least-once event target; implementations deduplicate by event_id."""

    def publish(self, event: ConnectorOutboxEvent) -> None:
        ...


@dataclass(frozen=True, slots=True)
class ConnectorComposition:
    repository: SQLiteConnectorRepository
    service: ConnectorService
    router: APIRouter
    maintenance: ConnectorMaintenanceSupervisor


def build_connector_composition(
    *,
    database_path: str | Path,
    oauth_return_uri: str,
    adapters: Mapping[str, ConnectorAdapter] | None = None,
    vault: CredentialVault | None = None,
    event_sink: ConnectorEventSink | None = None,
    audit_sink: Callable[[ConnectorInvocationRecord], None] | None = None,
    hard_deny_provider: HardDenyProvider | None = None,
    maintenance_interval_seconds: float = 15.0,
    maintenance_error_sink: MaintenanceErrorSink | None = None,
    maintenance_allowed: MaintenanceAllowed | None = None,
    outbox_publish_timeout_seconds: float = 2.0,
    maintenance_stop_timeout_seconds: float = 5.0,
    disconnect_drain_timeout: float = 30.0,
    initialize: bool = True,
    execution_gate: RuntimeExecutionGate | None = None,
) -> ConnectorComposition:
    """Build but do not start connector components.

    The parent Runtime includes ``router`` under ``/api/v1`` and starts/stops
    ``maintenance`` in its ASGI lifespan.
    """

    # ConnectorService owns the complete convergence boundary.  Construct the
    # repository in validation-only mode first so ``initialize=False`` performs
    # no partial metadata write before the service exists.
    repository = SQLiteConnectorRepository(database_path, initialize=False)
    registry = builtin_connector_registry(dict(adapters or {}))
    credential_vault = vault or (
        production_credential_vault() if initialize else RejectingCredentialVault()
    )
    publisher = event_sink.publish if event_sink is not None else None
    service = ConnectorService(
        registry,
        allowed_return_uris=frozenset({oauth_return_uri}),
        vault=credential_vault,
        audit_sink=audit_sink,
        repository=repository,
        outbox_publisher=publisher,
        outbox_publish_timeout_seconds=outbox_publish_timeout_seconds,
        initialize=initialize,
        execution_gate=execution_gate,
    )
    router = create_connector_router(
        service,
        oauth_return_uri=oauth_return_uri,
        hard_deny_provider=hard_deny_provider,
        disconnect_drain_timeout=disconnect_drain_timeout,
    )
    maintenance = ConnectorMaintenanceSupervisor(
        service,
        interval_seconds=maintenance_interval_seconds,
        error_sink=maintenance_error_sink,
        maintenance_allowed=maintenance_allowed,
        stop_timeout_seconds=maintenance_stop_timeout_seconds,
    )
    return ConnectorComposition(
        repository=repository,
        service=service,
        router=router,
        maintenance=maintenance,
    )


__all__ = [
    "ConnectorComposition",
    "ConnectorEventSink",
    "build_connector_composition",
]
