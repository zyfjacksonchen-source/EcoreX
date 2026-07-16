"""Fail-closed composition of one verified EcoreX product Runtime slot.

The Bootstrap is the root of process provenance: before it starts this module it
has verified the release, the platform core artifact and every extracted payload
member.  This module re-establishes that same slot identity, parses the bounded
``runtime-config.json`` contained in the signed payload, and maps only explicitly
declared paths into the installation root.

No credential value is accepted by this format.  Managed account tokens remain
in the platform credential vault and are reached exclusively through
``ManagedSessionService``.
"""

from __future__ import annotations

import base64
import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat as stat_module
import threading
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from ecorex.bootstrap import (
    BootstrapConfigurationError,
    CurrentSlotVerifier,
    DelayedRestartRequester,
    RUNTIME_RELOAD_EXIT_CODE,
    RuntimeEndpoint,
    VerifiedRuntimeSlot,
)
from ecorex.capabilities import (
    CapabilityPackManifest,
    CapabilityPackRuntime,
    VerifiedCapabilityPack,
    builtin_capability_registry,
    verify_capability_pack,
)
from ecorex.connectors import (
    CredentialVault,
    ManagedConnectorGatewayAdapter,
    production_credential_vault,
)
from ecorex.gateway import ManagedModelGatewayClient
from ecorex.integration import (
    ManagedImageOrchestrationClient,
    ManagedImageRetouchAdapter,
)
from ecorex.integration.pack_verification import verify_product_capability_pack
from ecorex.integration.windows_sandbox_security import WindowsSandboxSlotSecurity
from ecorex.migration import (
    MigrationError,
    ProductLegacyMigrationCoordinator,
    ProductMigrationError,
)
from ecorex.observability import (
    ManagedHTTPSAuditPublisher,
    ManagedOTLPHTTPTraceExporter,
)
from ecorex.output import standard_output_roots
from ecorex.pack_catalog import REQUIRED_CAPABILITY_PACK_IDS
from ecorex.runtime.database import SCHEMA_VERSION as RUNTIME_STORAGE_SCHEMA_VERSION
from ecorex.runtime.storage_migrations import (
    MAX_STORAGE_MIGRATION_BYTES,
    STORAGE_MIGRATION_FILE_NAME,
    StorageMigrationError,
    StorageMigrationIdentity,
    StorageMigrationManifest,
    apply_live_storage_migration,
    current_storage_schema_sha256,
    dry_run_storage_migration,
    load_live_storage_migration_receipt,
)
from ecorex.session import (
    DeviceAuthorizationBroker,
    Ed25519SessionLeaseVerifier,
    HTTPSDeviceAuthorizationBroker,
    ManagedDeviceAuthorizationService,
    ManagedSessionRefreshService,
    ManagedSessionService,
)
from ecorex.sharing import HTTPSSharePublisher
from ecorex.update import (
    ActivationIntentError,
    ActivationLaunchContext,
    Ed25519SignatureVerifier,
    ProductUpdateComposition,
    ProductUpdateSettings,
    ProvisionalActivationController,
    ReleaseChannel,
    SlotStore,
    VerificationError,
    build_product_update_composition,
    verify_artifact_signature,
    verify_manifest_signature,
)

from .activation import ActivationProbeSettings
from .app import ProductServerSettings
from .bundle import load_verified_web_bundle
from .errors import ServerConfigurationError


RUNTIME_CONFIG_FILE_NAME = "runtime-config.json"
RUNTIME_CONFIG_SCHEMA_VERSION = 1
MAX_RUNTIME_CONFIG_BYTES = 256 * 1024
RUNTIME_API_VERSION = "1.0.0"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_FIELD = re.compile(
    r"(?:^|_)(?:token|bearer|password|secret|private_key|api_key|access_key)(?:$|_)",
    re.IGNORECASE,
)


class ProductRuntimeConfigurationError(ServerConfigurationError):
    """The signed product Runtime configuration is invalid or unsafe."""

    def __init__(self, message: str, *, stage_code: str | None = None) -> None:
        super().__init__(message)
        self.stage_code = (
            stage_code
            if stage_code is not None and _SAFE_ID.fullmatch(stage_code)
            else None
        )


class ProductRuntimeTrustError(ProductRuntimeConfigurationError):
    """The selected Runtime slot cannot be tied to its signed identity."""


PackAdapterResolver = Callable[
    [VerifiedCapabilityPack, tuple[Path, ...], Path], Mapping[str, Callable[..., Any]]
]
VaultFactory = Callable[[], CredentialVault]


@dataclass(frozen=True, slots=True)
class RuntimeIdentityConfig:
    version: str
    platform: str
    architecture: str


@dataclass(frozen=True, slots=True)
class RuntimePathsConfig:
    database: str
    web_root: str
    web_manifest: str
    workspace_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    endpoint: str
    allowed_hosts: frozenset[str]


@dataclass(frozen=True, slots=True)
class DeviceAuthorizationConfig:
    base_url: str
    allowed_hosts: frozenset[str]
    client_id: str
    timeout_seconds: float
    supervisor_poll_seconds: float


DeviceBrokerFactory = Callable[[DeviceAuthorizationConfig], DeviceAuthorizationBroker]
ReloadRequesterFactory = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class UpdateConfig:
    release_feed_endpoint: str
    signal_endpoint: str
    control_plane_hosts: frozenset[str]
    artifact_hosts: frozenset[str]
    channel: ReleaseChannel
    poll_interval_seconds: float


@dataclass(frozen=True, slots=True)
class ShareServiceConfig:
    endpoint: str
    allowed_hosts: frozenset[str]
    public_hosts: frozenset[str]


@dataclass(frozen=True, slots=True)
class ImageOrchestrationConfig:
    root_url: str
    allowed_hosts: frozenset[str]


@dataclass(frozen=True, slots=True)
class AuditServiceConfig:
    endpoint: str
    allowed_hosts: frozenset[str]
    dispatch_seconds: float
    raw_retention_days: int
    aggregate_retention_days: int


@dataclass(frozen=True, slots=True)
class TraceServiceConfig:
    endpoint: str
    allowed_hosts: frozenset[str]
    dispatch_seconds: float
    max_spans_per_batch: int
    max_request_bytes: int
    retention_days: int


@dataclass(frozen=True, slots=True)
class ConnectorServiceConfig:
    endpoint: str
    allowed_hosts: frozenset[str]
    enabled_connectors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CapabilityPackConfig:
    pack_id: str
    manifest: str
    artifact: str


@dataclass(frozen=True, slots=True)
class ProductRuntimeConfig:
    schema_version: int
    identity: RuntimeIdentityConfig
    paths: RuntimePathsConfig
    release_public_keys: Mapping[str, bytes]
    rollback_public_keys: Mapping[str, bytes]
    session_public_keys: Mapping[str, bytes]
    gateway: GatewayConfig
    device_authorization: DeviceAuthorizationConfig
    update: UpdateConfig
    share: ShareServiceConfig | None
    image_orchestration: ImageOrchestrationConfig | None
    audit: AuditServiceConfig | None
    tracing: TraceServiceConfig | None
    connectors: ConnectorServiceConfig | None
    capability_packs: tuple[CapabilityPackConfig, ...]

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ProductRuntimeConfig":
        if (
            not isinstance(payload, bytes)
            or not 1 <= len(payload) <= MAX_RUNTIME_CONFIG_BYTES
        ):
            raise ProductRuntimeConfigurationError(
                "Runtime configuration size is invalid"
            )
        try:
            decoded = payload.decode("utf-8")
            raw = json.loads(decoded, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ProductRuntimeConfigurationError(
                "Runtime configuration must be unique-key UTF-8 JSON"
            ) from None
        if not isinstance(raw, Mapping):
            raise ProductRuntimeConfigurationError(
                "Runtime configuration root must be an object"
            )
        _reject_sensitive_fields(raw)
        _exact_keys(
            raw,
            {
                "schema_version",
                "identity",
                "paths",
                "release_public_keys",
                "rollback_public_keys",
                "session_public_keys",
                "gateway",
                "device_authorization",
                "update",
                "share",
                "image_orchestration",
                "audit",
                "tracing",
                "connectors",
                "capability_packs",
            },
            "Runtime configuration",
        )
        schema_version = _integer(raw, "schema_version")
        if schema_version != RUNTIME_CONFIG_SCHEMA_VERSION:
            raise ProductRuntimeConfigurationError(
                "Runtime configuration schema is unsupported"
            )
        identity_raw = _mapping(raw, "identity")
        _exact_keys(
            identity_raw,
            {
                "version",
                "platform",
                "architecture",
            },
            "Runtime identity",
        )
        identity = RuntimeIdentityConfig(
            version=_semver(identity_raw, "version"),
            platform=_choice(identity_raw, "platform", {"windows", "macos"}),
            architecture=_choice(identity_raw, "architecture", {"x64", "arm64"}),
        )
        if identity.platform == "windows" and identity.architecture != "x64":
            raise ProductRuntimeConfigurationError(
                "Windows Runtime configuration must target x64"
            )

        paths_raw = _mapping(raw, "paths")
        _exact_keys(
            paths_raw,
            {"database", "web_root", "web_manifest", "workspace_roots"},
            "Runtime paths",
        )
        workspace_roots = _relative_path_array(paths_raw, "workspace_roots", maximum=32)
        if not workspace_roots:
            raise ProductRuntimeConfigurationError(
                "Runtime configuration requires a workspace root"
            )
        paths = RuntimePathsConfig(
            database=_relative_path(paths_raw, "database"),
            web_root=_relative_path(paths_raw, "web_root"),
            web_manifest=_relative_path(paths_raw, "web_manifest"),
            workspace_roots=workspace_roots,
        )
        managed_workspace = PurePosixPath("workspace")
        workspace_paths = tuple(PurePosixPath(value) for value in workspace_roots)
        try:
            for workspace_path in workspace_paths:
                workspace_path.relative_to(managed_workspace)
        except ValueError:
            raise ProductRuntimeConfigurationError(
                "Workspace roots must remain inside the managed workspace directory"
            ) from None
        for position, workspace_path in enumerate(workspace_paths):
            for other in workspace_paths[position + 1 :]:
                try:
                    workspace_path.relative_to(other)
                    overlaps = True
                except ValueError:
                    try:
                        other.relative_to(workspace_path)
                        overlaps = True
                    except ValueError:
                        overlaps = False
                if overlaps:
                    raise ProductRuntimeConfigurationError(
                        "Workspace roots cannot contain one another"
                    )
        try:
            PurePosixPath(paths.database).relative_to(managed_workspace)
        except ValueError:
            pass
        else:
            raise ProductRuntimeConfigurationError(
                "Runtime state cannot be stored inside the managed workspace directory"
            )
        try:
            PurePosixPath(paths.web_manifest).relative_to(PurePosixPath(paths.web_root))
        except ValueError:
            pass
        else:
            raise ProductRuntimeConfigurationError(
                "Web manifest must remain outside the exact Web root allowlist"
            )

        gateway_raw = _mapping(raw, "gateway")
        _exact_keys(gateway_raw, {"endpoint", "allowed_hosts"}, "Model Gateway")
        gateway_hosts = _hosts(gateway_raw, "allowed_hosts")
        gateway_endpoint = _https_endpoint(
            gateway_raw,
            "endpoint",
            allowed_hosts=gateway_hosts,
            websocket=False,
        )
        gateway = GatewayConfig(gateway_endpoint, gateway_hosts)

        device_raw = _mapping(raw, "device_authorization")
        _exact_keys(
            device_raw,
            {
                "base_url",
                "allowed_hosts",
                "client_id",
                "timeout_seconds",
                "supervisor_poll_seconds",
            },
            "Device authorization",
        )
        device_hosts = _hosts(device_raw, "allowed_hosts")
        device_base_url = _https_origin(
            device_raw,
            "base_url",
            allowed_hosts=device_hosts,
        )
        device_client_id = _safe_id(device_raw, "client_id")
        device_timeout = _number(device_raw, "timeout_seconds")
        if not 1 <= device_timeout <= 120:
            raise ProductRuntimeConfigurationError(
                "Device authorization timeout is outside its product bound"
            )
        supervisor_poll = _number(device_raw, "supervisor_poll_seconds")
        if not 0.05 <= supervisor_poll <= 30:
            raise ProductRuntimeConfigurationError(
                "Device authorization supervisor interval is outside its product bound"
            )
        device_authorization = DeviceAuthorizationConfig(
            base_url=device_base_url,
            allowed_hosts=device_hosts,
            client_id=device_client_id,
            timeout_seconds=device_timeout,
            supervisor_poll_seconds=supervisor_poll,
        )

        update_raw = _mapping(raw, "update")
        _exact_keys(
            update_raw,
            {
                "release_feed_endpoint",
                "signal_endpoint",
                "control_plane_hosts",
                "artifact_hosts",
                "channel",
                "poll_interval_seconds",
            },
            "Update configuration",
        )
        control_plane_hosts = _hosts(update_raw, "control_plane_hosts")
        artifact_hosts = _hosts(update_raw, "artifact_hosts")
        release_feed_endpoint = _https_endpoint(
            update_raw,
            "release_feed_endpoint",
            allowed_hosts=control_plane_hosts,
            websocket=False,
        )
        signal_endpoint = _https_endpoint(
            update_raw,
            "signal_endpoint",
            allowed_hosts=control_plane_hosts,
            websocket=True,
        )
        try:
            channel = ReleaseChannel(_string(update_raw, "channel"))
        except ValueError:
            raise ProductRuntimeConfigurationError(
                "Update channel is unsupported"
            ) from None
        poll_interval = _number(update_raw, "poll_interval_seconds")
        if not 5 <= poll_interval <= 86_400:
            raise ProductRuntimeConfigurationError(
                "Update poll interval is outside its product bound"
            )
        update = UpdateConfig(
            release_feed_endpoint=release_feed_endpoint,
            signal_endpoint=signal_endpoint,
            control_plane_hosts=control_plane_hosts,
            artifact_hosts=artifact_hosts,
            channel=channel,
            poll_interval_seconds=poll_interval,
        )

        share = _optional_share_service(raw.get("share"))
        image_orchestration = _optional_image_orchestration(
            raw.get("image_orchestration")
        )
        audit = _optional_audit_service(raw.get("audit"))
        tracing = _optional_trace_service(raw.get("tracing"))
        connectors = _optional_connector_service(raw.get("connectors"))

        raw_packs = raw.get("capability_packs")
        if not isinstance(raw_packs, list) or len(raw_packs) > 64:
            raise ProductRuntimeConfigurationError(
                "Capability Pack configuration must be a bounded array"
            )
        packs: list[CapabilityPackConfig] = []
        for raw_pack in raw_packs:
            if not isinstance(raw_pack, Mapping):
                raise ProductRuntimeConfigurationError(
                    "Capability Pack configuration must contain objects"
                )
            _exact_keys(
                raw_pack,
                {"pack_id", "manifest", "artifact"},
                "Capability Pack entry",
            )
            packs.append(
                CapabilityPackConfig(
                    pack_id=_safe_id(raw_pack, "pack_id"),
                    manifest=_relative_path(raw_pack, "manifest"),
                    artifact=_relative_path(raw_pack, "artifact"),
                )
            )
        pack_ids = tuple(pack.pack_id for pack in packs)
        if pack_ids != tuple(sorted(pack_ids)) or len(pack_ids) != len(set(pack_ids)):
            raise ProductRuntimeConfigurationError(
                "Capability Pack entries must be unique and sorted"
            )

        release_keys = _public_keys(raw, "release_public_keys")
        rollback_keys = _public_keys(raw, "rollback_public_keys")
        session_keys = _public_keys(raw, "session_public_keys")
        if set(release_keys).intersection(rollback_keys) or {
            hashlib.sha256(value).digest() for value in release_keys.values()
        }.intersection(
            hashlib.sha256(value).digest() for value in rollback_keys.values()
        ):
            raise ProductRuntimeConfigurationError(
                "release and rollback trust roles must use distinct keys"
            )
        configuration = cls(
            schema_version=schema_version,
            identity=identity,
            paths=paths,
            release_public_keys=release_keys,
            rollback_public_keys=rollback_keys,
            session_public_keys=session_keys,
            gateway=gateway,
            device_authorization=device_authorization,
            update=update,
            share=share,
            image_orchestration=image_orchestration,
            audit=audit,
            tracing=tracing,
            connectors=connectors,
            capability_packs=tuple(packs),
        )
        if configuration.to_bytes() != payload:
            raise ProductRuntimeConfigurationError(
                "Runtime configuration JSON must be canonical"
            )
        return configuration

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": {
                "version": self.identity.version,
                "platform": self.identity.platform,
                "architecture": self.identity.architecture,
            },
            "paths": {
                "database": self.paths.database,
                "web_root": self.paths.web_root,
                "web_manifest": self.paths.web_manifest,
                "workspace_roots": list(self.paths.workspace_roots),
            },
            "release_public_keys": {
                key_id: base64.b64encode(value).decode("ascii")
                for key_id, value in self.release_public_keys.items()
            },
            "rollback_public_keys": {
                key_id: base64.b64encode(value).decode("ascii")
                for key_id, value in self.rollback_public_keys.items()
            },
            "session_public_keys": {
                key_id: base64.b64encode(value).decode("ascii")
                for key_id, value in self.session_public_keys.items()
            },
            "gateway": {
                "endpoint": self.gateway.endpoint,
                "allowed_hosts": sorted(self.gateway.allowed_hosts),
            },
            "device_authorization": {
                "base_url": self.device_authorization.base_url,
                "allowed_hosts": sorted(self.device_authorization.allowed_hosts),
                "client_id": self.device_authorization.client_id,
                "timeout_seconds": (
                    int(self.device_authorization.timeout_seconds)
                    if self.device_authorization.timeout_seconds.is_integer()
                    else self.device_authorization.timeout_seconds
                ),
                "supervisor_poll_seconds": (
                    int(self.device_authorization.supervisor_poll_seconds)
                    if self.device_authorization.supervisor_poll_seconds.is_integer()
                    else self.device_authorization.supervisor_poll_seconds
                ),
            },
            "update": {
                "release_feed_endpoint": self.update.release_feed_endpoint,
                "signal_endpoint": self.update.signal_endpoint,
                "control_plane_hosts": sorted(self.update.control_plane_hosts),
                "artifact_hosts": sorted(self.update.artifact_hosts),
                "channel": self.update.channel.value,
                "poll_interval_seconds": (
                    int(self.update.poll_interval_seconds)
                    if self.update.poll_interval_seconds.is_integer()
                    else self.update.poll_interval_seconds
                ),
            },
            "share": (
                {
                    "endpoint": self.share.endpoint,
                    "allowed_hosts": sorted(self.share.allowed_hosts),
                    "public_hosts": sorted(self.share.public_hosts),
                }
                if self.share is not None
                else None
            ),
            "image_orchestration": (
                {
                    "root_url": self.image_orchestration.root_url,
                    "allowed_hosts": sorted(self.image_orchestration.allowed_hosts),
                }
                if self.image_orchestration is not None
                else None
            ),
            "audit": (
                {
                    "endpoint": self.audit.endpoint,
                    "allowed_hosts": sorted(self.audit.allowed_hosts),
                    "dispatch_seconds": (
                        int(self.audit.dispatch_seconds)
                        if self.audit.dispatch_seconds.is_integer()
                        else self.audit.dispatch_seconds
                    ),
                    "raw_retention_days": self.audit.raw_retention_days,
                    "aggregate_retention_days": self.audit.aggregate_retention_days,
                }
                if self.audit is not None
                else None
            ),
            "tracing": (
                {
                    "endpoint": self.tracing.endpoint,
                    "allowed_hosts": sorted(self.tracing.allowed_hosts),
                    "dispatch_seconds": (
                        int(self.tracing.dispatch_seconds)
                        if self.tracing.dispatch_seconds.is_integer()
                        else self.tracing.dispatch_seconds
                    ),
                    "max_spans_per_batch": self.tracing.max_spans_per_batch,
                    "max_request_bytes": self.tracing.max_request_bytes,
                    "retention_days": self.tracing.retention_days,
                }
                if self.tracing is not None
                else None
            ),
            "connectors": (
                {
                    "endpoint": self.connectors.endpoint,
                    "allowed_hosts": sorted(self.connectors.allowed_hosts),
                    "enabled_connectors": list(self.connectors.enabled_connectors),
                }
                if self.connectors is not None
                else None
            ),
            "capability_packs": [
                {
                    "pack_id": pack.pack_id,
                    "manifest": pack.manifest,
                    "artifact": pack.artifact,
                }
                for pack in self.capability_packs
            ],
        }

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")


class _ProductCompositionCleanup:
    def __init__(self, resources: tuple[Any, ...]) -> None:
        self._resources = resources
        self._lock = threading.Lock()
        self._finished = False

    def close_once(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        _close_unstarted_resources(reversed(self._resources))

    def transfer(self) -> None:
        with self._lock:
            self._finished = True


@dataclass(frozen=True, slots=True)
class ProductRuntimeComposition:
    install_root: Path
    slot: VerifiedRuntimeSlot
    config: ProductRuntimeConfig
    managed_session: ManagedSessionService
    device_authorization: ManagedDeviceAuthorizationService
    session_reload_requester: Any
    gateway: ManagedModelGatewayClient
    share_publisher: HTTPSSharePublisher | None
    image_orchestration_client: ManagedImageOrchestrationClient | None
    retouch_adapter: ManagedImageRetouchAdapter | None
    audit_publisher: ManagedHTTPSAuditPublisher | None
    trace_exporter: ManagedOTLPHTTPTraceExporter | None
    connector_adapters: Mapping[str, ManagedConnectorGatewayAdapter]
    capability_packs: CapabilityPackRuntime
    update: ProductUpdateComposition
    server_settings: ProductServerSettings
    _cleanup: _ProductCompositionCleanup = field(repr=False, compare=False)

    def close_unstarted(self) -> None:
        self._cleanup.close_once()

    def transfer_to_app(self) -> None:
        self._cleanup.transfer()


@dataclass(frozen=True, slots=True)
class ActivationProbeComposition:
    install_root: Path
    slot: VerifiedRuntimeSlot
    config: ProductRuntimeConfig
    server_settings: ActivationProbeSettings

    def close_unstarted(self) -> None:
        return None

    def transfer_to_app(self) -> None:
        return None


def _production_device_broker(
    settings: DeviceAuthorizationConfig,
) -> DeviceAuthorizationBroker:
    return HTTPSDeviceAuthorizationBroker(
        settings.base_url,
        client_id=settings.client_id,
        allowed_hosts=settings.allowed_hosts,
        timeout_seconds=settings.timeout_seconds,
    )


def _production_reload_requester() -> DelayedRestartRequester:
    return DelayedRestartRequester(exit_code=RUNTIME_RELOAD_EXIT_CODE)


def load_product_runtime(
    *,
    payload_root: str | os.PathLike[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    environment: Mapping[str, str] | None = None,
    vault_factory: VaultFactory = production_credential_vault,
    device_broker_factory: DeviceBrokerFactory = _production_device_broker,
    reload_requester_factory: ReloadRequesterFactory = _production_reload_requester,
    pack_adapter_resolver: PackAdapterResolver | None = None,
    host_platform: str | None = None,
    host_architecture: str | None = None,
) -> ProductRuntimeComposition | ActivationProbeComposition:
    """Load and compose one product Runtime from the Bootstrap-selected slot.

    ``vault_factory`` and target overrides are dependency seams for platform
    conformance tests.  The production CLI never exposes them as arguments.
    """

    source_environment = os.environ if environment is None else environment
    if source_environment.get("ECOREX_BOOTSTRAPPED") != "1":
        raise ProductRuntimeTrustError(
            "Product Runtime must be launched by the signed Bootstrap"
        )
    try:
        endpoint = RuntimeEndpoint(host, port)
    except BootstrapConfigurationError:
        raise ProductRuntimeConfigurationError(
            "Product Runtime endpoint must be a literal loopback address"
        ) from None
    payload = _discover_payload_root(payload_root)
    slot_path = payload.parent
    slots_dir = slot_path.parent
    install_root = slots_dir.parent
    if slot_path.name.startswith(".") or slots_dir.name != "slots":
        raise ProductRuntimeTrustError("Runtime payload is outside the slot layout")
    _require_real_directory_tree(install_root)
    _require_real_directory_tree(payload, stop=install_root)

    config_path = payload / RUNTIME_CONFIG_FILE_NAME
    config_payload = _read_stable_file(
        config_path,
        max_bytes=MAX_RUNTIME_CONFIG_BYTES,
        label="Runtime configuration",
        stop=install_root,
    )
    try:
        config = ProductRuntimeConfig.from_bytes(config_payload)
    except ProductRuntimeConfigurationError:
        # This file is a member of the signed Core payload.  A malformed value
        # is therefore a broken/tampered slot, not a mutable user preference.
        raise ProductRuntimeTrustError(
            "Signed Runtime configuration is invalid"
        ) from None
    if host_platform is None or host_architecture is None:
        host_platform, host_architecture = _host_target()
    if (
        config.identity.platform != host_platform
        or config.identity.architecture != host_architecture
    ):
        raise ProductRuntimeTrustError("Runtime configuration targets a different host")

    release_verifier = Ed25519SignatureVerifier(config.release_public_keys)
    try:
        activation_launch = ActivationLaunchContext.from_environment(source_environment)
    except ActivationIntentError:
        raise ProductRuntimeTrustError(
            "Provisional activation environment is invalid"
        ) from None
    activation_controller = ProvisionalActivationController(
        install_root,
        verifier=release_verifier,
        host_platform=host_platform,
        host_architecture=host_architecture,
        pack_content_verifier=verify_product_capability_pack,
    )
    provisional = None
    try:
        if activation_launch is None:
            selected = CurrentSlotVerifier(
                SlotStore(install_root),
                verifier=release_verifier,
                host_platform=host_platform,
                host_architecture=host_architecture,
                pack_content_verifier=verify_product_capability_pack,
            ).verify_current()
        else:
            provisional = activation_controller.ensure_pending_current(
                activation_launch.transaction_id
            )
            if provisional is None:
                raise ActivationIntentError("provisional activation intent is missing")
            selected = VerifiedRuntimeSlot(
                slot_id=provisional.intent.slot_id,
                slot_path=provisional.slot_path,
                payload_root=provisional.payload_root,
                manifest=provisional.manifest,
                artifact=provisional.artifact,
            )
    except Exception:
        raise ProductRuntimeTrustError(
            "Runtime slot verification did not succeed"
        ) from None
    if selected.slot_path != slot_path.resolve(strict=True):
        raise ProductRuntimeTrustError(
            "Runtime process is not executing from the active slot"
        )
    _verify_runtime_identity(config, selected, release_verifier)

    database_path = _resolve_writable_file(
        install_root,
        config.paths.database,
        label="Runtime database",
    )
    web_root = _resolve_existing_directory(
        payload,
        config.paths.web_root,
        label="Web root",
    )
    web_manifest = _resolve_existing_file(
        payload,
        config.paths.web_manifest,
        label="Web manifest",
    )
    workspace_roots = tuple(
        _resolve_existing_directory(
            install_root,
            relative,
            label="Workspace root",
        )
        for relative in config.paths.workspace_roots
    )
    sandbox_security: WindowsSandboxSlotSecurity | None = None
    if host_platform == "windows":
        security_marker = (
            SlotStore(install_root).marker(selected.slot_id).get("security_provision")
        )
        if not isinstance(security_marker, Mapping):
            raise ProductRuntimeTrustError(
                "Runtime sandbox security provision is unavailable"
            )
        sandbox_security = WindowsSandboxSlotSecurity(
            install_root,
            install_root / "bootstrap" / "bin" / "ecorex-sandbox-host.exe",
            expected_helper_sha256=str(
                security_marker.get("provision_helper_sha256", "")
            ),
        )
        if not sandbox_security.validate(
            selected.slot_path,
            selected.manifest,
            selected.artifact,
            security_marker,
        ):
            raise ProductRuntimeTrustError(
                "Runtime sandbox security provision did not attest"
            )
    # Verify the entire exact Web allowlist before constructing any network
    # client. create_product_app repeats this check at the final handoff, which
    # also fences a mutation between composition and ASGI construction.
    try:
        load_verified_web_bundle(
            web_root=web_root,
            release_manifest_path=selected.slot_path / "release-manifest.json",
            web_manifest_path=web_manifest,
            trusted_public_keys=config.release_public_keys,
        )
    except Exception:
        raise ProductRuntimeTrustError(
            "Signed Web bundle verification did not succeed"
        ) from None

    if activation_launch is not None:
        assert provisional is not None
        if provisional.intent.health_identity.slot_id != selected.slot_id:
            raise ProductRuntimeTrustError(
                "Activation health identity does not match the selected slot"
            )
        # Bootstrap has already verified the exact signed Pack set immediately
        # before it starts this one-shot candidate. Do not repeat a complete
        # cold-disk Pack hash or construct the credential vault merely to
        # answer the nonce-bound probe: this process exposes no business
        # endpoint and is stopped after confirmation. The next, full Runtime
        # launch still verifies and binds every Pack before it can cross the
        # data barrier or serve user traffic.
        return ActivationProbeComposition(
            install_root=install_root,
            slot=selected,
            config=config,
            server_settings=ActivationProbeSettings(
                host=endpoint.host,
                port=endpoint.port,
                identity=provisional.intent.health_identity,
                nonce=activation_launch.nonce,
            ),
        )

    # Full Runtime startup validates every immutable capability binding and
    # platform vault implementation before it can cross the data barrier.
    try:
        vault = vault_factory()
    except Exception:
        raise ProductRuntimeConfigurationError(
            "Platform credential vault is unavailable",
            stage_code="credential_vault",
        ) from None
    if vault is None:
        raise ProductRuntimeConfigurationError(
            "Platform credential vault is unavailable",
            stage_code="credential_vault",
        )
    try:
        pack_runtime = load_verified_capability_packs(
            config,
            install_root=install_root,
            verifier=release_verifier,
            platform=host_platform,
            architecture=host_architecture,
            workspace_roots=workspace_roots,
            runtime_payload_root=payload,
            resolver=pack_adapter_resolver,
        )
    except ProductRuntimeConfigurationError as exc:
        if exc.stage_code is not None:
            raise
        raise ProductRuntimeConfigurationError(
            "Product Runtime capability Pack validation failed",
            stage_code="capability_pack_binding",
        ) from None
    except Exception:
        raise ProductRuntimeConfigurationError(
            "Product Runtime capability Pack validation failed",
            stage_code="capability_pack_binding",
        ) from None

    migration_manifest = _load_storage_migration_manifest(selected)
    migration_identity = _storage_migration_identity(selected)
    migration_receipt_root = database_path.parent / "migration-receipts"
    storage_schema_authorizer = _verified_applied_storage_schema_authorizer(
        database_path=database_path,
        receipt_root=migration_receipt_root,
        install_root=install_root,
        verifier=release_verifier,
        platform=host_platform,
        architecture=host_architecture,
    )
    try:
        legacy_migration = ProductLegacyMigrationCoordinator(
            install_root,
            database_path,
            vault=vault,
            storage_schema_authorizer=storage_schema_authorizer,
        )
        if legacy_migration.has_plan:
            # This full Runtime launch is still inside Bootstrap's confirmed
            # pre-data rollback window.  The import is independently dry-run,
            # verified and directory-swapped before the v1 data barrier.
            legacy_migration.commit(selected.slot_path)
    except (MigrationError, RuntimeError, OSError):
        raise ProductRuntimeConfigurationError(
            "Product Runtime legacy data migration failed",
            stage_code="legacy_data_migration",
        ) from None

    migration_preflight = None
    try:
        live_migration = load_live_storage_migration_receipt(
            database_path,
            manifest=migration_manifest,
            identity=migration_identity,
            receipt_root=migration_receipt_root,
        )
        if live_migration is None:
            migration_preflight = dry_run_storage_migration(
                database_path,
                manifest=migration_manifest,
                identity=migration_identity,
                receipt_root=migration_receipt_root,
                phase="live_preflight",
            )
    except StorageMigrationError:
        raise ProductRuntimeConfigurationError(
            "Product Runtime storage migration preflight failed",
            stage_code="storage_migration_preflight",
        ) from None

    # This is the durable point after which the newly confirmed slot may open
    # and migrate live storage. No rollback path is permitted beyond it. The
    # exact plan has already passed against a CoW snapshot above.
    try:
        activation_controller.mark_data_barrier_crossed(selected.slot_id)
    except Exception:
        # Pointer, journal, receipt, signature and filesystem failures are all
        # trust-boundary failures here.  None may fall through as a generic
        # process crash after a successful preflight because Bootstrap needs a
        # deterministic pre-data decision and must not expose native details.
        raise ProductRuntimeTrustError(
            "Activation data barrier could not be committed"
        ) from None
    if migration_preflight is not None:
        try:
            apply_live_storage_migration(
                database_path,
                manifest=migration_manifest,
                identity=migration_identity,
                receipt_root=migration_receipt_root,
                preflight=migration_preflight,
            )
        except StorageMigrationError:
            # The data barrier is already durable: Bootstrap must keep this
            # signed candidate selected and require a roll-forward repair.
            raise ProductRuntimeConfigurationError(
                "Product Runtime live storage migration failed",
                stage_code="storage_migration_live",
            ) from None
    if legacy_migration.has_completion:
        try:
            legacy_migration.cleanup_prior_state()
        except ProductMigrationError:
            # Retention cleanup is recoverable and cannot invalidate a data
            # barrier that is already durable.
            pass

    managed_session = ManagedSessionService(
        database_path,
        vault=vault,
        verifier=Ed25519SessionLeaseVerifier(config.session_public_keys),
        initialize=False,
    )
    # An absent/expired lease is an authenticated-capability state, not a
    # process-start condition. Runtime will project an unauthenticated
    # bootstrap and keep all model/update mutations closed until device login
    # installs a new signed lease. Recovering a valid data-scope is repeated by
    # Runtime composition, which also fences account changes to a reload.
    device_settings = config.device_authorization
    cleanup: list[Any] = []
    composition_stage = "device_authorization_broker"
    try:
        device_broker = device_broker_factory(device_settings)
        if device_broker is None:
            raise ProductRuntimeConfigurationError(
                "Managed device authorization could not be composed"
            )
        cleanup.append(device_broker)
        if any(
            not callable(getattr(device_broker, method, None))
            for method in ("begin", "poll", "refresh", "aclose")
        ):
            raise ProductRuntimeConfigurationError(
                "Managed device authorization could not be composed"
            )
        composition_stage = "device_authorization_service"
        device_authorization = ManagedDeviceAuthorizationService(
            database_path,
            session=managed_session,
            vault=vault,
            broker=device_broker,
            initialize=False,
        )
        composition_stage = "managed_session_refresh"
        session_refresh = ManagedSessionRefreshService(
            database_path,
            session=managed_session,
            broker=device_broker,
            initialize=False,
        )

        composition_stage = "update_runtime"
        update = _build_update(
            config,
            database_path=database_path,
            install_root=install_root,
            current_version=selected.manifest.version,
            platform=host_platform,
            architecture=host_architecture,
            credentials=managed_session,
            sandbox_security=sandbox_security,
            legacy_migration=legacy_migration,
            initialize=False,
            create_storage=False,
        )
        cleanup.extend((update.signal_source, update.feed, update.fetcher))
        composition_stage = "model_gateway"
        gateway = ManagedModelGatewayClient(
            config.gateway.endpoint,
            credentials=managed_session,
            allowed_hosts=config.gateway.allowed_hosts,
        )
        cleanup.append(gateway)
        image_orchestration_client: ManagedImageOrchestrationClient | None = None
        if config.image_orchestration is not None:
            composition_stage = "image_orchestration"
            image_orchestration_client = ManagedImageOrchestrationClient(
                config.image_orchestration.root_url,
                session=managed_session,
                allowed_hosts=config.image_orchestration.allowed_hosts,
                database_path=database_path,
            )
            cleanup.append(image_orchestration_client)
        share_publisher: HTTPSSharePublisher | None = None
        if config.share is not None:
            composition_stage = "share_publisher"
            share_publisher = HTTPSSharePublisher(
                config.share.endpoint,
                credentials=managed_session,
                allowed_hosts=config.share.allowed_hosts,
            )
            cleanup.append(share_publisher)
        retouch_adapter: ManagedImageRetouchAdapter | None = None
        if (
            image_orchestration_client is not None
            and "image" in pack_runtime.installed_pack_ids
        ):
            retouch_adapter = ManagedImageRetouchAdapter(image_orchestration_client)
        audit_publisher: ManagedHTTPSAuditPublisher | None = None
        if config.audit is not None:
            composition_stage = "audit_publisher"
            audit_publisher = ManagedHTTPSAuditPublisher(
                base_url=config.audit.endpoint,
                session=managed_session,
                allowed_hosts=config.audit.allowed_hosts,
            )
            cleanup.append(audit_publisher)
        trace_exporter: ManagedOTLPHTTPTraceExporter | None = None
        if config.tracing is not None:
            composition_stage = "trace_exporter"
            trace_exporter = ManagedOTLPHTTPTraceExporter(
                endpoint=config.tracing.endpoint,
                session=managed_session,
                allowed_hosts=config.tracing.allowed_hosts,
                max_request_bytes=config.tracing.max_request_bytes,
            )
            cleanup.append(trace_exporter)
        connector_adapters: dict[str, ManagedConnectorGatewayAdapter] = {}
        if config.connectors is not None:
            composition_stage = "connector_adapters"
            for connector_id in config.connectors.enabled_connectors:
                adapter = ManagedConnectorGatewayAdapter(
                    connector_id=connector_id,
                    endpoint=config.connectors.endpoint,
                    allowed_hosts=config.connectors.allowed_hosts,
                    session=managed_session,
                )
                connector_adapters[connector_id] = adapter
                cleanup.append(adapter)
        composition_stage = "session_reload_requester"
        session_reload_requester = reload_requester_factory()
        reload_callback = getattr(session_reload_requester, "request", None)
        if not callable(reload_callback):
            raise ProductRuntimeConfigurationError(
                "Session reload requester could not be composed"
            )
        composition_stage = "server_settings"
        settings = ProductServerSettings(
            database_path=database_path,
            web_root=web_root,
            release_manifest_path=selected.slot_path / "release-manifest.json",
            web_manifest_path=web_manifest,
            trusted_public_keys=config.release_public_keys,
            host=endpoint.host,
            port=endpoint.port,
            platform=config.identity.platform,
            architecture=config.identity.architecture,
            managed_session_service=managed_session,
            managed_session_refresh_service=session_refresh,
            managed_session_refresh_poll_seconds=30.0,
            device_authorization_service=device_authorization,
            device_authorization_poll_seconds=(device_settings.supervisor_poll_seconds),
            close_device_authorization_broker_on_shutdown=True,
            session_reload_requester=reload_callback,
            first_install_registration_recorder=(
                update.coordinator.record_registration_authority
            ),
            first_install_runtime_ready_recorder=(
                update.coordinator.mark_runtime_ready
            ),
            model_gateway=gateway,
            image_orchestration_client=image_orchestration_client,
            capability_pack_runtime=pack_runtime,
            workspace_roots=workspace_roots,
            output_roots=standard_output_roots(workspace_roots),
            output_default_location="documents",
            update_service=update.service,
            connector_vault=vault,
            connector_adapters=connector_adapters,
            share_publisher=share_publisher,
            share_public_hosts=(
                config.share.public_hosts if config.share is not None else frozenset()
            ),
            retouch_adapter=retouch_adapter,
            audit_publisher=audit_publisher,
            audit_dispatch_seconds=(
                config.audit.dispatch_seconds if config.audit is not None else 5.0
            ),
            audit_raw_retention_days=(
                config.audit.raw_retention_days if config.audit is not None else 30
            ),
            audit_aggregate_retention_days=(
                config.audit.aggregate_retention_days
                if config.audit is not None
                else 180
            ),
            trace_exporter=trace_exporter,
            trace_dispatch_seconds=(
                config.tracing.dispatch_seconds if config.tracing is not None else 5.0
            ),
            trace_max_spans_per_batch=(
                config.tracing.max_spans_per_batch if config.tracing is not None else 64
            ),
            trace_max_request_bytes=(
                config.tracing.max_request_bytes
                if config.tracing is not None
                else 1024 * 1024
            ),
            trace_retention_days=(
                config.tracing.retention_days if config.tracing is not None else 7
            ),
        )
    except ProductRuntimeConfigurationError as exc:
        _close_unstarted_resources(reversed(cleanup))
        if exc.stage_code is not None:
            raise
        raise ProductRuntimeConfigurationError(
            str(exc), stage_code=composition_stage
        ) from None
    except Exception:
        _close_unstarted_resources(reversed(cleanup))
        raise ProductRuntimeConfigurationError(
            f"Product Runtime dependency composition failed at {composition_stage}",
            stage_code=composition_stage,
        ) from None
    except BaseException:
        _close_unstarted_resources(reversed(cleanup))
        raise
    return ProductRuntimeComposition(
        install_root=install_root,
        slot=selected,
        config=config,
        managed_session=managed_session,
        device_authorization=device_authorization,
        session_reload_requester=session_reload_requester,
        gateway=gateway,
        share_publisher=share_publisher,
        image_orchestration_client=image_orchestration_client,
        retouch_adapter=retouch_adapter,
        audit_publisher=audit_publisher,
        trace_exporter=trace_exporter,
        connector_adapters=MappingProxyType(dict(connector_adapters)),
        capability_packs=pack_runtime,
        update=update,
        server_settings=settings,
        _cleanup=_ProductCompositionCleanup(tuple(cleanup)),
    )


def _close_unstarted_resources(resources: Any) -> None:
    seen: set[int] = set()
    for resource in resources:
        if resource is None:
            continue
        if id(resource) in seen:
            continue
        seen.add(id(resource))
        close = getattr(resource, "aclose", None)
        if not callable(close):
            close = getattr(resource, "close", None)
        if callable(close):
            try:
                result = close()
                if inspect.isawaitable(result):
                    _complete_async_cleanup(result)
            except Exception:
                pass


def _complete_async_cleanup(awaitable: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(awaitable)
        except Exception:
            pass
        return
    # load_product_runtime is synchronous. If a test/admin integration invokes
    # it from an active loop, schedule cleanup on that owner rather than trying
    # to nest an event loop or leaking the already-created coroutine.
    loop.create_task(awaitable)


def _verify_runtime_identity(
    config: ProductRuntimeConfig,
    slot: VerifiedRuntimeSlot,
    verifier: Ed25519SignatureVerifier,
) -> None:
    identity = config.identity
    manifest = slot.manifest
    if (
        identity.version != manifest.version
        or identity.platform != slot.artifact.platform
        or identity.architecture != slot.artifact.architecture
    ):
        raise ProductRuntimeTrustError(
            "Runtime configuration identity does not match the signed slot"
        )
    try:
        verify_manifest_signature(manifest, verifier)
        verify_artifact_signature(manifest, slot.artifact, verifier)
    except VerificationError:
        raise ProductRuntimeTrustError(
            "Runtime release signature verification failed"
        ) from None


def load_verified_capability_packs(
    config: ProductRuntimeConfig,
    *,
    install_root: Path,
    verifier: Ed25519SignatureVerifier,
    platform: str,
    architecture: str,
    workspace_roots: tuple[Path, ...],
    runtime_payload_root: Path,
    resolver: PackAdapterResolver | None,
) -> CapabilityPackRuntime:
    runtime = CapabilityPackRuntime(builtin_capability_registry())
    configured_ids = tuple(definition.pack_id for definition in config.capability_packs)
    if configured_ids not in {(), REQUIRED_CAPABILITY_PACK_IDS}:
        raise ProductRuntimeConfigurationError(
            "Product Runtime requires the complete required Capability Pack set"
        )
    if config.capability_packs and resolver is None:
        raise ProductRuntimeConfigurationError(
            "Configured Capability Packs have no trusted product adapter"
        )
    for definition in config.capability_packs:
        try:
            expected_prefix = (
                f"capability-packs/{definition.pack_id}/ecorex-capability-pack-"
                f"{definition.pack_id}-{platform}-{architecture}-{config.identity.version}"
            )
            if (
                definition.manifest != expected_prefix + ".json"
                or definition.artifact != expected_prefix + ".zip"
            ):
                raise ProductRuntimeConfigurationError(
                    "Capability Pack path does not match the active slot projection"
                )
            manifest_path = _resolve_existing_file(
                runtime_payload_root,
                definition.manifest,
                label="Capability Pack manifest",
            )
            artifact_path = _resolve_existing_file(
                runtime_payload_root,
                definition.artifact,
                label="Capability Pack artifact",
            )
            payload = _read_stable_file(
                manifest_path,
                max_bytes=256 * 1024,
                label="Capability Pack manifest",
                stop=runtime_payload_root,
            )
            manifest = CapabilityPackManifest.from_bytes(payload)
            if manifest.pack_id != definition.pack_id:
                raise ProductRuntimeConfigurationError(
                    "Capability Pack identity does not match Runtime configuration"
                )
            verified = verify_capability_pack(
                manifest,
                artifact_path,
                verifier=verifier,
                platform=platform,
                architecture=architecture,
                runtime_api_version=RUNTIME_API_VERSION,
            )
            handlers = (
                resolver(verified, workspace_roots, runtime_payload_root)
                if resolver is not None
                else {}
            )
            runtime.bind(verified, handlers)
        except ProductRuntimeConfigurationError as exc:
            if exc.stage_code is not None:
                raise
            raise ProductRuntimeConfigurationError(
                "Capability Pack validation failed",
                stage_code=f"capability_pack_{definition.pack_id}",
            ) from None
        except Exception:
            raise ProductRuntimeConfigurationError(
                "Capability Pack verification or binding failed",
                stage_code=f"capability_pack_{definition.pack_id}",
            ) from None
    return runtime


def _build_update(
    config: ProductRuntimeConfig,
    *,
    database_path: Path,
    install_root: Path,
    current_version: str,
    platform: str,
    architecture: str,
    credentials: ManagedSessionService,
    sandbox_security: WindowsSandboxSlotSecurity | None,
    legacy_migration: ProductLegacyMigrationCoordinator,
    initialize: bool = True,
    create_storage: bool | None = None,
) -> ProductUpdateComposition:
    return build_product_update_composition(
        ProductUpdateSettings(
            database_path=database_path,
            install_root=install_root,
            release_feed_endpoint=config.update.release_feed_endpoint,
            update_signal_endpoint=config.update.signal_endpoint,
            trusted_public_keys=config.release_public_keys,
            rollback_public_keys=config.rollback_public_keys,
            credentials=credentials,
            control_plane_hosts=config.update.control_plane_hosts,
            artifact_hosts=config.update.artifact_hosts,
            current_version=current_version,
            channel=config.update.channel,
            platform=platform,
            architecture=architecture,
            health_checker=_candidate_health_check,
            drainer=lambda: _runtime_is_drained(database_path),
            migration_dry_run=lambda candidate: (
                _migration_dry_run(
                    database_path,
                    candidate,
                    config=config,
                    install_root=install_root,
                    platform=platform,
                    architecture=architecture,
                )
                and legacy_migration.dry_run(candidate)
            ),
            migration_prepare=lambda candidate, transaction_id: legacy_migration.commit(
                candidate,
                transaction_id,
            ),
            poll_interval_seconds=config.update.poll_interval_seconds,
            pack_content_verifier=verify_product_capability_pack,
            payload_security_preparer=(
                sandbox_security.prepare if sandbox_security is not None else None
            ),
            payload_security_attester=(
                sandbox_security.attest if sandbox_security is not None else None
            ),
            payload_security_cleanup=(
                sandbox_security.cleanup_failed
                if sandbox_security is not None
                else None
            ),
            payload_security_orphan_cleanup=(
                sandbox_security.cleanup_abandoned
                if sandbox_security is not None
                else None
            ),
            slot_security_validator=(
                sandbox_security.validate if sandbox_security is not None else None
            ),
            slot_security_cleanup=(
                sandbox_security.cleanup_slot if sandbox_security is not None else None
            ),
        ),
        initialize=initialize,
        create_storage=create_storage,
    )


def _runtime_is_drained(database_path: Path) -> bool:
    if not database_path.exists():
        return True
    try:
        connection = sqlite3.connect(
            f"file:{database_path.as_posix()}?mode=ro",
            uri=True,
            timeout=1,
        )
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('leased','running')"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return row is not None and int(row[0]) == 0


def _candidate_health_check(slot_path: Path) -> bool:
    try:
        payload = _resolve_existing_directory(
            slot_path, "payload", label="candidate payload"
        )
        _read_stable_file(
            payload / RUNTIME_CONFIG_FILE_NAME,
            max_bytes=MAX_RUNTIME_CONFIG_BYTES,
            label="candidate Runtime configuration",
            stop=slot_path,
        )
        migration_payload = _read_stable_file(
            payload / STORAGE_MIGRATION_FILE_NAME,
            max_bytes=MAX_STORAGE_MIGRATION_BYTES,
            label="candidate storage migration manifest",
            stop=slot_path,
        )
        migration_manifest = StorageMigrationManifest.from_bytes(migration_payload)
        # Admission is performed by the currently installed Runtime against a
        # *future* signed candidate.  Requiring equality here would make every
        # real N -> N+1 database upgrade impossible before the candidate can be
        # activated.  Downgrades remain closed; the full candidate Runtime
        # later requires the plan target to equal its own compiled schema.
        if migration_manifest.target_schema_version < RUNTIME_STORAGE_SCHEMA_VERSION:
            return False
    except (ProductRuntimeConfigurationError, StorageMigrationError):
        return False
    return True


def _migration_dry_run(
    database_path: Path,
    candidate_slot: Path,
    *,
    config: ProductRuntimeConfig,
    install_root: Path,
    platform: str,
    architecture: str,
) -> bool:
    try:
        if not _candidate_health_check(candidate_slot):
            return False
        slots = SlotStore(install_root)
        candidate = candidate_slot.resolve(strict=True)
        slot_id = candidate.name
        if slots.slot_path(slot_id).resolve(strict=True) != candidate:
            return False
        manifest = slots.release_manifest(slot_id)
        verifier = Ed25519SignatureVerifier(config.release_public_keys)
        verify_manifest_signature(manifest, verifier)
        artifact = manifest.artifact(f"core-{platform}-{architecture}")
        verify_artifact_signature(manifest, artifact, verifier)
        if (
            slots.validate_receipt(
                slot_id=slot_id,
                manifest=manifest,
                artifact=artifact,
            ).resolve(strict=True)
            != candidate
        ):
            return False
        migration_manifest = _load_storage_migration_manifest_from_payload(
            candidate / "payload",
            stop=candidate,
            expected_target_schema_version=None,
        )
        if migration_manifest.target_schema_version < RUNTIME_STORAGE_SCHEMA_VERSION:
            return False
        identity = StorageMigrationIdentity(
            release_id=manifest.release_id,
            build_digest=manifest.build_digest,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
        )
        receipt = dry_run_storage_migration(
            database_path,
            manifest=migration_manifest,
            identity=identity,
            receipt_root=database_path.parent / "migration-receipts",
            phase="admission_dry_run",
        )
        return receipt.matches(
            identity=identity,
            manifest=migration_manifest,
            phase="admission_dry_run",
        )
    except Exception:
        return False


def _verified_applied_storage_schema_authorizer(
    *,
    database_path: Path,
    receipt_root: Path,
    install_root: Path,
    verifier: Ed25519SignatureVerifier,
    platform: str,
    architecture: str,
) -> Callable[[int, str], bool]:
    """Authorize only schema generations proven by an applied signed release.

    The selected candidate's pending migration plan is deliberately
    insufficient.  During an N -> N+1 launch, the database is still allowed
    by a verified live receipt from a retained N slot; only after N+1 applies
    and its live receipt matches the physical database can it authorize the
    successor generation.
    """

    slots = SlotStore(install_root)
    expected_artifact_id = f"core-{platform}-{architecture}"

    def authorize(observed_version: int, observed_schema_sha256: str) -> bool:
        if (
            isinstance(observed_version, bool)
            or not isinstance(observed_version, int)
            or observed_version < 1
            or not isinstance(observed_schema_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", observed_schema_sha256) is None
        ):
            return False
        try:
            before = slots.pointers()
            referenced = tuple(
                dict.fromkeys(
                    slot_id
                    for slot_id in (
                        before.current,
                        before.previous,
                        *before.known_good,
                    )
                    if slot_id is not None
                )
            )
            for slot_id in referenced:
                try:
                    manifest = slots.release_manifest(slot_id)
                    verify_manifest_signature(manifest, verifier)
                    artifact = manifest.artifact(expected_artifact_id)
                    verify_artifact_signature(manifest, artifact, verifier)
                    slot_path = slots.validate_receipt(
                        slot_id=slot_id,
                        manifest=manifest,
                        artifact=artifact,
                    )
                    migration_manifest = _load_storage_migration_manifest_from_payload(
                        slot_path / "payload",
                        stop=slot_path,
                        expected_target_schema_version=None,
                    )
                    identity = StorageMigrationIdentity(
                        release_id=manifest.release_id,
                        build_digest=manifest.build_digest,
                        artifact_id=artifact.artifact_id,
                        artifact_sha256=artifact.sha256,
                    )
                    receipt = load_live_storage_migration_receipt(
                        database_path,
                        manifest=migration_manifest,
                        identity=identity,
                        receipt_root=receipt_root,
                    )
                except Exception:
                    continue
                if (
                    receipt is not None
                    and receipt.target_schema_version == observed_version
                    and receipt.target_schema_sha256 == observed_schema_sha256
                    and slots.pointers() == before
                ):
                    return True
            return False
        except Exception:
            return False

    return authorize


def _load_storage_migration_manifest(
    slot: VerifiedRuntimeSlot,
) -> StorageMigrationManifest:
    try:
        return _load_storage_migration_manifest_from_payload(
            slot.payload_root, stop=slot.slot_path
        )
    except (ProductRuntimeConfigurationError, StorageMigrationError):
        raise ProductRuntimeTrustError(
            "Signed storage migration manifest is invalid"
        ) from None


def _load_storage_migration_manifest_from_payload(
    payload: Path,
    *,
    stop: Path,
    expected_target_schema_version: int | None = RUNTIME_STORAGE_SCHEMA_VERSION,
) -> StorageMigrationManifest:
    migration_payload = _read_stable_file(
        payload / STORAGE_MIGRATION_FILE_NAME,
        max_bytes=MAX_STORAGE_MIGRATION_BYTES,
        label="storage migration manifest",
        stop=stop,
    )
    manifest = StorageMigrationManifest.from_bytes(migration_payload)
    if manifest.target_schema_sha256 is None:
        raise StorageMigrationError(
            "storage migration manifest has no signed target schema digest"
        )
    if (
        expected_target_schema_version is not None
        and manifest.target_schema_version != expected_target_schema_version
    ):
        raise StorageMigrationError(
            "storage migration target does not match this Runtime"
        )
    if (
        expected_target_schema_version is not None
        and manifest.target_schema_sha256 != current_storage_schema_sha256()
    ):
        raise StorageMigrationError(
            "storage migration target schema does not match this Runtime"
        )
    return manifest


def _storage_migration_identity(
    slot: VerifiedRuntimeSlot,
) -> StorageMigrationIdentity:
    return StorageMigrationIdentity(
        release_id=slot.manifest.release_id,
        build_digest=slot.manifest.build_digest,
        artifact_id=slot.artifact.artifact_id,
        artifact_sha256=slot.artifact.sha256,
    )


def _discover_payload_root(value: str | os.PathLike[str] | None) -> Path:
    try:
        candidate = Path.cwd() if value is None else Path(value)
        absolute = Path(os.path.abspath(candidate))
        metadata = absolute.lstat()
    except (OSError, TypeError, ValueError):
        raise ProductRuntimeTrustError("Runtime payload root is unavailable") from None
    if absolute.name != "payload" or not stat_module.S_ISDIR(metadata.st_mode):
        raise ProductRuntimeTrustError(
            "Runtime payload root is outside the slot layout"
        )
    return absolute


def _host_target() -> tuple[str, str]:
    from ecorex.bootstrap import detect_host_target

    platform, architecture = detect_host_target()
    if platform not in {"windows", "macos"} or architecture not in {"x64", "arm64"}:
        raise ProductRuntimeConfigurationError("Product Runtime host is unsupported")
    if platform == "windows" and architecture != "x64":
        raise ProductRuntimeConfigurationError("Product Runtime host is unsupported")
    return platform, architecture


def _resolve_writable_file(root: Path, relative: str, *, label: str) -> Path:
    path = _join_under(root, relative, label=label)
    _require_real_directory_tree(path.parent, stop=root)
    if os.path.lexists(path):
        _require_regular(path, label=label)
    return path


def _resolve_existing_file(root: Path, relative: str, *, label: str) -> Path:
    path = _join_under(root, relative, label=label)
    _require_real_directory_tree(path.parent, stop=root)
    _require_regular(path, label=label)
    return path.resolve(strict=True)


def _resolve_existing_directory(root: Path, relative: str, *, label: str) -> Path:
    path = _join_under(root, relative, label=label)
    _require_real_directory_tree(path, stop=root)
    return path.resolve(strict=True)


def _join_under(root: Path, relative: str, *, label: str) -> Path:
    _validate_relative(relative, label)
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    try:
        candidate.absolute().relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise ProductRuntimeConfigurationError(
            f"{label} escapes its trusted root"
        ) from None
    return candidate


def _read_stable_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    stop: Path,
) -> bytes:
    _require_real_directory_tree(path.parent, stop=stop)
    before = _require_regular(path, label=label)
    if not 1 <= before.st_size <= max_bytes:
        raise ProductRuntimeConfigurationError(f"{label} size is invalid")
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ProductRuntimeConfigurationError(f"{label} changed while opening")
            payload = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
        current = _require_regular(path, label=label)
    except ProductRuntimeConfigurationError:
        raise
    except OSError:
        raise ProductRuntimeConfigurationError(f"{label} is unreadable") from None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        len(payload) != before.st_size
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != identity
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity
        or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        != identity
    ):
        raise ProductRuntimeConfigurationError(f"{label} changed while reading")
    return payload


def _require_regular(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise ProductRuntimeConfigurationError(f"{label} is missing") from None
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat_module.S_ISREG(metadata.st_mode)
        or stat_module.S_ISLNK(metadata.st_mode)
        or bool(attributes & reparse)
        or getattr(metadata, "st_nlink", 1) != 1
    ):
        raise ProductRuntimeConfigurationError(f"{label} must be a real regular file")
    return metadata


def _require_real_directory_tree(path: Path, *, stop: Path | None = None) -> None:
    stop_resolved = stop.resolve(strict=True) if stop is not None else None
    current = path
    while True:
        try:
            metadata = current.lstat()
        except OSError:
            raise ProductRuntimeConfigurationError(
                "Runtime path ancestry is unavailable"
            ) from None
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat_module.S_ISDIR(metadata.st_mode)
            or stat_module.S_ISLNK(metadata.st_mode)
            or bool(attributes & reparse)
        ):
            raise ProductRuntimeConfigurationError(
                "Runtime path ancestry contains a link or reparse point"
            )
        if stop_resolved is not None and current.resolve(strict=True) == stop_resolved:
            return
        if current.parent == current:
            if stop_resolved is not None:
                raise ProductRuntimeConfigurationError(
                    "Runtime path ancestry escapes its trusted root"
                )
            return
        current = current.parent


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_sensitive_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or _SENSITIVE_FIELD.search(key):
                raise ProductRuntimeConfigurationError(
                    "Runtime configuration cannot contain credential fields"
                )
            _reject_sensitive_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_fields(nested)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ProductRuntimeConfigurationError(
            f"{label} contains missing or unknown fields"
        )


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise ProductRuntimeConfigurationError(f"{key} must be an object")
    return result


def _string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if (
        not isinstance(result, str)
        or not result
        or len(result) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
    ):
        raise ProductRuntimeConfigurationError(f"{key} must be a safe non-empty string")
    return result


def _integer(value: Mapping[str, Any], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ProductRuntimeConfigurationError(f"{key} must be an integer")
    return result


def _number(value: Mapping[str, Any], key: str) -> float:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ProductRuntimeConfigurationError(f"{key} must be a number")
    return float(result)


def _safe_id(value: Mapping[str, Any], key: str) -> str:
    result = _string(value, key)
    if not _SAFE_ID.fullmatch(result):
        raise ProductRuntimeConfigurationError(f"{key} is unsafe")
    return result


def _semver(value: Mapping[str, Any], key: str) -> str:
    result = _string(value, key)
    if not _SEMVER.fullmatch(result):
        raise ProductRuntimeConfigurationError(f"{key} is not SemVer")
    return result


def _sha256(value: Mapping[str, Any], key: str) -> str:
    result = _string(value, key).casefold()
    if not _SHA256.fullmatch(result):
        raise ProductRuntimeConfigurationError(f"{key} is not a SHA-256 digest")
    return result


def _choice(value: Mapping[str, Any], key: str, choices: set[str]) -> str:
    result = _string(value, key)
    if result not in choices:
        raise ProductRuntimeConfigurationError(f"{key} is unsupported")
    return result


def _relative_path(value: Mapping[str, Any], key: str) -> str:
    result = _string(value, key)
    _validate_relative(result, key)
    return result


def _relative_path_array(
    value: Mapping[str, Any], key: str, *, maximum: int
) -> tuple[str, ...]:
    raw = value.get(key)
    if not isinstance(raw, list) or len(raw) > maximum:
        raise ProductRuntimeConfigurationError(f"{key} must be a bounded array")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ProductRuntimeConfigurationError(f"{key} must contain paths")
        _validate_relative(item, key)
        result.append(item)
    if len(result) != len(set(path.casefold() for path in result)):
        raise ProductRuntimeConfigurationError(f"{key} contains duplicate paths")
    return tuple(result)


def _validate_relative(value: str, label: str) -> None:
    if (
        not value
        or len(value.encode("utf-8")) > 1024
        or "\\" in value
        or "\x00" in value
        or ":" in value
    ):
        raise ProductRuntimeConfigurationError(
            f"{label} is not a portable relative path"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or any(
        part in {"", ".", ".."} or part.startswith(".") for part in path.parts
    ):
        raise ProductRuntimeConfigurationError(
            f"{label} is not a portable relative path"
        )


def _optional_share_service(value: Any) -> ShareServiceConfig | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProductRuntimeConfigurationError("share must be an object or null")
    _exact_keys(
        value,
        {"endpoint", "allowed_hosts", "public_hosts"},
        "Share service",
    )
    allowed_hosts = _hosts(value, "allowed_hosts")
    endpoint = _https_endpoint(
        value,
        "endpoint",
        allowed_hosts=allowed_hosts,
        websocket=False,
    ).rstrip("/")
    if urlsplit(endpoint).path != "/api/v1/shares":
        raise ProductRuntimeConfigurationError(
            "share endpoint must use the v1 ShareSnapshot route"
        )
    return ShareServiceConfig(
        endpoint=endpoint,
        allowed_hosts=allowed_hosts,
        public_hosts=_hosts(value, "public_hosts"),
    )


def _optional_image_orchestration(value: Any) -> ImageOrchestrationConfig | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProductRuntimeConfigurationError(
            "image_orchestration must be an object or null"
        )
    _exact_keys(
        value,
        {"root_url", "allowed_hosts"},
        "Image orchestration service",
    )
    allowed_hosts = _hosts(value, "allowed_hosts")
    root_url = _https_endpoint(
        value,
        "root_url",
        allowed_hosts=allowed_hosts,
        websocket=False,
    ).rstrip("/")
    if urlsplit(root_url).path != "/api/v1/images":
        raise ProductRuntimeConfigurationError(
            "image orchestration must use the unified v1 image root"
        )
    return ImageOrchestrationConfig(
        root_url=root_url,
        allowed_hosts=allowed_hosts,
    )


def _optional_audit_service(value: Any) -> AuditServiceConfig | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProductRuntimeConfigurationError("audit must be an object or null")
    _exact_keys(
        value,
        {
            "endpoint",
            "allowed_hosts",
            "dispatch_seconds",
            "raw_retention_days",
            "aggregate_retention_days",
        },
        "Audit service",
    )
    allowed_hosts = _hosts(value, "allowed_hosts")
    endpoint = _https_endpoint(
        value,
        "endpoint",
        allowed_hosts=allowed_hosts,
        websocket=False,
    ).rstrip("/")
    if urlsplit(endpoint).path != "/api/v1/audit/records":
        raise ProductRuntimeConfigurationError(
            "audit endpoint must use the v1 audit ingestion route"
        )
    dispatch_seconds = _number(value, "dispatch_seconds")
    if not 0.1 <= dispatch_seconds <= 300:
        raise ProductRuntimeConfigurationError(
            "audit dispatch interval is outside its product bound"
        )
    raw_retention_days = _integer(value, "raw_retention_days")
    aggregate_retention_days = _integer(value, "aggregate_retention_days")
    if not 1 <= raw_retention_days <= 30:
        raise ProductRuntimeConfigurationError(
            "audit raw retention must be between one and 30 days"
        )
    if not raw_retention_days <= aggregate_retention_days <= 180:
        raise ProductRuntimeConfigurationError(
            "audit aggregate retention must be between raw retention and 180 days"
        )
    return AuditServiceConfig(
        endpoint=endpoint,
        allowed_hosts=allowed_hosts,
        dispatch_seconds=dispatch_seconds,
        raw_retention_days=raw_retention_days,
        aggregate_retention_days=aggregate_retention_days,
    )


def _optional_trace_service(value: Any) -> TraceServiceConfig | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProductRuntimeConfigurationError("tracing must be an object or null")
    _exact_keys(
        value,
        {
            "endpoint",
            "allowed_hosts",
            "dispatch_seconds",
            "max_spans_per_batch",
            "max_request_bytes",
            "retention_days",
        },
        "Trace service",
    )
    allowed_hosts = _hosts(value, "allowed_hosts")
    endpoint = _https_endpoint(
        value,
        "endpoint",
        allowed_hosts=allowed_hosts,
        websocket=False,
    ).rstrip("/")
    if urlsplit(endpoint).path != "/v1/traces":
        raise ProductRuntimeConfigurationError(
            "tracing endpoint must use the OTLP /v1/traces route"
        )
    dispatch_seconds = _number(value, "dispatch_seconds")
    if not 0.1 <= dispatch_seconds <= 300:
        raise ProductRuntimeConfigurationError(
            "trace dispatch interval is outside its product bound"
        )
    max_spans_per_batch = _integer(value, "max_spans_per_batch")
    if not 1 <= max_spans_per_batch <= 512:
        raise ProductRuntimeConfigurationError(
            "trace span batch size is outside its product bound"
        )
    max_request_bytes = _integer(value, "max_request_bytes")
    if not 16 * 1024 <= max_request_bytes <= 8 * 1024 * 1024:
        raise ProductRuntimeConfigurationError(
            "trace request size is outside its product bound"
        )
    retention_days = _integer(value, "retention_days")
    if not 1 <= retention_days <= 30:
        raise ProductRuntimeConfigurationError(
            "trace retention must be between one and 30 days"
        )
    return TraceServiceConfig(
        endpoint=endpoint,
        allowed_hosts=allowed_hosts,
        dispatch_seconds=dispatch_seconds,
        max_spans_per_batch=max_spans_per_batch,
        max_request_bytes=max_request_bytes,
        retention_days=retention_days,
    )


def _optional_connector_service(value: Any) -> ConnectorServiceConfig | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProductRuntimeConfigurationError("connectors must be an object or null")
    _exact_keys(
        value,
        {"endpoint", "allowed_hosts", "enabled_connectors"},
        "Connector service",
    )
    allowed_hosts = _hosts(value, "allowed_hosts")
    endpoint = _https_endpoint(
        value,
        "endpoint",
        allowed_hosts=allowed_hosts,
        websocket=False,
    ).rstrip("/")
    if urlsplit(endpoint).path != "/api/v1/connectors":
        raise ProductRuntimeConfigurationError(
            "connector endpoint must use the v1 managed connector root"
        )
    raw_enabled = value.get("enabled_connectors")
    supported = {"feishu", "tencent-docs"}
    if (
        not isinstance(raw_enabled, list)
        or not 1 <= len(raw_enabled) <= len(supported)
        or any(
            not isinstance(item, str) or item not in supported for item in raw_enabled
        )
        or raw_enabled != sorted(raw_enabled)
        or len(raw_enabled) != len(set(raw_enabled))
    ):
        raise ProductRuntimeConfigurationError(
            "enabled connectors must be a unique sorted stable connector list"
        )
    return ConnectorServiceConfig(
        endpoint=endpoint,
        allowed_hosts=allowed_hosts,
        enabled_connectors=tuple(raw_enabled),
    )


def _hosts(value: Mapping[str, Any], key: str) -> frozenset[str]:
    raw = value.get(key)
    if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
        raise ProductRuntimeConfigurationError(
            f"{key} must be a non-empty bounded array"
        )
    hosts: set[str] = set()
    for item in raw:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > 253
            or item != item.casefold()
            or ":" in item
            or "/" in item
            or item.startswith(".")
            or item.endswith(".")
        ):
            raise ProductRuntimeConfigurationError(f"{key} contains an invalid host")
        hosts.add(item)
    if len(hosts) != len(raw):
        raise ProductRuntimeConfigurationError(f"{key} contains duplicate hosts")
    return frozenset(hosts)


def _https_endpoint(
    value: Mapping[str, Any],
    key: str,
    *,
    allowed_hosts: frozenset[str],
    websocket: bool,
) -> str:
    result = _string(value, key)
    parsed = urlsplit(result)
    expected_scheme = "wss" if websocket else "https"
    if (
        parsed.scheme != expected_scheme
        or not parsed.hostname
        or parsed.hostname.casefold() not in allowed_hosts
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (not parsed.path or parsed.path == "/")
    ):
        raise ProductRuntimeConfigurationError(
            f"{key} must be a credential-free allowlisted {expected_scheme.upper()} URL"
        )
    return result


def _https_origin(
    value: Mapping[str, Any],
    key: str,
    *,
    allowed_hosts: frozenset[str],
) -> str:
    result = _string(value, key).rstrip("/")
    parsed = urlsplit(result)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.casefold() not in allowed_hosts
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProductRuntimeConfigurationError(
            f"{key} must be a credential-free allowlisted HTTPS origin"
        )
    return result


def _public_keys(value: Mapping[str, Any], key: str) -> Mapping[str, bytes]:
    raw = value.get(key)
    if not isinstance(raw, Mapping) or not 1 <= len(raw) <= 32:
        raise ProductRuntimeConfigurationError(f"{key} must contain signing keys")
    decoded: dict[str, bytes] = {}
    for key_id, encoded in raw.items():
        if not isinstance(key_id, str) or not _SAFE_ID.fullmatch(key_id):
            raise ProductRuntimeConfigurationError(f"{key} contains an unsafe key id")
        if not isinstance(encoded, str) or len(encoded) > 128:
            raise ProductRuntimeConfigurationError(
                f"{key} contains an invalid public key"
            )
        try:
            public_key = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ProductRuntimeConfigurationError(
                f"{key} contains an invalid public key"
            ) from None
        if len(public_key) != 32:
            raise ProductRuntimeConfigurationError(
                f"{key} contains an invalid public key"
            )
        decoded[key_id] = public_key
    return MappingProxyType(dict(sorted(decoded.items())))


__all__ = [
    "ActivationProbeComposition",
    "CapabilityPackConfig",
    "DeviceAuthorizationConfig",
    "GatewayConfig",
    "MAX_RUNTIME_CONFIG_BYTES",
    "PackAdapterResolver",
    "ProductRuntimeComposition",
    "ProductRuntimeConfig",
    "ProductRuntimeConfigurationError",
    "ProductRuntimeTrustError",
    "ImageOrchestrationConfig",
    "RUNTIME_API_VERSION",
    "RUNTIME_CONFIG_FILE_NAME",
    "RUNTIME_CONFIG_SCHEMA_VERSION",
    "RuntimeIdentityConfig",
    "RuntimePathsConfig",
    "ShareServiceConfig",
    "TraceServiceConfig",
    "UpdateConfig",
    "load_product_runtime",
    "load_verified_capability_packs",
]
