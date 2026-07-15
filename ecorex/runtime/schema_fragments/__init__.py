"""Compiled local-product schema fragments.

Domain modules do not register DDL dynamically.  Each fragment is imported
here so a fresh product database has the same physical schema regardless of
feature flags.  Repositories only validate the compiled result.
"""

from __future__ import annotations

from typing import Final

from ..schema_catalog import SchemaFragment
from .audit_outbox import AUDIT_OUTBOX_SCHEMA_FRAGMENT
from .artifacts import ARTIFACT_SCHEMA_FRAGMENT
from .connectors import CONNECTORS_SCHEMA_FRAGMENT
from .connector_agent import CONNECTOR_AGENT_SCHEMA_FRAGMENT
from .device_authorization import DEVICE_AUTHORIZATION_SCHEMA_FRAGMENT
from .execution import EXECUTION_SCHEMA_FRAGMENTS
from .extensions import EXTENSIONS_SCHEMA_FRAGMENT
from .integration import INTEGRATION_SCHEMA_FRAGMENT
from .legacy_import import LEGACY_IMPORT_SCHEMA_FRAGMENT
from .managed_session import MANAGED_SESSION_SCHEMA_FRAGMENT
from .memory import MEMORY_SCHEMA_FRAGMENT
from .output import OUTPUT_SCHEMA_FRAGMENT
from .permissions import PERMISSIONS_SCHEMA_FRAGMENT
from .sharing import SHARING_SCHEMA_FRAGMENT
from .system_observability import SYSTEM_OBSERVABILITY_SCHEMA_FRAGMENT
from .trace_outbox import TRACE_OUTBOX_SCHEMA_FRAGMENT
from .turn_execution import TURN_EXECUTION_SCHEMA_FRAGMENT
from .update import UPDATE_SCHEMA_FRAGMENT


PRODUCT_SCHEMA_FRAGMENTS: Final[tuple[SchemaFragment, ...]] = (
    MANAGED_SESSION_SCHEMA_FRAGMENT,
    DEVICE_AUTHORIZATION_SCHEMA_FRAGMENT,
    MEMORY_SCHEMA_FRAGMENT,
    CONNECTORS_SCHEMA_FRAGMENT,
    CONNECTOR_AGENT_SCHEMA_FRAGMENT,
    *EXECUTION_SCHEMA_FRAGMENTS,
    TURN_EXECUTION_SCHEMA_FRAGMENT,
    ARTIFACT_SCHEMA_FRAGMENT,
    LEGACY_IMPORT_SCHEMA_FRAGMENT,
    INTEGRATION_SCHEMA_FRAGMENT,
    EXTENSIONS_SCHEMA_FRAGMENT,
    OUTPUT_SCHEMA_FRAGMENT,
    PERMISSIONS_SCHEMA_FRAGMENT,
    SHARING_SCHEMA_FRAGMENT,
    UPDATE_SCHEMA_FRAGMENT,
    SYSTEM_OBSERVABILITY_SCHEMA_FRAGMENT,
    TRACE_OUTBOX_SCHEMA_FRAGMENT,
    AUDIT_OUTBOX_SCHEMA_FRAGMENT,
)


__all__ = ["PRODUCT_SCHEMA_FRAGMENTS"]
