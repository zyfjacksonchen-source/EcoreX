"""Unified connector contracts exposed by the EcoreX v1 backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import re
from types import MappingProxyType
from typing import Any, Mapping

from ecorex.capabilities.schema import validate_schema_contract


_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_PUBLIC_OUTPUT_KINDS = frozenset(
    {
        "connector_cursor",
        "enum",
        "mime_type",
        "public_id",
        "public_uri",
        "text",
        "timestamp",
    }
)


def _plain_contract(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_contract(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_contract(item) for item in value]
    return value


def _freeze_contract(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_contract(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_contract(item) for item in value)
    return value


def _validate_connector_output_contract(
    schema: Mapping[str, Any],
    *,
    label: str,
) -> None:
    """Validate the Connector nullable/public output extension safely."""

    def normalize(value: Any, depth: int = 0) -> Any:
        if depth > 24 or not isinstance(value, Mapping):
            raise ValueError(f"{label} is nested too deeply or invalid")
        public_kind = value.get("x-ecorex-public-kind")
        if public_kind is not None and public_kind not in _PUBLIC_OUTPUT_KINDS:
            raise ValueError(f"{label} has an invalid public output kind")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "x-ecorex-public-kind":
                continue
            if key == "properties":
                if not isinstance(item, Mapping):
                    raise ValueError(f"{label} properties are invalid")
                result[key] = {
                    str(name): normalize(child, depth + 1)
                    for name, child in item.items()
                }
            elif key in {"items", "additionalProperties"} and isinstance(item, Mapping):
                result[key] = normalize(item, depth + 1)
            elif key == "type" and isinstance(item, (list, tuple)):
                declared = tuple(item)
                if (
                    len(declared) != 2
                    or "null" not in declared
                    or any(not isinstance(candidate, str) for candidate in declared)
                ):
                    raise ValueError(f"{label} nullable type is invalid")
                result[key] = next(candidate for candidate in declared if candidate != "null")
            else:
                result[key] = _plain_contract(item)
        return result

    validate_schema_contract(normalize(schema), label=label)


class ConnectorTier(StrEnum):
    STABLE = "stable"
    BETA = "beta"


class ConnectorAuthKind(StrEnum):
    OAUTH2 = "oauth2"
    DEVICE_CODE = "device_code"
    APP_CREDENTIALS = "app_credentials"
    API_TOKEN = "api_token"


class ConnectorHealth(StrEnum):
    UNCONFIGURED = "unconfigured"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    ERROR = "error"
    DISABLED = "disabled"


class ConnectorEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    SUBSCRIBE = "subscribe"


@dataclass(frozen=True, slots=True)
class ConnectorActionSpec:
    action_id: str
    display_name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    effects: frozenset[ConnectorEffect]
    required_scopes: frozenset[str] = frozenset()
    idempotent: bool = True
    intent_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.action_id):
            raise ValueError(f"invalid connector action_id: {self.action_id!r}")
        if not self.display_name.strip() or not self.description.strip():
            raise ValueError("connector action display name and description are required")
        if not self.effects:
            raise ValueError("connector action must declare effects")
        if (
            len(self.intent_aliases) > 32
            or len(set(value.casefold() for value in self.intent_aliases))
            != len(self.intent_aliases)
            or any(
                not value.strip()
                or len(value) > 64
                or any(character in value for character in ("\x00", "\r", "\n"))
                for value in self.intent_aliases
            )
        ):
            raise ValueError("connector action intent aliases are invalid")
        validate_schema_contract(
            self.input_schema,
            label=f"connector {self.action_id} input",
        )
        _validate_connector_output_contract(
            self.output_schema,
            label=f"connector {self.action_id} output",
        )
        object.__setattr__(
            self,
            "input_schema",
            _freeze_contract(_plain_contract(self.input_schema)),
        )
        object.__setattr__(
            self,
            "output_schema",
            _freeze_contract(_plain_contract(self.output_schema)),
        )
        if self.effects & {ConnectorEffect.WRITE, ConnectorEffect.SUBSCRIBE} and not self.idempotent:
            # External writes can still be intrinsically non-idempotent, but
            # EcoreX wraps every supported write with an idempotency contract.
            raise ValueError("connector write actions must support an idempotency key")

    @property
    def requires_idempotency_key(self) -> bool:
        return bool(
            self.effects & {ConnectorEffect.WRITE, ConnectorEffect.SUBSCRIBE}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "display_name": self.display_name,
            "description": self.description,
            "input_schema": _plain_contract(self.input_schema),
            "output_schema": _plain_contract(self.output_schema),
            "effects": sorted(effect.value for effect in self.effects),
            "required_scopes": sorted(self.required_scopes),
            "idempotent": self.idempotent,
            "requires_idempotency_key": self.requires_idempotency_key,
            "intent_aliases": list(self.intent_aliases),
        }


@dataclass(frozen=True, slots=True)
class ConnectorEventSpec:
    event_id: str
    display_name: str
    required_scopes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.event_id):
            raise ValueError(f"invalid connector event_id: {self.event_id!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "display_name": self.display_name,
            "required_scopes": sorted(self.required_scopes),
        }


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    connector_id: str
    contract_version: str
    display_name: str
    description: str
    tier: ConnectorTier
    auth_kinds: tuple[ConnectorAuthKind, ...]
    config_schema: Mapping[str, Any]
    actions: tuple[ConnectorActionSpec, ...]
    events: tuple[ConnectorEventSpec, ...] = ()
    icon_key: str | None = None

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.connector_id):
            raise ValueError(f"invalid connector_id: {self.connector_id!r}")
        if self.contract_version != "1.0":
            raise ValueError("v1 runtime only accepts connector contract 1.0")
        if not self.display_name.strip() or not self.description.strip():
            raise ValueError("connector display name and description are required")
        if not self.auth_kinds:
            raise ValueError("connector must declare at least one auth kind")
        validate_schema_contract(
            self.config_schema,
            label=f"connector {self.connector_id} config",
        )
        object.__setattr__(
            self,
            "config_schema",
            _freeze_contract(_plain_contract(self.config_schema)),
        )
        action_ids = [action.action_id for action in self.actions]
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("connector action IDs must be unique")
        event_ids = [event.event_id for event in self.events]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("connector event IDs must be unique")

    def action(self, action_id: str) -> ConnectorActionSpec:
        for action in self.actions:
            if action.action_id == action_id:
                return action
        raise KeyError(action_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "contract_version": self.contract_version,
            "display_name": self.display_name,
            "description": self.description,
            "tier": self.tier.value,
            "auth_kinds": [kind.value for kind in self.auth_kinds],
            "config_schema": _plain_contract(self.config_schema),
            "actions": [action.to_dict() for action in self.actions],
            "events": [event.to_dict() for event in self.events],
            "icon_key": self.icon_key,
        }


@dataclass(frozen=True, slots=True)
class ConnectorInstance:
    instance_id: str
    connector_id: str
    account_subject: str
    account_display_name: str
    credential_ref: str
    granted_scopes: frozenset[str]
    health: ConnectorHealth
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_error_code: str | None = None

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.connector_id):
            raise ValueError("invalid instance connector_id")
        if not self.instance_id.strip() or not self.credential_ref.strip():
            raise ValueError("connector instance identity and credential_ref are required")

    def to_projection(self, definition: ConnectorDefinition) -> "ConnectorInstanceProjection":
        available_actions = (
            tuple(
                action.action_id
                for action in definition.actions
                if action.required_scopes <= self.granted_scopes
            )
            if self.enabled
            else ()
        )
        return ConnectorInstanceProjection(
            instance_id=self.instance_id,
            connector_id=self.connector_id,
            account_display_name=self.account_display_name,
            health=self.health if self.enabled else ConnectorHealth.DISABLED,
            granted_scopes=tuple(sorted(self.granted_scopes)),
            available_actions=available_actions,
            last_error_code=self.last_error_code,
        )


@dataclass(frozen=True, slots=True)
class ConnectorInstanceProjection:
    instance_id: str
    connector_id: str
    account_display_name: str
    health: ConnectorHealth
    granted_scopes: tuple[str, ...]
    available_actions: tuple[str, ...]
    last_error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "connector_id": self.connector_id,
            "account_display_name": self.account_display_name,
            "health": self.health.value,
            "granted_scopes": list(self.granted_scopes),
            "available_actions": list(self.available_actions),
            "last_error_code": self.last_error_code,
        }


@dataclass(frozen=True, slots=True)
class ConnectorCatalogItem:
    definition: ConnectorDefinition
    adapter_available: bool
    instances: tuple[ConnectorInstanceProjection, ...]
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "definition": self.definition.to_dict(),
            "adapter_available": self.adapter_available,
            "instances": [instance.to_dict() for instance in self.instances],
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class AuthChallenge:
    flow_id: str
    connector_id: str
    auth_kind: ConnectorAuthKind
    expires_at: datetime
    authorization_url: str | None = None
    user_code: str | None = None
    verification_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "flow_id": self.flow_id,
            "connector_id": self.connector_id,
            "auth_kind": self.auth_kind.value,
            "expires_at": self.expires_at.isoformat(),
            "authorization_url": self.authorization_url,
            "user_code": self.user_code,
            "verification_url": self.verification_url,
        }


@dataclass(frozen=True, slots=True)
class AuthGrant:
    account_subject: str
    account_display_name: str
    granted_scopes: frozenset[str]
    credential_material: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ConnectorHealthResult:
    health: ConnectorHealth
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectorInvocationRecord:
    invocation_id: str
    instance_id: str
    connector_id: str
    action_id: str
    input_sha256: str
    idempotency_key_sha256: str | None
    status: str
    created_at: datetime
    runtime_context: "ConnectorInvocationContext | None" = None
    admission_policy_sha256: str | None = None

    @property
    def idempotency_key(self) -> str | None:
        """Compatibility alias; only the non-reversible digest is exposed."""

        return self.idempotency_key_sha256


@dataclass(frozen=True, slots=True)
class ConnectorInvocationContext:
    """Secret-free Runtime correlation for a model-originated invocation."""

    job_id: str
    thread_id: str
    turn_id: str
    execution_batch_id: str
    tool_call_id: str
    capability_snapshot_id: str
    permission_snapshot_id: str
    connector_catalog_snapshot_id: str
    discovery_id: str

    def __post_init__(self) -> None:
        compact = (
            self.job_id,
            self.thread_id,
            self.turn_id,
            self.execution_batch_id,
            self.tool_call_id,
            self.capability_snapshot_id,
            self.permission_snapshot_id,
            self.connector_catalog_snapshot_id,
        )
        if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or any(character in value for character in ("\x00", "\r", "\n"))
            for value in compact
        ):
            raise ValueError("connector invocation Runtime context is invalid")
        if (
            not isinstance(self.discovery_id, str)
            or not self.discovery_id.startswith("connector:")
            or len(self.discovery_id) > 512
            or any(character in self.discovery_id for character in ("\x00", "\r", "\n"))
        ):
            raise ValueError("connector invocation discovery identity is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "job_id": self.job_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "execution_batch_id": self.execution_batch_id,
            "tool_call_id": self.tool_call_id,
            "capability_snapshot_id": self.capability_snapshot_id,
            "permission_snapshot_id": self.permission_snapshot_id,
            "connector_catalog_snapshot_id": self.connector_catalog_snapshot_id,
            "discovery_id": self.discovery_id,
        }
