"""CowAgent-compatible MCP execution for configured local and HTTP servers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Protocol
import unicodedata
from urllib.parse import urljoin, urlsplit
import uuid

import httpx

from ecorex import __version__
from ecorex.capabilities import (
    ApprovalRequirement,
    CapabilityEffect,
    Exposure,
    IdempotencyClass,
    SandboxLevel,
    ToolInvocationContext,
    ToolProviderKind,
    ToolProviderProvenance,
    ToolProviderTrust,
    ToolSpec,
    normalize_reference,
)
from ecorex.capabilities.schema import SchemaInstanceError, canonical_json_value

from .errors import ExtensionIntegrityError
from .execution import MCPContribution
from .models import (
    SUPPORTED_MCP_PROTOCOL_VERSIONS,
    ExtensionExportKind,
    ExtensionHealth,
    ExtensionKind,
    ExtensionManifest,
    ExtensionStatus,
    ExtensionTransport,
    VerifiedExtensionManifest,
    canonical_digest,
)
from .mcp_oauth import MCPOAuthRegistration, MCPOAuthService
from .service import ExtensionService


MCP_PROTOCOL_VERSION = "2024-11-05"
MAX_MCP_MESSAGE_BYTES = 1024 * 1024
MAX_MCP_TOOLS = 256
MAX_MCP_TOOL_PAGES = 16
MAX_MCP_TOOL_NAME_BYTES = 128
MAX_MCP_DESCRIPTION_BYTES = 4096
MAX_MCP_SCHEMA_BYTES = 64 * 1024
MAX_MCP_INTENT_TAG_BYTES = 128
# ToolSpec permits 32 search tags.  ``extension_id`` and the exact MCP tool
# name are Runtime-owned provenance/routing tags added during composition, so
# an MCP contract may consume at most the remaining 30 slots.
MAX_MCP_CONTRACT_INTENT_TAGS = 30
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SESSION_ID = re.compile(r"^[\x21-\x7e]{1,256}$")
_BIDI_CONTROL_CLASSES = frozenset(
    {"BN", "LRE", "LRO", "RLE", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
)
def _normalize_mcp_text(
    value: Any,
    *,
    label: str,
    maximum_bytes: int,
    allow_newlines: bool,
) -> str:
    """Return one bounded display/search string with no spoofing controls."""

    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    # Keep ordinary multiline descriptions deterministic across transports.
    # Tabs are presentation controls rather than contract data.
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").strip()
    if not normalized or len(normalized.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} is invalid")
    for character in normalized:
        if character == "\n" and allow_newlines:
            continue
        if (
            unicodedata.category(character) in {"Cc", "Cf"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROL_CLASSES
        ):
            raise ValueError(f"{label} contains unsafe control characters")
    return normalized


def _validate_mcp_schema(schema: Mapping[str, Any], *, label: str) -> None:
    """Keep the MCP server's one JSON schema intact for model and execution."""

    if not isinstance(schema, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        canonical = canonical_json_value(schema, label=label)
    except SchemaInstanceError as exc:
        raise ValueError(f"{label} is not canonical JSON") from exc
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > MAX_MCP_SCHEMA_BYTES:
        raise ValueError(f"{label} exceeds the MCP schema size limit")


def _validate_mcp_initialize_result(result: Any) -> None:
    """Accept the protocol subset used by CowAgent MCP servers."""

    if not isinstance(result, Mapping):
        raise MCPProtocolError("mcp_initialize_contract_invalid")
    if result.get("protocolVersion") not in SUPPORTED_MCP_PROTOCOL_VERSIONS:
        raise MCPProtocolError("mcp_initialize_contract_invalid")
    capabilities = result.get("capabilities")
    if not isinstance(capabilities, Mapping):
        raise MCPProtocolError("mcp_initialize_capabilities_invalid")
    tools = capabilities.get("tools", {})
    if not isinstance(tools, Mapping):
        raise MCPProtocolError("mcp_initialize_capabilities_invalid")
    server_info = result.get("serverInfo")
    if not isinstance(server_info, Mapping) or not {"name", "version"} <= set(
        server_info
    ):
        raise MCPProtocolError("mcp_server_info_invalid")
    try:
        _normalize_mcp_text(
            server_info.get("name"),
            label="MCP server name",
            maximum_bytes=128,
            allow_newlines=False,
        )
        _normalize_mcp_text(
            server_info.get("version"),
            label="MCP server version",
            maximum_bytes=128,
            allow_newlines=False,
        )
    except ValueError:
        raise MCPProtocolError("mcp_server_info_invalid") from None


class MCPError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", code) else "mcp_failed"
        self.retryable = bool(retryable)
        super().__init__(self.code)


class MCPTransportError(MCPError):
    pass


class MCPProtocolError(MCPError):
    pass


class MCPTransportSession(Protocol):
    transport_kind: ExtensionTransport

    async def exchange(
        self,
        message: Mapping[str, Any],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]: ...

    async def notify(
        self,
        message: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> None: ...

    async def close(self) -> None: ...


class MCPBearerTokenProvider(Protocol):
    async def access_token(self) -> str | None: ...

    async def refresh_after_unauthorized(self) -> str | None: ...


SessionFactory = Callable[[str], MCPTransportSession | Awaitable[MCPTransportSession]]


@dataclass(frozen=True, slots=True)
class MCPToolContract:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] = field(default_factory=lambda: {"type": "object"})
    effects: frozenset[CapabilityEffect] = frozenset(
        {CapabilityEffect.READ, CapabilityEffect.NETWORK}
    )
    idempotency: IdempotencyClass = IdempotencyClass.READ_ONLY
    approval_requirement: ApprovalRequirement = ApprovalRequirement.NEVER
    required_sandbox: SandboxLevel = SandboxLevel.DANGER_FULL_ACCESS
    exposure: Exposure = Exposure.DIRECT
    intent_tags: frozenset[str] = frozenset({"mcp"})

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or not _TOOL_NAME.fullmatch(self.name)
            or len(self.name.encode("utf-8")) > MAX_MCP_TOOL_NAME_BYTES
        ):
            raise ValueError("MCP tool name is unsafe")
        object.__setattr__(
            self,
            "description",
            _normalize_mcp_text(
                self.description,
                label="MCP tool description",
                maximum_bytes=MAX_MCP_DESCRIPTION_BYTES,
                allow_newlines=True,
            ),
        )
        _validate_mcp_schema(self.input_schema, label=f"MCP {self.name} input")
        _validate_mcp_schema(self.output_schema, label=f"MCP {self.name} output")
        if not isinstance(self.intent_tags, frozenset):
            raise ValueError("MCP tool intent tags must be a frozenset")
        if len(self.intent_tags) > MAX_MCP_CONTRACT_INTENT_TAGS:
            raise ValueError("MCP tool intent tags consume reserved Runtime slots")
        safe_tags: list[str] = []
        normalized_tags: list[str] = []
        for tag in self.intent_tags:
            safe_tag = _normalize_mcp_text(
                tag,
                label="MCP tool intent tag",
                maximum_bytes=MAX_MCP_INTENT_TAG_BYTES,
                allow_newlines=False,
            )
            safe_tags.append(safe_tag)
            normalized_tags.append(normalize_reference(safe_tag))
        if any(not tag for tag in normalized_tags) or len(set(normalized_tags)) != len(
            normalized_tags
        ):
            raise ValueError("MCP tool intent tags are invalid or ambiguous")
        object.__setattr__(self, "intent_tags", frozenset(safe_tags))
        if CapabilityEffect.NETWORK not in self.effects:
            raise ValueError("MCP tools must declare the network effect")
        if self.idempotency is IdempotencyClass.READ_ONLY and self.effects & {
            CapabilityEffect.WRITE,
            CapabilityEffect.EXECUTE,
        }:
            raise ValueError("write/execute MCP tools cannot claim read-only idempotency")
        if (
            CapabilityEffect.EXECUTE in self.effects
            and self.idempotency is IdempotencyClass.IDEMPOTENT
        ):
            raise ValueError(
                "opaque execute MCP tools cannot claim idempotent replay semantics"
            )

    def tool_id(self, extension_id: str) -> str:
        value = f"mcp.{extension_id}:{self.name.casefold()}"
        if len(value) > 128:
            raise ValueError("namespaced MCP tool identity is too long")
        return value

    def to_tool_spec(
        self,
        extension_id: str,
        version: str,
        *,
        provider: ToolProviderProvenance,
    ) -> ToolSpec:
        if (
            provider.kind is not ToolProviderKind.MCP
            or provider.provider_id != extension_id
        ):
            raise ValueError("MCP tool requires exact verified provider provenance")
        return ToolSpec(
            tool_id=self.tool_id(extension_id),
            version=version,
            display_name=self.name,
            description=self.description,
            input_schema=dict(self.input_schema),
            output_schema=dict(self.output_schema),
            effects=self.effects,
            idempotency=self.idempotency,
            concurrency_safe=False,
            required_sandbox=self.required_sandbox,
            approval_requirement=self.approval_requirement,
            default_exposure=self.exposure,
            intent_tags=self.intent_tags | frozenset({extension_id, self.name}),
            provider=provider,
        )

    def expected_list_item(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
            "outputSchema": dict(self.output_schema),
        }


@dataclass(frozen=True, slots=True)
class MCPRuntimeBinding:
    """Non-serializable, product-owned binding to one verified revision."""

    extension_id: str
    revision_id: str
    artifact_sha256: str
    transport: ExtensionTransport
    tools: tuple[MCPToolContract, ...]
    verified_manifest: VerifiedExtensionManifest = field(repr=False, compare=False)
    session_factory: SessionFactory = field(repr=False, compare=False)
    request_timeout_seconds: float = 60.0
    oauth_registration: MCPOAuthRegistration | None = None

    def __post_init__(self) -> None:
        if self.transport not in {
            ExtensionTransport.STDIO,
            ExtensionTransport.STREAMABLE_HTTP,
        }:
            raise ValueError("MCP binding transport is invalid")
        if not callable(self.session_factory):
            raise TypeError("MCP binding requires a verified session factory")
        if not 0.05 <= self.request_timeout_seconds <= 300:
            raise ValueError("MCP request timeout is invalid")
        names = tuple(tool.name for tool in self.tools)
        if not names or len(names) > MAX_MCP_TOOLS or names != tuple(sorted(set(names))):
            raise ValueError("MCP tool contracts must be non-empty, unique, and sorted")
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("MCP tool names collide in the EcoreX capability namespace")
        if not re.fullmatch(r"extrev_[0-9a-f]{64}", self.revision_id):
            raise ValueError("MCP binding revision identity is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.artifact_sha256):
            raise ValueError("MCP binding artifact digest is invalid")
        if not isinstance(self.verified_manifest, VerifiedExtensionManifest):
            raise ValueError("MCP binding requires a verified Extension manifest")
        if self.oauth_registration is not None and (
            self.transport is not ExtensionTransport.STREAMABLE_HTTP
            or self.oauth_registration.service_id != self.extension_id
        ):
            raise ValueError("MCP OAuth registration disagrees with its Runtime binding")
        manifest = self.verified_manifest.manifest
        if (
            manifest.extension_id != self.extension_id
            or manifest.revision_id != self.revision_id
            or manifest.artifact_sha256 != self.artifact_sha256
            or manifest.transport is not self.transport
            or manifest.kind is not ExtensionKind.MCP_SERVER
        ):
            raise ValueError("verified MCP manifest disagrees with its Runtime binding")


@dataclass(slots=True)
class _LiveSession:
    transport: MCPTransportSession
    binding: MCPRuntimeBinding
    tenant_id: str
    request_prefix: str
    next_id: int = 1
    observed_response_ids: set[str | int] = field(default_factory=set)
    initialized: bool = False

    def request_id(self) -> str:
        value = f"{self.request_prefix}:{self.next_id}"
        self.next_id += 1
        return value


@dataclass(slots=True)
class _Circuit:
    failures: int = 0
    opened_until: datetime | None = None


class MCPClientSupervisor:
    """Tenant/revision isolated MCP lifecycle with exact catalog fencing."""

    def __init__(
        self,
        service: ExtensionService,
        bindings: Sequence[MCPRuntimeBinding],
        *,
        failure_threshold: int = 3,
        circuit_seconds: int = 30,
        snapshot_resolver: Callable[[str], str] | None = None,
        tenant_resolver: Callable[[ToolInvocationContext], str] | None = None,
        oauth_service: MCPOAuthService | None = None,
    ) -> None:
        if not 1 <= failure_threshold <= 20 or not 1 <= circuit_seconds <= 3600:
            raise ValueError("MCP circuit policy is invalid")
        by_extension = {binding.extension_id: binding for binding in bindings}
        if len(by_extension) != len(tuple(bindings)):
            raise ValueError("MCP binding extension identities must be unique")
        self.service = service
        self.bindings = MappingProxyType(by_extension)
        self.failure_threshold = failure_threshold
        self.circuit_seconds = circuit_seconds
        self.snapshot_resolver = snapshot_resolver
        self.tenant_resolver = tenant_resolver or (lambda _context: "local-user")
        self.oauth_service = oauth_service
        self._sessions: dict[tuple[str, str], _LiveSession] = {}
        self._circuits: dict[tuple[str, str], _Circuit] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        specs: list[ToolSpec] = []
        for extension_id, binding in sorted(self.bindings.items()):
            manifest = self.service.repository.manifest(binding.revision_id)
            self._validate_binding(binding, manifest)
            provider = self._provider_provenance(binding)
            specs.extend(
                tool.to_tool_spec(
                    extension_id,
                    manifest.version,
                    provider=provider,
                )
                for tool in binding.tools
            )
        return tuple(sorted(specs, key=lambda item: item.tool_id))

    def contribution_records(self, extension_snapshot_id: str) -> tuple[MCPContribution, ...]:
        payload = self.service.repository.snapshot_payload(extension_snapshot_id)
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ExtensionIntegrityError("extension snapshot items are invalid")
        records: list[MCPContribution] = []
        for item in raw_items:
            if not isinstance(item, Mapping) or item.get("kind") != ExtensionKind.MCP_SERVER.value:
                continue
            if (
                item.get("status") != ExtensionStatus.ENABLED.value
                or item.get("health") != ExtensionHealth.HEALTHY.value
            ):
                continue
            extension_id = str(item.get("extension_id", ""))
            revision_id = str(item.get("active_revision_id", ""))
            binding = self.bindings.get(extension_id)
            if binding is None or binding.revision_id != revision_id:
                # A newly installed/replaced executable provider is staged for
                # the next Runtime composition, never hot-loaded.
                continue
            manifest = self.service.repository.manifest(revision_id)
            self._validate_binding(binding, manifest)
            provider = self._provider_provenance(binding)
            specs = tuple(
                tool.to_tool_spec(
                    extension_id,
                    manifest.version,
                    provider=provider,
                )
                for tool in binding.tools
            )
            export = next(
                (
                    exported
                    for exported in manifest.exports
                    if exported.kind is ExtensionExportKind.MCP_SERVER
                    and exported.export_id == extension_id
                ),
                None,
            )
            if export is None:
                raise ExtensionIntegrityError("MCP provider lacks its exact server export")
            records.append(
                MCPContribution(
                    extension_id=extension_id,
                    revision_id=revision_id,
                    artifact_sha256=manifest.artifact_sha256,
                    transport=binding.transport.value,
                    protocol_version=MCP_PROTOCOL_VERSION,
                    export_digest=canonical_digest(
                        {
                            "revision_id": revision_id,
                            "artifact_sha256": manifest.artifact_sha256,
                            "export": export.to_dict(),
                        }
                    ),
                    tool_catalog_digest=canonical_digest(
                        {"tools": [spec.to_dict() for spec in specs]}
                    ),
                    tool_ids=tuple(spec.tool_id for spec in specs),
                    provider=provider,
                )
            )
        return tuple(sorted(records, key=lambda item: (item.extension_id, item.revision_id)))

    def handlers(self) -> Mapping[str, Any]:
        return {
            tool.tool_id(binding.extension_id): _MCPToolHandler(self, binding, tool)
            for binding in self.bindings.values()
            for tool in binding.tools
        }

    def owns_tool(self, tool_id: str) -> bool:
        return any(
            tool.tool_id(binding.extension_id) == tool_id
            for binding in self.bindings.values()
            for tool in binding.tools
        ) or self.service.owns_tool(tool_id)

    def assert_tool_invocable(self, extension_snapshot_id: str, tool_id: str) -> None:
        del extension_snapshot_id
        if not self.owns_tool(tool_id):
            raise ExtensionIntegrityError("MCP tool is not configured")

    async def call(
        self,
        extension_snapshot_id: str,
        binding: MCPRuntimeBinding,
        tool: MCPToolContract,
        arguments: Mapping[str, Any],
        *,
        tenant_id: str,
        idempotency_key: str | None = None,
    ) -> Any:
        del extension_snapshot_id
        key = (_safe_tenant(tenant_id), binding.revision_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            self._assert_circuit(key)
            usable_idempotency_key = (
                idempotency_key
                if isinstance(idempotency_key, str) and idempotency_key.strip()
                else None
            )
            retry_tool_call = tool.idempotency is IdempotencyClass.READ_ONLY or (
                tool.idempotency is IdempotencyClass.IDEMPOTENT
                and usable_idempotency_key is not None
            )
            for call_attempt in range(2):
                # Session creation, initialize, and tools/list happen before
                # tools/call can produce a business side effect.  They retain
                # one bounded transport retry (subject to the circuit breaker)
                # independently of the tool's idempotency class.
                live = await self._ready_session_with_safe_retry(key, binding)
                try:
                    call_params: dict[str, Any] = {
                        "name": tool.name,
                        "arguments": dict(arguments),
                    }
                    if usable_idempotency_key is not None:
                        call_params["_meta"] = {
                            "com.ecorex/idempotency-key": usable_idempotency_key
                        }
                    response = await self._request(
                        live,
                        "tools/call",
                        call_params,
                    )
                    result = response.get("result")
                    if not isinstance(result, Mapping):
                        raise MCPProtocolError("mcp_tool_result_invalid")
                    self._success(key)
                    return canonical_json_value(result, label="MCP tool result")
                except asyncio.CancelledError:
                    await self._discard(key)
                    raise
                except MCPTransportError as error:
                    await self._discard(key)
                    self._failure(key)
                    if (
                        call_attempt == 0
                        and error.retryable
                        and retry_tool_call
                        and not self._circuit_open(key)
                    ):
                        continue
                    if error.retryable and not retry_tool_call:
                        # A tools/call request may already have committed at
                        # the provider.  Mark this transport failure
                        # non-retryable so the Durable Worker does not enter a
                        # generic retry path.  NON_IDEMPOTENT executions are
                        # then persisted as uncertain and handed to HITL.
                        raise MCPTransportError(
                            error.code,
                            retryable=False,
                        ) from error
                    raise
                except MCPProtocolError:
                    await self._discard(key)
                    self._failure(key)
                    raise
        raise MCPTransportError("mcp_transport_failed", retryable=True)

    async def _ready_session_with_safe_retry(
        self,
        key: tuple[str, str],
        binding: MCPRuntimeBinding,
    ) -> _LiveSession:
        """Establish the side-effect-free MCP session with one bounded retry.

        Only session creation, initialize, initialized notification, and the
        immutable tools/list verification execute here.  Keeping this retry
        loop separate prevents a successful tools/call from being confused
        with a failed handshake when a connection dies.
        """

        for attempt in range(2):
            try:
                return await self._session(key, binding)
            except asyncio.CancelledError:
                await self._discard(key)
                raise
            except MCPTransportError as error:
                await self._discard(key)
                self._failure(key)
                if attempt == 0 and error.retryable and not self._circuit_open(key):
                    continue
                raise
            except MCPProtocolError:
                await self._discard(key)
                self._failure(key)
                raise
        raise MCPTransportError("mcp_transport_failed", retryable=True)

    async def close(self) -> None:
        for key in tuple(self._sessions):
            await self._discard(key)

    async def start(self) -> None:
        """Lifecycle compatibility; sessions remain lazy per tenant/revision."""

    async def stop(self) -> None:
        await self.close()

    async def _session(
        self,
        key: tuple[str, str],
        binding: MCPRuntimeBinding,
    ) -> _LiveSession:
        live = self._sessions.get(key)
        if live is not None:
            return live
        try:
            transport = binding.session_factory(key[0])
            if asyncio.iscoroutine(transport) or isinstance(transport, Awaitable):
                transport = await transport
        except Exception:
            raise MCPTransportError("mcp_session_start_failed", retryable=True) from None
        if not hasattr(transport, "exchange") or not hasattr(transport, "notify"):
            raise MCPTransportError("mcp_session_factory_invalid")
        if getattr(transport, "transport_kind", None) is not binding.transport:
            raise MCPTransportError("mcp_transport_binding_mismatch")
        if binding.oauth_registration is not None:
            if self.oauth_service is None or not isinstance(
                transport, ManagedHTTPMCPTransport
            ):
                raise MCPTransportError("mcp_oauth_binding_unavailable")
            transport.bind_oauth(
                self.oauth_service.provider(key[0], binding.oauth_registration.service_id)
            )
        if any(existing.transport is transport for existing in self._sessions.values()):
            raise MCPTransportError("mcp_tenant_session_reuse_forbidden")
        live = _LiveSession(
            transport=transport,
            binding=binding,
            tenant_id=key[0],
            request_prefix=uuid.uuid4().hex,
        )
        try:
            initialize = await self._request(
                live,
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "e-Mate", "version": __version__},
                },
            )
            result = initialize.get("result")
            _validate_mcp_initialize_result(result)
            await self._notify(live, "notifications/initialized", {})
            listed = await self._list_tools(live)
            self._validate_tool_catalog(binding, listed)
            live.initialized = True
            self._sessions[key] = live
            return live
        except BaseException:
            try:
                await live.transport.close()
            except Exception:
                pass
            raise

    async def _list_tools(self, live: _LiveSession) -> Mapping[str, Any]:
        tools: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(MAX_MCP_TOOL_PAGES):
            params = {"cursor": cursor} if cursor is not None else {}
            response = await self._request(live, "tools/list", params)
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise MCPProtocolError("mcp_tool_catalog_shape_invalid")
            page_tools = result.get("tools")
            if not isinstance(page_tools, list):
                raise MCPProtocolError("mcp_tool_catalog_shape_invalid")
            tools.extend(page_tools)
            if len(tools) > MAX_MCP_TOOLS:
                raise MCPProtocolError("mcp_tool_catalog_size_invalid")
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return {"result": {"tools": tools}}
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or len(next_cursor.encode("utf-8")) > 256
                or any(
                    unicodedata.category(character) in {"Cc", "Cf"}
                    or unicodedata.bidirectional(character) in _BIDI_CONTROL_CLASSES
                    for character in next_cursor
                )
                or next_cursor in seen_cursors
            ):
                raise MCPProtocolError("mcp_tool_catalog_cursor_invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise MCPProtocolError("mcp_tool_catalog_page_limit")

    async def _request(
        self,
        live: _LiveSession,
        method: str,
        params: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        request_id = live.request_id()
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        _bounded_json(message, label="MCP request")
        try:
            response = await live.transport.exchange(
                message,
                timeout_seconds=live.binding.request_timeout_seconds,
                max_response_bytes=MAX_MCP_MESSAGE_BYTES,
            )
        except asyncio.CancelledError:
            cancellation = {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {
                    "requestId": request_id,
                    "reason": "e-Mate Turn was cancelled",
                },
            }
            try:
                await asyncio.shield(
                    live.transport.notify(
                        cancellation,
                        timeout_seconds=min(2.0, live.binding.request_timeout_seconds),
                    )
                )
            except BaseException:
                pass
            raise
        except MCPError:
            raise
        except TimeoutError:
            raise MCPTransportError("mcp_request_timeout", retryable=True) from None
        except Exception:
            raise MCPTransportError("mcp_transport_failed", retryable=True) from None
        response_payload = _bounded_json(response, label="MCP response")
        # Continue only with the canonical JSON graph that crossed the byte
        # boundary.  A custom Mapping returned by an in-process transport may
        # not retain Python equality or iteration behavior inside catalog
        # verification.
        response = json.loads(response_payload.decode("utf-8"))
        if set(response) - {"jsonrpc", "id", "result", "error"}:
            raise MCPProtocolError("mcp_response_fields_invalid")
        response_id = response.get("id")
        if response_id != request_id or response_id in live.observed_response_ids:
            raise MCPProtocolError("mcp_response_id_invalid")
        live.observed_response_ids.add(response_id)
        if response.get("jsonrpc") != "2.0" or (("result" in response) == ("error" in response)):
            raise MCPProtocolError("mcp_response_shape_invalid")
        if "error" in response:
            error = response["error"]
            if not isinstance(error, Mapping) or not isinstance(error.get("code"), int):
                raise MCPProtocolError("mcp_error_shape_invalid")
            raise MCPError("mcp_server_error")
        return dict(response)

    async def _notify(
        self,
        live: _LiveSession,
        method: str,
        params: Mapping[str, Any],
    ) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        _bounded_json(message, label="MCP notification")
        try:
            await live.transport.notify(
                message,
                timeout_seconds=live.binding.request_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except MCPError:
            raise
        except Exception:
            raise MCPTransportError("mcp_notification_failed", retryable=True) from None

    @staticmethod
    def _validate_tool_catalog(
        binding: MCPRuntimeBinding,
        response: Mapping[str, Any],
    ) -> None:
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise MCPProtocolError("mcp_tool_catalog_shape_invalid")
        tools = result.get("tools")
        if not isinstance(tools, list) or not 1 <= len(tools) <= MAX_MCP_TOOLS:
            raise MCPProtocolError("mcp_tool_catalog_size_invalid")
        normalized: list[dict[str, Any]] = []
        names: set[str] = set()
        for raw in tools:
            if (
                not isinstance(raw, Mapping)
                or not {"name", "inputSchema"} <= set(raw)
            ):
                raise MCPProtocolError("mcp_tool_descriptor_invalid")
            name = raw.get("name")
            if (
                not isinstance(name, str)
                or not _TOOL_NAME.fullmatch(name)
                or len(name.encode("utf-8")) > MAX_MCP_TOOL_NAME_BYTES
                or name in names
            ):
                raise MCPProtocolError("mcp_tool_name_invalid")
            try:
                description = _normalize_mcp_text(
                    raw.get("description", f"MCP tool {name}"),
                    label="MCP tool description",
                    maximum_bytes=MAX_MCP_DESCRIPTION_BYTES,
                    allow_newlines=True,
                )
                input_schema = raw.get("inputSchema")
                output_schema = raw.get("outputSchema", {"type": "object"})
                _validate_mcp_schema(input_schema, label=f"MCP {name} input")
                _validate_mcp_schema(output_schema, label=f"MCP {name} output")
            except (SchemaInstanceError, TypeError, ValueError):
                raise MCPProtocolError("mcp_tool_descriptor_invalid") from None
            names.add(name)
            normalized.append(
                {
                    "name": name,
                    "description": description,
                    "inputSchema": dict(input_schema),
                    "outputSchema": dict(output_schema),
                }
            )
        normalized.sort(key=lambda item: item["name"])
        expected = [tool.expected_list_item() for tool in binding.tools]
        if normalized != expected:
            raise MCPProtocolError("mcp_tool_catalog_digest_mismatch")

    def _validate_binding(
        self,
        binding: MCPRuntimeBinding,
        manifest: ExtensionManifest,
    ) -> None:
        if (
            manifest.kind is not ExtensionKind.MCP_SERVER
            or manifest.transport is not binding.transport
            or manifest.artifact_sha256 != binding.artifact_sha256
        ):
            raise ExtensionIntegrityError("MCP Runtime binding disagrees with its verified manifest")
        server_export = next(
            (
                exported
                for exported in manifest.exports
                if exported.kind is ExtensionExportKind.MCP_SERVER
                and exported.export_id == binding.extension_id
            ),
            None,
        )
        if server_export is None:
            raise ExtensionIntegrityError("MCP Runtime binding lacks an exact server export")
        declared_effects = set(server_export.permission_effects)
        for tool in binding.tools:
            if {effect.value for effect in tool.effects} - declared_effects:
                raise ExtensionIntegrityError(
                    "MCP tool effects exceed the signed provider export"
                )
        self.service.assert_revision_runtime_bound(binding.revision_id)
        self.service.assert_verified_runtime_binding(binding.verified_manifest)
        if MCP_PROTOCOL_VERSION not in SUPPORTED_MCP_PROTOCOL_VERSIONS:
            raise ExtensionIntegrityError("MCP stable protocol was removed from this Core")

    def _provider_provenance(
        self,
        binding: MCPRuntimeBinding,
    ) -> ToolProviderProvenance:
        manifest = self.service.assert_verified_runtime_binding(
            binding.verified_manifest
        )
        trust = {
            "builtin": ToolProviderTrust.BUILTIN,
            "administrator": ToolProviderTrust.ADMINISTRATOR,
            "verified_publisher": ToolProviderTrust.VERIFIED_PUBLISHER,
            "user_configured": ToolProviderTrust.USER_CONFIGURED,
        }.get(manifest.trust.value)
        if trust is None:
            raise ExtensionIntegrityError("MCP provider trust is not executable")
        # ``manifest_sha256`` binds the exact accepted signature bytes, but
        # only its digest and non-secret key identifier cross into capability
        # projections. Provider text has no input into this evidence.
        evidence_sha256 = canonical_digest(
            {
                "revision_id": manifest.revision_id,
                "manifest_sha256": manifest.manifest_sha256,
                "signature_key_id": manifest.signature.key_id,
                "signature_algorithm": manifest.signature.algorithm,
            }
        )
        return ToolProviderProvenance(
            kind=ToolProviderKind.MCP,
            provider_id=manifest.extension_id,
            revision_id=manifest.revision_id,
            trust=trust,
            key_id=manifest.signature.key_id,
            evidence_sha256=evidence_sha256,
            product_reviewed=False,
        )

    async def _discard(self, key: tuple[str, str]) -> None:
        live = self._sessions.pop(key, None)
        if live is not None:
            try:
                await live.transport.close()
            except Exception:
                pass

    def _assert_circuit(self, key: tuple[str, str]) -> None:
        circuit = self._circuits.setdefault(key, _Circuit())
        if circuit.opened_until is not None:
            if datetime.now(UTC) < circuit.opened_until:
                raise MCPTransportError("mcp_circuit_open", retryable=True)
            circuit.opened_until = None
            circuit.failures = 0

    def _failure(self, key: tuple[str, str]) -> None:
        circuit = self._circuits.setdefault(key, _Circuit())
        circuit.failures += 1
        if circuit.failures >= self.failure_threshold:
            circuit.opened_until = datetime.now(UTC) + timedelta(seconds=self.circuit_seconds)

    def _success(self, key: tuple[str, str]) -> None:
        self._circuits[key] = _Circuit()

    def _circuit_open(self, key: tuple[str, str]) -> bool:
        circuit = self._circuits.get(key)
        return bool(circuit and circuit.opened_until and datetime.now(UTC) < circuit.opened_until)


class _MCPToolHandler:
    def __init__(
        self,
        supervisor: MCPClientSupervisor,
        binding: MCPRuntimeBinding,
        tool: MCPToolContract,
    ) -> None:
        self.supervisor = supervisor
        self.binding = binding
        self.tool = tool

    async def __call__(
        self,
        arguments: Mapping[str, Any],
        context: ToolInvocationContext,
    ) -> Any:
        return await self.supervisor.call(
            "local-mcp",
            self.binding,
            self.tool,
            arguments,
            tenant_id=self.supervisor.tenant_resolver(context),
            idempotency_key=context.idempotency_key,
        )


class MCPStdioTransport:
    """Line-delimited JSON-RPC over an already isolated pack process.

    The transport intentionally has no spawn or command API.  A verified pack
    launcher owns process creation and hands this class its pipe object.
    """

    transport_kind = ExtensionTransport.STDIO

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        if process.stdin is None or process.stdout is None:
            raise ValueError("MCP stdio process requires stdin and stdout pipes")
        self.process = process
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None

    async def exchange(
        self,
        message: Mapping[str, Any],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]:
        payload = _bounded_json(message, label="MCP stdio request") + b"\n"
        self._ensure_stderr_drain()
        async with self._lock:
            if self.process.returncode is not None:
                raise MCPTransportError("mcp_process_exited", retryable=True)
            try:
                self.process.stdin.write(payload)
                await asyncio.wait_for(self.process.stdin.drain(), timeout_seconds)
                raw = await asyncio.wait_for(self.process.stdout.readline(), timeout_seconds)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise MCPTransportError("mcp_request_timeout", retryable=True) from None
            except (BrokenPipeError, ConnectionResetError, OSError):
                raise MCPTransportError("mcp_process_crashed", retryable=True) from None
        if not raw or len(raw) > max_response_bytes or not raw.endswith(b"\n"):
            raise MCPTransportError("mcp_stdio_response_invalid", retryable=not raw)
        return _decode_json_object(raw[:-1], label="MCP stdio response")

    async def notify(
        self,
        message: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> None:
        payload = _bounded_json(message, label="MCP stdio notification") + b"\n"
        self._ensure_stderr_drain()
        async with self._lock:
            if self.process.returncode is not None:
                raise MCPTransportError("mcp_process_exited", retryable=True)
            try:
                self.process.stdin.write(payload)
                await asyncio.wait_for(self.process.stdin.drain(), timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise MCPTransportError("mcp_process_crashed", retryable=True) from None

    async def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass

    def _ensure_stderr_drain(self) -> None:
        if self._stderr_task is None and self.process.stderr is not None:
            self._stderr_task = asyncio.create_task(
                _drain_process_stderr(self.process.stderr)
            )


class ManagedHTTPMCPTransport:
    """CowAgent-compatible HTTP/HTTPS Streamable-HTTP endpoint."""

    transport_kind = ExtensionTransport.STREAMABLE_HTTP

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient | None = None,
        expected_host: str,
        own_client: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.hostname.casefold() != expected_host.casefold()
        ):
            raise ValueError("MCP endpoint must be one fixed HTTP or HTTPS origin")
        self.endpoint = endpoint
        self._headers = dict(headers or {})
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=60, write=30, pool=10),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            follow_redirects=True,
            trust_env=True,
        )
        self._owns_client = client is None or own_client
        self._session_id: str | None = None
        self._oauth_provider: MCPBearerTokenProvider | None = None

    def bind_oauth(self, provider: MCPBearerTokenProvider) -> None:
        if self._oauth_provider is not None and self._oauth_provider is not provider:
            raise ValueError("MCP OAuth provider is already bound")
        self._oauth_provider = provider

    async def exchange(
        self,
        message: Mapping[str, Any],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]:
        return await self._post(
            message,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            expect_body=True,
        )

    async def notify(
        self,
        message: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> None:
        await self._post(
            message,
            timeout_seconds=timeout_seconds,
            max_response_bytes=64 * 1024,
            expect_body=False,
        )

    async def _post(
        self,
        message: Mapping[str, Any],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        expect_body: bool,
        _oauth_retried: bool = False,
    ) -> Mapping[str, Any]:
        headers = {
            **self._headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self._session_id is not None:
            headers["MCP-Session-Id"] = self._session_id
        if self._oauth_provider is not None:
            token = await self._oauth_provider.access_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        try:
            async with self.client.stream(
                "POST",
                self.endpoint,
                content=_bounded_json(message, label="MCP HTTP request"),
                headers=headers,
                timeout=timeout_seconds,
                follow_redirects=True,
            ) as response:
                status_code = response.status_code
                response_headers = dict(response.headers)
                content_buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    content_buffer.extend(chunk)
                    if len(content_buffer) > max_response_bytes:
                        raise MCPProtocolError("mcp_http_response_size_invalid")
                content = bytes(content_buffer)
        except httpx.TimeoutException:
            raise MCPTransportError("mcp_request_timeout", retryable=True) from None
        except httpx.TransportError:
            raise MCPTransportError("mcp_http_transport_failed", retryable=True) from None
        if status_code == 401 and self._oauth_provider is not None:
            token = (
                await self._oauth_provider.refresh_after_unauthorized()
                if not _oauth_retried
                else None
            )
            if token:
                return await self._post(
                    message,
                    timeout_seconds=timeout_seconds,
                    max_response_bytes=max_response_bytes,
                    expect_body=expect_body,
                    _oauth_retried=True,
                )
            raise MCPTransportError("mcp_oauth_authorization_required")
        if status_code == 404 and self._session_id is not None:
            self._session_id = None
            raise MCPTransportError("mcp_http_session_expired", retryable=True)
        if status_code not in ({200} if expect_body else {200, 202, 204}):
            raise MCPTransportError(
                "mcp_http_status_failed",
                retryable=status_code in {408, 425, 429, 500, 502, 503, 504},
            )
        session_id = response_headers.get("mcp-session-id")
        if session_id is not None:
            if not _SESSION_ID.fullmatch(session_id):
                raise MCPProtocolError("mcp_http_session_invalid")
            if self._session_id is not None and session_id != self._session_id:
                raise MCPProtocolError("mcp_http_session_changed")
            self._session_id = session_id
        if not expect_body:
            return {}
        if not content:
            raise MCPProtocolError("mcp_http_response_size_invalid")
        media_type = response_headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if media_type == "application/json":
            return _decode_json_object(content, label="MCP HTTP response")
        if media_type == "text/event-stream":
            return _decode_single_sse_json(content)
        raise MCPProtocolError("mcp_http_content_type_invalid")

    async def close(self) -> None:
        if self._session_id is not None:
            try:
                headers = {
                    **self._headers,
                    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                    "MCP-Session-Id": self._session_id,
                }
                if self._oauth_provider is not None:
                    token = await self._oauth_provider.access_token()
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                await self.client.delete(
                    self.endpoint,
                    headers=headers,
                    timeout=2,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                pass
            finally:
                self._session_id = None
        if self._owns_client:
            await self.client.aclose()


class LegacySSEMCPTransport:
    """CowAgent-compatible legacy MCP SSE transport."""

    transport_kind = ExtensionTransport.STREAMABLE_HTTP

    def __init__(
        self,
        endpoint: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP SSE endpoint must be HTTP or HTTPS")
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10),
            follow_redirects=True,
            trust_env=True,
        )
        self._stream_context: Any | None = None
        self._stream_response: httpx.Response | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._post_endpoint: str | None = None
        self._pending: dict[str | int, asyncio.Future[Mapping[str, Any]]] = {}
        self._ready_lock = asyncio.Lock()

    async def exchange(
        self,
        message: Mapping[str, Any],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, Any]:
        await self._ensure_ready(timeout_seconds)
        request_id = message.get("id")
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            raise MCPProtocolError("mcp_request_id_invalid")
        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        self._pending[request_id] = pending
        try:
            await self._post(message, timeout_seconds)
            response = await asyncio.wait_for(pending, timeout_seconds)
            if len(_bounded_json(response, label="MCP SSE response")) > max_response_bytes:
                raise MCPProtocolError("mcp_http_response_size_invalid")
            return response
        except TimeoutError:
            raise MCPTransportError("mcp_request_timeout", retryable=True) from None
        finally:
            self._pending.pop(request_id, None)

    async def notify(
        self,
        message: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> None:
        await self._ensure_ready(timeout_seconds)
        await self._post(message, timeout_seconds)

    async def _ensure_ready(self, timeout_seconds: float) -> None:
        if self._post_endpoint is not None:
            return
        async with self._ready_lock:
            if self._post_endpoint is not None:
                return
            self._stream_context = self.client.stream(
                "GET",
                self.endpoint,
                headers={**self.headers, "Accept": "text/event-stream"},
            )
            try:
                self._stream_response = await self._stream_context.__aenter__()
                self._stream_response.raise_for_status()
                iterator = self._stream_response.aiter_lines()
                event, data = await asyncio.wait_for(_next_sse_event(iterator), timeout_seconds)
            except Exception:
                await self.close()
                raise MCPTransportError("mcp_http_transport_failed", retryable=True) from None
            if event != "endpoint" or not data:
                await self.close()
                raise MCPProtocolError("mcp_sse_endpoint_invalid")
            self._post_endpoint = urljoin(self.endpoint, data)
            self._reader_task = asyncio.create_task(self._read_events(iterator))

    async def _post(self, message: Mapping[str, Any], timeout_seconds: float) -> None:
        assert self._post_endpoint is not None
        try:
            response = await self.client.post(
                self._post_endpoint,
                content=_bounded_json(message, label="MCP SSE request"),
                headers={**self.headers, "Content-Type": "application/json"},
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException:
            raise MCPTransportError("mcp_request_timeout", retryable=True) from None
        except httpx.TransportError:
            raise MCPTransportError("mcp_http_transport_failed", retryable=True) from None
        if response.status_code not in {200, 202, 204}:
            raise MCPTransportError(
                "mcp_http_status_failed",
                retryable=response.status_code in {408, 425, 429, 500, 502, 503, 504},
            )

    async def _read_events(self, iterator: Any) -> None:
        try:
            while True:
                event, data = await _next_sse_event(iterator)
                if event not in {"message", None} or not data:
                    continue
                response = _decode_json_object(
                    data.encode("utf-8"), label="MCP SSE response"
                )
                request_id = response.get("id")
                pending = self._pending.get(request_id)
                if pending is not None and not pending.done():
                    pending.set_result(response)
        except (asyncio.CancelledError, EOFError):
            pass
        except Exception as error:
            for pending in self._pending.values():
                if not pending.done():
                    pending.set_exception(
                        MCPTransportError("mcp_http_transport_failed", retryable=True)
                    )

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._stream_context is not None:
            try:
                await self._stream_context.__aexit__(None, None, None)
            except Exception:
                pass
            self._stream_context = None
        self._stream_response = None
        self._post_endpoint = None
        await self.client.aclose()


async def _next_sse_event(iterator: Any) -> tuple[str | None, str]:
    event: str | None = None
    data: list[str] = []
    async for line in iterator:
        if not line:
            if data:
                return event, "\n".join(data)
            event = None
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    raise EOFError("MCP SSE stream ended")


async def discover_mcp_tools(
    transport: MCPTransportSession,
) -> tuple[MCPToolContract, ...]:
    """Discover one CowAgent MCP server without rewriting its schemas."""

    prefix = uuid.uuid4().hex
    initialize = await transport.exchange(
        {
            "jsonrpc": "2.0",
            "id": f"{prefix}:1",
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "e-Mate", "version": __version__},
            },
        },
        timeout_seconds=30,
        max_response_bytes=MAX_MCP_MESSAGE_BYTES,
    )
    _validate_mcp_initialize_result(initialize.get("result"))
    await transport.notify(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        timeout_seconds=30,
    )
    raw_tools: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    for page in range(MAX_MCP_TOOL_PAGES):
        response = await transport.exchange(
            {
                "jsonrpc": "2.0",
                "id": f"{prefix}:{page + 2}",
                "method": "tools/list",
                "params": ({"cursor": cursor} if cursor is not None else {}),
            },
            timeout_seconds=30,
            max_response_bytes=MAX_MCP_MESSAGE_BYTES,
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise MCPProtocolError("mcp_tool_catalog_shape_invalid")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise MCPProtocolError("mcp_tool_catalog_shape_invalid")
        raw_tools.extend(tools)
        if not 1 <= len(raw_tools) <= MAX_MCP_TOOLS:
            raise MCPProtocolError("mcp_tool_catalog_size_invalid")
        next_cursor = result.get("nextCursor")
        if next_cursor is None:
            break
        if (
            not isinstance(next_cursor, str)
            or not next_cursor
            or len(next_cursor.encode("utf-8")) > 256
            or next_cursor in seen
        ):
            raise MCPProtocolError("mcp_tool_catalog_cursor_invalid")
        seen.add(next_cursor)
        cursor = next_cursor
    else:
        raise MCPProtocolError("mcp_tool_catalog_page_limit")

    contracts: list[MCPToolContract] = []
    for raw in raw_tools:
        if not isinstance(raw, Mapping):
            raise MCPProtocolError("mcp_tool_descriptor_invalid")
        name = raw.get("name")
        if not isinstance(name, str):
            raise MCPProtocolError("mcp_tool_descriptor_invalid")
        description = raw.get("description")
        if not isinstance(description, str) or not description.strip():
            description = f"MCP tool {name}"
        input_schema = raw.get("inputSchema")
        output_schema = raw.get("outputSchema", {"type": "object"})
        try:
            contracts.append(
                MCPToolContract(
                    name=name,
                    description=description,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    effects=frozenset(
                        {
                            CapabilityEffect.EXECUTE,
                            CapabilityEffect.NETWORK,
                            CapabilityEffect.READ,
                            CapabilityEffect.WRITE,
                        }
                    ),
                    idempotency=IdempotencyClass.NON_IDEMPOTENT,
                    approval_requirement=ApprovalRequirement.NEVER,
                    required_sandbox=SandboxLevel.DANGER_FULL_ACCESS,
                    exposure=Exposure.DIRECT,
                )
            )
        except (TypeError, ValueError):
            raise MCPProtocolError("mcp_tool_descriptor_invalid") from None
    names = tuple(tool.name for tool in contracts)
    if len(set(names)) != len(names) or len({name.casefold() for name in names}) != len(
        names
    ):
        raise MCPProtocolError("mcp_tool_name_invalid")
    return tuple(sorted(contracts, key=lambda item: item.name))


def _safe_tenant(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}", value):
        raise MCPProtocolError("mcp_tenant_identity_invalid")
    return value


async def _drain_process_stderr(reader: asyncio.StreamReader) -> None:
    # MCP explicitly permits arbitrary diagnostic logging on stderr. Drain it
    # without retaining or surfacing potentially sensitive extension output.
    while await reader.read(64 * 1024):
        pass


def _bounded_json(value: Any, *, label: str) -> bytes:
    try:
        canonical = canonical_json_value(value, label=label)
        payload = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except Exception:
        raise MCPProtocolError("mcp_json_invalid") from None
    if not 1 <= len(payload) <= MAX_MCP_MESSAGE_BYTES:
        raise MCPProtocolError("mcp_message_size_invalid")
    return payload


def _decode_json_object(payload: bytes, *, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise MCPProtocolError("mcp_json_invalid") from None
    if not isinstance(value, Mapping):
        raise MCPProtocolError("mcp_json_root_invalid")
    _bounded_json(value, label=label)
    return dict(value)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _decode_single_sse_json(payload: bytes) -> Mapping[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise MCPProtocolError("mcp_sse_utf8_invalid") from None
    events = [block for block in text.replace("\r\n", "\n").split("\n\n") if block.strip()]
    if len(events) != 1:
        raise MCPProtocolError("mcp_sse_event_count_invalid")
    data: list[str] = []
    for line in events[0].split("\n"):
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line and not line.startswith(("event:", "id:", "retry:")):
            raise MCPProtocolError("mcp_sse_field_invalid")
    if not data:
        raise MCPProtocolError("mcp_sse_data_missing")
    return _decode_json_object("\n".join(data).encode("utf-8"), label="MCP SSE response")


__all__ = [
    "MAX_MCP_MESSAGE_BYTES",
    "MCPClientSupervisor",
    "MCPError",
    "MCPProtocolError",
    "MCPRuntimeBinding",
    "MCPStdioTransport",
    "MCPToolContract",
    "MCPTransportError",
    "MCPTransportSession",
    "ManagedHTTPMCPTransport",
    "LegacySSEMCPTransport",
    "discover_mcp_tools",
]
