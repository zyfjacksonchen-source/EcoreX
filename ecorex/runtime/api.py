"""FastAPI adapter for the local EcoreX v1 runtime."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import inspect
import json
import os
import platform as platform_module
import re
import secrets
import sys
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ecorex import __version__
from ecorex.artifacts import (
    ArtifactActionExecutor,
    ArtifactActionUnavailable,
    ArtifactLauncher,
    ArtifactService,
)
from ecorex.artifacts.api import create_artifact_router
from ecorex.capabilities import (
    CapabilityIntentError,
    ManagedModelCatalog,
    ManagedModelSpec,
    ModelCatalogError,
    ModelModality,
    RuntimeAvailability,
    builtin_model_catalog,
)
from ecorex.connectors import (
    ConnectorAuthKind,
    ConnectorComposition,
    ConnectorError,
    RejectingCredentialVault,
    build_connector_composition,
    production_credential_vault,
)
from ecorex.gateway import (
    GatewayAccountUsageProjection,
    ManagedModelGatewayClient,
    ModelGateway,
)
from ecorex.ids import is_id
from ecorex.extensions.api import register_extension_routes
from ecorex.extensions.local_bundle import LocalSkillBundleStore
from ecorex.extensions.repository import SQLiteExtensionRepository
from ecorex.extensions.service import ExtensionService
from ecorex.integration import (
    ArtifactEventOutbox,
    ArtifactEventOutboxSupervisor,
    CloudImageRetouchAdapter,
    ManagedImageOrchestrationClient,
    RetouchCoordinator,
    RetouchWorker,
    RetouchWorkerSupervisor,
    RuntimeArtifactEventPublisher,
    RuntimeConnectorEventSink,
    RuntimeConnectorResultCoordinator,
    RuntimeImageToolBackend,
    RuntimeRetouchBridge,
)
from ecorex.memory import MemoryService, create_memory_router
from ecorex.input_attachments import (
    InputAttachmentConflict,
    InputAttachmentError,
    InputAttachmentService,
    InputAttachmentUnavailable,
    MAX_INPUT_ATTACHMENT_BYTES,
)
from ecorex.migration import (
    MigrationQuarantineService,
    create_migration_quarantine_router,
)
from ecorex.output import OutputService, create_output_router
from ecorex.projects import (
    FolderPicker,
    ProjectFolderSelectionCancelled,
    ProjectNotFound,
    ProjectService,
    pick_project_folder,
)
from ecorex.observability import (
    AuditDispatcher,
    AuditError,
    AuditIntegrityError,
    AuditOutbox,
    AuditPayloadCipher,
    AuditRetentionPolicy,
    TraceDispatcher,
    TraceOutbox,
    TraceProjector,
    RuntimeSignalRegistry,
    SystemObservabilityService,
    SystemObservabilitySupervisor,
    create_system_observability_router,
)
from ecorex.protocol import (
    AuditDrainRequest,
    AuditDrainResponse,
    AuditListResponse,
    AuditRetentionResponse,
    BootstrapResponse,
    ActivateUpdateRequest,
    ActivateUpdateResponse,
    CheckUpdateResponse,
    ConnectorLoginBeginResponse,
    ConnectorLoginCancelResponse,
    ConnectorLoginCheckResponse,
    ConnectorDescriptor,
    ConversationUsageProjection,
    CreateThreadRequest,
    CreateTurnRequest,
    EventListResponse,
    ExtensionCatalogSnapshot,
    ForkThreadRequest,
    InteractionListResponse,
    InteractionActionType,
    ConnectorInteractionState,
    InteractionKind,
    InteractionMutationResponse,
    InteractionStatus,
    InterruptTurnRequest,
    InputAttachmentProjection,
    LoginSnapshot,
    LiveReplayRequest,
    LiveReplayResponse,
    LogoutSessionRequest,
    LogoutSessionResponse,
    ModelCatalog,
    ModelDescriptor,
    ModelServiceSnapshot,
    MockReplayResponse,
    PermissionMutationResponse,
    PermissionSnapshot,
    PickProjectFolderRequest,
    PolicyLeaseSnapshot,
    ProjectListResponse,
    ProjectProjection,
    QueueTurnRequest,
    RenameThreadRequest,
    QuotaSnapshot,
    ReplaceTurnRequest,
    ReplaceTurnResponse,
    RespondInteractionRequest,
    SteerTurnRequest,
    ThreadListResponse,
    ThreadPinRequest,
    ThreadProjection,
    ThreadProjectionResponse,
    ThreadStatus,
    ThreadStatusRequest,
    TokenUsageWindow,
    TraceProjectionResponse,
    TurnMutationResponse,
    UpdateSnapshot,
    UpdatePermissionRequest,
)
from ecorex.protocol.models import utc_now
from ecorex.replay import ReplayIntegrityError, ReplayService
from ecorex.sharing import (
    ShareOperationWorker,
    SharePublisher,
    ShareRepository,
    ShareSnapshotService,
    ShareWorkerSupervisor,
    create_share_router,
)
from ecorex.session import (
    DeviceAuthorizationConflict,
    DeviceAuthorizationUnavailable,
    DeviceAuthorizationSupervisor,
    ManagedDeviceAuthorizationService,
    ManagedSessionError,
    ManagedSessionService,
    ManagedSessionRefreshService,
    ManagedSessionRefreshSupervisor,
    ManagedSessionSnapshot,
    SessionConflict,
    SessionRestartRequired,
    SessionUnavailable,
    create_device_authorization_router,
)

from .errors import ConflictError, NotFoundError, RuntimeDomainError
from .activation_drain import RuntimeActivationDrainController
from .ids import new_id
from .interaction_maintenance import InteractionMaintenanceSupervisor
from .database import transaction_commit_guard
from .invariant_guard import (
    RuntimeExecutionDenied,
    RuntimeExecutionGate,
    RuntimeInvariantSupervisor,
)
from .recovery_gate import (
    RecoveryExecutionDenied,
    RecoveryExecutionGate,
    RecoveryExecutionPermit,
    RecoveryExecutionScope,
)
from .kernel import RuntimeKernel
from .permissions import PermissionAuthority
from .shutdown import stop_service_phases_isolated
from .supervisor import AgentWorkerSupervisor
from .usage import UsageProjectionService
from .worker import AgentTurnWorker
from .composition import (
    RuntimeComposition,
    project_connector_catalog,
    project_model_catalog,
)


@dataclass(slots=True)
class RuntimeSettings:
    database_path: str | Path
    product_version: str = __version__
    account_id: str = "local-user"
    account_display_name: str = "EcoreX User"
    authenticated: bool = True
    require_managed_session: bool = False
    allow_unmanaged_model_gateway_for_testing: bool = False
    managed_session_service: ManagedSessionService | None = field(
        default=None, repr=False
    )
    managed_session_refresh_service: ManagedSessionRefreshService | None = field(
        default=None, repr=False
    )
    managed_session_refresh_poll_seconds: float = 30.0
    session_reload_requester: Any | None = field(default=None, repr=False)
    first_install_registration_recorder: Any | None = field(default=None, repr=False)
    first_install_runtime_ready_recorder: Any | None = field(default=None, repr=False)
    device_authorization_service: ManagedDeviceAuthorizationService | None = field(
        default=None, repr=False
    )
    device_authorization_poll_seconds: float = 1.0
    close_device_authorization_broker_on_shutdown: bool = True
    full_access: bool = False
    admin_hard_denies: list[str] = field(default_factory=list)
    runtime_bearer_token: str | None = field(default=None, repr=False)
    csrf_token: str | None = field(default=None, repr=False)
    webui_origins: tuple[str, ...] = ("http://127.0.0.1:8765", "http://localhost:8765")
    event_poll_interval_seconds: float = 0.04
    event_idle_poll_interval_seconds: float = 0.25
    event_notification_fallback_seconds: float = 1.0
    sse_keepalive_seconds: float = 15.0
    # Calendar windows in the Composer are product-facing usage facts, not a
    # browser-locale guess. Deployments can bind this to an account preference.
    usage_timezone: str = "Asia/Shanghai"
    platform: str = field(default_factory=lambda: sys.platform)
    architecture: str = field(default_factory=platform_module.machine)
    extension_service: ExtensionService | None = field(default=None, repr=False)
    installed_capability_packs: frozenset[str] = frozenset()
    disabled_capability_tools: Mapping[str, str] = field(
        default_factory=dict, repr=False
    )
    capability_sandbox_profile_availability: Mapping[str, Mapping[str, str | None]] = (
        field(default_factory=dict, repr=False)
    )
    connected_connectors: frozenset[str] = frozenset()
    online: bool = True
    artifact_root: str | Path | None = None
    output_roots: Mapping[str, str | Path] | None = field(default=None, repr=False)
    output_default_location: str = "workspace"
    artifact_action_launcher: ArtifactLauncher | None = field(default=None, repr=False)
    model_gateway: ModelGateway | None = field(default=None, repr=False)
    image_orchestration_client: ManagedImageOrchestrationClient | None = field(
        default=None, repr=False
    )
    close_image_orchestration_client_on_shutdown: bool = True
    capability_handlers: Mapping[str, Any] = field(default_factory=dict, repr=False)
    mcp_runtime_bindings: tuple[Any, ...] = field(default_factory=tuple, repr=False)
    model_worker_concurrency: int = 2
    model_worker_poll_seconds: float = 0.25
    model_worker_shutdown_seconds: float = 5.0
    interaction_maintenance_seconds: float = 1.0
    interaction_maintenance_shutdown_seconds: float = 5.0
    invariant_audit_seconds: float = 60.0
    invariant_audit_timeout_seconds: float = 30.0
    invariant_shutdown_seconds: float = 5.0
    lifecycle_shutdown_seconds: float = 5.0
    close_model_gateway_on_shutdown: bool = True
    update_service: Any | None = field(default=None, repr=False)
    update_drain_timeout_seconds: float = 120.0
    update_drain_poll_seconds: float = 0.05
    connector_adapters: Mapping[str, Any] = field(default_factory=dict, repr=False)
    close_connector_adapters_on_shutdown: bool = True
    connector_vault: Any | None = field(default=None, repr=False)
    connector_oauth_return_uri: str | None = None
    connector_maintenance_seconds: float = 15.0
    share_publisher: SharePublisher | None = field(default=None, repr=False)
    share_public_hosts: frozenset[str] = frozenset()
    share_worker_concurrency: int = 1
    share_worker_poll_seconds: float = 0.25
    share_worker_shutdown_seconds: float = 5.0
    share_worker_lease_seconds: int = 30
    share_worker_retry_seconds: int = 2
    share_worker_max_attempts: int = 3
    share_operation_deadline_seconds: int = 3600
    retouch_adapter: CloudImageRetouchAdapter | None = field(default=None, repr=False)
    retouch_worker_concurrency: int = 1
    retouch_worker_poll_seconds: float = 0.25
    retouch_worker_shutdown_seconds: float = 5.0
    close_retouch_adapter_on_shutdown: bool = True
    audit_publisher: Any | None = field(default=None, repr=False)
    close_audit_publisher_on_shutdown: bool = True
    audit_encryption_key: bytes | None = field(default=None, repr=False)
    audit_raw_retention_days: int = 30
    audit_aggregate_retention_days: int = 180
    audit_dispatch_seconds: float = 5.0
    trace_exporter: Any | None = field(default=None, repr=False)
    close_trace_exporter_on_shutdown: bool = True
    trace_dispatch_seconds: float = 5.0
    trace_max_spans_per_batch: int = 64
    trace_max_request_bytes: int = 1024 * 1024
    trace_retention_days: int = 7
    project_folder_picker: FolderPicker = field(default=pick_project_folder, repr=False)


class _ManagedSessionRestartRequired(RuntimeError):
    pass


def _session_binding(snapshot: ManagedSessionSnapshot) -> tuple[object, ...]:
    return (
        snapshot.account_id,
        snapshot.organization_id,
        frozenset(snapshot.allowed_model_ids),
        frozenset(snapshot.admin_denies),
    )


def _first_install_registration(
    snapshot: ManagedSessionSnapshot,
) -> dict[str, Any]:
    """Project only the signed lease identity needed by Bootstrap handoff."""

    return {
        "account_id": snapshot.account_id,
        "organization_id": snapshot.organization_id,
        "lease_id": snapshot.lease_id,
        "lease_digest": snapshot.lease_digest,
        "session_generation": snapshot.generation,
        "lease_revision": snapshot.revision,
    }


def _filter_model_catalog(
    catalog: ManagedModelCatalog,
    allowed_model_ids: frozenset[str],
) -> ManagedModelCatalog | None:
    models: dict[str, ManagedModelSpec] = {}
    for modality in ModelModality:
        for model in catalog.for_modality(modality):
            if model.model_id in allowed_model_ids:
                models[model.model_id] = model
    if not models:
        return None

    # A signed subset may exclude a catalog's original modality default. Give
    # each retained modality one deterministic default without widening the
    # cloud allowlist.
    defaults = {
        modality
        for model in models.values()
        for modality in model.default_for
        if modality in model.modalities
    }
    replacements: dict[str, frozenset[ModelModality]] = {
        model_id: model.default_for for model_id, model in models.items()
    }
    for modality in ModelModality:
        eligible = sorted(
            model.model_id for model in models.values() if modality in model.modalities
        )
        if eligible and modality not in defaults:
            selected = eligible[0]
            replacements[selected] = frozenset({*replacements[selected], modality})
    return ManagedModelCatalog(
        replace(model, default_for=replacements[model.model_id])
        for model in models.values()
    )


def _empty_model_catalog(snapshot: ManagedSessionSnapshot | None) -> ModelCatalog:
    snapshot_id = None
    if snapshot is not None:
        snapshot_id = (
            "models_"
            + hashlib.sha256(
                (
                    "ecorex-managed-model-deny-v1\n"
                    + snapshot.lease_digest
                    + "\n"
                    + "\n".join(snapshot.allowed_model_ids)
                ).encode("utf-8")
            ).hexdigest()
        )
    return ModelCatalog(snapshot_id=snapshot_id)


def _overlay_cloud_model_catalog(
    base: ModelCatalog,
    gateway_catalog: Mapping[str, object],
) -> ModelCatalog:
    """Apply secret-free tested cloud names/defaults to the Web projection."""

    raw_catalog = gateway_catalog.get("catalog")
    raw_models = gateway_catalog.get("models")
    if not isinstance(raw_catalog, list) or not isinstance(raw_models, list):
        return base
    allowed = {value for value in raw_models if isinstance(value, str)}
    metadata = {
        str(item["local_model_id"]): item
        for item in raw_catalog
        if isinstance(item, Mapping)
        and isinstance(item.get("local_model_id"), str)
        and item.get("local_model_id") in allowed
    }

    def project(
        descriptors: list[ModelDescriptor], *, modalities: frozenset[str]
    ) -> list[ModelDescriptor]:
        projected: list[ModelDescriptor] = []
        for descriptor in descriptors:
            item = metadata.get(descriptor.model_id)
            if item is None or item.get("modality") not in modalities:
                continue
            updates: dict[str, object] = {
                "display_name": str(
                    item.get("display_name") or descriptor.display_name
                ),
                "is_default": bool(item.get("is_default")),
            }
            upstream = item.get("upstream_model_id")
            if descriptor.model_policy is not None and isinstance(upstream, str):
                updates["model_policy"] = descriptor.model_policy.model_copy(
                    update={"upstream_model_id": upstream}
                )
            projected.append(descriptor.model_copy(update=updates))
        return projected

    digest = hashlib.sha256(
        json.dumps(
            {"models": sorted(allowed), "catalog": raw_catalog},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ModelCatalog(
        snapshot_id="models_cloud_" + digest,
        chat=project(base.chat, modalities=frozenset({"chat"})),
        image=project(base.image, modalities=frozenset({"image_generation"})),
        vision=project(base.vision, modalities=frozenset({"chat"})),
        audio=project(base.audio, modalities=frozenset({"audio"})),
        embedding=project(base.embedding, modalities=frozenset({"embedding"})),
    )


class RuntimeUpdateController(Protocol):
    def snapshot(self) -> UpdateSnapshot: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def check_now(self) -> UpdateSnapshot: ...

    async def activate(
        self, *, transaction_id: str, client_request_id: str
    ) -> ActivateUpdateResponse: ...

    async def activate_verified_local(
        self,
        *,
        transaction_id: str,
        client_request_id: str,
        execution_guard: Callable[[], None],
    ) -> ActivateUpdateResponse: ...


class _AsyncResourceCloser:
    """Own one async close boundary when no domain supervisor does.

    ``start`` deliberately has no side effect.  Idempotent ``stop`` makes this
    safe in lifespan unwind paths without allowing two owners for the same
    transport.
    """

    def __init__(self, resource: Any) -> None:
        self.resource = resource
        self.closed = False

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        if self.closed:
            return
        # Claim ownership before invoking native cleanup so an exception cannot
        # cause a second close attempt during a nested lifespan unwind.
        self.closed = True
        close = getattr(self.resource, "aclose", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result


class _TransactionalEventSinkFanout:
    """Commit multiple derived outboxes atomically with their source Event."""

    def __init__(self, *sinks: Any) -> None:
        self.sinks = tuple(sink for sink in sinks if sink is not None)

    def record_in_transaction(self, connection: Any, event: Any) -> None:
        for sink in self.sinks:
            sink.record_in_transaction(connection, event)


def _retouch_capability_available(settings: RuntimeSettings) -> bool:
    return (
        "image" in settings.installed_capability_packs
        and "imagegen" not in settings.disabled_capability_tools
    )


def _bootstrap(
    settings: RuntimeSettings,
    *,
    models: ModelCatalog,
    model_service: ModelServiceSnapshot,
    permissions: PermissionSnapshot,
    update: UpdateSnapshot,
    connectors: list[ConnectorDescriptor],
    extensions: ExtensionCatalogSnapshot,
    issued_at: datetime | None = None,
    lease_id: str | None = None,
    managed_session: ManagedSessionSnapshot | None = None,
    managed_authenticated: bool | None = None,
    retouch_service: ModelServiceSnapshot | None = None,
) -> BootstrapResponse:
    issued_at = issued_at or utc_now()
    if managed_authenticated is None:
        login = LoginSnapshot(
            authenticated=settings.authenticated,
            account_id=settings.account_id if settings.authenticated else None,
            display_name=(
                settings.account_display_name if settings.authenticated else None
            ),
        )
        policy_lease: PolicyLeaseSnapshot | None = PolicyLeaseSnapshot(
            lease_id=lease_id or new_id("lease"),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(hours=72),
        )
        quota = QuotaSnapshot(remaining=None, unit="managed_requests")
    else:
        login = LoginSnapshot(
            authenticated=managed_authenticated,
            account_id=(managed_session.account_id if managed_session else None),
            display_name=(managed_session.display_name if managed_session else None),
            organization_id=(
                managed_session.organization_id if managed_session else None
            ),
            roles=(list(managed_session.roles) if managed_session else []),
            session_revision=(managed_session.revision if managed_session else None),
            session_lease_digest=(
                managed_session.lease_digest if managed_session else None
            ),
        )
        if managed_session is None:
            policy_lease = None
            quota = QuotaSnapshot(
                remaining=None,
                unit="managed_requests",
                limits={},
            )
        else:
            duration_seconds = int(
                (managed_session.expires_at - managed_session.issued_at).total_seconds()
            )
            policy_lease = PolicyLeaseSnapshot(
                lease_id=managed_session.lease_id,
                issued_at=managed_session.issued_at,
                expires_at=managed_session.expires_at,
                duration_hours=max(1, min(72, (duration_seconds + 3599) // 3600)),
            )
            quota = QuotaSnapshot(
                remaining=managed_session.quota.get("managed_requests"),
                unit="managed_requests",
                limits=dict(managed_session.quota),
            )
    return BootstrapResponse(
        login=login,
        policy_lease=policy_lease,
        models=models,
        model_service=model_service,
        login_service=ModelServiceSnapshot(
            state=(
                "ready"
                if settings.device_authorization_service is not None
                else "unavailable"
            ),
            reason=(
                None
                if settings.device_authorization_service is not None
                else "device_authorization_not_configured"
            ),
        ),
        share_service=ModelServiceSnapshot(
            state="ready" if settings.share_publisher is not None else "unavailable",
            reason=(
                None
                if settings.share_publisher is not None
                else "share_service_not_configured"
            ),
        ),
        retouch_service=(
            retouch_service
            or ModelServiceSnapshot(
                state=(
                    "ready"
                    if settings.retouch_adapter is not None
                    and _retouch_capability_available(settings)
                    else "unavailable"
                ),
                reason=(
                    None
                    if settings.retouch_adapter is not None
                    and _retouch_capability_available(settings)
                    else (
                        "image_capability_pack_not_installed"
                        if not _retouch_capability_available(settings)
                        else "managed_image_edit_not_configured"
                    )
                ),
            )
        ),
        quota=quota,
        permissions=permissions,
        connectors=connectors,
        extensions=extensions,
        update=update,
        csrf_token=settings.csrf_token or "",
        server_time=issued_at,
    )


def _durable_bootstrap(
    kernel: RuntimeKernel,
    settings: RuntimeSettings,
    *,
    models: ModelCatalog,
    model_service: ModelServiceSnapshot,
    permissions: PermissionSnapshot,
    update: UpdateSnapshot,
    connectors: list[ConnectorDescriptor],
    extensions: ExtensionCatalogSnapshot,
) -> BootstrapResponse:
    now = utc_now()
    fingerprint = json.dumps(
        {
            "account_id": settings.account_id,
            "authenticated": settings.authenticated,
            "admin_hard_denies": sorted(settings.admin_hard_denies),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with kernel.database.transaction() as connection:
        row = connection.execute(
            "SELECT value FROM runtime_meta WHERE key = 'bootstrap_security_snapshot'"
        ).fetchone()
        if row is not None:
            try:
                stored = json.loads(row["value"])
                issued_at = datetime.fromisoformat(stored["issued_at"])
                expires_at = datetime.fromisoformat(stored["expires_at"])
                if issued_at.tzinfo is None or expires_at.tzinfo is None:
                    raise ValueError("bootstrap timestamps must be timezone-aware")
                issued_at = issued_at.astimezone(timezone.utc)
                expires_at = expires_at.astimezone(timezone.utc)
                if stored["fingerprint"] == fingerprint and expires_at > now:
                    return _bootstrap(
                        settings,
                        models=models,
                        model_service=model_service,
                        permissions=permissions,
                        update=update,
                        connectors=connectors,
                        extensions=extensions,
                        issued_at=issued_at,
                        lease_id=stored["lease_id"],
                    )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    "durable bootstrap security snapshot is invalid"
                ) from error
        snapshot = _bootstrap(
            settings,
            models=models,
            model_service=model_service,
            permissions=permissions,
            update=update,
            connectors=connectors,
            extensions=extensions,
            issued_at=now,
        )
        stored = json.dumps(
            {
                "fingerprint": fingerprint,
                "issued_at": snapshot.policy_lease.issued_at.isoformat(),
                "expires_at": snapshot.policy_lease.expires_at.isoformat(),
                "lease_id": snapshot.policy_lease.lease_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO runtime_meta(key, value) VALUES "
            "('bootstrap_security_snapshot', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (stored,),
        )
        return snapshot


def _event_data(event: object) -> str:
    return json.dumps(
        event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )


def _encode_thread_cursor(
    thread,
    *,
    status_filter: str,
    secret: str,
) -> str:
    payload = json.dumps(
        {
            "updated_at": thread.updated_at.isoformat(),
            "thread_id": thread.thread_id,
            "status_filter": status_filter,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _decode_thread_cursor(
    value: str,
    *,
    status_filter: str,
    secret: str,
) -> tuple[datetime, str]:
    if not value or len(value) > 2048:
        raise ValueError("thread cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
        if not hmac.compare_digest(canonical, value):
            raise ValueError
        if len(decoded) <= 32:
            raise ValueError
        payload, signature = decoded[:-32], decoded[-32:]
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        parsed = json.loads(payload)
        if set(parsed) != {"updated_at", "thread_id", "status_filter"}:
            raise ValueError
        if parsed["status_filter"] != status_filter:
            raise ValueError
        updated_at = datetime.fromisoformat(parsed["updated_at"])
        if updated_at.tzinfo is None or not isinstance(parsed["thread_id"], str):
            raise ValueError
        if not parsed["thread_id"] or len(parsed["thread_id"]) > 256:
            raise ValueError
        return updated_at.astimezone(timezone.utc), parsed["thread_id"]
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        binascii.Error,
    ) as error:
        raise ValueError("thread cursor is invalid") from error


async def _stream_events(
    request: Request,
    kernel: RuntimeKernel,
    settings: RuntimeSettings,
    thread_id: str,
    after_seq: int,
    follow: bool,
) -> AsyncIterator[str]:
    cursor = after_seq
    announced_watermark: int | None = None
    last_output_at = asyncio.get_running_loop().time()
    request_app = getattr(request, "app", None)
    registry = getattr(
        getattr(request_app, "state", None), "runtime_signal_registry", None
    )
    if isinstance(registry, RuntimeSignalRegistry):
        registry.sse_connected()
    try:
        while True:
            notification_generation = kernel.events.notification_generation(thread_id)
            page = await asyncio.to_thread(
                kernel.events.page, thread_id, after_seq=cursor, limit=200
            )
            emitted_events = bool(page.events)
            if emitted_events and isinstance(registry, RuntimeSignalRegistry):
                registry.sse_events_sent(len(page.events))
            for event in page.events:
                cursor = event.seq
                yield (
                    f"id: {event.seq}\n"
                    f"event: {event.event_type}\n"
                    f"data: {_event_data(event)}\n\n"
                )
                last_output_at = asyncio.get_running_loop().time()
            if page.has_more:
                continue
            if announced_watermark != page.watermark:
                yield (
                    "event: watermark\n"
                    f"data: {json.dumps({'watermark': page.watermark}, separators=(',', ':'))}\n\n"
                )
                announced_watermark = page.watermark
                last_output_at = asyncio.get_running_loop().time()
            if not follow or await request.is_disconnected():
                return
            now = asyncio.get_running_loop().time()
            if now - last_output_at >= settings.sse_keepalive_seconds:
                yield ": keepalive\n\n"
                last_output_at = asyncio.get_running_loop().time()
            until_keepalive = max(
                0.0,
                settings.sse_keepalive_seconds
                - (asyncio.get_running_loop().time() - last_output_at),
            )
            wait_timeout = min(
                settings.event_notification_fallback_seconds,
                until_keepalive,
            )
            if wait_timeout <= 0:
                await asyncio.sleep(0)
                continue
            await kernel.events.wait_for_notification(
                thread_id,
                notification_generation,
                timeout=wait_timeout,
            )
    finally:
        if isinstance(registry, RuntimeSignalRegistry):
            registry.sse_disconnected()


def _has_encrypted_audit_rows(kernel: RuntimeKernel) -> bool:
    with kernel.database.reader() as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'observability_audit_outbox'"
        ).fetchone()
        if table is None:
            return False
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(observability_audit_outbox)"
            ).fetchall()
        }
        if "payload_format" not in columns:
            return False
        row = connection.execute(
            "SELECT 1 FROM observability_audit_outbox WHERE payload_format = ? LIMIT 1",
            (AuditPayloadCipher.FORMAT,),
        ).fetchone()
        if row is not None:
            return True
        trace_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'observability_trace_outbox'"
        ).fetchone()
        if trace_table is None:
            return False
        return (
            connection.execute(
                "SELECT 1 FROM observability_trace_outbox LIMIT 1"
            ).fetchone()
            is not None
        )


def _local_audit_key(
    database_path: str | Path,
    account_id: str,
    *,
    create: bool = True,
) -> bytes:
    """Unsupported-platform development fallback; production uses OS vaults."""

    database = Path(database_path).expanduser().resolve()
    account = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
    path = database.with_name(f".{database.name}.{account}.audit-key")
    try:
        material = path.read_bytes()
    except FileNotFoundError:
        if not create:
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        candidate = os.urandom(32)
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            material = path.read_bytes()
        else:
            try:
                if os.write(descriptor, candidate) != len(candidate):
                    raise OSError("audit key write was incomplete")
            finally:
                os.close(descriptor)
            material = candidate
    if len(material) != 32:
        raise RuntimeError("local audit encryption key is invalid")
    return material


def _resolve_audit_encryption_key(
    settings: RuntimeSettings,
    *,
    kernel: RuntimeKernel,
    credential_vault: Any,
    create: bool = True,
) -> bytes:
    if settings.audit_encryption_key is not None:
        material = bytes(settings.audit_encryption_key)
        if len(material) != 32:
            raise ValueError("audit encryption key must contain 32 bytes")
        return material
    if sys.platform not in {"win32", "darwin"}:
        try:
            return _local_audit_key(
                settings.database_path,
                settings.account_id,
                create=create,
            )
        except FileNotFoundError:
            if _has_encrypted_audit_rows(kernel):
                raise RuntimeError(
                    "local audit key cannot unlock encrypted observability data"
                ) from None
            return os.urandom(32)

    reference = (
        "ecorex/observability/audit/"
        + hashlib.sha256(settings.account_id.encode("utf-8")).hexdigest()[:32]
    )
    try:
        stored = credential_vault.get(reference)
    except (KeyError, RuntimeError):
        if _has_encrypted_audit_rows(kernel):
            raise RuntimeError(
                "OS credential vault cannot unlock the encrypted audit outbox"
            ) from None
        if not create:
            return os.urandom(32)
        candidate = os.urandom(32)
        credential_vault.put(
            reference,
            {"aes256_gcm_key": base64.urlsafe_b64encode(candidate).decode("ascii")},
        )
        stored = credential_vault.get(reference)
    try:
        encoded = stored["aes256_gcm_key"]
        material = base64.b64decode(str(encoded), altchars=b"-_", validate=True)
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(
            "OS credential vault returned an invalid audit key"
        ) from None
    if len(material) != 32:
        raise RuntimeError("OS credential vault returned an invalid audit key")
    return material


class _UnavailableRetouchCoordinator:
    """Fail before persistence when managed image editing is absent."""

    def request(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ArtifactActionUnavailable(
            "precise retouch is unavailable because managed image editing is not configured"
        )


def create_app(
    database_path: str | Path | None = None,
    *,
    settings: RuntimeSettings | None = None,
    app: FastAPI | None = None,
) -> FastAPI:
    if settings is None:
        settings = RuntimeSettings(
            database_path=database_path or Path.cwd() / ".ecorex" / "runtime-v1.db"
        )
    elif database_path is not None:
        raise ValueError("pass database_path or settings, not both")
    if settings.runtime_bearer_token is None:
        settings.runtime_bearer_token = secrets.token_urlsafe(32)
    if settings.csrf_token is None:
        settings.csrf_token = secrets.token_urlsafe(32)
    if len(settings.runtime_bearer_token) < 32:
        raise ValueError("runtime bearer token must contain at least 32 characters")
    if len(settings.csrf_token) < 32:
        raise ValueError("CSRF token must contain at least 32 characters")
    if settings.event_poll_interval_seconds <= 0:
        raise ValueError("event poll interval must be positive")
    if settings.event_idle_poll_interval_seconds < settings.event_poll_interval_seconds:
        raise ValueError(
            "idle event poll interval cannot be shorter than active polling"
        )
    if (
        settings.event_notification_fallback_seconds
        < settings.event_idle_poll_interval_seconds
    ):
        raise ValueError(
            "event notification fallback cannot be shorter than idle polling"
        )
    if settings.sse_keepalive_seconds <= 0:
        raise ValueError("SSE keepalive interval must be positive")
    if not 1 <= settings.model_worker_concurrency <= 8:
        raise ValueError("model worker concurrency must be between one and eight")
    if not 0.01 <= settings.model_worker_poll_seconds <= 60:
        raise ValueError("model worker poll interval is invalid")
    if not 0.1 <= settings.model_worker_shutdown_seconds <= 120:
        raise ValueError("model worker shutdown timeout is invalid")
    if not 0.01 <= settings.interaction_maintenance_seconds <= 3600:
        raise ValueError("interaction maintenance interval is invalid")
    if not 0.05 <= settings.interaction_maintenance_shutdown_seconds <= 120:
        raise ValueError("interaction maintenance shutdown timeout is invalid")
    if not 0.01 <= settings.invariant_audit_seconds <= 3600:
        raise ValueError("invariant audit interval is invalid")
    if not 0.05 <= settings.invariant_audit_timeout_seconds <= 300:
        raise ValueError("invariant audit timeout is invalid")
    if not 0.05 <= settings.invariant_shutdown_seconds <= 120:
        raise ValueError("invariant shutdown timeout is invalid")
    if not 0.05 <= settings.lifecycle_shutdown_seconds <= 120:
        raise ValueError("Runtime lifecycle shutdown timeout is invalid")
    if not 0.01 <= settings.connector_maintenance_seconds <= 3600:
        raise ValueError("connector maintenance interval is invalid")
    if not 1 <= settings.retouch_worker_concurrency <= 4:
        raise ValueError("retouch worker concurrency must be between one and four")
    if not 0.01 <= settings.retouch_worker_poll_seconds <= 60:
        raise ValueError("retouch worker poll interval is invalid")
    if not 0.1 <= settings.retouch_worker_shutdown_seconds <= 120:
        raise ValueError("retouch worker shutdown timeout is invalid")
    if settings.share_publisher is not None and not settings.share_public_hosts:
        raise ValueError("configured share publisher requires a public host allowlist")
    if settings.session_reload_requester is not None and not callable(
        settings.session_reload_requester
    ):
        raise ValueError("session reload requester must be callable")
    for callback, label in (
        (
            settings.first_install_registration_recorder,
            "first-install registration recorder",
        ),
        (
            settings.first_install_runtime_ready_recorder,
            "first-install Runtime readiness recorder",
        ),
    ):
        if callback is not None and not callable(callback):
            raise ValueError(f"{label} must be callable")
    if not 0.05 <= settings.device_authorization_poll_seconds <= 30:
        raise ValueError("device authorization poll interval is invalid")
    if settings.retouch_adapter is not None and not _retouch_capability_available(
        settings
    ):
        raise ValueError(
            "configured retouch adapter requires the verified image capability pack"
        )
    if not 0.1 <= settings.audit_dispatch_seconds <= 300:
        raise ValueError("audit dispatch interval is invalid")
    if not settings.webui_origins:
        raise ValueError("at least one exact WebUI origin is required")
    for origin in settings.webui_origins:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1", "testserver"}
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                f"WebUI origin must be an exact loopback origin: {origin!r}"
            )
    managed_session = settings.managed_session_service
    session_refresh_service = settings.managed_session_refresh_service
    if session_refresh_service is not None and (
        managed_session is None
        or session_refresh_service.session is not managed_session
    ):
        raise ValueError(
            "managed session refresh must rotate the configured managed session"
        )
    if not 1 <= settings.managed_session_refresh_poll_seconds <= 300:
        raise ValueError("managed session refresh poll interval is invalid")
    if settings.device_authorization_service is not None and (
        managed_session is None
        or settings.device_authorization_service.session is not managed_session
    ):
        raise ValueError(
            "device authorization must install into the configured managed session"
        )
    if (
        settings.model_gateway is not None
        and managed_session is None
        and not settings.allow_unmanaged_model_gateway_for_testing
    ):
        raise ValueError("a production Model Gateway requires a managed signed session")
    if (
        isinstance(settings.model_gateway, ManagedModelGatewayClient)
        and managed_session is not None
        and settings.model_gateway.credentials is not managed_session
        and not settings.allow_unmanaged_model_gateway_for_testing
    ):
        raise ValueError(
            "ManagedModelGatewayClient credentials must be the ManagedSessionService"
        )
    if (
        settings.image_orchestration_client is not None
        and (
            managed_session is None
            or settings.image_orchestration_client.session is not managed_session
        )
        and not settings.allow_unmanaged_model_gateway_for_testing
    ):
        raise ValueError(
            "managed image orchestration must use the exact ManagedSessionService"
        )

    # Phase A owns only the compiled product schema. Audit the authoritative
    # Runtime graph immediately after that boundary, before account recovery,
    # factory defaults, catalog snapshots, outbox backfill or filesystem root
    # preparation can change semantic state. A failed preflight remains
    # projection-only for the complete process lifetime.
    kernel = RuntimeKernel(settings.database_path)
    project_service = ProjectService(kernel.database)
    runtime_execution_gate = RuntimeExecutionGate()
    recovery_execution_gate = RecoveryExecutionGate()
    kernel.jobs.bind_execution_gate(runtime_execution_gate)
    try:
        runtime_execution_gate.record_report(kernel.invariants.audit())
    except BaseException as error:
        runtime_execution_gate.record_audit_exception(error)
    startup_convergence_allowed = runtime_execution_gate.snapshot().healthy

    startup_session: ManagedSessionSnapshot | None = None
    startup_data_scope: ManagedSessionSnapshot | None = None
    startup_session_error: ManagedSessionError | None = None
    if managed_session is not None:
        try:
            if startup_convergence_allowed:
                with (
                    runtime_execution_gate.new_admission(
                        scope="session_startup",
                        subject="managed_session_convergence",
                    ) as permit,
                    transaction_commit_guard(
                        lambda: runtime_execution_gate.assert_permit(permit)
                    ),
                ):
                    converge_session = getattr(
                        managed_session,
                        "converge_startup",
                        None,
                    )
                    if callable(converge_session):
                        converge_session()
                    managed_session.recover()
                    if session_refresh_service is not None:
                        session_refresh_service.converge_startup()
                    startup_session = managed_session.snapshot()
            else:
                startup_session = managed_session.read_snapshot()
        except ManagedSessionError as error:
            startup_session_error = error
            try:
                startup_data_scope = (
                    managed_session.data_scope_snapshot()
                    if startup_convergence_allowed
                    else managed_session.read_data_scope_snapshot()
                )
            except ManagedSessionError:
                startup_data_scope = None
        else:
            startup_data_scope = startup_session
        if startup_data_scope is not None:
            managed_session.bind_runtime(startup_data_scope)
            settings.account_id = startup_data_scope.account_id
            settings.account_display_name = startup_data_scope.display_name
            settings.admin_hard_denies = sorted(
                {
                    *settings.admin_hard_denies,
                    *startup_data_scope.admin_denies,
                }
            )
        if startup_session is not None:
            settings.authenticated = True
        else:
            settings.authenticated = False
    elif settings.require_managed_session:
        settings.authenticated = False
    device_authorization_service = settings.device_authorization_service
    if device_authorization_service is not None:
        device_authorization_service.bind_execution_gate(runtime_execution_gate)
        if startup_convergence_allowed:
            with (
                runtime_execution_gate.new_admission(
                    scope="device_startup",
                    subject="device_authorization_convergence",
                ) as permit,
                transaction_commit_guard(
                    lambda: runtime_execution_gate.assert_permit(permit)
                ),
            ):
                device_authorization_service.converge_startup()
    startup_session_binding = (
        _session_binding(startup_data_scope) if startup_data_scope is not None else None
    )
    managed_runtime_state = {"logged_out": False}

    def current_managed_session() -> ManagedSessionSnapshot:
        if managed_session is None:
            raise SessionUnavailable("managed session is not configured")
        if managed_runtime_state["logged_out"]:
            raise SessionUnavailable(
                "managed session was logged out; controlled restart required"
            )
        try:
            snapshot = managed_session.read_snapshot()
        except SessionRestartRequired as error:
            raise _ManagedSessionRestartRequired(str(error)) from error
        if startup_session is None:
            raise _ManagedSessionRestartRequired(
                "managed session became active after startup; controlled restart required"
            )
        binding = _session_binding(snapshot)
        if startup_session_binding is None or binding != startup_session_binding:
            raise _ManagedSessionRestartRequired(
                "managed session identity or policy changed; controlled restart required"
            )
        return snapshot

    interaction_maintenance_supervisor = InteractionMaintenanceSupervisor(
        kernel.interactions,
        execution_gate=runtime_execution_gate,
        interval_seconds=settings.interaction_maintenance_seconds,
        convergence_timeout_seconds=settings.interaction_maintenance_shutdown_seconds,
        shutdown_timeout_seconds=settings.interaction_maintenance_shutdown_seconds,
    )
    invariant_supervisor = RuntimeInvariantSupervisor(
        kernel.invariants,
        runtime_execution_gate,
        audit_interval_seconds=settings.invariant_audit_seconds,
        audit_timeout_seconds=settings.invariant_audit_timeout_seconds,
        shutdown_timeout_seconds=settings.invariant_shutdown_seconds,
    )
    if app is None:
        app = FastAPI(
            title="EcoreX Local Runtime",
            version=settings.product_version,
            docs_url=None,
            redoc_url=None,
            openapi_url="/api/v1/openapi.json",
        )
    app.state.runtime_execution_gate = runtime_execution_gate
    app.state.recovery_execution_gate = recovery_execution_gate
    app.state.project_service = project_service
    app.state.interaction_maintenance_supervisor = interaction_maintenance_supervisor
    app.state.invariant_supervisor = invariant_supervisor
    permission_authority = PermissionAuthority(
        kernel.database,
        account_id=settings.account_id,
        initial_full_access=settings.full_access,
        admin_hard_denies=frozenset(settings.admin_hard_denies),
        initialize=startup_convergence_allowed,
    )
    permission_projection = permission_authority.current()
    builtin_models = builtin_model_catalog()
    managed_mode = managed_session is not None or settings.require_managed_session
    signed_models = (
        _filter_model_catalog(
            builtin_models,
            frozenset(startup_session.allowed_model_ids),
        )
        if startup_session is not None
        else None
    )
    managed_models = signed_models or builtin_models
    usage_projection_service = UsageProjectionService(
        kernel.database,
        model_catalog=managed_models,
        timezone_name=settings.usage_timezone,
    )
    app.state.usage_projection_service = usage_projection_service
    oauth_return_uri = settings.connector_oauth_return_uri
    if oauth_return_uri is None:
        candidate_origin = urlsplit(settings.webui_origins[0])
        if (
            candidate_origin.hostname in {"127.0.0.1", "localhost", "::1"}
            and candidate_origin.port is not None
        ):
            oauth_return_uri = (
                settings.webui_origins[0].rstrip("/")
                + "/api/v1/connectors/oauth/callback"
            )
        else:
            # ASGI test clients do not expose a real callback authority. Keep
            # the production contract loopback-only instead of allowlisting
            # their synthetic host.
            oauth_return_uri = "http://127.0.0.1:8765/api/v1/connectors/oauth/callback"
    connector_vault = settings.connector_vault
    if connector_vault is None:
        try:
            connector_vault = production_credential_vault()
        except RuntimeError:
            connector_vault = RejectingCredentialVault()
    connector_event_sink = RuntimeConnectorEventSink(
        kernel,
        account_id=settings.account_id,
    )
    connector_composition: ConnectorComposition = build_connector_composition(
        database_path=settings.database_path,
        oauth_return_uri=oauth_return_uri,
        adapters=settings.connector_adapters,
        vault=connector_vault,
        event_sink=connector_event_sink,
        hard_deny_provider=lambda _instance_id, _action_id: frozenset(
            permission_authority.current().admin_hard_denies
        ),
        maintenance_interval_seconds=settings.connector_maintenance_seconds,
        maintenance_stop_timeout_seconds=settings.lifecycle_shutdown_seconds,
        maintenance_allowed=lambda: runtime_execution_gate.snapshot().healthy,
        initialize=startup_convergence_allowed,
        execution_gate=runtime_execution_gate,
    )
    connector_registry = connector_composition.service.registry
    connector_catalog = connector_composition.service.catalog()
    extension_governance_enabled = settings.extension_service is not None
    extension_service = settings.extension_service or ExtensionService(
        SQLiteExtensionRepository(
            kernel.database,
            initialize=startup_convergence_allowed,
        ),
        runtime_api_version="1.0.0",
        platform=settings.platform,
        architecture=settings.architecture,
        local_bundle_store=LocalSkillBundleStore(
            Path(settings.database_path).expanduser().resolve().parent
            / "extension-cas",
            create=startup_convergence_allowed,
        ),
    )
    if startup_convergence_allowed:
        converge_extensions = getattr(extension_service, "converge_startup", None)
        if callable(converge_extensions):
            with (
                runtime_execution_gate.new_admission(
                    scope="extension_startup",
                    subject="extension_authority_convergence",
                ) as permit,
                transaction_commit_guard(
                    lambda: runtime_execution_gate.assert_permit(permit)
                ),
            ):
                converge_extensions()
    initial_extensions = ExtensionCatalogSnapshot.model_validate(
        extension_service.project_snapshot().to_dict()
    )
    model_projection = (
        project_model_catalog(signed_models)
        if signed_models is not None
        else (
            _empty_model_catalog(startup_data_scope)
            if managed_mode
            else project_model_catalog(managed_models)
        )
    )
    connector_projection = project_connector_catalog(
        connector_registry,
        connector_catalog,
    )
    connected_connectors = (
        frozenset(
            {
                item.definition.connector_id
                for item in connector_catalog
                if any(
                    instance.health.value == "connected" for instance in item.instances
                )
            }
        )
        | settings.connected_connectors
    )

    def current_availability() -> RuntimeAvailability:
        catalog = connector_composition.service.catalog()
        connected = (
            frozenset(
                {
                    item.definition.connector_id
                    for item in catalog
                    if any(
                        instance.health.value == "connected"
                        for instance in item.instances
                    )
                }
            )
            | settings.connected_connectors
        )
        disabled_tools = dict(settings.disabled_capability_tools)
        active_permission = permission_authority.current()
        sandbox_profile = (
            "danger-full-access" if active_permission.full_access else "workspace-write"
        )
        for (
            tool_id,
            profiles,
        ) in settings.capability_sandbox_profile_availability.items():
            reason = profiles.get(sandbox_profile)
            if reason:
                disabled_tools[str(tool_id)] = str(reason)
            else:
                prior = disabled_tools.get(str(tool_id))
                profile_reasons = {
                    value for value in profiles.values() if isinstance(value, str)
                }
                if prior in profile_reasons:
                    disabled_tools.pop(str(tool_id), None)
        return RuntimeAvailability(
            platform=settings.platform,
            installed_packs=settings.installed_capability_packs,
            connected_connectors=connected,
            disabled_tools=disabled_tools,
            online=settings.online,
        )

    signed_chat_available = bool(model_projection.chat)
    model_worker_enabled = bool(
        settings.model_gateway is not None
        and (
            (startup_session is not None and signed_chat_available)
            if managed_mode
            else True
        )
    )
    if settings.model_gateway is None:
        model_service = ModelServiceSnapshot(
            state="unavailable", reason="managed_gateway_not_configured"
        )
    elif managed_mode and startup_session is None:
        model_service = ModelServiceSnapshot(
            state="unavailable", reason="managed_session_unavailable"
        )
    elif managed_mode and not signed_chat_available:
        model_service = ModelServiceSnapshot(
            state="unavailable", reason="signed_model_allowlist_empty"
        )
    else:
        model_service = ModelServiceSnapshot(state="ready", reason=None)
    update_service: RuntimeUpdateController | None = settings.update_service
    activation_drain_controller: RuntimeActivationDrainController | None = None
    if update_service is not None:
        bind_activation_drainer = getattr(
            update_service,
            "bind_runtime_activation_drainer",
            None,
        )
        if callable(bind_activation_drainer):
            activation_drain_controller = RuntimeActivationDrainController(
                kernel.jobs,
                runtime_execution_gate,
                timeout_seconds=settings.update_drain_timeout_seconds,
                poll_seconds=settings.update_drain_poll_seconds,
            )
            bind_activation_drainer(activation_drain_controller)
    app.state.activation_drain_controller = activation_drain_controller
    if update_service is not None and startup_convergence_allowed:
        converge_update = getattr(update_service, "converge_startup", None)
        if callable(converge_update):
            with (
                runtime_execution_gate.new_admission(
                    scope="update_startup",
                    subject="update_authority_convergence",
                ) as permit,
                transaction_commit_guard(
                    lambda: runtime_execution_gate.assert_permit(permit)
                ),
            ):
                converge_update()
    update_projection = (
        update_service.snapshot()
        if update_service is not None
        else UpdateSnapshot(current_version=settings.product_version, state="idle")
    )
    if update_projection.current_version != settings.product_version:
        raise ValueError(
            "update service current version does not match the product bundle"
        )
    if managed_mode:
        if startup_session is None:
            retouch_projection = ModelServiceSnapshot(
                state="unavailable", reason="managed_session_unavailable"
            )
        elif not _retouch_capability_available(settings):
            retouch_projection = ModelServiceSnapshot(
                state="unavailable", reason="image_capability_pack_not_installed"
            )
        elif settings.retouch_adapter is None:
            retouch_projection = ModelServiceSnapshot(
                state="unavailable", reason="managed_image_edit_not_configured"
            )
        elif not model_projection.image:
            retouch_projection = ModelServiceSnapshot(
                state="unavailable", reason="signed_image_model_not_allowed"
            )
        else:
            retouch_projection = ModelServiceSnapshot(state="ready", reason=None)
        bootstrap_snapshot = _bootstrap(
            settings,
            models=model_projection,
            model_service=model_service,
            permissions=permission_projection,
            update=update_projection,
            connectors=connector_projection,
            extensions=initial_extensions,
            managed_session=startup_data_scope,
            managed_authenticated=startup_session is not None,
            retouch_service=retouch_projection,
        )
    elif startup_convergence_allowed:
        bootstrap_snapshot = _durable_bootstrap(
            kernel,
            settings,
            models=model_projection,
            model_service=model_service,
            permissions=permission_projection,
            update=update_projection,
            connectors=connector_projection,
            extensions=initial_extensions,
        )
    else:
        bootstrap_snapshot = _bootstrap(
            settings,
            models=model_projection,
            model_service=model_service,
            permissions=permission_projection,
            update=update_projection,
            connectors=connector_projection,
            extensions=initial_extensions,
        )
    kernel.events.default_permission_snapshot_id = (
        bootstrap_snapshot.permissions.snapshot_id
    )
    artifact_root = settings.artifact_root or (
        Path(settings.database_path).expanduser().resolve().parent / "artifacts"
    )
    artifact_service = ArtifactService(
        artifact_root,
        database_path=settings.database_path,
        create_storage=startup_convergence_allowed,
    )
    input_attachment_service = InputAttachmentService(
        artifact_service,
        account_id=settings.account_id,
    )
    connector_result_coordinator = RuntimeConnectorResultCoordinator(
        kernel,
        artifact_service,
        connector_composition.repository,
        account_id=settings.account_id,
    )
    connector_composition.service.bind_result_coordinator(connector_result_coordinator)
    if settings.output_roots is None:
        configured_output_roots: Mapping[str, str | Path] = {
            "workspace": Path(settings.database_path).expanduser().resolve().parent
            / "outputs"
        }
        output_default_location = "workspace"
    else:
        configured_output_roots = settings.output_roots
        output_default_location = settings.output_default_location
    output_service = OutputService(
        artifact_service=artifact_service,
        database_path=settings.database_path,
        runtime_database_path=settings.database_path,
        account_id=settings.account_id,
        configured_roots=configured_output_roots,
        default_alias=output_default_location,
        prepare_output_roots=startup_convergence_allowed,
    )

    composition = RuntimeComposition(
        database_path=str(settings.database_path),
        product_version=settings.product_version,
        permission_snapshot_id=bootstrap_snapshot.permissions.snapshot_id,
        permission_payload=bootstrap_snapshot.permissions.model_dump(mode="json"),
        full_access=permission_projection.full_access,
        admin_hard_denies=frozenset(permission_projection.admin_hard_denies),
        platform=settings.platform,
        architecture=settings.architecture,
        installed_packs=settings.installed_capability_packs,
        connected_connectors=connected_connectors,
        online=settings.online,
        disabled_tools=settings.disabled_capability_tools,
        model_catalog=managed_models,
        connector_registry=connector_registry,
        connector_service=connector_composition.service,
        artifact_service=artifact_service,
        capability_handlers=settings.capability_handlers,
        permission_provider=permission_authority.current,
        permission_state_digest_provider=permission_authority.current_state_digest,
        permission_sample_scope_provider=permission_authority.verified_sample_scope,
        permission_mutation_lock=permission_authority.mutation_lock,
        availability_provider=current_availability,
        output_policy_provider=(
            (lambda: output_service.current_policy().output_policy_snapshot_id)
            if startup_convergence_allowed
            else (lambda: output_service.project_preference().output_policy_snapshot_id)
        ),
        extension_service=extension_service,
        extension_governance_enabled=extension_governance_enabled,
        mcp_runtime_bindings=tuple(settings.mcp_runtime_bindings),
        tenant_id=settings.account_id,
        persist_startup_snapshots=startup_convergence_allowed,
    )

    app.state.runtime = kernel
    app.state.runtime_settings = settings
    app.state.runtime_bearer_token = settings.runtime_bearer_token
    app.state.csrf_token = settings.csrf_token
    app.state.runtime_composition = composition
    app.state.mcp_client_supervisor = composition.mcp_supervisor
    app.state.permission_authority = permission_authority
    app.state.extension_service = extension_service
    app.state.managed_session_service = managed_session
    app.state.managed_session_startup_error = startup_session_error
    memory_service = MemoryService(
        kernel.database,
        initialize=startup_convergence_allowed,
    )
    app.state.memory_service = memory_service
    app.include_router(create_memory_router(memory_service))
    migration_quarantine_service = MigrationQuarantineService(
        Path(settings.database_path).expanduser().resolve().parent
    )
    app.state.migration_quarantine_service = migration_quarantine_service
    app.include_router(create_migration_quarantine_router(migration_quarantine_service))
    register_extension_routes(app, extension_service)
    replay_service = ReplayService(kernel, composition=composition)
    trace_projector = TraceProjector(
        replay_service, service_version=settings.product_version
    )
    audit_cipher = AuditPayloadCipher(
        _resolve_audit_encryption_key(
            settings,
            kernel=kernel,
            credential_vault=connector_vault,
            create=startup_convergence_allowed,
        )
    )
    audit_outbox = AuditOutbox(
        kernel.database,
        account_id=settings.account_id,
        cipher=audit_cipher,
        publisher=settings.audit_publisher,
        retention=AuditRetentionPolicy(
            raw_days=settings.audit_raw_retention_days,
            aggregate_days=settings.audit_aggregate_retention_days,
        ),
        initialize=startup_convergence_allowed,
    )
    # Restart recovery is idempotent. Once installed, the sink records each new
    # encrypted, redacted audit fact in the Event Store source transaction.
    if startup_convergence_allowed:
        audit_outbox.backfill_events()
        audit_outbox.backfill_permissions()
        audit_outbox.enforce_retention()
    trace_outbox = (
        TraceOutbox(
            kernel.database,
            account_id=settings.account_id,
            cipher=audit_cipher,
            projector=trace_projector,
            publisher=settings.trace_exporter,
            max_spans_per_batch=settings.trace_max_spans_per_batch,
            max_request_bytes=settings.trace_max_request_bytes,
            retention_days=settings.trace_retention_days,
            initialize=startup_convergence_allowed,
        )
        if settings.trace_exporter is not None
        else None
    )
    if trace_outbox is not None and startup_convergence_allowed:
        trace_outbox.backfill_events()
        trace_outbox.enforce_retention()
    kernel.events.event_sink = _TransactionalEventSinkFanout(
        audit_outbox,
        trace_outbox,
    )
    # Provider results that crossed the durable staging boundary are completed
    # locally before workers can lease turns.  Recovery never contacts a
    # Connector provider.
    connector_result_recovery = (
        connector_result_coordinator.recover_pending()
        if startup_convergence_allowed
        else {"completed": 0, "deferred": 0}
    )
    app.state.connector_result_coordinator = connector_result_coordinator
    app.state.connector_result_recovery = connector_result_recovery
    audit_dispatcher = (
        AuditDispatcher(
            audit_outbox,
            poll_seconds=settings.audit_dispatch_seconds,
        )
        if settings.audit_publisher is not None and startup_convergence_allowed
        else None
    )
    app.state.replay_service = replay_service
    app.state.trace_projector = trace_projector
    app.state.audit_outbox = audit_outbox
    trace_dispatcher = (
        TraceDispatcher(
            trace_outbox,
            poll_seconds=settings.trace_dispatch_seconds,
        )
        if trace_outbox is not None and startup_convergence_allowed
        else None
    )
    app.state.trace_outbox = trace_outbox
    app.state.trace_dispatcher = trace_dispatcher
    audit_publisher_lifecycle = (
        _AsyncResourceCloser(settings.audit_publisher)
        if settings.audit_publisher is not None
        and settings.close_audit_publisher_on_shutdown
        else None
    )
    app.state.audit_publisher_lifecycle = audit_publisher_lifecycle
    trace_exporter_lifecycle = (
        _AsyncResourceCloser(settings.trace_exporter)
        if settings.trace_exporter is not None
        and settings.close_trace_exporter_on_shutdown
        else None
    )
    app.state.trace_exporter_lifecycle = trace_exporter_lifecycle
    worker_supervisor: AgentWorkerSupervisor | None = None
    if model_worker_enabled and settings.model_gateway is not None:
        worker_supervisor = AgentWorkerSupervisor(
            AgentTurnWorker(
                kernel,
                gateway=settings.model_gateway,
                capabilities=composition.capability_service,
                extension_fence=(
                    composition.extension_invocation_fence
                    if extension_governance_enabled
                    else None
                ),
                turn_preparer=composition.prepare_turn,
                permission_mutation_lock=composition.permission_mutation_lock,
                permission_account_id=composition.permission_account_id,
                connector_uncertain_resolver=(
                    connector_composition.repository.resolve_uncertain_invocation
                ),
            ),
            concurrency=settings.model_worker_concurrency,
            idle_poll_seconds=settings.model_worker_poll_seconds,
            shutdown_timeout_seconds=settings.model_worker_shutdown_seconds,
            close_gateway_on_stop=settings.close_model_gateway_on_shutdown,
        )
    app.state.model_worker_supervisor = worker_supervisor
    gateway_lifecycle = (
        _AsyncResourceCloser(settings.model_gateway)
        if settings.model_gateway is not None
        and worker_supervisor is None
        and settings.close_model_gateway_on_shutdown
        else None
    )
    app.state.model_gateway_lifecycle = gateway_lifecycle
    image_client_lifecycle = (
        _AsyncResourceCloser(settings.image_orchestration_client)
        if settings.image_orchestration_client is not None
        and settings.close_image_orchestration_client_on_shutdown
        else None
    )
    app.state.image_orchestration_client_lifecycle = image_client_lifecycle
    app.state.update_service = update_service
    app.state.connector_composition = connector_composition
    connector_adapter_lifecycles = (
        tuple(
            _AsyncResourceCloser(adapter)
            for adapter in {
                id(value): value for value in settings.connector_adapters.values()
            }.values()
        )
        if settings.close_connector_adapters_on_shutdown
        else ()
    )
    app.state.connector_adapter_lifecycles = connector_adapter_lifecycles
    app.include_router(connector_composition.router, prefix="/api/v1")
    artifact_publisher = RuntimeArtifactEventPublisher(
        kernel.events,
        account_id=settings.account_id,
    )
    artifact_outbox = ArtifactEventOutbox(
        kernel.database,
        publisher=artifact_publisher,
    )
    artifact_outbox_supervisor = ArtifactEventOutboxSupervisor(
        artifact_outbox,
        execution_gate=runtime_execution_gate,
    )
    app.state.artifact_service = artifact_service
    app.state.input_attachment_service = input_attachment_service
    app.state.artifact_event_outbox = artifact_outbox
    app.state.artifact_event_outbox_supervisor = artifact_outbox_supervisor
    app.state.output_service = output_service
    output_service_lifecycle = _AsyncResourceCloser(output_service)
    app.state.output_service_lifecycle = output_service_lifecycle
    app.include_router(
        create_output_router(
            output_service,
            folder_picker=settings.project_folder_picker,
        )
    )
    artifact_action_executor = ArtifactActionExecutor(
        artifact_service,
        launcher=settings.artifact_action_launcher,
        create_storage=startup_convergence_allowed,
    )
    app.state.artifact_action_executor = artifact_action_executor
    image_tool_backend = RuntimeImageToolBackend(
        database_path=kernel.database,
        artifacts=artifact_service,
        kernel=kernel,
        account_id=settings.account_id,
        client=settings.image_orchestration_client,
    )
    composition.capability_service.bind_invocation_backend(image_tool_backend)
    app.state.image_tool_backend = image_tool_backend
    retouch_supervisor: RetouchWorkerSupervisor | None = None
    if settings.retouch_adapter is not None and _retouch_capability_available(settings):
        retouch_bridge = RuntimeRetouchBridge(
            kernel,
            snapshot_context_provider=(
                lambda **values: (
                    composition.prepare_turn(values["turn_request"]).snapshot_context
                )
            ),
            permission_mutation_lock=composition.permission_mutation_lock,
        )
        retouch_coordinator: Any = RetouchCoordinator(artifact_service, retouch_bridge)
        retouch_supervisor = RetouchWorkerSupervisor(
            RetouchWorker(retouch_coordinator, settings.retouch_adapter),
            concurrency=settings.retouch_worker_concurrency,
            idle_poll_seconds=settings.retouch_worker_poll_seconds,
            shutdown_timeout_seconds=settings.retouch_worker_shutdown_seconds,
            close_adapter_on_stop=settings.close_retouch_adapter_on_shutdown,
        )
        retouch_coordinator.notify = retouch_supervisor.notify
    else:
        retouch_coordinator = _UnavailableRetouchCoordinator()
    app.state.retouch_coordinator = retouch_coordinator
    app.state.retouch_worker_supervisor = retouch_supervisor
    app.include_router(
        create_artifact_router(
            artifact_service,
            event_sink=artifact_outbox,
            account_id=settings.account_id,
            retouch_coordinator=retouch_coordinator,
            action_executor=artifact_action_executor,
        )
    )
    share_service: ShareSnapshotService | None = None
    share_supervisor: ShareWorkerSupervisor | None = None
    if settings.share_publisher is not None:
        share_repository = ShareRepository(kernel.database, jobs=kernel.jobs)
        share_service = ShareSnapshotService(
            kernel,
            repository=share_repository,
            publisher=settings.share_publisher,
            account_id=settings.account_id,
            allowed_public_hosts=settings.share_public_hosts,
            artifacts=artifact_service,
            max_attempts=settings.share_worker_max_attempts,
            operation_deadline_seconds=settings.share_operation_deadline_seconds,
        )
        share_supervisor = ShareWorkerSupervisor(
            ShareOperationWorker(
                share_repository,
                settings.share_publisher,
                allowed_public_hosts=settings.share_public_hosts,
                lease_seconds=settings.share_worker_lease_seconds,
                retry_delay_seconds=settings.share_worker_retry_seconds,
                media_loader=artifact_service.blobs.read_bytes,
                execution_gate=runtime_execution_gate,
            ),
            concurrency=settings.share_worker_concurrency,
            idle_poll_seconds=settings.share_worker_poll_seconds,
            shutdown_timeout_seconds=settings.share_worker_shutdown_seconds,
            execution_allowed=lambda: runtime_execution_gate.snapshot().healthy,
            execution_gate=runtime_execution_gate,
        )
        share_service.notify = share_supervisor.notify
        app.include_router(create_share_router(share_service), prefix="/api/v1")
    app.state.share_service = share_service
    app.state.share_worker_supervisor = share_supervisor
    device_authorization_supervisor: DeviceAuthorizationSupervisor | None = None
    if settings.device_authorization_service is not None:

        def device_authenticated() -> bool:
            if managed_session is None:
                return bool(settings.authenticated)
            try:
                current_managed_session()
            except (ManagedSessionError, _ManagedSessionRestartRequired):
                return False
            return True

        def schedule_device_reload(flow) -> None:
            callback_error: Exception | None = None
            try:
                if (
                    settings.first_install_registration_recorder is not None
                    and flow.session_generation is not None
                ):
                    snapshot = settings.device_authorization_service.session.snapshot()
                    if snapshot.generation != flow.session_generation:
                        raise RuntimeError(
                            "device authorization session generation changed"
                        )
                    settings.first_install_registration_recorder(
                        _first_install_registration(snapshot)
                    )
            except Exception as error:
                callback_error = error
            finally:
                if (
                    settings.session_reload_requester is not None
                    and flow.session_generation is not None
                ):
                    settings.session_reload_requester(
                        f"session-login:{flow.session_generation}"
                    )
            if callback_error is not None:
                raise callback_error

        device_authorization_supervisor = DeviceAuthorizationSupervisor(
            settings.device_authorization_service,
            poll_seconds=settings.device_authorization_poll_seconds,
            on_authorized=schedule_device_reload,
            maintenance_allowed=lambda: runtime_execution_gate.snapshot().healthy,
            execution_gate=runtime_execution_gate,
            close_broker_on_stop=(
                settings.close_device_authorization_broker_on_shutdown
            ),
        )
        app.include_router(
            create_device_authorization_router(
                settings.device_authorization_service,
                supervisor=device_authorization_supervisor,
                authenticated=device_authenticated,
                reload_requester=settings.session_reload_requester,
            ),
            prefix="/api/v1",
        )
    app.state.device_authorization_service = settings.device_authorization_service
    app.state.device_authorization_supervisor = device_authorization_supervisor
    session_refresh_supervisor: ManagedSessionRefreshSupervisor | None = None
    if session_refresh_service is not None:
        session_refresh_supervisor = ManagedSessionRefreshSupervisor(
            session_refresh_service,
            poll_seconds=settings.managed_session_refresh_poll_seconds,
        )
    app.state.managed_session_refresh_service = session_refresh_service
    app.state.managed_session_refresh_supervisor = session_refresh_supervisor

    def system_worker_metrics() -> dict[str, Any]:
        if worker_supervisor is None:
            return {
                "state": "paused",
                "reason": "model_worker_not_configured",
                "desired_workers": 0,
                "live_workers": 0,
                "restarted_slots": 0,
                "gate_status": runtime_execution_gate.snapshot().status,
            }
        snapshot = worker_supervisor.snapshot()
        return {
            "state": "ready" if snapshot.running else "paused",
            "concurrency": snapshot.concurrency,
            "desired_workers": snapshot.desired_workers,
            "live_workers": snapshot.live_workers,
            "restarted_slots": snapshot.restarted_slots,
            "gate_status": runtime_execution_gate.snapshot().status,
            "completed_runs": snapshot.completed_runs,
            "failed_runs": snapshot.failed_runs,
            "last_outcome": (
                snapshot.last_outcome.value
                if snapshot.last_outcome is not None
                else None
            ),
            "has_last_error": snapshot.last_error is not None,
        }

    def system_invariant_metrics() -> dict[str, Any]:
        snapshot = runtime_execution_gate.snapshot()
        return {
            "status": snapshot.status,
            "checked_at": (
                None if snapshot.checked_at is None else snapshot.checked_at.isoformat()
            ),
            "violation_codes": list(snapshot.violation_codes),
            "violation_count": snapshot.violation_count,
            "last_error_code": snapshot.last_error_code,
        }

    def system_lifecycle_metrics() -> dict[str, Any]:
        failures = tuple(getattr(app.state, "logout_shutdown_failures", ()))
        return {
            "state": "degraded" if failures else "ready",
            "failure_count": len(failures),
            "failures": [
                {
                    "service": failure.service,
                    "reason": failure.reason,
                    "error_code": failure.error_code,
                }
                for failure in failures
            ],
        }

    def system_extension_metrics() -> dict[str, Any]:
        snapshot = extension_service.project_snapshot()
        unhealthy = sum(
            item.health in {"degraded", "unhealthy", "circuit_open"}
            or item.status == "quarantined"
            for item in snapshot.items
        )
        return {
            "state": "degraded" if unhealthy else "ready",
            "total": len(snapshot.items),
            "enabled": sum(item.status == "enabled" for item in snapshot.items),
            "unhealthy": unhealthy,
            "snapshot_id": snapshot.snapshot_id,
        }

    def system_connector_metrics() -> dict[str, Any]:
        catalog = connector_composition.service.catalog()
        instances = [instance for item in catalog for instance in item.instances]
        degraded = sum(
            instance.health.value in {"degraded", "error"} for instance in instances
        )
        outbox = connector_composition.service.outbox_delivery_health()
        delivery_degraded = outbox.status in {"degraded", "stuck"}
        return {
            "state": "degraded" if degraded or delivery_degraded else "ready",
            "definitions": len(catalog),
            "instances": len(instances),
            "connected": sum(
                instance.health.value == "connected" for instance in instances
            ),
            "degraded": degraded,
            "outbox_status": outbox.status,
            "outbox_pending": outbox.pending,
            "outbox_active": outbox.active,
            "outbox_requested_generation": outbox.requested_generation,
            "outbox_completed_generation": outbox.completed_generation,
            "outbox_last_error_code": outbox.last_error_code,
        }

    def system_memory_metrics() -> dict[str, Any]:
        snapshot = memory_service.snapshot()
        return {
            "state": "ready",
            "revision": snapshot.revision,
            "active_learned_records": snapshot.active_learned_records,
            "active_user_files": snapshot.active_user_files,
            "factory_records": snapshot.factory_records,
        }

    def system_update_metrics() -> dict[str, Any]:
        snapshot = (
            update_service.snapshot()
            if update_service is not None
            else UpdateSnapshot(current_version=settings.product_version, state="idle")
        )
        return {
            "state": "degraded" if snapshot.state == "failed" else "ready",
            "update_state": snapshot.state,
            "current_version": snapshot.current_version,
            "target_version": snapshot.target_version,
        }

    def system_output_metrics() -> dict[str, Any]:
        preference = output_service.project_preference()
        return {
            "state": "ready",
            "location_alias": preference.location_alias.value,
            "revision": preference.revision,
        }

    def system_artifact_event_metrics() -> dict[str, Any]:
        snapshot = artifact_outbox_supervisor.snapshot()
        pending = len(artifact_outbox.pending(limit=1000))
        return {
            "state": "degraded" if snapshot.failures or pending else "ready",
            "running": snapshot.running,
            "pending": pending,
            "cycles": snapshot.cycles,
            "published": snapshot.published,
            "failures": snapshot.failures,
            "last_error_code": snapshot.last_error_code,
        }

    def system_audit_metrics() -> dict[str, Any]:
        pending = audit_outbox.count(pending_only=True)
        dispatcher_error = (
            audit_dispatcher.last_error_code if audit_dispatcher is not None else None
        )
        return {
            "state": "degraded"
            if dispatcher_error
            else ("ready" if audit_dispatcher is not None else "paused"),
            "publisher_configured": settings.audit_publisher is not None,
            "dispatcher_running": (
                audit_dispatcher.running if audit_dispatcher is not None else False
            ),
            "pending": pending,
            "last_error_code": dispatcher_error,
        }

    def system_trace_metrics() -> dict[str, Any]:
        if trace_outbox is None:
            return {
                "state": "disabled",
                "exporter_configured": False,
                "dispatcher_running": False,
                "pending": 0,
            }
        dispatcher_error = (
            trace_dispatcher.last_error_code if trace_dispatcher is not None else None
        )
        return {
            "state": "degraded"
            if dispatcher_error
            else ("ready" if trace_dispatcher is not None else "paused"),
            "exporter_configured": settings.trace_exporter is not None,
            "dispatcher_running": (
                trace_dispatcher.running if trace_dispatcher is not None else False
            ),
            "pending": trace_outbox.count(pending_only=True),
            "last_error_code": dispatcher_error,
        }

    def system_share_metrics() -> dict[str, Any]:
        if share_supervisor is None:
            return {"state": "disabled", "running": False}
        snapshot = share_supervisor.snapshot
        return {
            "state": "degraded"
            if snapshot.last_error
            else ("ready" if snapshot.running else "paused"),
            "running": snapshot.running,
            "concurrency": snapshot.concurrency,
            "completed_runs": snapshot.completed_runs,
            "retry_runs": snapshot.retry_runs,
            "failed_runs": snapshot.failed_runs,
            "last_outcome": (
                snapshot.last_outcome.value
                if snapshot.last_outcome is not None
                else None
            ),
            "has_last_error": snapshot.last_error is not None,
        }

    def system_retouch_metrics() -> dict[str, Any]:
        if retouch_supervisor is None:
            return {"state": "disabled", "running": False}
        snapshot = retouch_supervisor.snapshot()
        return {
            "state": "degraded"
            if snapshot.last_error
            else ("ready" if snapshot.running else "paused"),
            "running": snapshot.running,
            "concurrency": snapshot.concurrency,
            "completed_runs": snapshot.completed_runs,
            "retry_runs": snapshot.retry_runs,
            "failed_runs": snapshot.failed_runs,
            "last_outcome": (
                snapshot.last_outcome.value
                if snapshot.last_outcome is not None
                else None
            ),
            "has_last_error": snapshot.last_error is not None,
        }

    def system_device_authorization_metrics() -> dict[str, Any]:
        if device_authorization_supervisor is None:
            return {"state": "disabled", "running": False, "due_flows": 0}
        due = settings.device_authorization_service.due_flow_ids(
            limit=device_authorization_supervisor.max_concurrent_polls
        )
        return {
            "state": "ready" if device_authorization_supervisor.running else "paused",
            "running": device_authorization_supervisor.running,
            "due_flows": len(due),
            "max_concurrent_polls": (
                device_authorization_supervisor.max_concurrent_polls
            ),
            "broker_closed": device_authorization_supervisor.broker_closed,
        }

    def system_session_refresh_metrics() -> dict[str, Any]:
        if session_refresh_service is None:
            return {"state": "disabled", "running": False, "attempt": 0}
        projection = session_refresh_service.repository.projection()
        return {
            "state": projection.status,
            "running": bool(
                session_refresh_supervisor and session_refresh_supervisor.running
            ),
            "attempt": projection.attempt,
            "reauthorization_required": (
                projection.status == "reauthorization_required"
            ),
            "has_error": projection.error_code is not None,
        }

    def system_image_metrics() -> dict[str, Any]:
        with kernel.database.reader() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM image_tool_publications "
                "GROUP BY status ORDER BY status"
            ).fetchall()
        publications = {str(row["status"]): int(row["count"]) for row in rows}
        configured = image_tool_backend.client is not None
        return {
            "state": "ready" if configured else "paused",
            "provider_configured": configured,
            "publications": publications,
            "active_publications": publications.get("publishing", 0),
            "completed_publications": publications.get("completed", 0),
            "publication_lease_seconds": image_tool_backend.publication_lease_seconds,
        }

    runtime_signal_registry = RuntimeSignalRegistry()

    @contextmanager
    def system_observability_persistence_scope():
        with (
            runtime_execution_gate.new_admission(
                scope="system_observability",
                subject="health_sample",
            ) as permit,
            transaction_commit_guard(
                lambda: runtime_execution_gate.assert_permit(permit)
            ),
        ):
            yield

    system_observability_service = SystemObservabilityService(
        kernel.database,
        registry=runtime_signal_registry,
        providers={
            "agent_worker": system_worker_metrics,
            "connectors": system_connector_metrics,
            "extensions": system_extension_metrics,
            "memory": system_memory_metrics,
            "output": system_output_metrics,
            "updates": system_update_metrics,
            "invariant": system_invariant_metrics,
            "lifecycle": system_lifecycle_metrics,
            "artifact_events": system_artifact_event_metrics,
            "audit": system_audit_metrics,
            "traces": system_trace_metrics,
            "shares": system_share_metrics,
            "retouch": system_retouch_metrics,
            "device_authorization": system_device_authorization_metrics,
            "managed_session_refresh": system_session_refresh_metrics,
            "images": system_image_metrics,
        },
        persistence_allowed=lambda: runtime_execution_gate.snapshot().healthy,
        persistence_scope=system_observability_persistence_scope,
    )
    system_observability_supervisor = SystemObservabilitySupervisor(
        system_observability_service
    )
    app.state.runtime_signal_registry = runtime_signal_registry
    app.state.system_observability_service = system_observability_service
    app.state.system_observability_supervisor = system_observability_supervisor
    app.include_router(create_system_observability_router(system_observability_service))

    share_publisher_lifecycle = (
        settings.share_publisher
        if settings.share_publisher is not None
        and callable(getattr(settings.share_publisher, "start", None))
        and callable(getattr(settings.share_publisher, "stop", None))
        else None
    )
    lifecycle_candidates: list[tuple[int, str, Any | None]] = [
        (1, "interaction_maintenance", interaction_maintenance_supervisor),
        # Stop producers first, flush durable delivery while the Runtime epoch
        # is still healthy, close the invariant gate, then close transports.
        (3, "runtime_invariant", invariant_supervisor),
        (1, "agent_worker", worker_supervisor),
        (1, "mcp", composition.mcp_supervisor),
        (4, "model_gateway", gateway_lifecycle),
        (4, "image_gateway", image_client_lifecycle),
        (1, "retouch_worker", retouch_supervisor),
        (1, "update", update_service),
        (4, "output_filesystem", output_service_lifecycle),
    ]
    lifecycle_candidates.extend(
        (4, f"connector_adapter_{index}", service)
        for index, service in enumerate(connector_adapter_lifecycles)
    )
    lifecycle_candidates.extend(
        [
            (2, "connector_maintenance", connector_composition.maintenance),
            (4, "audit_publisher", audit_publisher_lifecycle),
            (1, "audit_dispatcher", audit_dispatcher),
            (2, "artifact_event_outbox", artifact_outbox_supervisor),
            (4, "trace_exporter", trace_exporter_lifecycle),
            (1, "trace_dispatcher", trace_dispatcher),
            (4, "share_publisher", share_publisher_lifecycle),
            (1, "share_worker", share_supervisor),
            (1, "device_authorization", device_authorization_supervisor),
            (1, "managed_session_refresh", session_refresh_supervisor),
            (1, "system_observability", system_observability_supervisor),
        ]
    )
    lifecycle_services = [
        (phase, name, service)
        for phase, name, service in lifecycle_candidates
        if service is not None
    ]
    app.state.runtime_shutdown_failures = ()
    app.state.logout_shutdown_failures = ()
    if lifecycle_services or managed_session is not None:
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def runtime_lifespan(current_app: FastAPI):
            owned_for_shutdown: list[tuple[int, str, Any]] = []
            async with original_lifespan(current_app):
                try:
                    managed_session_ready = not managed_mode
                    managed_session_snapshot: ManagedSessionSnapshot | None = None
                    if managed_session is not None:
                        try:
                            if runtime_execution_gate.snapshot().healthy:
                                await asyncio.to_thread(managed_session.recover)
                                managed_session_snapshot = await asyncio.to_thread(
                                    current_managed_session
                                )
                            else:
                                managed_session_snapshot = await asyncio.to_thread(
                                    managed_session.read_snapshot
                                )
                        except (
                            ManagedSessionError,
                            _ManagedSessionRestartRequired,
                        ):
                            managed_session_ready = False
                        else:
                            managed_session_ready = True
                    for shutdown_phase, service_name, service in lifecycle_services:
                        # Some supervisors own an already-created network
                        # adapter even when an unauthenticated managed shell
                        # must not start their worker loops. Keep that resource
                        # in the shutdown stack so first-login/expired-session
                        # processes cannot leak transports.
                        owned_for_shutdown.append(
                            (shutdown_phase, service_name, service)
                        )
                        if not runtime_execution_gate.snapshot().healthy and all(
                            service is not candidate
                            for candidate in (
                                invariant_supervisor,
                                system_observability_supervisor,
                            )
                        ):
                            continue
                        if (
                            any(
                                service is candidate
                                for candidate in (
                                    worker_supervisor,
                                    retouch_supervisor,
                                    share_supervisor,
                                )
                            )
                            and not managed_session_ready
                        ):
                            continue
                        await service.start()
                    if (
                        managed_session_snapshot is not None
                        and runtime_execution_gate.snapshot().healthy
                        and settings.first_install_runtime_ready_recorder is not None
                    ):
                        await asyncio.to_thread(
                            settings.first_install_runtime_ready_recorder,
                            _first_install_registration(managed_session_snapshot),
                        )
                    yield
                finally:
                    current_app.state.runtime_shutdown_failures = (
                        await stop_service_phases_isolated(
                            owned_for_shutdown,
                            timeout_seconds=settings.lifecycle_shutdown_seconds,
                        )
                    )

        app.router.lifespan_context = runtime_lifespan

    def security_error(status_code: int, detail: str) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={"detail": detail},
            headers={"Cache-Control": "no-store"},
        )

    recovery_mutation_scopes: dict[tuple[str, str], RecoveryExecutionScope] = {
        ("POST", "/api/v1/session/logout"): "session_logout",
        ("POST", "/api/v1/update/activate"): "update_activate",
    }

    def require_recovery_permit(
        request: Request,
        scope: RecoveryExecutionScope,
    ) -> RecoveryExecutionPermit:
        permit = getattr(request.state, "recovery_execution_permit", None)
        if not isinstance(permit, RecoveryExecutionPermit) or permit.scope != scope:
            raise RecoveryExecutionDenied(
                "request has no matching recovery execution permit"
            )
        recovery_execution_gate.assert_permit(permit)
        return permit

    def is_local_artifact_action_mutation(request: Request) -> bool:
        """Recognize the only local mutation allowed without a cloud lease.

        Runtime bearer, loopback Origin, CSRF, server-selected launch targets,
        and the account-scoped public Artifact projection still protect the
        request. Keep the identifier check coupled to the ID authority rather
        than to a historical UUID/ULID representation.
        """

        if request.method != "POST":
            return False
        match = re.fullmatch(
            r"/api/v1/artifacts/([^/]+)/actions/(open|reveal)",
            request.url.path,
        )
        return match is not None and is_id(match.group(1), "art")

    @app.middleware("http")
    async def local_origin_and_cache_policy(request: Request, call_next):
        commit_guard = None
        runtime_owner_probe = (
            request.method == "GET"
            and request.url.path == "/api/v1/runtime-owner"
        )
        if request.url.path.startswith("/api/v1") and not runtime_owner_probe:
            recovery_scope = recovery_mutation_scopes.get(
                (request.method, request.url.path)
            )
            oauth_callback = (
                request.method == "GET"
                and request.url.path == "/api/v1/connectors/oauth/callback"
            )
            if not oauth_callback:
                authorization = request.headers.get("authorization", "")
                scheme, _, supplied = authorization.partition(" ")
                if (
                    scheme.lower() != "bearer"
                    or not supplied
                    or not secrets.compare_digest(
                        supplied, settings.runtime_bearer_token or ""
                    )
                ):
                    return security_error(401, "runtime bearer token is required")
            mutation_request = (
                request.method not in {"GET", "HEAD", "OPTIONS"} or oauth_callback
            )
            device_login_mutation = (
                request.method == "POST"
                and settings.device_authorization_service is not None
                and (
                    request.url.path == "/api/v1/session/login"
                    or request.url.path == "/api/v1/session/device"
                    or re.fullmatch(
                        r"/api/v1/session/device/devflow_[0-9a-f]{32}/poll",
                        request.url.path,
                    )
                    is not None
                )
            )
            local_artifact_action_mutation = is_local_artifact_action_mutation(request)
            local_verified_update_activation = recovery_scope == "update_activate"
            if mutation_request and not (
                device_login_mutation
                or local_artifact_action_mutation
                or local_verified_update_activation
            ):
                if managed_mode:
                    if managed_session is None:
                        return security_error(
                            401, "managed account authentication is required"
                        )
                    try:
                        await asyncio.to_thread(current_managed_session)
                    except _ManagedSessionRestartRequired:
                        return JSONResponse(
                            status_code=409,
                            content={
                                "detail": "managed account changed; restart EcoreX to continue",
                                "code": "managed_session_restart_required",
                            },
                            headers={"Cache-Control": "no-store"},
                        )
                    except ManagedSessionError:
                        return security_error(
                            401, "managed account authentication is required"
                        )
                elif not settings.authenticated:
                    return security_error(401, "account authentication is required")
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                if origin not in settings.webui_origins:
                    return security_error(
                        403, "request origin is not the configured WebUI"
                    )
                csrf = request.headers.get("x-ecorex-csrf", "")
                if not csrf or not secrets.compare_digest(
                    csrf, settings.csrf_token or ""
                ):
                    return security_error(403, "valid CSRF token is required")
            # Mutation authority is semantic, not tied to the HTTP verb:
            # Connector OAuth callback is a GET that persists credentials and
            # state.  Keep this fence after authentication/CSRF handling so
            # read-only mode never becomes an authorization bypass.
            if (
                mutation_request
                and runtime_execution_gate.snapshot().status == "critical"
                and recovery_scope is None
            ):
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": (
                            "运行状态校验未通过，EcoreX 已进入只读保护；"
                            "历史记录和诊断仍可查看。"
                        ),
                        "code": "RUNTIME_READ_ONLY",
                    },
                    headers={"Cache-Control": "no-store"},
                )
            if mutation_request and recovery_scope is not None:
                try:
                    recovery_permit = recovery_execution_gate.issue_permit(
                        scope=recovery_scope,
                        subject=(
                            f"{request.method.casefold()}:{secrets.token_hex(16)}"
                        ),
                    )
                except RecoveryExecutionDenied:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "detail": "本地恢复通道已关闭，本次操作未执行。",
                            "code": "RECOVERY_LANE_CLOSED",
                        },
                        headers={"Cache-Control": "no-store"},
                    )
                request.state.recovery_execution_permit = recovery_permit

                def validate_recovery_commit() -> None:
                    recovery_execution_gate.assert_permit(recovery_permit)

                commit_guard = validate_recovery_commit
            elif mutation_request:
                try:
                    permit = runtime_execution_gate.issue_permit(
                        scope="http_mutation",
                        subject=(
                            f"{request.method.casefold()}:{secrets.token_hex(16)}"
                        ),
                    )
                except RuntimeExecutionDenied:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "detail": (
                                "运行状态校验未通过，EcoreX 已进入只读保护；"
                                "请刷新状态后再重试。"
                            ),
                            "code": "RUNTIME_READ_ONLY",
                        },
                        headers={"Cache-Control": "no-store"},
                    )

                def validate_commit() -> None:
                    runtime_execution_gate.assert_permit(permit)

                request.state.runtime_execution_permit = permit
                commit_guard = validate_commit
        if commit_guard is None:
            response = await call_next(request)
        else:
            with transaction_commit_guard(commit_guard):
                response = await call_next(request)
        if request.url.path.startswith("/api/v1"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request: Request, error: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(RuntimeExecutionDenied)
    async def execution_epoch_handler(
        _request: Request, _error: RuntimeExecutionDenied
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "运行状态已切换为只读保护，本次修改未提交。",
                "code": "RUNTIME_READ_ONLY",
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RecoveryExecutionDenied)
    async def recovery_execution_handler(
        _request: Request,
        _error: RecoveryExecutionDenied,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "本地恢复通道已关闭，本次操作未提交。",
                "code": "RECOVERY_LANE_CLOSED",
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI's default payload echoes the rejected input.  Apart from
        # leaking user text, an unpaired Unicode surrogate makes Starlette's
        # JSON encoder fail while trying to return the 422.  Keep only the
        # stable diagnostic fields needed by the WebUI.
        def safe_text(value: object, fallback: str) -> str:
            text = str(value) if value is not None else fallback
            return text.encode("utf-8", errors="replace").decode("utf-8")

        detail = [
            {
                "type": safe_text(item.get("type"), "validation_error"),
                "loc": [
                    value if isinstance(value, int) else safe_text(value, "unknown")
                    for value in item.get("loc", ())
                ],
                "msg": safe_text(item.get("msg"), "Request validation failed"),
            }
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request: Request, error: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(RuntimeDomainError)
    async def domain_handler(_request: Request, error: RuntimeDomainError):
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @app.exception_handler(ProjectNotFound)
    async def project_not_found_handler(_request: Request, _error: ProjectNotFound):
        return JSONResponse(status_code=404, content={"detail": "project_not_found"})

    @app.exception_handler(ProjectFolderSelectionCancelled)
    async def project_selection_cancelled_handler(
        _request: Request,
        _error: ProjectFolderSelectionCancelled,
    ):
        return JSONResponse(
            status_code=409,
            content={"detail": "project_folder_selection_cancelled"},
        )

    @app.exception_handler(ReplayIntegrityError)
    async def replay_integrity_handler(_request: Request, _error: ReplayIntegrityError):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Event Store replay integrity verification failed",
                "code": "replay_integrity_error",
            },
        )

    @app.exception_handler(AuditIntegrityError)
    async def audit_integrity_handler(_request: Request, _error: AuditIntegrityError):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Audit outbox integrity verification failed",
                "code": "audit_integrity_error",
            },
        )

    @app.get("/api/v1/bootstrap", response_model=BootstrapResponse)
    async def bootstrap() -> BootstrapResponse:
        current_update = (
            update_service.snapshot()
            if update_service is not None
            else bootstrap_snapshot.update
        )
        current_connectors = project_connector_catalog(
            connector_registry,
            connector_composition.service.catalog(),
        )
        current_permissions = permission_authority.current()
        current_extensions = ExtensionCatalogSnapshot.model_validate(
            extension_service.project_snapshot().to_dict()
        )
        if managed_mode:
            session_snapshot: ManagedSessionSnapshot | None = None
            if managed_session is not None:
                try:
                    session_snapshot = current_managed_session()
                except _ManagedSessionRestartRequired as error:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "managed_session_restart_required",
                            "message": "managed account changed; restart EcoreX to continue",
                        },
                    ) from error
                except ManagedSessionError:
                    session_snapshot = None
            context = session_snapshot or startup_data_scope
            if session_snapshot is None:
                current_models = _empty_model_catalog(context)
                current_model_service = ModelServiceSnapshot(
                    state="unavailable", reason="managed_session_unavailable"
                )
            else:
                filtered = _filter_model_catalog(
                    builtin_models,
                    frozenset(session_snapshot.allowed_model_ids),
                )
                current_models = (
                    project_model_catalog(filtered)
                    if filtered is not None
                    else _empty_model_catalog(session_snapshot)
                )
                if isinstance(settings.model_gateway, ManagedModelGatewayClient):
                    try:
                        cloud_catalog = await asyncio.wait_for(
                            settings.model_gateway.catalog(), timeout=3.0
                        )
                        current_models = _overlay_cloud_model_catalog(
                            current_models, cloud_catalog
                        )
                    except Exception:
                        # A transient catalog refresh cannot corrupt the signed
                        # local lease. Streaming still resolves the active cloud
                        # revision per request and fails closed when unavailable.
                        pass
                if settings.model_gateway is None:
                    current_model_service = ModelServiceSnapshot(
                        state="unavailable", reason="managed_gateway_not_configured"
                    )
                elif not current_models.chat:
                    current_model_service = ModelServiceSnapshot(
                        state="unavailable",
                        reason="signed_model_allowlist_empty",
                    )
                else:
                    current_model_service = ModelServiceSnapshot(
                        state="ready", reason=None
                    )
            if session_snapshot is None:
                current_retouch_service = ModelServiceSnapshot(
                    state="unavailable", reason="managed_session_unavailable"
                )
            elif not _retouch_capability_available(settings):
                current_retouch_service = ModelServiceSnapshot(
                    state="unavailable",
                    reason="image_capability_pack_not_installed",
                )
            elif settings.retouch_adapter is None:
                current_retouch_service = ModelServiceSnapshot(
                    state="unavailable", reason="managed_image_edit_not_configured"
                )
            elif not current_models.image:
                current_retouch_service = ModelServiceSnapshot(
                    state="unavailable", reason="signed_image_model_not_allowed"
                )
            else:
                current_retouch_service = ModelServiceSnapshot(
                    state="ready", reason=None
                )
            return _bootstrap(
                settings,
                models=current_models,
                model_service=current_model_service,
                permissions=current_permissions,
                update=current_update,
                connectors=current_connectors,
                extensions=current_extensions,
                issued_at=utc_now(),
                managed_session=context,
                managed_authenticated=session_snapshot is not None,
                retouch_service=current_retouch_service,
            )
        return bootstrap_snapshot.model_copy(
            update={
                "server_time": utc_now(),
                "permissions": current_permissions,
                "update": current_update,
                "connectors": current_connectors,
                "extensions": current_extensions,
            }
        )

    def require_model_task_service() -> None:
        if not managed_mode:
            return
        try:
            session_snapshot = current_managed_session()
        except _ManagedSessionRestartRequired as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "managed_session_restart_required",
                    "message": "managed account changed; restart EcoreX to continue",
                },
            ) from error
        except ManagedSessionError as error:
            raise HTTPException(
                status_code=401,
                detail="managed account authentication is required",
            ) from error
        filtered = _filter_model_catalog(
            builtin_models,
            frozenset(session_snapshot.allowed_model_ids),
        )
        if (
            settings.model_gateway is None
            or filtered is None
            or not filtered.for_modality(ModelModality.CHAT)
            or worker_supervisor is None
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "managed_model_service_unavailable",
                    "message": "managed model service is unavailable",
                },
            )

    @app.post(
        "/api/v1/session/logout",
        response_model=LogoutSessionResponse,
    )
    async def logout_session(
        payload: LogoutSessionRequest,
        http_request: Request,
    ) -> LogoutSessionResponse:
        recovery_permit = require_recovery_permit(
            http_request,
            "session_logout",
        )
        if managed_session is None:
            raise HTTPException(
                status_code=503,
                detail="managed session service is not configured",
            )
        current = await asyncio.to_thread(current_managed_session)
        recovery_execution_gate.assert_permit(recovery_permit)
        if not hmac.compare_digest(current.lease_digest, payload.lease_digest):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "session_lease_changed",
                    "message": "session lease changed; refresh before logging out",
                },
            )
        try:
            if settings.device_authorization_service is not None:
                receipt = await settings.device_authorization_service.revoke_and_logout(
                    client_request_id=payload.client_request_id,
                    expected_lease_digest=payload.lease_digest,
                )
            else:
                receipt = await asyncio.to_thread(
                    managed_session.logout,
                    client_request_id=payload.client_request_id,
                    expected_lease_digest=payload.lease_digest,
                )
            recovery_execution_gate.assert_permit(recovery_permit)
        except DeviceAuthorizationUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "remote_revocation_pending",
                    "message": "remote session revocation is pending; retry logout",
                },
            ) from error
        except (DeviceAuthorizationConflict, SessionConflict) as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "session_lease_changed",
                    "message": "session lease changed; refresh before logging out",
                },
            ) from error
        managed_runtime_state["logged_out"] = True
        settings.authenticated = False
        # Logout removes the managed credential lease before this point. Stop
        # every account-scoped executor, maintenance loop and transport even
        # when a host reload callback is unavailable. The updater remains
        # local and usable so a signed security update can still be activated.
        app.state.logout_shutdown_failures = await stop_service_phases_isolated(
            (
                (phase, name, service)
                for phase, name, service in lifecycle_services
                if name not in {"update", "output_filesystem"}
            ),
            timeout_seconds=settings.lifecycle_shutdown_seconds,
        )
        recovery_execution_gate.assert_permit(recovery_permit)
        restart_scheduled = False
        if settings.session_reload_requester is not None:
            try:
                restart_scheduled = bool(
                    settings.session_reload_requester(
                        f"session-logout:{receipt.generation}"
                    )
                )
            except Exception:
                restart_scheduled = False
        return LogoutSessionResponse(
            generation=receipt.generation,
            restart_scheduled=restart_scheduled,
        )

    @app.get("/api/v1/update", response_model=CheckUpdateResponse)
    def update_status() -> CheckUpdateResponse:
        snapshot = (
            update_service.snapshot()
            if update_service is not None
            else bootstrap_snapshot.update
        )
        return CheckUpdateResponse(update=snapshot)

    @app.post("/api/v1/update/check", response_model=CheckUpdateResponse)
    async def check_update(http_request: Request) -> CheckUpdateResponse:
        if update_service is None:
            raise HTTPException(
                status_code=503, detail="update service is not configured"
            )
        runtime_permit = getattr(
            http_request.state,
            "runtime_execution_permit",
            None,
        )
        runtime_execution_gate.assert_permit(runtime_permit)
        try:
            snapshot = await update_service.check_now()
            runtime_execution_gate.assert_permit(runtime_permit)
        except RuntimeExecutionDenied:
            raise
        except RuntimeError as error:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": error.__class__.__name__.casefold(),
                    "message": "update check failed",
                },
            ) from error
        return CheckUpdateResponse(update=snapshot)

    @app.post("/api/v1/update/activate", response_model=ActivateUpdateResponse)
    async def activate_update(
        payload: ActivateUpdateRequest,
        http_request: Request,
    ) -> ActivateUpdateResponse:
        if update_service is None:
            raise HTTPException(
                status_code=503, detail="update service is not configured"
            )
        recovery_permit = require_recovery_permit(
            http_request,
            "update_activate",
        )
        local_activate = getattr(update_service, "activate_verified_local", None)
        if not callable(local_activate):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "local_update_activation_unavailable",
                    "message": "verified local update activation is unavailable",
                },
            )

        def assert_recovery_execution() -> None:
            recovery_execution_gate.assert_permit(recovery_permit)

        try:
            response = await local_activate(
                transaction_id=payload.transaction_id,
                client_request_id=payload.client_request_id,
                execution_guard=assert_recovery_execution,
            )
            assert_recovery_execution()
            return response
        except RecoveryExecutionDenied:
            raise
        except RuntimeError as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": error.__class__.__name__.casefold(),
                    "message": "update activation could not be completed",
                },
            ) from error

    @app.put(
        "/api/v1/settings/permissions",
        response_model=PermissionMutationResponse,
    )
    def update_permissions(
        request: UpdatePermissionRequest,
    ) -> PermissionMutationResponse:
        # Keep the durable mutation, immutable snapshot publication, and default
        # event context in request order even when FastAPI runs sync handlers on
        # different worker threads. New Turns independently read the authority,
        # so they can never inherit a stale in-memory preference.
        with permission_authority.mutation_lock:
            with kernel.jobs.control_transaction(
                scope="permission_update",
                subject=request.client_request_id,
            ) as connection:
                permissions = permission_authority.update_in_transaction(
                    connection,
                    request.profile,
                    expected_revision=request.expected_revision,
                    client_request_id=request.client_request_id,
                )
                permission_snapshot, permission_policy = (
                    composition.record_permission_in_transaction(
                        connection,
                        permissions,
                    )
                )
                audit_outbox.backfill_permissions_in_transaction(connection)
            composition.apply_recorded_permission(
                permission_snapshot,
                permission_policy,
            )
            kernel.events.default_permission_snapshot_id = permissions.snapshot_id
            return PermissionMutationResponse(permissions=permissions)

    @app.get("/api/v1/projects", response_model=ProjectListResponse)
    def list_projects() -> ProjectListResponse:
        return project_service.list()

    @app.post(
        "/api/v1/projects/pick",
        response_model=ProjectProjection,
        status_code=201,
    )
    def pick_project(request: PickProjectFolderRequest) -> ProjectProjection:
        selected = settings.project_folder_picker()
        return project_service.create_from_path(
            selected,
            client_request_id=request.client_request_id,
        )

    @app.post(
        "/api/v1/input-attachments",
        response_model=InputAttachmentProjection,
        status_code=201,
    )
    async def upload_input_attachment(
        file: UploadFile = File(...),
        client_request_id: str = Form(..., min_length=1, max_length=256),
    ) -> InputAttachmentProjection:
        filename = str(file.filename or "").strip()
        if not filename:
            raise HTTPException(
                status_code=422, detail="attachment filename is required"
            )
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_INPUT_ATTACHMENT_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="attachment exceeds the 64 MiB limit",
                    )
                chunks.append(chunk)
            return input_attachment_service.upload(
                b"".join(chunks),
                filename=filename,
                mime_type=file.content_type,
                client_request_id=client_request_id,
            )
        except InputAttachmentConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except InputAttachmentUnavailable as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InputAttachmentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        finally:
            await file.close()

    @app.get("/api/v1/input-attachments/{attachment_id}/content")
    def get_input_attachment_content(attachment_id: str) -> Response:
        try:
            projection, content = input_attachment_service.read(attachment_id)
        except InputAttachmentUnavailable as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return Response(
            content=content,
            media_type=projection.mime_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline",
            },
        )

    def bind_input_attachments(request: CreateTurnRequest) -> CreateTurnRequest:
        if not request.attachment_ids:
            return request
        attachments = input_attachment_service.resolve(request.attachment_ids)
        metadata = dict(request.metadata)
        metadata["input_attachments"] = [
            attachment.model_dump(mode="json") for attachment in attachments
        ]
        return request.model_copy(update={"metadata": metadata})

    def bind_steer_attachments(request: SteerTurnRequest) -> SteerTurnRequest:
        if not request.attachment_ids:
            return request
        attachments = input_attachment_service.resolve(request.attachment_ids)
        metadata = dict(request.metadata)
        metadata["input_attachments"] = [
            attachment.model_dump(mode="json") for attachment in attachments
        ]
        return request.model_copy(update={"metadata": metadata})

    @app.post(
        "/api/v1/threads",
        status_code=201,
        response_model=ThreadProjection,
    )
    def create_thread(request: CreateThreadRequest) -> ThreadProjection:
        metadata = dict(request.metadata)
        project_id = metadata.get("project_id")
        if project_id is not None:
            if not isinstance(project_id, str) or not project_id:
                raise HTTPException(status_code=422, detail="project_id is invalid")
            metadata.update(project_service.thread_metadata(project_id))
            request = CreateThreadRequest(
                title=request.title,
                metadata=metadata,
                client_request_id=request.client_request_id,
            )
        return kernel.create_thread(request)

    @app.get("/api/v1/threads", response_model=ThreadListResponse)
    def list_threads(
        status: str = Query(default="active"),
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=2048),
    ) -> ThreadListResponse:
        if status == "all":
            status_value = None
        else:
            try:
                status_value = ThreadStatus(status)
            except ValueError as error:
                raise HTTPException(
                    status_code=422, detail="thread status filter is invalid"
                ) from error
        before_updated_at = None
        before_thread_id = None
        if cursor is not None:
            try:
                before_updated_at, before_thread_id = _decode_thread_cursor(
                    cursor,
                    status_filter=status,
                    secret=settings.runtime_bearer_token or "",
                )
            except ValueError as error:
                raise HTTPException(
                    status_code=400, detail="thread cursor is invalid"
                ) from error
        items, has_more = kernel.list_threads(
            status=status_value,
            limit=limit,
            before_updated_at=before_updated_at,
            before_thread_id=before_thread_id,
        )
        return ThreadListResponse(
            items=items,
            next_cursor=(
                _encode_thread_cursor(
                    items[-1],
                    status_filter=status,
                    secret=settings.runtime_bearer_token or "",
                )
                if has_more and items
                else None
            ),
        )

    @app.put(
        "/api/v1/threads/{thread_id}",
        response_model=ThreadProjection,
    )
    def rename_thread(thread_id: str, request: RenameThreadRequest) -> ThreadProjection:
        try:
            return kernel.rename_thread(
                thread_id,
                request.title,
                client_request_id=request.client_request_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post(
        "/api/v1/threads/{thread_id}/archive",
        response_model=ThreadProjection,
    )
    def archive_thread(
        thread_id: str, request: ThreadStatusRequest
    ) -> ThreadProjection:
        return kernel.archive_thread(
            thread_id, client_request_id=request.client_request_id
        )

    @app.post(
        "/api/v1/threads/{thread_id}/restore",
        response_model=ThreadProjection,
    )
    def restore_thread(
        thread_id: str, request: ThreadStatusRequest
    ) -> ThreadProjection:
        return kernel.restore_thread(
            thread_id, client_request_id=request.client_request_id
        )

    @app.put(
        "/api/v1/threads/{thread_id}/pin",
        response_model=ThreadProjection,
    )
    def set_thread_pinned(
        thread_id: str, request: ThreadPinRequest
    ) -> ThreadProjection:
        return kernel.set_thread_pinned(
            thread_id,
            request.pinned,
            client_request_id=request.client_request_id,
        )

    @app.post(
        "/api/v1/threads/{thread_id}/turns",
        status_code=202,
        response_model=TurnMutationResponse,
    )
    def create_turn(thread_id: str, request: CreateTurnRequest) -> TurnMutationResponse:
        require_model_task_service()
        try:
            request = bind_input_attachments(request)
            return composition.admit_turn(
                request,
                lambda prepared: kernel.create_turn(
                    thread_id,
                    prepared.request,
                    snapshot_context=prepared.snapshot_context,
                    permission_account_id=composition.permission_account_id,
                ),
            )
        except CapabilityIntentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except ModelCatalogError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except InputAttachmentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post(
        "/api/v1/turns/{turn_id}/steer",
        status_code=202,
        response_model=TurnMutationResponse,
    )
    def steer_turn(turn_id: str, request: SteerTurnRequest) -> TurnMutationResponse:
        require_model_task_service()
        try:
            request = bind_steer_attachments(request)
            active_turn = kernel.get_turn(turn_id)
            agent_model_id, image_model_id = composition.resolve_model_selection(
                agent_model_id=request.agent_model_id or active_turn.agent_model_id,
                image_model_id=request.image_model_id or active_turn.image_model_id,
            )
        except ModelCatalogError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except InputAttachmentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return kernel.steer_turn(
            turn_id,
            request.model_copy(
                update={
                    "agent_model_id": agent_model_id,
                    "image_model_id": image_model_id,
                }
            ),
        )

    @app.post(
        "/api/v1/threads/{thread_id}/queue",
        status_code=202,
        response_model=TurnMutationResponse,
    )
    def queue_turn(thread_id: str, request: QueueTurnRequest) -> TurnMutationResponse:
        require_model_task_service()
        try:
            turn_request = bind_input_attachments(
                CreateTurnRequest.model_validate(request.model_dump())
            )
            return composition.admit_turn(
                turn_request,
                lambda prepared: kernel.queue_turn(
                    thread_id,
                    prepared.request,
                    snapshot_context=prepared.snapshot_context,
                    permission_account_id=composition.permission_account_id,
                ),
            )
        except CapabilityIntentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except ModelCatalogError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except InputAttachmentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post(
        "/api/v1/turns/{turn_id}/replace",
        status_code=202,
        response_model=ReplaceTurnResponse,
    )
    def replace_turn(turn_id: str, request: ReplaceTurnRequest) -> ReplaceTurnResponse:
        require_model_task_service()
        try:
            turn_request = bind_input_attachments(
                CreateTurnRequest.model_validate(request.model_dump(exclude={"reason"}))
            )
        except InputAttachmentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        def accept_replacement(prepared):
            canonical = ReplaceTurnRequest.model_validate(
                {
                    **prepared.request.model_dump(),
                    "reason": request.reason,
                }
            )
            return kernel.replace_turn(
                turn_id,
                canonical,
                snapshot_context=prepared.snapshot_context,
                permission_account_id=composition.permission_account_id,
            )

        try:
            return composition.admit_turn(
                turn_request,
                accept_replacement,
            )
        except CapabilityIntentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except ModelCatalogError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except InputAttachmentError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post(
        "/api/v1/threads/{thread_id}/fork",
        status_code=201,
        response_model=ThreadProjection,
    )
    def fork_thread(thread_id: str, request: ForkThreadRequest) -> ThreadProjection:
        return kernel.fork_thread(thread_id, request)

    @app.post(
        "/api/v1/turns/{turn_id}/interrupt",
        response_model=TurnMutationResponse,
    )
    def interrupt_turn(
        turn_id: str, request: InterruptTurnRequest
    ) -> TurnMutationResponse:
        return kernel.interrupt_turn(turn_id, reason=request.reason)

    @app.get(
        "/api/v1/threads/{thread_id}/projection",
        response_model=ThreadProjectionResponse,
    )
    def projection(thread_id: str) -> ThreadProjectionResponse:
        return kernel.projection(thread_id)

    @app.get(
        "/api/v1/threads/{thread_id}/usage",
        response_model=ConversationUsageProjection,
    )
    async def conversation_usage(thread_id: str) -> ConversationUsageProjection:
        try:
            local_projection = usage_projection_service.project(thread_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="thread not found") from error
        remote_usage = getattr(settings.model_gateway, "usage", None)
        if not callable(remote_usage):
            return local_projection
        try:
            account_projection = await asyncio.wait_for(
                remote_usage(settings.usage_timezone),
                timeout=3.0,
            )
        except Exception:
            # Conversation history and the local provider facts remain useful
            # when the managed account projection is temporarily unavailable.
            return local_projection
        if not isinstance(account_projection, GatewayAccountUsageProjection):
            return local_projection
        if (
            account_projection.coverage_started_at is None
            or account_projection.coverage_started_at
            > account_projection.week_started_at
        ):
            # Do not replace a complete local week with a partially deployed
            # cloud ledger. The switch becomes authoritative at the first
            # Monday whose whole window is covered by the Gateway.
            return local_projection
        return local_projection.model_copy(
            update={
                "scope": "account",
                "source": "managed_gateway",
                "complete_across_devices": True,
                "today": TokenUsageWindow(
                    **account_projection.today.model_dump()
                ),
                "week": TokenUsageWindow(
                    **account_projection.week.model_dump()
                ),
                "calculated_at": account_projection.calculated_at,
            }
        )

    @app.get(
        "/api/v1/threads/{thread_id}/replay",
        response_model=MockReplayResponse,
    )
    def mock_replay(
        thread_id: str,
        through_seq: int | None = Query(default=None, ge=1),
    ) -> MockReplayResponse:
        return replay_service.mock_replay(thread_id, through_seq=through_seq)

    @app.post(
        "/api/v1/threads/{thread_id}/replay/live",
        response_model=LiveReplayResponse,
        status_code=202,
    )
    def live_replay(
        thread_id: str,
        request: LiveReplayRequest,
    ) -> LiveReplayResponse:
        require_model_task_service()
        try:
            return replay_service.live_replay(thread_id, request)
        except ModelCatalogError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get(
        "/api/v1/threads/{thread_id}/trace",
        response_model=TraceProjectionResponse,
    )
    def trace_projection(
        thread_id: str,
        through_seq: int | None = Query(default=None, ge=1),
    ) -> TraceProjectionResponse:
        return trace_projector.project(thread_id, through_seq=through_seq)

    @app.get(
        "/api/v1/observability/audit",
        response_model=AuditListResponse,
    )
    def audit_records(
        thread_id: str | None = None,
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> AuditListResponse:
        try:
            records = audit_outbox.list(
                thread_id=thread_id,
                status=status,
                limit=limit,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return AuditListResponse(records=list(records), count=len(records))

    @app.post(
        "/api/v1/observability/audit/drain",
        response_model=AuditDrainResponse,
    )
    async def drain_audit(request: AuditDrainRequest) -> AuditDrainResponse:
        try:
            return await audit_outbox.drain(limit=request.limit)
        except AuditIntegrityError:
            raise
        except AuditError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post(
        "/api/v1/observability/audit/retention",
        response_model=AuditRetentionResponse,
    )
    def enforce_audit_retention() -> AuditRetentionResponse:
        return audit_outbox.enforce_retention()

    @app.get(
        "/api/v1/threads/{thread_id}/interactions",
        response_model=InteractionListResponse,
    )
    def interactions(thread_id: str, pending_only: bool = True):
        return kernel.list_interactions(thread_id, pending_only=pending_only)

    @app.post(
        "/api/v1/interactions/{interaction_id}/connector-login/begin",
        response_model=ConnectorLoginBeginResponse,
    )
    async def begin_connector_login_interaction(
        interaction_id: str,
    ) -> ConnectorLoginBeginResponse:
        interaction = await asyncio.to_thread(kernel.interactions.get, interaction_id)
        if (
            interaction.kind is not InteractionKind.CONNECTOR_LOGIN
            or interaction.status is not InteractionStatus.PENDING
            or interaction.contract.connector is None
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "connector_login_interaction_unavailable",
                    "message": "连接器登录请求已失效",
                },
            )
        if not any(
            action.action_type is InteractionActionType.CONNECTOR_BEGIN_LOGIN
            for action in interaction.contract.actions
        ):
            raise HTTPException(
                status_code=409, detail="connector login action is unavailable"
            )
        connector_id = interaction.contract.connector.connector_id
        definition = connector_registry.definition(connector_id)
        if ConnectorAuthKind.OAUTH2 in definition.auth_kinds:
            auth_kind = ConnectorAuthKind.OAUTH2
        else:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "connector_interactive_login_unavailable",
                    "message": "当前版本尚不支持该连接器的交互式登录",
                },
            )
        catalog = await asyncio.to_thread(connector_composition.service.catalog)
        item = next(
            (
                candidate
                for candidate in catalog
                if candidate.definition.connector_id == connector_id
            ),
            None,
        )
        if item is None or not item.adapter_available:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "connector_adapter_unavailable",
                    "message": "连接器适配器尚未安装",
                },
            )
        existing_binding = await asyncio.to_thread(
            connector_composition.repository.interaction_login_binding,
            interaction_id,
        )
        reauthorize = (
            interaction.contract.connector.state
            is ConnectorInteractionState.REAUTHORIZATION_REQUIRED
            or (
                existing_binding is not None
                and existing_binding.mode == "reauthorize"
                and existing_binding.status
                in {
                    "starting",
                    "awaiting_callback",
                    "completing",
                    "failed",
                    "reauthorization_required",
                }
            )
        )
        target_instance_id: str | None = None
        if reauthorize and existing_binding is not None:
            target_instance_id = (
                existing_binding.target_instance_id
                or existing_binding.completed_instance_id
            )
        elif reauthorize:
            required_actions = set(interaction.contract.connector.required_action_ids)
            unavailable_instances = [
                instance
                for instance in item.instances
                if (
                    instance.health.value not in {"connected", "degraded"}
                    or (
                        required_actions
                        and not required_actions.issubset(
                            set(instance.available_actions)
                        )
                    )
                )
            ]
            if len(unavailable_instances) != 1:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "connector_instance_selection_required",
                        "message": "请在连接器菜单中选择需要重新登录的账号",
                    },
                )
            target_instance_id = unavailable_instances[0].instance_id
        reservation = await asyncio.to_thread(
            connector_composition.repository.reserve_interaction_login,
            interaction_id=interaction_id,
            connector_id=connector_id,
            mode="reauthorize" if reauthorize else "connect",
            target_instance_id=target_instance_id,
        )
        if reservation.outcome == "in_progress":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "connector_login_in_progress",
                    "message": "连接器登录正在启动",
                },
            )
        if reservation.outcome == "completed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "connector_login_already_completed",
                    "message": "连接器登录已完成，请检查状态",
                },
            )
        binding = reservation.binding
        try:
            interaction_binding = None
            if reservation.outcome == "reserved":
                if binding.operation_token is None:
                    raise RuntimeError("connector login start lease is unavailable")
                interaction_binding = (
                    interaction_id,
                    binding.generation,
                    binding.operation_token,
                )
            if binding.mode == "reauthorize":
                assert binding.target_instance_id is not None
                challenge = await connector_composition.service.begin_reauthorize(
                    binding.target_instance_id,
                    auth_kind=auth_kind,
                    return_uri=oauth_return_uri,
                    client_request_id=binding.lifecycle_request_id,
                    interaction_binding=interaction_binding,
                )
            else:
                challenge = await connector_composition.service.begin_connect(
                    connector_id,
                    auth_kind=auth_kind,
                    return_uri=oauth_return_uri,
                    client_request_id=binding.lifecycle_request_id,
                    interaction_binding=interaction_binding,
                )
            binding = await asyncio.to_thread(
                connector_composition.repository.interaction_login_binding,
                interaction_id,
            )
            if binding is None or binding.flow_id != challenge.flow_id:
                raise RuntimeError("connector login replay changed its flow identity")
        except BaseException as error:
            if reservation.outcome == "reserved":
                current_binding = await asyncio.to_thread(
                    connector_composition.repository.interaction_login_binding,
                    interaction_id,
                )
                owns_active_flow = (
                    current_binding is not None
                    and current_binding.generation == reservation.binding.generation
                    and current_binding.status == "awaiting_callback"
                    and current_binding.flow_id is not None
                )
                if not owns_active_flow:
                    lifecycle = await asyncio.to_thread(
                        connector_composition.repository.lifecycle_request_state,
                        reservation.binding.lifecycle_request_id,
                    )
                    flow_id = (
                        str(lifecycle.get("result", {}).get("flow_id", ""))
                        if lifecycle is not None
                        and isinstance(lifecycle.get("result"), dict)
                        else ""
                    )
                    if not flow_id and "challenge" in locals():
                        flow_id = challenge.flow_id
                    if flow_id:
                        await connector_composition.service.cancel_auth_flow(flow_id)
                    await asyncio.to_thread(
                        connector_composition.repository.fail_interaction_login,
                        interaction_id,
                        reservation.binding.generation,
                        error_code=str(
                            getattr(error, "code", "connector_login_failed")
                        ),
                    )
            if isinstance(error, ConnectorError):
                raise HTTPException(
                    status_code=409,
                    detail={"code": error.code, "message": str(error)},
                ) from error
            raise
        await asyncio.to_thread(
            kernel.interactions.update_connector_state,
            interaction_id,
            ConnectorInteractionState.AWAITING_CALLBACK,
        )
        return ConnectorLoginBeginResponse(
            interaction_id=interaction_id,
            connector_id=connector_id,
            state="awaiting_callback",
            authorization_url=challenge.authorization_url,
            verification_url=challenge.verification_url,
            user_code=challenge.user_code,
            expires_at=challenge.expires_at,
        )

    @app.post(
        "/api/v1/interactions/{interaction_id}/connector-login/check",
        response_model=ConnectorLoginCheckResponse,
        responses={
            202: {
                "description": "Connector authorization is still pending",
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": "#/components/schemas/ConnectorLoginCheckResponse"
                        }
                    }
                },
            }
        },
    )
    async def check_connector_login_interaction(
        interaction_id: str,
        http_response: Response,
    ) -> ConnectorLoginCheckResponse:
        interaction = await asyncio.to_thread(kernel.interactions.get, interaction_id)
        if interaction.kind is not InteractionKind.CONNECTOR_LOGIN:
            raise HTTPException(
                status_code=409, detail="interaction is not connector login"
            )
        if interaction.contract.connector is None:
            raise HTTPException(
                status_code=409, detail="connector login context is missing"
            )
        connector_id = interaction.contract.connector.connector_id
        if interaction.status is InteractionStatus.RESOLVED:
            if (
                interaction.response is None
                or interaction.response.action_id != "check_status"
            ):
                raise HTTPException(
                    status_code=409, detail="connector login was not completed"
                )
            revisions = await asyncio.to_thread(
                kernel.turn_inputs.list_for_turn,
                str(interaction.turn_id),
            )
            revision = next(
                (
                    candidate
                    for candidate in revisions
                    if candidate.metadata.get("authority_refresh", {}).get(
                        "interaction_id"
                    )
                    == interaction_id
                ),
                None,
            )
            mutation = await asyncio.to_thread(
                kernel.get_interaction_mutation,
                interaction_id,
            )
            return ConnectorLoginCheckResponse(
                interaction_id=interaction_id,
                connector_id=connector_id,
                connected=True,
                state="connected",
                authority_refresh_revision_id=(
                    revision.revision_id if revision is not None else None
                ),
                mutation=mutation,
            )
        if interaction.status is not InteractionStatus.PENDING:
            raise HTTPException(
                status_code=409, detail="connector login is not pending"
            )
        if not any(
            action.action_type is InteractionActionType.CONNECTOR_CHECK_STATUS
            for action in interaction.contract.actions
        ):
            raise HTTPException(
                status_code=409, detail="connector status action is unavailable"
            )
        await asyncio.to_thread(
            connector_composition.repository.recover_expired_interaction_logins
        )
        binding = await asyncio.to_thread(
            connector_composition.repository.interaction_login_binding,
            interaction_id,
        )
        if binding is not None and binding.status in {
            "failed",
            "authorization_required",
            "reauthorization_required",
        }:
            retry_state = (
                ConnectorInteractionState.REAUTHORIZATION_REQUIRED
                if binding.mode == "reauthorize"
                else ConnectorInteractionState.AUTHORIZATION_REQUIRED
            )
            await asyncio.to_thread(
                kernel.interactions.update_connector_state,
                interaction_id,
                retry_state,
            )
            http_response.status_code = 202
            return ConnectorLoginCheckResponse(
                interaction_id=interaction_id,
                connector_id=connector_id,
                connected=False,
                state=retry_state.value,
                reason=binding.last_error_code or "connector_login_retry_required",
            )
        if binding is None or binding.flow_id is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "connector_login_not_started",
                    "message": "请先开始连接器登录",
                },
            )
        if binding.status not in {"awaiting_callback", "completing", "completed"}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "connector_login_not_checkable",
                    "message": "请重新开始连接器登录",
                },
            )
        await asyncio.to_thread(
            kernel.interactions.update_connector_state,
            interaction_id,
            ConnectorInteractionState.VERIFYING,
        )
        completion = await asyncio.to_thread(
            connector_composition.repository.interaction_login_completion,
            interaction_id,
        )
        if completion is None:
            await asyncio.to_thread(
                kernel.interactions.update_connector_state,
                interaction_id,
                ConnectorInteractionState.AWAITING_CALLBACK,
            )
            http_response.status_code = 202
            return ConnectorLoginCheckResponse(
                interaction_id=interaction_id,
                connector_id=connector_id,
                connected=False,
                state="awaiting_callback",
            )
        binding, completed_instance_id = completion
        completed_instance = await asyncio.to_thread(
            connector_composition.repository.get_instance,
            completed_instance_id,
        )
        if (
            completed_instance is None
            or completed_instance.connector_id != connector_id
            or completed_instance.health.value not in {"connected", "degraded"}
        ):
            if completed_instance is None:
                await asyncio.to_thread(
                    connector_composition.repository.mark_interaction_connect_required,
                    interaction_id,
                    completed_instance_id=completed_instance_id,
                    error_code="completed_instance_missing",
                )
                retry_state = ConnectorInteractionState.AUTHORIZATION_REQUIRED
            else:
                await asyncio.to_thread(
                    connector_composition.repository.mark_interaction_reauthorization_required,
                    interaction_id,
                    target_instance_id=completed_instance_id,
                    error_code="completed_instance_unavailable",
                )
                retry_state = ConnectorInteractionState.REAUTHORIZATION_REQUIRED
            await asyncio.to_thread(
                kernel.interactions.update_connector_state,
                interaction_id,
                retry_state,
            )
            http_response.status_code = 202
            return ConnectorLoginCheckResponse(
                interaction_id=interaction_id,
                connector_id=connector_id,
                connected=False,
                state=retry_state.value,
                reason=(
                    "completed_instance_missing"
                    if completed_instance is None
                    else "completed_instance_unavailable"
                ),
            )
        definition = connector_registry.definition(connector_id)
        available_actions = set(
            completed_instance.to_projection(definition).available_actions
        )
        required_actions = set(interaction.contract.connector.required_action_ids)
        if required_actions and not required_actions.issubset(available_actions):
            await asyncio.to_thread(
                connector_composition.repository.mark_interaction_reauthorization_required,
                interaction_id,
                target_instance_id=completed_instance_id,
                error_code="required_connector_scope_missing",
            )
            await asyncio.to_thread(
                kernel.interactions.update_connector_state,
                interaction_id,
                ConnectorInteractionState.REAUTHORIZATION_REQUIRED,
            )
            http_response.status_code = 202
            return ConnectorLoginCheckResponse(
                interaction_id=interaction_id,
                connector_id=connector_id,
                connected=False,
                state="reauthorization_required",
                reason="required_connector_scope_missing",
            )
        if interaction.turn_id is None:
            raise HTTPException(status_code=409, detail="connector login has no Turn")
        turn = await asyncio.to_thread(kernel.get_turn, interaction.turn_id)
        refresh_client_id = (
            "connector_refresh_"
            + hashlib.sha256(interaction_id.encode("utf-8")).hexdigest()
        )
        refresh_request = SteerTurnRequest(
            input=(
                f"{interaction.contract.connector.display_name}连接器授权已完成，"
                "请继续原请求。"
            ),
            agent_model_id=turn.agent_model_id,
            image_model_id=turn.image_model_id,
            explicit_tool_ids=[],
            metadata={
                "authority_refresh": {
                    "kind": "connector_login",
                    "interaction_id": interaction_id,
                    "connector_id": connector_id,
                }
            },
            client_message_id=refresh_client_id,
        )
        response_client_id = (
            "connector_check_"
            + hashlib.sha256(interaction_id.encode("utf-8")).hexdigest()
        )

        def resolve_under_permission_lock():
            with composition.permission_mutation_lock:
                return kernel.resolve_connector_login_interaction(
                    interaction_id,
                    connector_id=connector_id,
                    refresh_request=refresh_request,
                    client_request_id=response_client_id,
                )

        mutation, revision_id = await asyncio.to_thread(resolve_under_permission_lock)
        return ConnectorLoginCheckResponse(
            interaction_id=interaction_id,
            connector_id=connector_id,
            connected=True,
            state="connected",
            authority_refresh_revision_id=revision_id,
            mutation=mutation,
        )

    @app.post(
        "/api/v1/interactions/{interaction_id}/connector-login/cancel",
        response_model=ConnectorLoginCancelResponse,
    )
    async def cancel_connector_login_interaction(
        interaction_id: str,
    ) -> ConnectorLoginCancelResponse:
        interaction = await asyncio.to_thread(
            kernel.interactions.get,
            interaction_id,
        )
        if interaction.kind is not InteractionKind.CONNECTOR_LOGIN:
            raise HTTPException(
                status_code=409, detail="interaction is not connector login"
            )
        if interaction.contract.connector is None:
            raise HTTPException(
                status_code=409, detail="connector login context is missing"
            )
        if interaction.status is InteractionStatus.RESOLVED:
            if (
                interaction.response is None
                or interaction.response.action_id != "cancel"
            ):
                raise HTTPException(
                    status_code=409, detail="connector login already completed"
                )
        elif interaction.status is InteractionStatus.PENDING:
            await connector_composition.service.cancel_interaction_login(interaction_id)
        else:
            raise HTTPException(
                status_code=409, detail="connector login cannot be cancelled"
            )
        request_id = (
            "connector_cancel_"
            + hashlib.sha256(interaction_id.encode("utf-8")).hexdigest()
        )
        mutation = await asyncio.to_thread(
            kernel.cancel_connector_login_interaction,
            interaction_id,
            client_request_id=request_id,
        )
        return ConnectorLoginCancelResponse(
            interaction_id=interaction_id,
            connector_id=interaction.contract.connector.connector_id,
            cancelled=True,
            mutation=mutation,
        )

    @app.post(
        "/api/v1/interactions/{interaction_id}/respond",
        response_model=InteractionMutationResponse,
    )
    def respond_interaction(interaction_id: str, request: RespondInteractionRequest):
        require_model_task_service()
        interaction = kernel.interactions.get(interaction_id)
        if interaction.kind is InteractionKind.CONNECTOR_LOGIN:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "connector_login_dedicated_endpoint_required",
                    "message": "连接器登录操作必须使用专用生命周期端点",
                },
            )
        return kernel.respond_interaction(
            interaction_id,
            request.response,
            client_request_id=request.client_request_id,
        )

    def _cursor(request: Request, after_seq: int) -> int:
        last_event_id = request.headers.get("last-event-id")
        if last_event_id:
            try:
                parsed = int(last_event_id)
                if parsed < 0:
                    raise ValueError
                return max(after_seq, parsed)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="Last-Event-ID must be non-negative"
                )
        return after_seq

    async def validate_cursor(thread_id: str, cursor: int) -> JSONResponse | None:
        watermark = await asyncio.to_thread(kernel.events.watermark, thread_id)
        if cursor > watermark:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "event cursor is ahead of this thread",
                    "code": "cursor_ahead",
                    "watermark": watermark,
                    "reset_after_seq": watermark,
                },
                headers={"Cache-Control": "no-store"},
            )
        return None

    @app.get("/api/v1/threads/{thread_id}/events", response_model=None)
    async def events(
        request: Request,
        thread_id: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        follow: bool | None = None,
    ):
        await asyncio.to_thread(kernel.get_thread, thread_id)
        cursor = _cursor(request, after_seq)
        invalid = await validate_cursor(thread_id, cursor)
        if invalid is not None:
            return invalid
        if "text/event-stream" in request.headers.get("accept", ""):
            return StreamingResponse(
                _stream_events(
                    request,
                    kernel,
                    settings,
                    thread_id,
                    cursor,
                    True if follow is None else follow,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-store",
                    "X-Accel-Buffering": "no",
                },
            )
        page = await asyncio.to_thread(
            kernel.events.page, thread_id, after_seq=cursor, limit=limit
        )
        response = EventListResponse(
            events=page.events,
            after_seq=page.after_seq,
            watermark=page.watermark,
            has_more=page.has_more,
        )
        return JSONResponse(content=response.model_dump(mode="json"))

    @app.get("/api/v1/threads/{thread_id}/events/stream", response_model=None)
    async def event_stream(
        request: Request,
        thread_id: str,
        after_seq: int = Query(default=0, ge=0),
        follow: bool = True,
    ):
        await asyncio.to_thread(kernel.get_thread, thread_id)
        cursor = _cursor(request, after_seq)
        invalid = await validate_cursor(thread_id, cursor)
        if invalid is not None:
            return invalid
        return StreamingResponse(
            _stream_events(request, kernel, settings, thread_id, cursor, follow),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    original_openapi = app.openapi
    secured_openapi_schema = None

    def secured_openapi():
        nonlocal secured_openapi_schema
        if secured_openapi_schema is not None:
            return secured_openapi_schema
        schema = original_openapi()
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["RuntimeBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "opaque-runtime-secret",
        }
        schema["security"] = [{"RuntimeBearer": []}]
        for path in schema.get("paths", {}).values():
            for method, operation in path.items():
                if method.lower() not in {"post", "put", "patch", "delete"}:
                    continue
                operation.setdefault("parameters", []).append(
                    {
                        "name": "X-EcoreX-CSRF",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                )
        secured_openapi_schema = schema
        return secured_openapi_schema

    app.openapi = secured_openapi

    # Phase A already audited the core graph before semantic construction.
    # Healthy startup has now converged the explicitly allowed defaults and
    # recovery facts; projection-only startup has written none of them. Audit
    # the resulting graph again before guarded maintenance or any Worker can
    # run. Direct ASGI consumers receive the same two fences without lifespan.
    try:
        runtime_execution_gate.record_report(kernel.invariants.audit())
    except BaseException as error:
        runtime_execution_gate.record_audit_exception(error)
    if runtime_execution_gate.snapshot().healthy:
        try:
            with kernel.jobs.control_transaction(
                scope="startup_maintenance",
                subject="interaction_expiry",
            ) as connection:
                kernel.interactions.expire_due_in_transaction(connection)
            with runtime_execution_gate.new_admission(
                scope="retouch_recovery",
                subject=settings.account_id,
            ) as permit:
                artifact_service.recover_interrupted_retouch_workspace_submissions(
                    account_id=settings.account_id,
                    before_commit=lambda: runtime_execution_gate.assert_permit(permit),
                )
        except BaseException as error:
            runtime_execution_gate.mark_critical(
                error_code=(
                    f"interaction_maintenance_failed:{type(error).__name__.casefold()}"
                )
            )
        if runtime_execution_gate.snapshot().healthy:
            try:
                runtime_execution_gate.record_report(kernel.invariants.audit())
            except BaseException as error:
                runtime_execution_gate.record_audit_exception(error)

    return app
