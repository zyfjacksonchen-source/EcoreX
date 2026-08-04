"""Single-origin FastAPI product app for the signed React bundle and Runtime API."""

from __future__ import annotations

import ipaddress
import json
import os
import platform as platform_module
import re
import secrets
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ecorex.capabilities import (
    CapabilityPackRuntime,
    CapabilityUnavailableError,
    build_capability_handler_set,
    builtin_capability_registry,
)
from ecorex.connectors import ManagedConnectorGatewayAdapter
from ecorex.connectors import builtin_connector_registry
from ecorex.extensions import compose_extension_service
from ecorex.gateway import ManagedModelGatewayClient, ModelGateway
from ecorex.integration import ManagedImageOrchestrationClient
from ecorex.observability import ManagedOTLPHTTPTraceExporter
from ecorex.runtime import RuntimeSettings
from ecorex.runtime.api import create_app as register_runtime
from ecorex.projects import ProjectWorkspaceAuthority
from ecorex.session import (
    ManagedDeviceAuthorizationService,
    ManagedSessionRefreshService,
    ManagedSessionService,
)
from ecorex.update import Ed25519SignatureVerifier

from .bundle import (
    RUNTIME_CONFIG_MARKER,
    VerifiedWebBundle,
    _validate_index,
    load_verified_web_bundle,
)
from .skill_runner import create_production_controlled_skill_runner
from .errors import ServerConfigurationError


SecretFactory = Callable[[int], str]
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUNTIME_OWNER_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    ),
}


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        return False
    return parsed in {
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("::1"),
    }


def _origin(host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host else host
    if port == 80:
        return f"http://{authority}"
    return f"http://{authority}:{port}"


def _execute_feishu_cli(arguments: Mapping[str, Any], _context: Any) -> dict[str, Any]:
    from agent.tools.feishu_cli import FeishuCli

    result = FeishuCli().execute(dict(arguments))
    if result.status != "success":
        raise CapabilityUnavailableError("Feishu CLI returned a verified failure")
    return {"status": result.status, "result": result.result}


@dataclass(frozen=True, slots=True)
class ProductServerSettings:
    database_path: str | Path
    web_root: str | Path
    release_manifest_path: str | Path
    web_manifest_path: str | Path
    trusted_public_keys: Mapping[str, bytes]
    host: str = "127.0.0.1"
    port: int = 8765
    platform: str = field(default_factory=platform_module.system)
    architecture: str = field(default_factory=platform_module.machine)
    web_manifest_artifact_id: str = "web-manifest"
    runtime_owner_nonce: str | None = field(default=None, repr=False, compare=False)
    secret_factory: SecretFactory | None = field(
        default=None, repr=False, compare=False
    )
    full_access: bool = True
    admin_hard_denies: tuple[str, ...] = ()
    enforce_admin_tool_denies: bool = False
    managed_session_service: ManagedSessionService | None = field(
        default=None, repr=False, compare=False
    )
    managed_session_refresh_service: ManagedSessionRefreshService | None = field(
        default=None, repr=False, compare=False
    )
    managed_session_refresh_poll_seconds: float = 30.0
    device_authorization_service: ManagedDeviceAuthorizationService | None = field(
        default=None, repr=False, compare=False
    )
    device_authorization_poll_seconds: float = 1.0
    close_device_authorization_broker_on_shutdown: bool = True
    session_reload_requester: Any | None = field(
        default=None, repr=False, compare=False
    )
    first_install_registration_recorder: Any | None = field(
        default=None, repr=False, compare=False
    )
    first_install_runtime_ready_recorder: Any | None = field(
        default=None, repr=False, compare=False
    )
    allow_unmanaged_session_for_testing: bool = False
    model_gateway: ModelGateway | None = field(default=None, repr=False, compare=False)
    image_orchestration_client: ManagedImageOrchestrationClient | None = field(
        default=None, repr=False, compare=False
    )
    capability_handlers: Mapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )
    mcp_runtime_bindings: tuple[Any, ...] = field(
        default_factory=tuple, repr=False, compare=False
    )
    workspace_roots: tuple[str | Path, ...] = ()
    output_roots: Mapping[str, str | Path] = field(default_factory=dict)
    output_default_location: str = "workspace"
    capability_pack_runtime: CapabilityPackRuntime | None = field(
        default=None, repr=False, compare=False
    )
    capability_pack_services: Mapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )
    model_worker_concurrency: int = 2
    update_service: Any | None = field(default=None, repr=False, compare=False)
    connector_adapters: Mapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )
    connector_vault: Any | None = field(default=None, repr=False, compare=False)
    connector_maintenance_seconds: float = 15.0
    share_publisher: Any | None = field(default=None, repr=False, compare=False)
    share_public_hosts: frozenset[str] = frozenset()
    share_worker_concurrency: int = 1
    share_worker_poll_seconds: float = 0.25
    share_worker_shutdown_seconds: float = 5.0
    share_worker_lease_seconds: int = 30
    share_worker_retry_seconds: int = 2
    share_worker_max_attempts: int = 3
    share_operation_deadline_seconds: int = 3600
    retouch_adapter: Any | None = field(default=None, repr=False, compare=False)
    retouch_worker_concurrency: int = 1
    audit_publisher: Any | None = field(default=None, repr=False, compare=False)
    audit_raw_retention_days: int = 30
    audit_aggregate_retention_days: int = 180
    audit_dispatch_seconds: float = 5.0
    trace_exporter: ManagedOTLPHTTPTraceExporter | None = field(
        default=None, repr=False, compare=False
    )
    trace_dispatch_seconds: float = 5.0
    trace_max_spans_per_batch: int = 64
    trace_max_request_bytes: int = 1024 * 1024
    trace_retention_days: int = 7

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not _is_loopback_host(self.host):
            raise ServerConfigurationError("production server host must be loopback")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ServerConfigurationError(
                "production server port must be between 1 and 65535"
            )
        if (
            not isinstance(self.trusted_public_keys, Mapping)
            or not self.trusted_public_keys
        ):
            raise ServerConfigurationError(
                "at least one trusted release public key is required"
            )
        if (
            not isinstance(self.platform, str)
            or not _SAFE_ID.fullmatch(self.platform)
            or not isinstance(self.architecture, str)
            or not _SAFE_ID.fullmatch(self.architecture)
        ):
            raise ServerConfigurationError(
                "product platform and architecture are invalid"
            )
        if not isinstance(self.web_manifest_artifact_id, str) or not _SAFE_ID.fullmatch(
            self.web_manifest_artifact_id
        ):
            raise ServerConfigurationError("web manifest artifact id is invalid")
        if self.runtime_owner_nonce is not None and (
            not isinstance(self.runtime_owner_nonce, str)
            or _RUNTIME_OWNER_NONCE.fullmatch(self.runtime_owner_nonce) is None
        ):
            raise ServerConfigurationError("Runtime owner nonce is invalid")
        copied_keys: dict[str, bytes] = {}
        for key_id, public_key in self.trusted_public_keys.items():
            if not isinstance(key_id, str) or not key_id:
                raise ServerConfigurationError("trusted release key id is invalid")
            if not isinstance(public_key, bytes) or len(public_key) != 32:
                raise ServerConfigurationError(
                    f"trusted Ed25519 public key {key_id!r} must contain 32 bytes"
                )
            copied_keys[key_id] = bytes(public_key)
        object.__setattr__(self, "trusted_public_keys", MappingProxyType(copied_keys))
        if not 1 <= self.model_worker_concurrency <= 8:
            raise ServerConfigurationError(
                "model worker concurrency must be between one and eight"
            )
        if not 0.01 <= self.connector_maintenance_seconds <= 3600:
            raise ServerConfigurationError("connector maintenance interval is invalid")
        if not 1 <= self.retouch_worker_concurrency <= 4:
            raise ServerConfigurationError(
                "retouch worker concurrency must be between one and four"
            )
        if (
            self.model_gateway is not None
            and self.managed_session_service is None
            and not self.allow_unmanaged_session_for_testing
        ):
            raise ServerConfigurationError(
                "production Model Gateway requires a managed signed session"
            )
        if (
            self.model_gateway is not None
            and not isinstance(self.model_gateway, ManagedModelGatewayClient)
            and not self.allow_unmanaged_session_for_testing
        ):
            raise ServerConfigurationError(
                "production Model Gateway must use ManagedModelGatewayClient"
            )
        if (
            isinstance(self.model_gateway, ManagedModelGatewayClient)
            and self.managed_session_service is not None
            and self.model_gateway.credentials is not self.managed_session_service
            and not self.allow_unmanaged_session_for_testing
        ):
            raise ServerConfigurationError(
                "ManagedModelGatewayClient must use ManagedSessionService as credentials"
            )
        if (
            self.image_orchestration_client is not None
            and (
                not isinstance(
                    self.image_orchestration_client,
                    ManagedImageOrchestrationClient,
                )
                or self.managed_session_service is None
                or self.image_orchestration_client.session
                is not self.managed_session_service
            )
            and not self.allow_unmanaged_session_for_testing
        ):
            raise ServerConfigurationError(
                "production image orchestration requires the exact managed session"
            )
        for connector_id, adapter in self.connector_adapters.items():
            if (
                not isinstance(connector_id, str)
                or not isinstance(adapter, ManagedConnectorGatewayAdapter)
                or self.managed_session_service is None
                or adapter.session is not self.managed_session_service
            ) and not self.allow_unmanaged_session_for_testing:
                raise ServerConfigurationError(
                    "production connector adapters require the exact managed session"
                )
        if (
            self.trace_exporter is not None
            and (
                not isinstance(self.trace_exporter, ManagedOTLPHTTPTraceExporter)
                or self.managed_session_service is None
                or self.trace_exporter.session is not self.managed_session_service
            )
            and not self.allow_unmanaged_session_for_testing
        ):
            raise ServerConfigurationError(
                "production OTLP trace export requires the exact managed session"
            )
        if self.session_reload_requester is not None and not callable(
            self.session_reload_requester
        ):
            raise ServerConfigurationError("session reload requester must be callable")
        for callback, label in (
            (
                self.first_install_registration_recorder,
                "first-install registration recorder",
            ),
            (
                self.first_install_runtime_ready_recorder,
                "first-install Runtime readiness recorder",
            ),
        ):
            if callback is not None and not callable(callback):
                raise ServerConfigurationError(f"{label} must be callable")
        if not 0.05 <= self.device_authorization_poll_seconds <= 30:
            raise ServerConfigurationError(
                "device authorization poll interval is invalid"
            )
        if self.device_authorization_service is not None and (
            self.managed_session_service is None
            or self.device_authorization_service.session
            is not self.managed_session_service
        ):
            raise ServerConfigurationError(
                "device authorization must install into the configured managed session"
            )
        if not isinstance(self.workspace_roots, tuple) or not self.workspace_roots:
            raise ServerConfigurationError(
                "production server requires at least one explicit workspace root"
            )
        normalized_roots: list[Path] = []
        for raw_root in self.workspace_roots:
            try:
                root = Path(raw_root)
                metadata = root.lstat()
                attributes = getattr(metadata, "st_file_attributes", 0)
                reparse_flag = getattr(
                    stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                )
                if (
                    not stat_module.S_ISDIR(metadata.st_mode)
                    or stat_module.S_ISLNK(metadata.st_mode)
                    or bool(attributes & reparse_flag)
                ):
                    raise OSError("workspace root is not a real directory")
                resolved = root.resolve(strict=True)
            except (OSError, TypeError, ValueError):
                raise ServerConfigurationError(
                    "production workspace root must be a real existing directory"
                ) from None
            if resolved in normalized_roots:
                raise ServerConfigurationError(
                    "production workspace roots must be unique"
                )
            normalized_roots.append(resolved)
        object.__setattr__(self, "workspace_roots", tuple(normalized_roots))
        if self.output_default_location not in {"documents", "downloads", "workspace"}:
            raise ServerConfigurationError("default output location alias is invalid")
        normalized_output_roots: dict[str, Path] = {}
        for alias, raw_root in self.output_roots.items():
            if alias not in {"documents", "downloads", "workspace"}:
                raise ServerConfigurationError("output location alias is invalid")
            try:
                root = Path(raw_root).expanduser()
            except (TypeError, ValueError):
                raise ServerConfigurationError(
                    "output location root is invalid"
                ) from None
            if not root.is_absolute():
                raise ServerConfigurationError("output location root must be absolute")
            normalized_output_roots[alias] = Path(os.path.abspath(root))
        if (
            normalized_output_roots
            and self.output_default_location not in normalized_output_roots
        ):
            raise ServerConfigurationError(
                "default output location must be present in the configured roots"
            )
        object.__setattr__(
            self, "output_roots", MappingProxyType(normalized_output_roots)
        )

    @property
    def origin(self) -> str:
        return _origin(self.host, self.port)

    @property
    def authority(self) -> str:
        return self.origin.removeprefix("http://")


def _safe_script_json(value: Mapping[str, str]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _runtime_script(bundle: VerifiedWebBundle, bearer_token: str, nonce: str) -> str:
    configuration = _safe_script_json(
        {
            "apiBase": "/api/v1",
            "bearerToken": bearer_token,
            "releaseId": bundle.release_manifest.release_id,
            "version": bundle.release_manifest.version,
        }
    )
    return (
        f'<script nonce="{nonce}">'
        f"window.__ECOREX_RUNTIME__=Object.freeze({configuration});"
        'Object.defineProperty(window,"__ECOREX_RUNTIME__",'
        "{writable:false,configurable:false});"
        "</script>"
    )


def _index_response(
    request: Request,
    bundle: VerifiedWebBundle,
    bearer_token: str,
    *,
    template: str | None = None,
) -> Response:
    nonce = secrets.token_urlsafe(24)
    injected = (template or bundle.index_template).replace(
        RUNTIME_CONFIG_MARKER, _runtime_script(bundle, bearer_token, nonce)
    ).encode("utf-8")
    content = b"" if request.method == "HEAD" else injected
    csp = (
        "default-src 'none'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self'; img-src 'self' data: blob:; "
        "font-src 'self'; connect-src 'self'; media-src 'self' blob:; "
        "frame-src blob:; "
        "worker-src 'self' blob:; manifest-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    return Response(
        content=content,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Expires": "0",
            "Content-Security-Policy": csp,
        },
    )


def _not_found() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={"detail": "Not Found"},
        headers={"Cache-Control": "no-store"},
    )


def _apply_security_headers(response: Response) -> Response:
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    response.headers.setdefault(
        "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
    )
    return response


def _safe_request_path(path: str) -> bool:
    if "\\" in path or "\x00" in path or path.startswith("/"):
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and all(
        part not in {"", ".", ".."} and not part.startswith(".") and ":" not in part
        for part in parts
    )


def _is_spa_route(path: str) -> bool:
    if not path:
        return True
    if not _safe_request_path(path):
        return False
    return PurePosixPath(path).suffix == ""


def _secret(factory: SecretFactory, *, label: str) -> str:
    value = factory(48)
    if not isinstance(value, str) or len(value) < 32:
        raise ServerConfigurationError(f"{label} factory returned a weak secret")
    return value


def create_product_app(settings: ProductServerSettings) -> FastAPI:
    bundle = load_verified_web_bundle(
        web_root=settings.web_root,
        release_manifest_path=settings.release_manifest_path,
        web_manifest_path=settings.web_manifest_path,
        trusted_public_keys=settings.trusted_public_keys,
        web_manifest_artifact_id=settings.web_manifest_artifact_id,
    )
    secret_factory = settings.secret_factory or secrets.token_urlsafe
    bearer_token = _secret(secret_factory, label="runtime bearer")
    csrf_token = _secret(secret_factory, label="CSRF")
    if secrets.compare_digest(bearer_token, csrf_token):
        raise ServerConfigurationError("runtime bearer and CSRF secrets must differ")

    capability_registry = builtin_capability_registry()
    capability_runtime = build_capability_handler_set(
        capability_registry,
        workspace_roots=settings.workspace_roots,
        trusted_core_handlers={
            "feishu_cli": _execute_feishu_cli,
            **settings.capability_handlers,
        },
        pack_runtime=settings.capability_pack_runtime,
        workspace_root_resolver=ProjectWorkspaceAuthority(settings.database_path),
    )
    expected_pack_services = (
        settings.capability_pack_runtime.installed_service_ids
        & {"ocr.extract", "office.formats"}
        if settings.capability_pack_runtime is not None
        else frozenset()
    )
    if set(settings.capability_pack_services) != set(expected_pack_services):
        raise ServerConfigurationError(
            "verified Capability Pack service adapters are incomplete"
        )
    disabled_capability_tools = dict(capability_runtime.disabled_tools)
    initial_sandbox_profile = (
        "danger-full-access" if settings.full_access else "workspace-write"
    )
    for tool_id, profiles in capability_runtime.sandbox_profile_availability.items():
        reason = profiles.get(initial_sandbox_profile)
        if reason:
            disabled_capability_tools[tool_id] = reason
    if (
        settings.image_orchestration_client is None
        and "imagegen" in capability_runtime.handlers
    ):
        disabled_capability_tools["imagegen"] = (
            "managed_image_orchestration_not_configured"
        )
    if (
        settings.retouch_adapter is not None
        and "image" not in capability_runtime.installed_pack_ids
    ):
        raise ServerConfigurationError(
            "retouch adapter requires a verified image capability pack"
        )
    shell_handler = capability_runtime.handlers.get("shell")
    skill_sandbox_authority = getattr(
        shell_handler, "controlled_skill_sandbox_authority", None
    )
    try:
        extension_service = compose_extension_service(
            database_path=settings.database_path,
            product_version=bundle.release_manifest.version,
            core_build_digest=bundle.release_manifest.build_digest,
            runtime_api_version="1.0.0",
            platform=settings.platform,
            architecture=settings.architecture,
            capability_registry=capability_registry,
            connector_registry=builtin_connector_registry(),
            installed_pack_ids=capability_runtime.installed_pack_ids,
            signature_verifier=Ed25519SignatureVerifier(settings.trusted_public_keys),
            builtin_skill_root=Path(__file__).resolve().parents[2] / "skills",
            legacy_skill_roots=tuple(root / "skills" for root in settings.workspace_roots),
            skill_runner_factory=lambda store: create_production_controlled_skill_runner(
                store,
                platform=settings.platform,
                sandbox_authority=skill_sandbox_authority,
                workspace_roots=settings.workspace_roots,
            ),
            initialize=False,
            create_storage=False,
        )
    except Exception as error:
        raise ServerConfigurationError(
            "verified Extension authority could not be composed"
        ) from error

    app = FastAPI(
        title="e-Mate",
        version=bundle.release_manifest.version,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    runtime_settings = RuntimeSettings(
        database_path=settings.database_path,
        product_version=bundle.release_manifest.version,
        platform=settings.platform,
        architecture=settings.architecture,
        extension_service=extension_service,
        full_access=settings.full_access,
        admin_hard_denies=list(settings.admin_hard_denies),
        enforce_admin_tool_denies=settings.enforce_admin_tool_denies,
        require_managed_session=not settings.allow_unmanaged_session_for_testing,
        allow_unmanaged_model_gateway_for_testing=(
            settings.allow_unmanaged_session_for_testing
        ),
        managed_session_service=settings.managed_session_service,
        managed_session_refresh_service=settings.managed_session_refresh_service,
        managed_session_refresh_poll_seconds=(
            settings.managed_session_refresh_poll_seconds
        ),
        device_authorization_service=settings.device_authorization_service,
        skill_hub_client=settings.device_authorization_service,
        device_authorization_poll_seconds=(settings.device_authorization_poll_seconds),
        close_device_authorization_broker_on_shutdown=(
            settings.close_device_authorization_broker_on_shutdown
        ),
        session_reload_requester=settings.session_reload_requester,
        first_install_registration_recorder=(
            settings.first_install_registration_recorder
        ),
        first_install_runtime_ready_recorder=(
            settings.first_install_runtime_ready_recorder
        ),
        runtime_bearer_token=bearer_token,
        csrf_token=csrf_token,
        webui_origins=(settings.origin,),
        model_gateway=settings.model_gateway,
        image_orchestration_client=settings.image_orchestration_client,
        capability_handlers=capability_runtime.handlers,
        capability_pack_services=settings.capability_pack_services,
        mcp_runtime_bindings=tuple(settings.mcp_runtime_bindings),
        installed_capability_packs=capability_runtime.installed_pack_ids,
        disabled_capability_tools=disabled_capability_tools,
        capability_sandbox_profile_availability=(
            capability_runtime.sandbox_profile_availability
        ),
        output_roots=(settings.output_roots or None),
        output_default_location=settings.output_default_location,
        model_worker_concurrency=settings.model_worker_concurrency,
        update_service=settings.update_service,
        connector_adapters=settings.connector_adapters,
        connector_vault=settings.connector_vault,
        connector_oauth_return_uri=(
            settings.origin + "/api/v1/connectors/oauth/callback"
        ),
        connector_maintenance_seconds=settings.connector_maintenance_seconds,
        share_publisher=settings.share_publisher,
        share_public_hosts=settings.share_public_hosts,
        share_worker_concurrency=settings.share_worker_concurrency,
        share_worker_poll_seconds=settings.share_worker_poll_seconds,
        share_worker_shutdown_seconds=settings.share_worker_shutdown_seconds,
        share_worker_lease_seconds=settings.share_worker_lease_seconds,
        share_worker_retry_seconds=settings.share_worker_retry_seconds,
        share_worker_max_attempts=settings.share_worker_max_attempts,
        share_operation_deadline_seconds=settings.share_operation_deadline_seconds,
        retouch_adapter=settings.retouch_adapter,
        retouch_worker_concurrency=settings.retouch_worker_concurrency,
        audit_publisher=settings.audit_publisher,
        audit_raw_retention_days=settings.audit_raw_retention_days,
        audit_aggregate_retention_days=settings.audit_aggregate_retention_days,
        audit_dispatch_seconds=settings.audit_dispatch_seconds,
        trace_exporter=settings.trace_exporter,
        trace_dispatch_seconds=settings.trace_dispatch_seconds,
        trace_max_spans_per_batch=settings.trace_max_spans_per_batch,
        trace_max_request_bytes=settings.trace_max_request_bytes,
        trace_retention_days=settings.trace_retention_days,
    )
    register_runtime(settings=runtime_settings, app=app)
    app.state.web_bundle = bundle
    app.state.runtime_bearer_token = bearer_token
    skill_hub_pages = [
        value
        for path, value in bundle.files.items()
        if path.startswith("assets/skill-hub-page.") and path.endswith(".json")
    ]
    skill_hub_template = (
        _validate_index(skill_hub_pages[0].content) if len(skill_hub_pages) == 1 else None
    )

    @app.get(
        "/api/v1/runtime-owner",
        status_code=204,
        include_in_schema=False,
    )
    async def runtime_owner(request: Request) -> Response:
        supplied = request.headers.get("X-EcoreX-Owner-Nonce", "")
        expected = settings.runtime_owner_nonce
        if (
            expected is None
            or not isinstance(supplied, str)
            or not secrets.compare_digest(supplied, expected)
        ):
            return _apply_security_headers(
                JSONResponse(
                    status_code=404,
                    content={"detail": "Not Found"},
                    headers={"Cache-Control": "no-store"},
                )
            )
        return _apply_security_headers(
            Response(
                status_code=204,
                headers={
                    "Cache-Control": "no-store",
                    "X-EcoreX-Runtime-Owner": "verified",
                },
            )
        )

    @app.middleware("http")
    async def product_boundary(request: Request, call_next):
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or hosts[0].casefold() != settings.authority.casefold():
            return _apply_security_headers(
                JSONResponse(
                    status_code=400,
                    content={"detail": "invalid Host header"},
                    headers={"Cache-Control": "no-store"},
                )
            )
        response = await call_next(request)
        return _apply_security_headers(response)

    @app.api_route(
        "/{requested_path:path}", methods=["GET", "HEAD"], include_in_schema=False
    )
    async def web(request: Request, requested_path: str):
        folded_path = requested_path.casefold()
        if folded_path == "api" or folded_path.startswith("api/"):
            return _not_found()
        if requested_path == "":
            return _index_response(request, bundle, bearer_token)
        if folded_path.rstrip("/") == "ecorex-agent/skills":
            if skill_hub_template is None:
                return _not_found()
            return _index_response(
                request,
                bundle,
                bearer_token,
                template=skill_hub_template,
            )
        if not _safe_request_path(requested_path):
            return _not_found()
        verified = bundle.file(requested_path)
        if verified is not None:
            if requested_path == bundle.web_manifest.entrypoint:
                return _index_response(request, bundle, bearer_token)
            content = b"" if request.method == "HEAD" else verified.content
            cache_control = (
                "public, max-age=31536000, immutable"
                if verified.record.immutable
                else "no-cache"
            )
            return Response(
                content=content,
                media_type=verified.media_type,
                headers={
                    "Cache-Control": cache_control,
                    "ETag": f'"{verified.record.sha256}"',
                },
            )
        if _is_spa_route(requested_path):
            return _index_response(request, bundle, bearer_token)
        return _not_found()

    return app
