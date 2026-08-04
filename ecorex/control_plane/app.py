"""FastAPI release Control Plane for admin rollout and client update discovery."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import re
from typing import Any, Protocol
import uuid

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.types import ASGIApp, Receive, Scope, Send

from ecorex import __version__
from ecorex.release.public_index import MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES
from ecorex.release.signing import ReleaseSigner
from ecorex.update import ReleaseChannel, ReleaseManifest, VerificationError
from ecorex.update.rollback import (
    ROLLBACK_AUTHORIZATION_HEADER,
    RollbackAuthorizationError,
    issue_rollback_authorization,
)
from ecorex.extensions import LocalSkillBundleStore

from .bootstrap_index_service import (
    BootstrapIndexPublicationError,
    BootstrapIndexPublicationService,
)
from .bootstrap_freshness import (
    BootstrapFreshnessRefreshError,
    BootstrapFreshnessRefresher,
)

from .audit import (
    CloudAuditBodyLimitMiddleware,
    CloudAuditRepository,
    create_cloud_audit_router,
)
from .admin_web import (
    AdminResumeAdapter,
    create_admin_resume_router,
    create_admin_web_router,
)
from .admin_management_router import create_admin_management_router
from .device_identity import ManagedDeviceIdentityBroker
from .device_identity_router import create_device_identity_router
from .management import (
    AdminManagementConflict,
    AdminManagementError,
    AdminManagementNotFound,
    AdminManagementRepository,
    ModelConnectionTester,
    RejectingModelConnectionTester,
)
from .models import (
    BootstrapFreshnessRunProjection,
    BootstrapFreshnessStatusProjection,
    CandidateProjection,
    ControlPlaneAuthenticator,
    ControlPrincipal,
    ControlUpdateSignal,
    CreateCandidateRequest,
    CreateRollbackRequest,
    CreateRolloutRequest,
    DistributionProjection,
    DirectAdmissionRequest,
    GateBundleRequest,
    GateResultRequest,
    KillSwitchProjection,
    RolloutActionRequest,
    RollbackProjection,
    RolloutProjection,
)
from .repository import (
    MAX_UPDATE_HINT_BATCH_SIZE,
    ControlPlaneConflict,
    ControlPlaneError,
    ControlPlaneNotFound,
    ControlPlaneRepository,
    UpdateHintClient,
)
from .skill_hub import SkillHubRegistry, create_skill_hub_router
from .release_replica import (
    CDNReleaseReplicaService,
    create_cdn_release_replica_router,
)
from .signals import DurableUpdateSignalPoller
from .shares import (
    CloudShareConflict,
    CloudShareNotFound,
    CloudShareRepository,
    render_public_share,
)
from .share_objects import ShareObjectCapacityError
from ecorex.sharing import (
    MAX_SHARED_MEDIA_BYTES,
    PublishedShare,
    ShareMediaContractError,
    SharePayload,
)


_SAFE_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_MAX_SHARE_REQUEST_BYTES = 8 * 1024 * 1024
_MAX_SHARE_MEDIA_REQUEST_BYTES = MAX_SHARED_MEDIA_BYTES
_SHARE_MEDIA_UPLOAD_PATH = re.compile(
    r"^/api/v1/shares/shr_[0-9a-f]{32}/media/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_BOOTSTRAP_ACTIVATION_BYTES = 16 * 1024
_MAX_DIRECT_ADMISSION_REQUEST_BYTES = 32 * 1024 * 1024
_DIRECT_ADMISSION_PATH = re.compile(
    r"^/api/v1/admin/releases/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/direct-admission$"
)


def _share_media_range(value: str | None, size_bytes: int) -> tuple[int, int] | None:
    if value is None:
        return None
    if len(value) > 128 or not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid share media range")
    bounds = value[6:].split("-", 1)
    if len(bounds) != 2:
        raise ValueError("invalid share media range")
    first, last = bounds
    if not first:
        if not last.isdigit() or int(last) < 1:
            raise ValueError("invalid share media range")
        length = min(int(last), size_bytes)
        return size_bytes - length, size_bytes - 1
    if not first.isdigit() or (last and not last.isdigit()):
        raise ValueError("invalid share media range")
    start = int(first)
    end = size_bytes - 1 if not last else int(last)
    if start >= size_bytes or end < start:
        raise ValueError("invalid share media range")
    return start, min(end, size_bytes - 1)


class _ShareBodyLimitMiddleware:
    """Bound share JSON and media before FastAPI materializes either body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        is_snapshot = (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/api/v1/shares"
        )
        is_media = (
            scope["type"] == "http"
            and scope.get("method") == "PUT"
            and bool(_SHARE_MEDIA_UPLOAD_PATH.fullmatch(scope.get("path", "")))
        )
        if not is_snapshot and not is_media:
            await self.app(scope, receive, send)
            return

        limit = (
            _MAX_SHARE_REQUEST_BYTES if is_snapshot else _MAX_SHARE_MEDIA_REQUEST_BYTES
        )

        content_lengths = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if content_lengths or is_media:
            if not content_lengths:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=411,
                    code="share_media_length_required",
                    message="share media requires an exact Content-Length",
                )
                return
            raw_length = content_lengths[0]
            if (
                len(content_lengths) != 1
                or len(raw_length) > 20
                or not raw_length.isdigit()
                or int(raw_length) > limit
                or (is_media and int(raw_length) < 1)
            ):
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    code="share_media_too_large"
                    if is_media
                    else "share_payload_too_large",
                    message=(
                        "share media exceeds its size limit"
                        if is_media
                        else "share snapshot exceeds its size limit"
                    ),
                )
                return
            declared_length = int(raw_length)
        else:
            declared_length = None

        # Media is consumed by the route only after it acquires one of four
        # process-level memory slots.  Do not pre-buffer it in middleware.
        if is_media:
            await self.app(scope, receive, send)
            return

        received = 0
        buffered: list[dict[str, Any]] = []
        while True:
            message = await receive()
            buffered.append(message)
            if len(buffered) > 4096:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    code="share_payload_too_large",
                    message="share snapshot exceeds its size limit",
                )
                return
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    await self._reject(
                        scope,
                        receive,
                        send,
                        status_code=413,
                        code="share_media_too_large"
                        if is_media
                        else "share_payload_too_large",
                        message=(
                            "share media exceeds its size limit"
                            if is_media
                            else "share snapshot exceeds its size limit"
                        ),
                    )
                    return
                if message.get("more_body", False):
                    continue
            break

        if declared_length is not None and received != declared_length:
            await self._reject(
                scope,
                receive,
                send,
                status_code=400,
                code="share_body_length_mismatch",
                message="share body does not match Content-Length",
            )
            return

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={
                "detail": {
                    "code": code,
                    "message": message,
                }
            },
        )
        await response(scope, receive, send)


class _DirectAdmissionBodyLimitMiddleware:
    """Authenticate, single-flight and bound direct evidence before parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        authenticator: ControlPlaneAuthenticator,
        max_inflight: int = 1,
    ) -> None:
        if max_inflight != 1:
            raise ValueError("direct admission requires one bounded memory slot")
        self.app = app
        self.authenticator = authenticator
        self._slots = asyncio.BoundedSemaphore(max_inflight)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope["type"] == "http"
            and scope.get("method") == "PUT"
            and _DIRECT_ADMISSION_PATH.fullmatch(scope.get("path", ""))
        ):
            await self.app(scope, receive, send)
            return
        try:
            current = _authenticate_control_principal(
                self.authenticator, scope.get("headers", [])
            )
        except PermissionError:
            await self._reject(
                scope,
                receive,
                send,
                status_code=401,
                code="direct_admission_authentication_failed",
                message="Control Plane authentication failed",
            )
            return
        if "release_admin" not in current.roles:
            await self._reject(
                scope,
                receive,
                send,
                status_code=403,
                code="direct_admission_role_required",
                message="release administrator role is required",
            )
            return
        lengths = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if (
            len(lengths) != 1
            or len(lengths[0]) > 20
            or not lengths[0].isdigit()
            or not 1 <= int(lengths[0]) <= _MAX_DIRECT_ADMISSION_REQUEST_BYTES
        ):
            status = 413 if lengths else 411
            await self._reject(
                scope,
                receive,
                send,
                status_code=status,
                code=(
                    "direct_admission_body_too_large"
                    if status == 413
                    else "direct_admission_length_required"
                ),
                message="direct admission requires an exact bounded body",
            )
            return
        if self._slots.locked():
            await self._reject(
                scope,
                receive,
                send,
                status_code=429,
                code="direct_admission_busy",
                message="another direct admission is already being verified",
            )
            return
        await self._slots.acquire()
        try:
            declared = int(lengths[0])
            received = 0
            messages: list[dict[str, Any]] = []
            while True:
                message = await receive()
                messages.append(message)
                if len(messages) > 16_384:
                    received = _MAX_DIRECT_ADMISSION_REQUEST_BYTES + 1
                    break
                if message.get("type") == "http.request":
                    received += len(message.get("body", b""))
                    if received > _MAX_DIRECT_ADMISSION_REQUEST_BYTES:
                        break
                    if message.get("more_body", False):
                        continue
                break
            if received > _MAX_DIRECT_ADMISSION_REQUEST_BYTES:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    code="direct_admission_body_too_large",
                    message="direct admission exceeds its size limit",
                )
                return
            if received != declared:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    code="direct_admission_body_length_mismatch",
                    message="direct admission body length differs",
                )
                return
            index = 0

            async def replay_receive():
                nonlocal index
                if index < len(messages):
                    message = messages[index]
                    index += 1
                    return message
                return await receive()

            await self.app(scope, replay_receive, send)
        finally:
            self._slots.release()

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"detail": {"code": code, "message": message}},
        )
        await response(scope, receive, send)


def _bearer(value: str) -> str:
    scheme, separator, token = value.partition(" ")
    if (
        separator != " "
        or scheme.casefold() != "bearer"
        or not 24 <= len(token) <= 4096
        or any(character.isspace() or ord(character) < 32 for character in token)
    ):
        raise PermissionError("a valid Control Plane bearer token is required")
    return token


def _authenticate_control_principal(
    authenticator: ControlPlaneAuthenticator,
    headers: list[tuple[bytes, bytes]],
) -> ControlPrincipal:
    values = [
        value for name, value in headers if name.lower() == b"authorization"
    ]
    if len(values) != 1:
        raise PermissionError("one authorization header is required")
    try:
        authorization = values[0].decode("ascii")
    except UnicodeDecodeError:
        raise PermissionError("authorization header must be ASCII") from None
    return authenticator.authenticate(_bearer(authorization))


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _exact_content_length(request: Request, *, maximum: int) -> int:
    values = [
        value
        for name, value in request.scope.get("headers", [])
        if name.lower() == b"content-length"
    ]
    if (
        len(values) != 1
        or not values[0].isdigit()
        or len(values[0]) > 20
        or not 1 <= int(values[0]) <= maximum
    ):
        raise HTTPException(
            status_code=411,
            detail="an exact bounded Content-Length is required",
        )
    return int(values[0])


@dataclass(slots=True)
class _ClientConnection:
    principal: ControlPrincipal
    channel: ReleaseChannel
    platform: str
    architecture: str
    current_version: str
    queue: asyncio.Queue[dict[str, Any] | None]
    current_release_id: str | None = None
    current_build_digest: str | None = None


class ControlPlaneServiceLifecycle(Protocol):
    """Production lifecycle seam kept outside the transport factory.

    The normal app factory remains dependency-injected and testable.  A
    production composition supplies this contract so health endpoints reflect
    real storage/object-store readiness and shutdown can stop accepting work
    before resources are released.
    """

    @property
    def accepting(self) -> bool: ...

    @property
    def live(self) -> bool: ...

    async def startup(self) -> None: ...

    async def readiness(self) -> bool: ...

    def begin_drain(self) -> None: ...

    async def shutdown(self) -> None: ...


class UpdateSignalHub:
    def __init__(self) -> None:
        self._connections: dict[str, _ClientConnection] = {}
        self._lock = asyncio.Lock()
        self._accepting = True

    async def add(self, connection: _ClientConnection) -> bool:
        async with self._lock:
            if not self._accepting:
                return False
            previous = self._connections.get(connection.principal.client_id)
            self._connections[connection.principal.client_id] = connection
            if previous is not None and previous is not connection:
                if previous.queue.full():
                    try:
                        previous.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                previous.queue.put_nowait(None)
            return True

    async def remove(self, client_id: str, connection: _ClientConnection) -> None:
        async with self._lock:
            if self._connections.get(client_id) is connection:
                self._connections.pop(client_id, None)

    async def begin_drain(self) -> None:
        """Reject new sockets and wake every active socket for a 1012 close."""

        async with self._lock:
            self._accepting = False
            connections = tuple(self._connections.values())
            for connection in connections:
                if connection.queue.full():
                    try:
                        connection.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                connection.queue.put_nowait(None)

    async def broadcast_signal(
        self,
        repository: ControlPlaneRepository,
        signal: ControlUpdateSignal,
    ) -> int:
        async with self._lock:
            connections = list(self._connections.values())
        candidates = tuple(
            connection
            for connection in connections
            if connection.channel.value == signal.channel
        )
        clients = tuple(
            UpdateHintClient(
                principal=connection.principal,
                channel=connection.channel,
                platform=connection.platform,
                architecture=connection.architecture,
                current_version=connection.current_version,
                current_release_id=connection.current_release_id,
                current_build_digest=connection.current_build_digest,
            )
            for connection in candidates
        )
        # Validate the complete copied set before opening the first batch.  A
        # malformed matching client therefore cannot cause earlier batches to
        # enqueue partial hints.
        for client in clients:
            repository.validate_update_hint_client(client)
        resolved: list[tuple[_ClientConnection, ReleaseManifest | None]] = []
        if not clients:
            # Fan-out still authenticates the exact durable fact when there are
            # no matching online clients.
            await asyncio.to_thread(repository.hint_manifests_for_clients, signal, ())
        else:
            for offset in range(0, len(clients), MAX_UPDATE_HINT_BATCH_SIZE):
                batch_clients = clients[offset : offset + MAX_UPDATE_HINT_BATCH_SIZE]
                manifests = await asyncio.to_thread(
                    repository.hint_manifests_for_clients,
                    signal,
                    batch_clients,
                )
                if len(manifests) != len(batch_clients):
                    raise ControlPlaneError("update hint batch result is incomplete")
                resolved.extend(
                    zip(
                        candidates[offset : offset + MAX_UPDATE_HINT_BATCH_SIZE],
                        manifests,
                        strict=True,
                    )
                )
        delivered = 0
        # Recheck identity under the Hub lock so a connection removed or
        # replaced while SQLite work ran cannot receive a stale queued hint.
        async with self._lock:
            if not self._accepting:
                return 0
            for connection, manifest in resolved:
                if (
                    manifest is None
                    or self._connections.get(connection.principal.client_id)
                    is not connection
                ):
                    continue
                self._enqueue(
                    connection,
                    {
                        "schema_version": 1,
                        "event_id": signal.event_id,
                        "event_type": "update.available",
                        "release_id": manifest.release_id,
                        "version": manifest.version,
                        "build_digest": manifest.build_digest,
                        "channel": manifest.channel.value,
                    },
                )
                delivered += 1
        return delivered

    @staticmethod
    def _enqueue(connection: _ClientConnection, payload: dict[str, Any]) -> None:
        if connection.queue.full():
            try:
                connection.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        connection.queue.put_nowait(payload)


def create_control_plane_app(
    repository: ControlPlaneRepository,
    *,
    authenticator: ControlPlaneAuthenticator,
    share_repository: CloudShareRepository | None = None,
    audit_repository: CloudAuditRepository | None = None,
    signal_consumer_id: str | None = None,
    signal_poll_interval_seconds: float = 0.25,
    signal_retention_seconds: int = 7 * 24 * 60 * 60,
    signal_retain_latest: int = 1024,
    service_lifecycle: ControlPlaneServiceLifecycle | None = None,
    bootstrap_index_service: BootstrapIndexPublicationService | None = None,
    bootstrap_freshness_refresher: BootstrapFreshnessRefresher | None = None,
    rollback_signer: ReleaseSigner | None = None,
    management_repository: AdminManagementRepository | None = None,
    model_connection_tester: ModelConnectionTester | None = None,
    device_identity_broker: ManagedDeviceIdentityBroker | None = None,
    release_replica_service: CDNReleaseReplicaService | None = None,
    skill_hub_registry: SkillHubRegistry | None = None,
    skill_hub_bundle_store: LocalSkillBundleStore | None = None,
) -> FastAPI:
    hub = UpdateSignalHub()
    resolved_model_tester = (
        model_connection_tester or RejectingModelConnectionTester()
        if management_repository is not None
        else None
    )
    environment_consumer_id = os.environ.get("ECOREX_CONTROL_PLANE_INSTANCE_ID")
    if signal_consumer_id is not None:
        resolved_consumer_id = signal_consumer_id
    elif environment_consumer_id is not None:
        resolved_consumer_id = environment_consumer_id
    else:
        resolved_consumer_id = "instance_" + uuid.uuid4().hex
    poller = DurableUpdateSignalPoller(
        repository,
        hub,
        consumer_id=resolved_consumer_id,
        poll_interval_seconds=signal_poll_interval_seconds,
        retention_seconds=signal_retention_seconds,
        retain_latest=signal_retain_latest,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        lifecycle_started = False
        try:
            if service_lifecycle is not None:
                lifecycle_started = True
                await service_lifecycle.startup()
            if bootstrap_freshness_refresher is not None:
                await bootstrap_freshness_refresher.start()
            await poller.start()
            yield
        finally:
            if service_lifecycle is not None:
                service_lifecycle.begin_drain()
            await hub.begin_drain()
            await poller.close()
            if bootstrap_freshness_refresher is not None:
                await bootstrap_freshness_refresher.close()
            if service_lifecycle is not None and lifecycle_started:
                await service_lifecycle.shutdown()
            closer = getattr(resolved_model_tester, "aclose", None)
            if callable(closer):
                await closer()

    app = FastAPI(
        title="e-Mate Control Plane",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.add_middleware(
        _DirectAdmissionBodyLimitMiddleware,
        authenticator=authenticator,
        max_inflight=1,
    )
    if share_repository is not None:
        app.add_middleware(_ShareBodyLimitMiddleware)
    if audit_repository is not None:
        app.add_middleware(CloudAuditBodyLimitMiddleware)
    app.state.release_repository = repository
    app.state.update_signal_hub = hub
    app.state.update_signal_poller = poller
    app.state.update_signal_consumer_id = resolved_consumer_id
    app.state.share_media_slots = asyncio.BoundedSemaphore(4)
    app.state.service_lifecycle = service_lifecycle
    app.state.bootstrap_index_service = bootstrap_index_service
    app.state.bootstrap_freshness_refresher = bootstrap_freshness_refresher
    app.state.rollback_signer = rollback_signer
    app.state.management_repository = management_repository
    app.state.device_identity_broker = device_identity_broker
    app.state.release_replica_service = release_replica_service
    app.state.skill_hub_registry = skill_hub_registry

    def principal(request: Request) -> ControlPrincipal:
        try:
            current = _authenticate_control_principal(
                authenticator, request.scope.get("headers", [])
            )
            if (
                device_identity_broker is not None
                and current.token_id is not None
                and device_identity_broker.access_token_is_current(
                    account_id=current.account_id,
                    token_id=current.token_id,
                )
                is False
            ):
                raise PermissionError("managed access token is no longer current")
            return current
        except PermissionError as error:
            raise HTTPException(
                status_code=401, detail="Control Plane authentication failed"
            ) from error

    def admin(current: ControlPrincipal = Depends(principal)) -> ControlPrincipal:
        if "release_admin" not in current.roles:
            raise HTTPException(
                status_code=403, detail="release administrator role is required"
            )
        return current

    def audit_admin(current: ControlPrincipal = Depends(principal)) -> ControlPrincipal:
        if "audit_admin" not in current.roles:
            raise HTTPException(
                status_code=403, detail="audit administrator role is required"
            )
        return current

    def user_admin(current: ControlPrincipal = Depends(principal)) -> ControlPrincipal:
        if not ({"platform_admin", "user_admin"} & current.roles):
            raise HTTPException(
                status_code=403, detail="user administrator role is required"
            )
        return current

    def model_admin(current: ControlPrincipal = Depends(principal)) -> ControlPrincipal:
        if not ({"platform_admin", "model_admin"} & current.roles):
            raise HTTPException(
                status_code=403, detail="model administrator role is required"
            )
        return current

    admin_resume_provider = AdminResumeAdapter(repository.admin_resume_facts)
    app.state.admin_resume_provider = admin_resume_provider
    app.include_router(
        create_admin_web_router(
            external_asset_prefix="/ecorex-agent/admin/assets"
        )
    )
    app.include_router(
        create_admin_resume_router(
            admin_resume_provider,
            authorization_dependency=admin,
        )
    )
    if management_repository is not None:
        assert resolved_model_tester is not None
        app.state.model_connection_tester = resolved_model_tester
        app.include_router(
            create_admin_management_router(
                management_repository,
                model_tester=resolved_model_tester,
                user_admin_dependency=user_admin,
                model_admin_dependency=model_admin,
            )
        )
    if device_identity_broker is not None:
        app.include_router(
            create_device_identity_router(
                device_identity_broker,
                admin_dependency=user_admin,
                account_dependency=principal,
                password_repository=management_repository,
            )
        )
    if audit_repository is not None:
        app.state.audit_repository = audit_repository
        app.include_router(
            create_cloud_audit_router(
                audit_repository,
                principal_dependency=principal,
                admin_dependency=audit_admin,
            )
        )
    if release_replica_service is not None:
        app.include_router(create_cdn_release_replica_router(release_replica_service))
    if (skill_hub_registry is None) != (skill_hub_bundle_store is None):
        raise ValueError("Skill Hub registry and CAS must be configured together")
    if skill_hub_registry is not None and skill_hub_bundle_store is not None:
        def skill_hub_nickname(account_id: str) -> str:
            if management_repository is None:
                return "e-Mate 用户"
            try:
                return management_repository.get_user(account_id).display_name
            except AdminManagementError:
                return "e-Mate 用户"

        app.include_router(
            create_skill_hub_router(
                skill_hub_registry,
                skill_hub_bundle_store,
                principal_dependency=principal,
                nickname_resolver=skill_hub_nickname,
            )
        )

    @app.get(
        "/api/v1/internal/release-admin-auth",
        status_code=204,
        response_class=Response,
        include_in_schema=False,
    )
    def release_admin_auth_probe(
        _current: ControlPrincipal = Depends(admin),
    ) -> Response:
        """No-body Nginx auth_request target; never returns token metadata."""

        return Response(status_code=204)

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        if (
            service_lifecycle is not None
            and not service_lifecycle.accepting
            and request.url.path not in {"/health/live", "/health/ready"}
        ):
            return JSONResponse(
                status_code=503,
                content={"status": "draining"},
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )
        response = await call_next(request)
        if request.url.path.startswith("/api/v1") or request.url.path.startswith(
            "/health/"
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/health/"):
            response.headers["X-EcoreX-Product-Version"] = __version__
        return response

    if service_lifecycle is not None:

        @app.get("/health/live", include_in_schema=False)
        async def health_live() -> JSONResponse:
            if not service_lifecycle.live:
                return JSONResponse(status_code=503, content={"status": "stopped"})
            return JSONResponse(content={"status": "live"})

        @app.get("/health/ready", include_in_schema=False)
        async def health_ready() -> JSONResponse:
            try:
                freshness_ready = (
                    bootstrap_freshness_refresher is None
                    or await asyncio.to_thread(
                        lambda: bootstrap_freshness_refresher.ready
                    )
                )
            except Exception:
                freshness_ready = False
            if (
                not service_lifecycle.accepting
                or not poller.running
                or poller.last_error is not None
                or not freshness_ready
            ):
                return JSONResponse(status_code=503, content={"status": "unavailable"})
            try:
                ready = await service_lifecycle.readiness()
            except Exception:
                ready = False
            return JSONResponse(
                status_code=200 if ready else 503,
                content={"status": "ready" if ready else "unavailable"},
            )

    @app.exception_handler(ControlPlaneNotFound)
    async def not_found(_request: Request, _error: ControlPlaneNotFound):
        return JSONResponse(status_code=404, content={"detail": "resource not found"})

    @app.exception_handler(AdminManagementNotFound)
    async def management_not_found(
        _request: Request, _error: AdminManagementNotFound
    ):
        return JSONResponse(status_code=404, content={"detail": "resource not found"})

    @app.exception_handler(RequestValidationError)
    async def request_validation(request: Request, error: RequestValidationError):
        if request.url.path.startswith("/api/v1/admin/models"):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "invalid_model_configuration",
                        "message": "model configuration is invalid",
                    }
                },
            )
        if request.url.path in {"/api/v1/shares", "/api/v1/audit/records"}:
            # Share validation inputs can contain full conversation text.  The
            # framework's default response echoes invalid values, so redact the
            # entire payload at this trust boundary.
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": (
                            "invalid_share_snapshot"
                            if request.url.path == "/api/v1/shares"
                            else "invalid_audit_record"
                        ),
                        "message": (
                            "share snapshot is invalid"
                            if request.url.path == "/api/v1/shares"
                            else "audit record is invalid"
                        ),
                    }
                },
            )
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(ControlPlaneConflict)
    async def conflict(_request: Request, error: ControlPlaneConflict):
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": error.__class__.__name__.casefold(),
                    "message": str(error),
                }
            },
        )

    @app.exception_handler(AdminManagementConflict)
    async def management_conflict(
        _request: Request, error: AdminManagementConflict
    ):
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "admin_management_conflict",
                    "message": str(error),
                }
            },
        )

    @app.exception_handler(AdminManagementError)
    async def management_error(_request: Request, _error: AdminManagementError):
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "admin_management_unavailable",
                    "message": "administrator operation is unavailable",
                }
            },
        )

    @app.exception_handler(ControlPlaneError)
    async def control_error(_request: Request, error: ControlPlaneError):
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": error.__class__.__name__.casefold(),
                    "message": "release control operation failed",
                }
            },
        )

    @app.exception_handler(BootstrapIndexPublicationError)
    async def bootstrap_publication_error(
        _request: Request, _error: BootstrapIndexPublicationError
    ):
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "bootstrap_index_publication_unavailable",
                    "message": "public Bootstrap publication is unavailable",
                }
            },
        )

    @app.exception_handler(BootstrapFreshnessRefreshError)
    async def bootstrap_freshness_error(
        _request: Request, _error: BootstrapFreshnessRefreshError
    ):
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "bootstrap_freshness_refresh_failed",
                    "message": "Bootstrap freshness refresh failed safely",
                }
            },
        )

    @app.exception_handler(VerificationError)
    async def verification_error(_request: Request, _error: VerificationError):
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "release_verification_failed",
                    "message": "signed release verification failed",
                }
            },
        )

    @app.exception_handler(ValueError)
    async def invalid_request(_request: Request, _error: ValueError):
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "invalid_release_control_request",
                    "message": "release control request is invalid",
                }
            },
        )

    @app.post(
        "/api/v1/admin/releases",
        response_model=CandidateProjection,
        status_code=201,
    )
    def create_candidate(
        request: CreateCandidateRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> CandidateProjection:
        manifest = ReleaseManifest.from_dict(request.manifest)
        return repository.create_candidate(
            manifest,
            manifest_file_sha256=request.manifest_sha256,
            actor=current,
            client_request_id=request.client_request_id,
        )

    @app.put(
        "/api/v1/bootstrap-index/candidates/{release_id}",
        response_model=None,
        status_code=201,
    )
    async def stage_bootstrap_index(
        release_id: str,
        request: Request,
        current: ControlPrincipal = Depends(admin),
    ) -> dict[str, Any]:
        service = bootstrap_index_service
        if service is None:
            raise BootstrapIndexPublicationError(
                "public Bootstrap publisher is not configured"
            )
        if _SAFE_TARGET.fullmatch(release_id) is None:
            raise HTTPException(status_code=422, detail="release identity is invalid")
        declared = _exact_content_length(
            request, maximum=MAX_PUBLIC_BOOTSTRAP_INDEX_BYTES
        )
        media_type = request.headers.get("content-type", "").split(";", 1)[0]
        digest = request.headers.get("x-ecorex-sha256", "")
        declared_size = request.headers.get("x-ecorex-size", "")
        idempotency_key = request.headers.get("idempotency-key", "")
        if (
            media_type != "application/json"
            or _SHA256.fullmatch(digest) is None
            or not declared_size.isdigit()
            or int(declared_size) != declared
            or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
        ):
            raise HTTPException(
                status_code=422, detail="Bootstrap index request headers are invalid"
            )
        payload = await request.body()
        if len(payload) != declared or hashlib.sha256(payload).hexdigest() != digest:
            raise HTTPException(
                status_code=400, detail="Bootstrap index body identity differs"
            )
        result = await asyncio.to_thread(
            service.stage,
            payload,
            actor=current,
            client_request_id=idempotency_key,
        )
        if result.get("release_id") != release_id:
            raise HTTPException(
                status_code=422, detail="Bootstrap index release identity differs"
            )
        return result

    @app.post(
        "/api/v1/bootstrap-index/candidates/{release_id}/activate",
        response_model=None,
    )
    async def activate_bootstrap_index(
        release_id: str,
        request: Request,
        current: ControlPrincipal = Depends(admin),
    ) -> dict[str, Any]:
        service = bootstrap_index_service
        if service is None:
            raise BootstrapIndexPublicationError(
                "public Bootstrap publisher is not configured"
            )
        if _SAFE_TARGET.fullmatch(release_id) is None:
            raise HTTPException(status_code=422, detail="release identity is invalid")
        declared = _exact_content_length(
            request, maximum=_MAX_BOOTSTRAP_ACTIVATION_BYTES
        )
        media_type = request.headers.get("content-type", "").split(";", 1)[0]
        idempotency_key = request.headers.get("idempotency-key", "")
        if (
            media_type != "application/json"
            or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
        ):
            raise HTTPException(
                status_code=422,
                detail="Bootstrap activation request headers are invalid",
            )
        payload = await request.body()
        if len(payload) != declared:
            raise HTTPException(
                status_code=400, detail="Bootstrap activation body length differs"
            )
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise HTTPException(
                status_code=422, detail="Bootstrap activation body is invalid"
            ) from None
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=422, detail="Bootstrap activation body is invalid"
            )
        return await asyncio.to_thread(
            service.activate,
            release_id=release_id,
            request=value,
            actor=current,
            client_request_id=idempotency_key,
        )

    @app.get(
        "/api/v1/admin/bootstrap-index/proofs/{release_id}",
        response_model=None,
    )
    async def trusted_bootstrap_index_proof(
        release_id: str,
        _current: ControlPrincipal = Depends(admin),
    ) -> dict[str, Any]:
        if bootstrap_index_service is None:
            raise BootstrapIndexPublicationError(
                "public Bootstrap publisher is not configured"
            )
        return await asyncio.to_thread(
            repository.trusted_bootstrap_index_proof, release_id
        )

    @app.get(
        "/api/v1/admin/bootstrap-index/freshness",
        response_model=BootstrapFreshnessStatusProjection,
    )
    async def bootstrap_freshness_status(
        _current: ControlPrincipal = Depends(admin),
    ) -> dict[str, Any]:
        if bootstrap_freshness_refresher is None:
            raise BootstrapFreshnessRefreshError(
                "Bootstrap freshness refresher is not configured"
            )
        return await asyncio.to_thread(bootstrap_freshness_refresher.status)

    @app.post(
        "/api/v1/admin/bootstrap-index/freshness/refresh",
        response_model=BootstrapFreshnessRunProjection,
    )
    async def refresh_bootstrap_freshness(
        request: RolloutActionRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> dict[str, Any]:
        if bootstrap_freshness_refresher is None:
            raise BootstrapFreshnessRefreshError(
                "Bootstrap freshness refresher is not configured"
            )
        return await asyncio.to_thread(
            bootstrap_freshness_refresher.run_once,
            force=True,
            raise_on_failure=True,
            actor=current,
            client_request_id=request.client_request_id,
        )

    @app.put(
        "/api/v1/admin/releases/{release_id}/gates/{gate_name}",
        response_model=CandidateProjection,
    )
    def record_gate(
        release_id: str,
        gate_name: str,
        request: GateResultRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> CandidateProjection:
        return repository.record_gate(
            release_id,
            gate_name,
            status=request.status,
            evidence=request.evidence,
            actor=current,
            client_request_id=request.client_request_id,
        )

    @app.put(
        "/api/v1/admin/releases/{release_id}/gate-bundle",
        response_model=CandidateProjection,
    )
    def record_gate_bundle(
        release_id: str,
        request: GateBundleRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> CandidateProjection:
        return repository.record_gate_bundle(
            release_id,
            request.attestation,
            actor=current,
            client_request_id=request.client_request_id,
        )

    @app.post(
        "/api/v1/admin/releases/{release_id}/publish",
        response_model=CandidateProjection,
    )
    def publish(
        release_id: str,
        request: RolloutActionRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> CandidateProjection:
        return repository.publish(
            release_id, actor=current, client_request_id=request.client_request_id
        )

    @app.put(
        "/api/v1/admin/releases/{release_id}/direct-admission",
        response_model=CandidateProjection,
    )
    def record_direct_admission(
        release_id: str,
        request: DirectAdmissionRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> CandidateProjection:
        return repository.record_direct_admission(
            release_id,
            request.attestation,
            actor=current,
            client_request_id=request.client_request_id,
        )

    @app.post(
        "/api/v1/admin/rollouts",
        response_model=RolloutProjection,
        status_code=201,
    )
    def create_rollout(
        request: CreateRolloutRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> RolloutProjection:
        return repository.create_rollout(
            request.release_id,
            percentage=request.percentage,
            organizations=request.target_organization_ids,
            accounts=request.target_account_ids,
            minimum_compatible_version=request.minimum_compatible_version,
            actor=current,
            client_request_id=request.client_request_id,
        )

    @app.post(
        "/api/v1/admin/rollouts/{rollout_id}/{action}",
        response_model=RolloutProjection,
    )
    async def rollout_action(
        rollout_id: str,
        action: str,
        request: RolloutActionRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> RolloutProjection:
        rollout = repository.rollout_action(
            rollout_id,
            action,
            actor=current,
            client_request_id=request.client_request_id,
        )
        signal = repository.rollout_signal_for_request(
            actor=current,
            client_request_id=request.client_request_id,
            rollout_id=rollout.rollout_id,
            action=action,
        )
        # Low-latency local delivery also preserves the existing contract when
        # an embedding explicitly disables ASGI lifespan.  The durable poller
        # will replay the same stable event ID; Runtime deduplication absorbs
        # the intentional at-least-once boundary.
        await hub.broadcast_signal(repository, signal)
        return rollout

    @app.post(
        "/api/v1/admin/rollbacks",
        response_model=RollbackProjection,
        status_code=201,
    )
    def create_rollback(
        request: CreateRollbackRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> RollbackProjection:
        return repository.create_rollback(
            request.source_release_id,
            request.target_release_id,
            percentage=request.percentage,
            organizations=request.target_organization_ids,
            accounts=request.target_account_ids,
            authorization_ttl_seconds=request.authorization_ttl_seconds,
            actor=current,
            client_request_id=request.client_request_id,
        )

    @app.post(
        "/api/v1/admin/rollbacks/{rollback_id}/{action}",
        response_model=RollbackProjection,
    )
    async def rollback_action(
        rollback_id: str,
        action: str,
        request: RolloutActionRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> RollbackProjection:
        rollback = repository.rollback_action(
            rollback_id,
            action,
            actor=current,
            client_request_id=request.client_request_id,
        )
        signal = repository.rollback_signal_for_request(
            actor=current,
            client_request_id=request.client_request_id,
            rollback_id=rollback.rollback_id,
            action=action,
        )
        await hub.broadcast_signal(repository, signal)
        return rollback

    @app.get(
        "/api/v1/admin/distribution",
        response_model=DistributionProjection,
    )
    def distribution(
        _current: ControlPrincipal = Depends(admin),
    ) -> DistributionProjection:
        return repository.distribution()

    @app.post(
        "/api/v1/admin/channels/{channel}/kill-switch",
        response_model=KillSwitchProjection,
    )
    def kill_switch(
        channel: str,
        request: RolloutActionRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> KillSwitchProjection:
        try:
            release_channel = ReleaseChannel(channel)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail="release channel is invalid"
            ) from error
        return repository.kill_channel(
            release_channel,
            actor=current,
            client_request_id=request.client_request_id,
        )

    @app.post(
        "/api/v1/admin/channels/{channel}/kill-switch/clear",
        response_model=KillSwitchProjection,
    )
    def clear_kill_switch(
        channel: str,
        request: RolloutActionRequest,
        current: ControlPrincipal = Depends(admin),
    ) -> KillSwitchProjection:
        try:
            release_channel = ReleaseChannel(channel)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail="release channel is invalid"
            ) from error
        return repository.clear_channel_kill(
            release_channel,
            actor=current,
            client_request_id=request.client_request_id,
        )

    @app.get("/api/v1/releases/latest", response_model=None)
    def latest_release(
        channel: str,
        platform: str,
        architecture: str,
        current_version: str,
        update_state: str = "idle",
        current_release_id: str | None = None,
        current_build_digest: str | None = None,
        rollback_nonce: str | None = None,
        current: ControlPrincipal = Depends(principal),
    ):
        try:
            release_channel = ReleaseChannel(channel)
        except ValueError as error:
            raise HTTPException(
                status_code=422, detail="release channel is invalid"
            ) from error
        if not _SAFE_TARGET.fullmatch(platform) or not _SAFE_TARGET.fullmatch(
            architecture
        ):
            raise HTTPException(status_code=422, detail="platform target is invalid")
        rollback_identity = (
            current_release_id,
            current_build_digest,
            rollback_nonce,
        )
        if any(value is not None for value in rollback_identity) and not all(
            value is not None for value in rollback_identity
        ):
            raise HTTPException(
                status_code=422, detail="current rollback identity is incomplete"
            )
        if rollback_nonce is not None and (
            len(rollback_nonce) > 128
            or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", rollback_nonce) is None
        ):
            raise HTTPException(status_code=422, detail="rollback nonce is invalid")
        try:
            decision = repository.latest_decision_for_client(
                current,
                channel=release_channel,
                platform=platform,
                architecture=architecture,
                current_version=current_version,
                update_state=update_state,
                current_release_id=current_release_id,
                current_build_digest=current_build_digest,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if decision is None:
            return Response(status_code=204)
        manifest = decision.manifest
        headers = {"ETag": f'"{manifest.build_digest}"'}
        if decision.is_rollback:
            if (
                rollback_signer is None
                or rollback_nonce is None
                or decision.rollback_id is None
                or decision.source_manifest is None
                or decision.authorization_ttl_seconds is None
            ):
                raise HTTPException(
                    status_code=503,
                    detail="rollback authorization signer is unavailable",
                )
            try:
                headers[ROLLBACK_AUTHORIZATION_HEADER] = issue_rollback_authorization(
                    signer=rollback_signer,
                    rollback_id=decision.rollback_id,
                    client_id=current.client_id,
                    source_manifest=decision.source_manifest,
                    target_manifest=manifest,
                    platform=platform,
                    architecture=architecture,
                    request_nonce=rollback_nonce,
                    ttl_seconds=decision.authorization_ttl_seconds,
                )
            except RollbackAuthorizationError:
                raise HTTPException(
                    status_code=503,
                    detail="rollback authorization could not be issued",
                ) from None
        return Response(
            content=manifest.to_json(),
            media_type="application/vnd.ecorex.release+json",
            headers=headers,
        )

    @app.websocket("/api/v1/client/updates/ws")
    async def update_socket(
        websocket: WebSocket,
        channel: str = Query(...),
        platform: str = Query(...),
        architecture: str = Query(...),
        current_version: str = Query(...),
        current_release_id: str | None = Query(default=None),
        current_build_digest: str | None = Query(default=None),
    ) -> None:
        if service_lifecycle is not None and not service_lifecycle.accepting:
            await websocket.close(code=1012)
            return
        try:
            current = authenticator.authenticate(
                _bearer(websocket.headers.get("authorization", ""))
            )
            release_channel = ReleaseChannel(channel)
            if not _SAFE_TARGET.fullmatch(platform) or not _SAFE_TARGET.fullmatch(
                architecture
            ):
                raise ValueError
            if not _SEMVER.fullmatch(current_version):
                raise ValueError
            if (current_release_id is None) != (current_build_digest is None):
                raise ValueError
            if current_release_id is not None and (
                _SAFE_TARGET.fullmatch(current_release_id) is None
                or _SHA256.fullmatch(str(current_build_digest)) is None
            ):
                raise ValueError
        except (PermissionError, ValueError):
            await websocket.close(code=1008)
            return
        connection = _ClientConnection(
            principal=current,
            channel=release_channel,
            platform=platform,
            architecture=architecture,
            current_version=current_version,
            current_release_id=current_release_id,
            current_build_digest=current_build_digest,
            queue=asyncio.Queue(maxsize=16),
        )
        if not await hub.add(connection):
            await websocket.close(code=1012)
            return
        try:
            await websocket.accept()
            while True:
                outbound = asyncio.create_task(connection.queue.get())
                inbound = asyncio.create_task(websocket.receive())
                done, pending = await asyncio.wait(
                    {outbound, inbound}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if inbound in done:
                    message = inbound.result()
                    if message.get("type") == "websocket.disconnect":
                        return
                    await websocket.close(code=1008)
                    return
                payload = outbound.result()
                if payload is None:
                    await websocket.close(code=1012)
                    return
                await websocket.send_text(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            await hub.remove(current.client_id, connection)

    if share_repository is not None:

        @app.put(
            "/api/v1/shares/{source_share_id}/media/{media_id}",
            status_code=204,
        )
        async def upload_share_media(
            source_share_id: str,
            media_id: str,
            request: Request,
            current: ControlPrincipal = Depends(principal),
        ) -> Response:
            content_lengths = request.headers.getlist("content-length")
            content_types = request.headers.getlist("content-type")
            content_digests = request.headers.getlist("x-content-sha256")
            media_kinds = request.headers.getlist("x-share-media-kind")
            idempotency_keys = request.headers.getlist("idempotency-key")
            if (
                len(content_lengths) != 1
                or not content_lengths[0].isdigit()
                or len(content_types) != 1
                or len(content_digests) != 1
                or len(media_kinds) != 1
                or len(idempotency_keys) != 1
            ):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "invalid_share_media_headers",
                        "message": "share media headers are invalid",
                    },
                )
            declared_length = int(content_lengths[0])
            if not 1 <= declared_length <= _MAX_SHARE_MEDIA_REQUEST_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "code": "share_media_too_large",
                        "message": "share media exceeds its size limit",
                    },
                )
            slots: asyncio.BoundedSemaphore = request.app.state.share_media_slots
            try:
                await asyncio.wait_for(slots.acquire(), timeout=0.1)
            except TimeoutError as error:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "share_media_busy",
                        "message": "share media upload capacity is busy",
                    },
                    headers={"Retry-After": "1"},
                ) from error
            try:
                buffered = bytearray()
                async for chunk in request.stream():
                    if len(buffered) + len(chunk) > _MAX_SHARE_MEDIA_REQUEST_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail={
                                "code": "share_media_too_large",
                                "message": "share media exceeds its size limit",
                            },
                        )
                    buffered.extend(chunk)
                content = bytes(buffered)
                if len(content) != declared_length:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "share_body_length_mismatch",
                            "message": "share body does not match Content-Length",
                        },
                    )
                await asyncio.to_thread(
                    share_repository.stage_media,
                    current.account_id,
                    source_share_id,
                    media_id,
                    content=content,
                    kind=media_kinds[0],
                    mime_type=content_types[0],
                    content_sha256=content_digests[0],
                    idempotency_key=idempotency_keys[0],
                )
            except CloudShareConflict as error:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "share_media_conflict",
                        "message": "share media conflicts with existing state",
                    },
                ) from error
            finally:
                slots.release()
            return Response(status_code=204)

        @app.post("/api/v1/shares", response_model=PublishedShare)
        def publish_share(
            payload: SharePayload,
            idempotency_key: str = Header(..., alias="Idempotency-Key"),
            current: ControlPrincipal = Depends(principal),
        ) -> PublishedShare:
            try:
                return share_repository.publish(
                    current.account_id,
                    payload,
                    idempotency_key=idempotency_key,
                )
            except ShareMediaContractError as error:
                raise HTTPException(
                    status_code=409,
                    detail=error.public_detail(),
                ) from error
            except CloudShareConflict as error:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "share_conflict",
                        "message": "share snapshot conflicts with existing state",
                    },
                ) from error

        @app.post("/api/v1/shares/{remote_snapshot_id}/revoke", status_code=204)
        def revoke_share(
            remote_snapshot_id: str,
            idempotency_key: str = Header(..., alias="Idempotency-Key"),
            current: ControlPrincipal = Depends(principal),
        ) -> Response:
            try:
                share_repository.revoke(
                    current.account_id,
                    remote_snapshot_id,
                    idempotency_key=idempotency_key,
                )
            except CloudShareNotFound as error:
                raise HTTPException(
                    status_code=404, detail="share snapshot was not found"
                ) from error
            except CloudShareConflict as error:
                raise HTTPException(
                    status_code=409, detail="share revoke conflicts with existing state"
                ) from error
            return Response(status_code=204)

        @app.get("/s/{token}", response_class=HTMLResponse, include_in_schema=False)
        def public_share(token: str) -> Response:
            try:
                content = render_public_share(
                    share_repository.resolve_public(token),
                    public_token=token,
                )
            except (
                CloudShareNotFound,
                CloudShareConflict,
                ShareMediaContractError,
            ):
                raise HTTPException(
                    status_code=404, detail="share snapshot was not found"
                ) from None
            return Response(
                content=content,
                media_type="text/html; charset=utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": (
                        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
                        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                    ),
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "X-Robots-Tag": "noindex, nofollow",
                },
            )

        @app.get(
            "/s/{token}/media/{media_id}",
            response_class=Response,
            include_in_schema=False,
        )
        def public_share_media(token: str, media_id: str, request: Request) -> Response:
            try:
                media = share_repository.resolve_public_media(token, media_id)
            except ShareObjectCapacityError as error:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "share_media_capacity_busy",
                        "message": "share media is temporarily busy",
                    },
                    headers={"Retry-After": "1"},
                ) from error
            except (
                CloudShareNotFound,
                CloudShareConflict,
                ShareMediaContractError,
            ):
                raise HTTPException(
                    status_code=404, detail="share media was not found"
                ) from None
            etag = f'"{media.etag}"'
            common_headers = {
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, no-cache, must-revalidate",
                "Content-Disposition": "inline",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cross-Origin-Resource-Policy": "same-origin",
                "ETag": etag,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Robots-Tag": "noindex, nofollow",
            }
            requested_range = request.headers.get("range")
            if_range = request.headers.get("if-range")
            if requested_range is not None and if_range not in {None, etag}:
                requested_range = None
            try:
                selected = _share_media_range(requested_range, media.size_bytes)
            except ValueError:
                media.stream.close()
                return Response(
                    status_code=416,
                    headers={
                        **common_headers,
                        "Content-Range": f"bytes */{media.size_bytes}",
                    },
                )
            if selected is None and request.headers.get("if-none-match") == etag:
                media.stream.close()
                return Response(status_code=304, headers=common_headers)
            start, end = selected or (0, media.size_bytes - 1)
            headers = {
                **common_headers,
                "Content-Length": str(end - start + 1),
            }
            status_code = 200
            if selected is not None:
                status_code = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{media.size_bytes}"
            return StreamingResponse(
                media.stream.iter_range(start, end),
                status_code=status_code,
                media_type=media.mime_type,
                headers=headers,
                background=BackgroundTask(media.stream.close),
            )

    return app
