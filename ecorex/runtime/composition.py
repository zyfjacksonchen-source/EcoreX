"""Product composition for catalog projections and Turn admission."""

from __future__ import annotations

from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import inspect
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, TypeVar

from ecorex.capabilities import (
    CapabilityIntentError,
    CapabilityRegistry,
    CapabilityService,
    CapabilitySnapshotRepository,
    ExecutionPolicy,
    Exposure,
    ManagedModelCatalog,
    ModelModality,
    ModelModalityMismatch,
    PermissionProfile,
    RuntimeAvailability,
    ToolExecutionScope,
    UnknownCapabilityError,
    UnknownModelError,
    builtin_capability_registry,
    builtin_model_catalog,
    intent_inherits_image_context,
    normalize_intent_clauses,
    normalize_intent_text,
    normalize_reference,
)
from ecorex.connectors import (
    ConnectorCatalogItem,
    ConnectorRegistry,
    ConnectorService,
    builtin_connector_registry,
)
from ecorex.protocol import (
    ConnectorDescriptor,
    CreateTurnRequest,
    ModelCatalog,
    ModelDescriptor,
    PermissionSnapshot,
)

if TYPE_CHECKING:
    from ecorex.artifacts import ArtifactService
    from ecorex.extensions.service import ExtensionService
    from ecorex.extensions.mcp import MCPRuntimeBinding
    from ecorex.extensions.mcp_oauth import MCPOAuthService

from .snapshots import RuntimeSnapshotRepository, TurnSnapshotContext
from .connector_execution import (
    ConnectorAgentRuntime,
    connector_catalog_snapshot_payload,
)
from .tool_executions import (
    DurableDeferredDisclosureAuthority,
    DurableInvocationAdmissionAuthority,
    ToolExecutionRepository,
)


_MAX_EXPLICIT_TOOL_REFERENCES = 64
_TurnAdmissionResult = TypeVar("_TurnAdmissionResult")
_ADMISSION_THREAD_ID: ContextVar[str | None] = ContextVar(
    "ecorex_admission_thread_id", default=None
)
_REFERENCE_NEGATION_PREFIXES = tuple(
    normalize_intent_text(value)
    for value in (
        "不要用",
        "不要使用",
        "别用",
        "不用",
        "禁止使用",
        "不要走",
        "不走",
        "do not use",
        "don't use",
        "dont use",
        "never use",
        "avoid using",
        "without using",
    )
)
_REFERENCE_SELECTION_PREFIXES = tuple(
    normalize_intent_text(value)
    for value in (
        "优先用",
        "优先使用",
        "优先走",
        "改用",
        "使用",
        "调用",
        "通过",
        "走",
        "用",
        "prefer",
        "use",
        "using",
        "call",
        "invoke",
        "run",
        "execute",
        "执行",
        "via",
        "with",
    )
)
_REFERENCE_DISCUSSION_TERMS = tuple(
    normalize_intent_text(value)
    for value in (
        "故障",
        "失败",
        "报错",
        "价格",
        "路由",
        "方案",
        "架构",
        "说明",
        "介绍",
        "功能",
        "性能",
        "延迟",
        "failed",
        "failure",
        "broken",
        "error",
        "pricing",
        "routing",
        "architecture",
        "overview",
        "performance",
        "latency",
    )
)


def _ends_with_phrase(value: str, phrase: str) -> bool:
    return value == phrase or value.endswith(" " + phrase)


def _reference_spans(clause: str, reference: str) -> tuple[tuple[int, int], ...]:
    normalized = normalize_intent_text(reference)
    if not normalized:
        return ()
    return tuple(
        match.span()
        for match in re.finditer(
            rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])",
            clause,
            flags=re.IGNORECASE,
        )
    )


def _reference_is_selected(intent: str, reference: str) -> bool:
    """Resolve a prose reference without turning a negation into authority.

    Exact structured selections still enter the planner through its
    ``explicit_tools`` contract.  This compatibility parser only upgrades a
    positive prose mention; a later explicit clause wins, and diagnostic
    discussion remains ordinary routing evidence.
    """

    state: bool | None = None
    for clause in normalize_intent_clauses(intent):
        for start, end in _reference_spans(clause, reference):
            before = clause[:start].rstrip()
            after = clause[end:].lstrip()
            if any(
                _ends_with_phrase(before, prefix)
                for prefix in _REFERENCE_NEGATION_PREFIXES
            ):
                state = False
                continue
            context = " ".join((before[-48:], after[:48]))
            if any(term and term in context for term in _REFERENCE_DISCUSSION_TERMS):
                state = False
                continue
            if any(
                _ends_with_phrase(before, prefix)
                for prefix in _REFERENCE_SELECTION_PREFIXES
            ):
                state = True
                continue
            # A bare name is a mention, not authority. Exact tool-menu choices
            # use CreateTurnRequest.explicit_tool_ids instead.
            if not before and not after:
                state = True
    return state is True


@dataclass(frozen=True, slots=True)
class PreparedTurn:
    request: CreateTurnRequest
    snapshot_context: TurnSnapshotContext


def project_model_catalog(catalog: ManagedModelCatalog) -> ModelCatalog:
    def project(modality: ModelModality) -> list[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id=model.model_id,
                display_name=model.display_name,
                capabilities=sorted(model.capabilities),
                aliases=list(model.aliases),
                is_default=modality in model.default_for,
                model_policy=(
                    model.model_policy.to_dict()
                    if model.model_policy is not None
                    else None
                ),
            )
            for model in catalog.for_modality(modality)
        ]

    return ModelCatalog(
        snapshot_id=catalog.snapshot_id,
        chat=project(ModelModality.CHAT),
        image=project(ModelModality.IMAGE),
        vision=project(ModelModality.VISION),
        audio=project(ModelModality.AUDIO),
        embedding=project(ModelModality.EMBEDDING),
    )


def project_connector_catalog(
    registry: ConnectorRegistry,
    catalog: tuple[ConnectorCatalogItem, ...] | None = None,
) -> list[ConnectorDescriptor]:
    catalog_by_id = {item.definition.connector_id: item for item in (catalog or ())}

    def health(connector_id: str) -> str:
        item = catalog_by_id.get(connector_id)
        if item is None:
            return "unconfigured"
        values = {instance.health.value for instance in item.instances}
        if "connected" in values:
            return "connected"
        if values & {"degraded", "error"}:
            return "degraded"
        return "disconnected" if item.adapter_available else "unconfigured"

    return [
        ConnectorDescriptor(
            connector_id=definition.connector_id,
            display_name=definition.display_name,
            tier=definition.tier.value,
            health=health(definition.connector_id),
            capabilities=[action.action_id for action in definition.actions],
            contract_version=definition.contract_version,
            description=definition.description,
            auth_kinds=[kind.value for kind in definition.auth_kinds],
            icon_key=definition.icon_key,
            adapter_available=(
                catalog_by_id[definition.connector_id].adapter_available
                if definition.connector_id in catalog_by_id
                else registry.has_adapter(definition.connector_id)
            ),
            unavailable_reason=(
                catalog_by_id[definition.connector_id].unavailable_reason
                if definition.connector_id in catalog_by_id
                else (
                    None
                    if registry.has_adapter(definition.connector_id)
                    else "adapter_not_installed"
                )
            ),
        )
        for definition in registry.definitions()
    ]


class RuntimeComposition:
    """Backend authority for model resolution and immutable Turn context."""

    def __init__(
        self,
        *,
        database_path: str,
        product_version: str,
        permission_snapshot_id: str,
        permission_payload: Mapping[str, Any],
        full_access: bool,
        admin_hard_denies: frozenset[str],
        platform: str,
        architecture: str = "generic",
        installed_packs: frozenset[str],
        connected_connectors: frozenset[str],
        online: bool,
        disabled_tools: Mapping[str, str] | None = None,
        model_catalog: ManagedModelCatalog | None = None,
        model_catalog_provider: Callable[[], ManagedModelCatalog] | None = None,
        capability_registry: CapabilityRegistry | None = None,
        connector_registry: ConnectorRegistry | None = None,
        connector_service: ConnectorService | None = None,
        artifact_service: "ArtifactService | None" = None,
        capability_handlers: Mapping[str, Any] | None = None,
        capability_pack_services: Mapping[str, Any] | None = None,
        permission_provider: Callable[[], PermissionSnapshot] | None = None,
        permission_state_digest_provider: Callable[[], str] | None = None,
        permission_sample_scope_provider: (
            Callable[[], AbstractContextManager[Any]] | None
        ) = None,
        permission_mutation_lock: Any | None = None,
        availability_provider: Callable[[], RuntimeAvailability] | None = None,
        output_policy_provider: Callable[[], str] | None = None,
        extension_service: "ExtensionService | None" = None,
        controlled_skill_runner: Any | None = None,
        extension_governance_enabled: bool | None = None,
        mcp_runtime_bindings: tuple["MCPRuntimeBinding", ...] = (),
        mcp_oauth_service: "MCPOAuthService | None" = None,
        tenant_id: str = "local-user",
        enforce_admin_tool_denies: bool = False,
        persist_startup_snapshots: bool = True,
    ) -> None:
        if not isinstance(persist_startup_snapshots, bool):
            raise TypeError("persist_startup_snapshots must be a boolean")
        self._persist_startup_snapshots = persist_startup_snapshots
        self.model_catalog = model_catalog or builtin_model_catalog()
        self._model_catalog_provider = model_catalog_provider
        self.permission_mutation_lock = permission_mutation_lock or threading.RLock()
        if not all(
            callable(getattr(self.permission_mutation_lock, member, None))
            for member in ("acquire", "release")
        ):
            raise ValueError("permission mutation lock is invalid")
        self.capability_registry = capability_registry or builtin_capability_registry()
        # The catalog present at Core composition owns the reserved exact
        # tool/alias namespace.  Later Skill or MCP metadata may be searchable,
        # but it cannot turn a mention of a Core name into an extra explicit
        # extension promotion.
        self._reserved_tool_references = frozenset(
            reference
            for spec in self.capability_registry.all()
            for reference in spec.references
        )
        self.connector_registry = connector_registry or builtin_connector_registry()
        self.connector_service = connector_service
        self.artifact_service = artifact_service
        if connector_service is not None:
            service_definitions = tuple(
                definition.to_dict()
                for definition in connector_service.registry.definitions()
            )
            runtime_definitions = tuple(
                definition.to_dict()
                for definition in self.connector_registry.definitions()
            )
            if service_definitions != runtime_definitions:
                raise ValueError(
                    "Connector service and Runtime catalog contracts do not match"
                )
        self.snapshot_repository = RuntimeSnapshotRepository(database_path)
        self.tool_execution_repository = ToolExecutionRepository(
            self.snapshot_repository.database
        )
        supplied_extension_service = extension_service is not None
        self._extension_governance_enabled = (
            supplied_extension_service
            if extension_governance_enabled is None
            else extension_governance_enabled
        )
        if self._extension_governance_enabled and not supplied_extension_service:
            raise ValueError(
                "extension governance requires a composed product Extension service"
            )
        if extension_service is None:
            from ecorex.extensions.local_bundle import LocalSkillBundleStore
            from ecorex.extensions.repository import SQLiteExtensionRepository
            from ecorex.extensions.service import ExtensionService

            extension_service = ExtensionService(
                SQLiteExtensionRepository(database_path),
                runtime_api_version="1.0.0",
                platform=platform,
                architecture=architecture,
                local_bundle_store=LocalSkillBundleStore(
                    Path(database_path).resolve().parent / "extension-cas"
                ),
            )
        self.extension_service = extension_service
        from ecorex.extensions.live_authority import bind_live_extension_service

        bind_live_extension_service(self.extension_service)
        if controlled_skill_runner is not None:
            self.extension_service.bind_skill_runner(controlled_skill_runner)
        from ecorex.extensions.execution import SkillRuntime
        from ecorex.extensions.mcp import MCPClientSupervisor

        self.skill_runtime = SkillRuntime(
            self.extension_service,
            snapshot_resolver=self._extension_snapshot_for_scope,
            turn_intent_resolver=self._turn_input_for_scope,
            search_fact_resolver=self._skill_search_fact,
            read_fact_resolver=self._skill_read_fact,
            controlled_runner=controlled_skill_runner,
        )
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("Extension execution tenant identity is required")
        self.permission_account_id = tenant_id
        mcp_tenant_id = (
            "tenant_" + hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        )
        effective_mcp_bindings = (
            mcp_runtime_bindings if self._persist_startup_snapshots else ()
        )
        for binding in effective_mcp_bindings:
            if binding.verified_manifest is not None:
                self.extension_service.register_runtime_bound(
                    binding.verified_manifest,
                    initially_enabled=True,
                )
        self.mcp_supervisor = (
            MCPClientSupervisor(
                self.extension_service,
                effective_mcp_bindings,
                snapshot_resolver=self._extension_snapshot_for_turn,
                tenant_resolver=lambda _context: mcp_tenant_id,
                oauth_service=mcp_oauth_service,
            )
            if effective_mcp_bindings
            else None
        )
        if self.mcp_supervisor is not None:
            for spec in self.mcp_supervisor.tool_specs():
                try:
                    existing = self.capability_registry.get(spec.tool_id)
                except Exception:
                    self.capability_registry.register(spec)
                else:
                    if existing.to_dict() != spec.to_dict():
                        raise ValueError(
                            "MCP tool contract collides with the Core catalog"
                        )
        resolved_handlers = dict(capability_handlers or {})
        for protected_tool_id in (
            "tool_search",
            "tool_describe",
            "connector_search",
            "connector_describe",
            "connector_read",
            "connector_write",
            "artifact_read",
            "input_attachment_read",
            "task_list",
        ):
            if protected_tool_id in resolved_handlers:
                raise ValueError(
                    "a caller cannot replace a Core capability-discovery handler"
                )
        self.connector_agent_runtime = (
            ConnectorAgentRuntime(
                connector_service,
                tool_executions=self.tool_execution_repository,
                snapshot_resolver=self._connector_snapshot_for_scope,
                turn_intent_resolver=self._turn_input_for_scope,
                admin_hard_denies_provider=lambda: (
                    frozenset(self._permission_provider().admin_hard_denies)
                    if self._enforce_admin_tool_denies
                    else frozenset()
                ),
                frozen_admin_hard_denies_resolver=(self._frozen_admin_hard_denies),
            )
            if connector_service is not None
            else None
        )
        if self.connector_agent_runtime is not None:
            resolved_handlers.update(self.connector_agent_runtime.handlers())
        self.artifact_read_runtime = None
        self.input_attachment_read_runtime = None
        self.input_attachment_ocr_runtime = None
        if artifact_service is not None:
            from ecorex.integration.connector_results import ArtifactReadRuntime
            from ecorex.input_attachments import (
                InputAttachmentOCRRuntime,
                InputAttachmentReadRuntime,
                InputAttachmentService,
            )

            self.artifact_read_runtime = ArtifactReadRuntime(
                artifact_service,
                account_id=tenant_id,
            )
            resolved_handlers["artifact_read"] = self.artifact_read_runtime.read
            input_attachments = InputAttachmentService(
                artifact_service, account_id=tenant_id
            )
            self.input_attachment_read_runtime = InputAttachmentReadRuntime(
                input_attachments
            )
            resolved_handlers["input_attachment_read"] = (
                self.input_attachment_read_runtime.read
            )
            if "ocr" in installed_packs:
                if "ocr" in resolved_handlers:
                    raise ValueError("a caller cannot replace the Core OCR handler")
                service_adapters = dict(capability_pack_services or {})
                ocr_provider = service_adapters.get("ocr.extract")
                if ocr_provider is None:
                    # Unit/in-process development compositions may supply only
                    # an installed-pack projection. ProductServerSettings
                    # independently requires an exact verified service set.
                    from ecorex.integration.ocr import OCRServiceAdapter

                    ocr_provider = OCRServiceAdapter()
                self.input_attachment_ocr_runtime = InputAttachmentOCRRuntime(
                    input_attachments,
                    ocr_provider,
                )
                resolved_handlers["ocr"] = self.input_attachment_ocr_runtime.extract
        for tool_id, handler in self.skill_runtime.handlers().items():
            if tool_id in resolved_handlers:
                raise ValueError("an injected handler cannot replace a Core Skill tool")
            resolved_handlers[tool_id] = handler
        resolved_handlers["task_list"] = self._task_list
        if self.mcp_supervisor is not None:
            for tool_id, handler in self.mcp_supervisor.handlers().items():
                if tool_id in resolved_handlers:
                    raise ValueError("an MCP tool cannot shadow an existing handler")
                resolved_handlers[tool_id] = handler
        self.capability_service = CapabilityService(
            self.capability_registry,
            handlers=resolved_handlers,
            snapshot_repository=CapabilitySnapshotRepository(database_path),
        )
        self.deferred_disclosure_authority = DurableDeferredDisclosureAuthority(
            self.tool_execution_repository
        )
        self.invocation_admission_authority = DurableInvocationAdmissionAuthority(
            self.tool_execution_repository
        )
        self.capability_service.bind_disclosure_authority(
            self.deferred_disclosure_authority
        )
        self.capability_service.bind_invocation_admission_authority(
            self.invocation_admission_authority
        )
        self.capability_service.handlers.update(
            {
                "tool_search": self._tool_search,
                "tool_describe": self._tool_describe,
            }
        )
        static_permission = PermissionSnapshot.model_validate(permission_payload)
        if static_permission.snapshot_id != permission_snapshot_id:
            raise ValueError("permission snapshot ID does not match its payload")
        if static_permission.full_access != full_access:
            raise ValueError("permission profile does not match its payload")
        if frozenset(static_permission.admin_hard_denies) != admin_hard_denies:
            raise ValueError(
                "administrator permission policy does not match its payload"
            )
        # Control-plane denial facts remain in the immutable permission
        # projection for audit and reconciliation.  They are not an execution
        # gate for the local product by default: local permissions, local
        # capability discovery and the selected sandbox profile own that
        # decision.  A regulated deployment can explicitly opt in.
        self._admin_hard_denies = admin_hard_denies
        self._enforce_admin_tool_denies = bool(enforce_admin_tool_denies)
        self._permission_provider = permission_provider or (lambda: static_permission)
        self._permission_state_digest_provider = permission_state_digest_provider or (
            lambda: self._read_permission_state_digest(tenant_id)
        )
        self._current_execution_policy_provider = self.current_execution_policy
        self.capability_service.bind_current_policy_provider(
            self._current_execution_policy_provider
        )
        self.capability_service.bind_current_permission_state_digest_provider(
            self._permission_state_digest_provider
        )
        if permission_sample_scope_provider is not None:
            self.capability_service.bind_permission_sample_scope_provider(
                permission_sample_scope_provider
            )
        self.availability = RuntimeAvailability(
            platform=platform,
            installed_packs=installed_packs,
            connected_connectors=connected_connectors,
            disabled_tools=dict(disabled_tools or {}),
            online=online,
        )
        self.availability = self._apply_bound_handler_availability(self.availability)
        self.availability = self._apply_connector_execution_availability(
            self.availability
        )
        self.availability = self._apply_artifact_read_availability(self.availability)
        self.availability = self._apply_input_attachment_read_availability(
            self.availability
        )
        self.availability = self._apply_input_attachment_ocr_availability(
            self.availability
        )
        self._availability_provider = availability_provider or (
            lambda: self.availability
        )
        # Invocation must see the same post-composition availability as Turn
        # planning.  Binding the raw provider here reintroduced stale
        # ``verified_handler_not_installed`` facts for Core handlers and
        # rejected tools after they had been disclosed to the model.
        self.capability_service.bind_current_availability_provider(
            self.current_invocation_availability
        )
        self._output_policy_provider = output_policy_provider
        self.model_snapshot = self._runtime_snapshot(
            "models",
            self.model_catalog.to_dict(),
            snapshot_id=self.model_catalog.snapshot_id,
        )
        self.permission_snapshot, self.permission_policy = self._record_permission(
            static_permission,
            allow_projection=True,
        )
        connector_payload = {
            "contract_version": "1.0",
            "definitions": [
                definition.to_dict()
                for definition in self.connector_registry.definitions()
            ],
        }
        connector_digest = hashlib.sha256(
            json.dumps(
                connector_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._product_version = product_version
        self._connector_catalog_digest = connector_digest
        self.connector_catalog_snapshot = self._record_connector_catalog()
        self.extension_snapshot = self._extension_snapshot()
        self.extension_contribution_snapshot = self._extension_contributions(
            self.extension_snapshot
        )
        self.extension_invocation_fence = (
            self.mcp_supervisor
            if self.mcp_supervisor is not None
            else self.extension_service
        )
        self.availability = self._apply_extension_execution_availability(
            self.availability,
            self.extension_contribution_snapshot,
        )
        self.config_snapshot = self._record_config(
            self.availability,
            self.extension_snapshot.snapshot_id,
            connector_catalog_snapshot_id=self.connector_catalog_snapshot.snapshot_id,
        )

    def prepare_turn(
        self, request: CreateTurnRequest, *, thread_id: str | None = None
    ) -> PreparedTurn:
        if thread_id is None:
            thread_id = _ADMISSION_THREAD_ID.get()
        if not self._persist_startup_snapshots:
            raise RuntimeError(
                "Runtime is in projection-only mode and cannot accept a Turn"
            )
        # Capture the current signed model lease for every new Turn. Bootstrap
        # and Turn admission must never disagree after an administrator changes
        # the managed allowlist while the local Runtime remains online.
        self._refresh_model_catalog()
        # Capture permission first. Product Turn admission holds the shared
        # mutation lock around this method and the accepted write; the Kernel
        # additionally verifies this frozen fact against the ledger in its
        # write transaction for cross-process fencing.
        current_permission = self._permission_provider()
        requested_image_model_id = request.image_model_id
        image_model_selection_source = (
            "request" if requested_image_model_id is not None else "default"
        )
        if requested_image_model_id is None:
            requested_image_model_id = self._prose_model_reference(
                request.input,
                modality=ModelModality.IMAGE,
            )
            if requested_image_model_id is not None:
                image_model_selection_source = "intent_alias"
        agent_model_id, image_model_id = self.resolve_model_selection(
            agent_model_id=request.agent_model_id,
            image_model_id=requested_image_model_id,
        )
        availability = self._availability_provider()
        if not isinstance(availability, RuntimeAvailability):
            raise TypeError("Runtime availability provider returned an invalid value")
        availability = self._apply_bound_handler_availability(availability)
        availability = self._apply_connector_execution_availability(availability)
        availability = self._apply_artifact_read_availability(availability)
        availability = self._apply_input_attachment_read_availability(availability)
        availability = self._apply_input_attachment_ocr_availability(availability)
        selected_modalities = {"chat"}
        selected_model_capabilities = {
            "chat": self.model_catalog.get(agent_model_id).capabilities,
        }
        if image_model_id is not None:
            selected_modalities.add("image")
            selected_model_capabilities["image"] = self.model_catalog.get(
                image_model_id
            ).capabilities
        availability = replace(
            availability,
            selected_model_modalities=frozenset(selected_modalities),
            selected_model_capabilities=selected_model_capabilities,
        )
        extension_snapshot = self.extension_service.snapshot()
        contribution_snapshot = self.skill_runtime.contribution_snapshot(
            extension_snapshot.snapshot_id,
            mcp_contributions=(
                self.mcp_supervisor.contribution_records(extension_snapshot.snapshot_id)
                if self.mcp_supervisor is not None
                else ()
            ),
        )
        availability = self._apply_extension_execution_availability(
            availability,
            contribution_snapshot,
        )
        explicit = self._explicit_tool_references(
            request.input,
            structured=tuple(request.explicit_tool_ids),
            contribution_snapshot=contribution_snapshot,
        )
        raw_input_attachments = request.metadata.get("input_attachments")
        has_bound_input_attachments = (
            isinstance(raw_input_attachments, list)
            and bool(raw_input_attachments)
            and all(
                isinstance(item, Mapping)
                and isinstance(item.get("attachment_id"), str)
                and item["attachment_id"]
                and isinstance(item.get("revision_id"), str)
                and item["revision_id"]
                for item in raw_input_attachments
            )
        )
        has_bound_image_attachments = bool(
            has_bound_input_attachments
            and any(
                str(item.get("mime_type") or "").casefold().startswith("image/")
                or item.get("media_kind") == "image"
                for item in raw_input_attachments
            )
        )
        inherits_image_context = bool(
            intent_inherits_image_context(request.input)
            and (
                has_bound_image_attachments
                or self._thread_has_successful_image_context(thread_id)
            )
        )
        if self._extension_governance_enabled:
            availability = self.extension_service.apply_availability(
                availability, extension_snapshot
            )
        config_snapshot = self._record_config(
            availability,
            extension_snapshot.snapshot_id,
            connector_catalog_snapshot_id=(
                self._record_connector_catalog().snapshot_id
            ),
            agent_model_id=agent_model_id,
            image_model_id=image_model_id,
            agent_model_selection_source=(
                "request" if request.agent_model_id is not None else "default"
            ),
            image_model_selection_source=(
                image_model_selection_source
                if image_model_id is not None
                else "unavailable"
            ),
        )
        permission_snapshot, permission_policy = self.record_permission(
            current_permission
        )
        plan = self.capability_service.create_plan(
            intent=request.input,
            explicit_tools=explicit,
            runtime_direct_tools=tuple(
                dict.fromkeys(
                    (
                        *(
                            ("input_attachment_read",)
                            if has_bound_input_attachments
                            else ()
                        ),
                        *(("vision", "ocr") if has_bound_image_attachments else ()),
                        *(("imagegen",) if inherits_image_context else ()),
                    )
                )
            ),
            availability=availability,
            policy=permission_policy,
        )
        canonical = request.model_copy(
            update={
                "agent_model_id": agent_model_id,
                "image_model_id": image_model_id,
            }
        )
        return PreparedTurn(
            request=canonical,
            snapshot_context=TurnSnapshotContext(
                config_snapshot_id=config_snapshot.snapshot_id,
                capability_snapshot_id=plan.snapshot_id,
                permission_snapshot_id=permission_snapshot.snapshot_id,
                model_catalog_snapshot_id=self.model_snapshot.snapshot_id,
                extension_snapshot_id=extension_snapshot.snapshot_id,
            ),
        )

    def _refresh_model_catalog(self) -> None:
        provider = self._model_catalog_provider
        if provider is None:
            return
        catalog = provider()
        if not isinstance(catalog, ManagedModelCatalog):
            raise UnknownModelError("managed model catalog is unavailable")
        if catalog.snapshot_id == self.model_catalog.snapshot_id:
            return
        snapshot = self._runtime_snapshot(
            "models",
            catalog.to_dict(),
            snapshot_id=catalog.snapshot_id,
        )
        self.model_catalog = catalog
        self.model_snapshot = snapshot

    def admit_turn(
        self,
        request: CreateTurnRequest,
        accept: Callable[[PreparedTurn], _TurnAdmissionResult],
        *,
        thread_id: str | None = None,
    ) -> _TurnAdmissionResult:
        """Linearize permission capture with durable Turn acceptance.

        The callback must perform only synchronous local persistence. Holding
        this process admission across an await or provider call would block
        permission revocation and is rejected explicitly.
        """

        if not callable(accept):
            raise TypeError("Turn acceptance callback must be callable")
        with self.permission_mutation_lock:
            token = _ADMISSION_THREAD_ID.set(thread_id)
            try:
                prepared = self.prepare_turn(request)
                result = accept(prepared)
                if inspect.isawaitable(result):
                    close = getattr(result, "close", None)
                    if callable(close):
                        close()
                    raise TypeError(
                        "Turn acceptance callback must not return an awaitable"
                    )
                return result
            finally:
                _ADMISSION_THREAD_ID.reset(token)

    def _thread_has_successful_image_context(self, thread_id: str | None) -> bool:
        """Trust only a durable image Artifact result from this Thread."""

        if not isinstance(thread_id, str) or not thread_id:
            return False
        with self.snapshot_repository.database.reader() as connection:
            rows = connection.execute(
                "SELECT execution.result_json FROM tool_executions AS execution "
                "JOIN turns AS turn ON turn.turn_id = execution.turn_id "
                "WHERE turn.thread_id = ? AND turn.status IN ('completed', 'partial') "
                "AND execution.tool_id = 'imagegen' "
                "AND execution.status = 'completed' "
                "ORDER BY execution.updated_at DESC LIMIT 8",
                (thread_id,),
            ).fetchall()
        for row in rows:
            result = json.loads(str(row["result_json"] or "null"))
            if isinstance(result, dict) and isinstance(result.get("artifact_id"), str):
                return bool(result["artifact_id"])
        return False

    def record_permission(
        self, permission: PermissionSnapshot
    ) -> tuple[Any, ExecutionPolicy]:
        """Persist one immutable policy fact and return its execution policy."""

        return self._record_permission(permission, allow_projection=False)

    def record_permission_in_transaction(
        self,
        connection: sqlite3.Connection,
        permission: PermissionSnapshot,
    ) -> tuple[Any, ExecutionPolicy]:
        """Persist a permission snapshot without publishing in-memory state."""

        if not self._persist_startup_snapshots:
            raise RuntimeError(
                "projection-only Runtime cannot publish permission authority"
            )
        permission = PermissionSnapshot.model_validate(permission)
        if frozenset(permission.admin_hard_denies) != self._admin_hard_denies:
            raise ValueError(
                "permission provider cannot weaken or replace administrator hard-denies"
            )
        snapshot = self.snapshot_repository.save_in_transaction(
            connection,
            "permission",
            permission.model_dump(mode="json"),
            snapshot_id=permission.snapshot_id,
        )
        return snapshot, self._execution_policy(permission)

    def apply_recorded_permission(
        self,
        snapshot: Any,
        policy: ExecutionPolicy,
    ) -> None:
        """Publish a permission fact only after its transaction committed."""

        if snapshot.snapshot_id != policy.snapshot_id:
            raise ValueError("permission snapshot and execution policy differ")
        self.permission_snapshot = snapshot
        self.permission_policy = policy

    def _record_permission(
        self,
        permission: PermissionSnapshot,
        *,
        allow_projection: bool,
    ) -> tuple[Any, ExecutionPolicy]:
        if not self._persist_startup_snapshots and not allow_projection:
            raise RuntimeError(
                "projection-only Runtime cannot publish permission authority"
            )

        permission = PermissionSnapshot.model_validate(permission)
        if frozenset(permission.admin_hard_denies) != self._admin_hard_denies:
            raise ValueError(
                "permission provider cannot weaken or replace administrator hard-denies"
            )
        snapshot = self._runtime_snapshot(
            "permission",
            permission.model_dump(mode="json"),
            snapshot_id=permission.snapshot_id,
        )
        policy = self._execution_policy(permission)
        self.permission_snapshot = snapshot
        self.permission_policy = policy
        return snapshot, policy

    def current_execution_policy(self) -> ExecutionPolicy:
        """Read the mutable authority for just-in-time invocation fencing."""

        permission = PermissionSnapshot.model_validate(self._permission_provider())
        if frozenset(permission.admin_hard_denies) != self._admin_hard_denies:
            raise ValueError(
                "permission provider cannot weaken or replace administrator hard-denies"
            )
        return self._execution_policy(permission)

    def current_invocation_availability(self) -> RuntimeAvailability:
        """Return current Runtime availability after all trusted bindings.

        Capability-pack discovery is intentionally unaware of handlers that
        product composition installs afterwards.  Planning already normalizes
        those Core/extension handlers; just-in-time governance must consume
        the same normalized view or a disclosed tool can be denied before a
        Tool Item is even created.
        """

        availability = self._availability_provider()
        if not isinstance(availability, RuntimeAvailability):
            raise TypeError("Runtime availability provider returned an invalid value")
        availability = self._apply_connector_execution_availability(availability)
        availability = self._apply_artifact_read_availability(availability)
        availability = self._apply_input_attachment_read_availability(availability)
        availability = self._apply_input_attachment_ocr_availability(availability)
        extension_snapshot = self.extension_service.snapshot()
        contribution_snapshot = self.skill_runtime.contribution_snapshot(
            extension_snapshot.snapshot_id,
            mcp_contributions=(
                self.mcp_supervisor.contribution_records(extension_snapshot.snapshot_id)
                if self.mcp_supervisor is not None
                else ()
            ),
        )
        availability = self._apply_extension_execution_availability(
            availability,
            contribution_snapshot,
        )
        if self._extension_governance_enabled:
            availability = self.extension_service.apply_availability(
                availability,
                extension_snapshot,
            )
        return availability

    def _frozen_admin_hard_denies(self, snapshot_id: str) -> frozenset[str]:
        if not self._enforce_admin_tool_denies:
            return frozenset()
        snapshot = self.snapshot_repository.get(snapshot_id)
        if snapshot.kind != "permission":
            raise ValueError("Connector permission snapshot kind is invalid")
        permission = PermissionSnapshot.model_validate(snapshot.payload)
        if permission.snapshot_id != snapshot_id:
            raise ValueError("Connector permission snapshot identity is invalid")
        return frozenset(
            str(value).casefold() for value in permission.admin_hard_denies
        )

    def _read_permission_state_digest(self, account_id: str) -> str:
        """Read a ledger-backed chain head for non-API composition tests."""

        with self.snapshot_repository.database.reader() as connection:
            row = connection.execute(
                "SELECT state.state_digest FROM runtime_permission_state AS state "
                "JOIN permission_state_ledger AS ledger "
                "ON ledger.account_id = state.account_id "
                "AND ledger.revision = state.revision "
                "AND ledger.profile = state.profile "
                "AND ledger.state_digest = state.state_digest "
                "AND ledger.created_at = state.updated_at "
                "WHERE state.account_id = ?",
                (account_id,),
            ).fetchone()
        if row is None or not isinstance(row["state_digest"], str):
            raise ValueError("permission ledger state is unavailable")
        return str(row["state_digest"])

    def _execution_policy(self, permission: PermissionSnapshot) -> ExecutionPolicy:
        return ExecutionPolicy(
            snapshot_id=permission.snapshot_id,
            profile=(
                PermissionProfile.FULL_ACCESS
                if permission.full_access
                else PermissionProfile.DEFAULT
            ),
            # Keep the signed facts on the policy so durable admission can
            # reconstruct the exact PermissionAuthority snapshot.  Governance
            # separately receives the product enforcement switch below.
            admin_hard_denies=frozenset(permission.admin_hard_denies),
            enforce_admin_hard_denies=self._enforce_admin_tool_denies,
        )

    def _record_connector_catalog(self):
        catalog = (
            self.connector_service.catalog()
            if self.connector_service is not None
            else None
        )
        payload = connector_catalog_snapshot_payload(
            self.connector_registry,
            catalog,
        )
        snapshot = self._runtime_snapshot("connectors", payload)
        self.connector_catalog_snapshot = snapshot
        return snapshot

    def _apply_connector_execution_availability(
        self,
        availability: RuntimeAvailability,
    ) -> RuntimeAvailability:
        disabled = dict(availability.disabled_tools)
        reason = "connector_runtime_not_bound"
        handler_missing_reasons = frozenset({"verified_handler_not_installed", reason})
        tool_ids = (
            "connector_search",
            "connector_describe",
            "connector_read",
            "connector_write",
        )
        if self.connector_service is None:
            for tool_id in tool_ids:
                # Normalize only Core handler-absence facts.  An administrator,
                # network policy or sandbox reason remains authoritative even
                # while the Connector Runtime is also unavailable.
                if (
                    tool_id not in disabled
                    or disabled[tool_id] in handler_missing_reasons
                ):
                    disabled[tool_id] = reason
        else:
            for tool_id in tool_ids:
                # RuntimeComposition has just bound the non-replaceable Core
                # handler, so the low-level builder's missing-handler fact is
                # stale.  Never clear an unrelated policy denial here.
                if disabled.get(tool_id) in handler_missing_reasons:
                    disabled.pop(tool_id, None)
        return replace(availability, disabled_tools=disabled)

    def _apply_bound_handler_availability(
        self,
        availability: RuntimeAvailability,
    ) -> RuntimeAvailability:
        """Clear only stale builder facts for handlers now bound by Runtime."""

        disabled = dict(availability.disabled_tools)
        for tool_id in self.capability_service.handlers:
            if disabled.get(tool_id) == "verified_handler_not_installed":
                disabled.pop(tool_id)
        return replace(availability, disabled_tools=disabled)

    def _apply_artifact_read_availability(
        self,
        availability: RuntimeAvailability,
    ) -> RuntimeAvailability:
        disabled = dict(availability.disabled_tools)
        reason = "artifact_runtime_not_bound"
        handler_missing_reasons = frozenset({"verified_handler_not_installed", reason})
        if self.artifact_service is None:
            if (
                "artifact_read" not in disabled
                or disabled["artifact_read"] in handler_missing_reasons
            ):
                disabled["artifact_read"] = reason
        elif disabled.get("artifact_read") in handler_missing_reasons:
            disabled.pop("artifact_read", None)
        return replace(availability, disabled_tools=disabled)

    def _apply_input_attachment_read_availability(
        self,
        availability: RuntimeAvailability,
    ) -> RuntimeAvailability:
        """Keep handler availability separate from per-Turn attachment scope.

        The reader is a verified Core handler when an Artifact service is bound.
        A later per-Turn check promotes it only for backend-bound uploads, so the
        low-level pack builder's absence fact must not leave it globally
        unavailable after RuntimeComposition has attached the handler.
        """
        disabled = dict(availability.disabled_tools)
        reason = "input_attachment_runtime_not_bound"
        handler_missing_reasons = frozenset({"verified_handler_not_installed", reason})
        if self.input_attachment_read_runtime is None:
            if (
                "input_attachment_read" not in disabled
                or disabled["input_attachment_read"] in handler_missing_reasons
            ):
                disabled["input_attachment_read"] = reason
        elif disabled.get("input_attachment_read") in handler_missing_reasons:
            # Do not override an administrator or policy denial.  The only
            # stale fact we clear is that a low-level capability-pack builder
            # did not know about this trusted Runtime handler yet.
            disabled.pop("input_attachment_read", None)
        return replace(availability, disabled_tools=disabled)

    def _apply_input_attachment_ocr_availability(
        self,
        availability: RuntimeAvailability,
    ) -> RuntimeAvailability:
        disabled = dict(availability.disabled_tools)
        reason = "input_attachment_ocr_runtime_not_bound"
        handler_missing_reasons = frozenset({"verified_handler_not_installed", reason})
        if self.input_attachment_ocr_runtime is None:
            if "ocr" not in disabled or disabled["ocr"] in handler_missing_reasons:
                disabled["ocr"] = reason
        elif disabled.get("ocr") in handler_missing_reasons:
            disabled.pop("ocr", None)
        return replace(availability, disabled_tools=disabled)

    def _record_config(
        self,
        availability: RuntimeAvailability,
        extension_snapshot_id: str,
        *,
        connector_catalog_snapshot_id: str,
        agent_model_id: str | None = None,
        image_model_id: str | None = None,
        agent_model_selection_source: str | None = None,
        image_model_selection_source: str | None = None,
    ):
        payload = {
            "product_version": self._product_version,
            "model_catalog_snapshot_id": self.model_snapshot.snapshot_id,
            "capability_catalog_digest": self.capability_registry.digest,
            "connector_catalog_digest": self._connector_catalog_digest,
            "connector_catalog_snapshot_id": connector_catalog_snapshot_id,
            "extension_snapshot_id": extension_snapshot_id,
            "availability": availability.to_dict(),
            "agent_model_id": agent_model_id,
            "image_model_id": image_model_id,
            "agent_model_selection_source": agent_model_selection_source,
            "image_model_selection_source": image_model_selection_source,
        }
        if self._output_policy_provider is not None:
            output_policy_snapshot_id = self._output_policy_provider()
            if (
                not isinstance(output_policy_snapshot_id, str)
                or not output_policy_snapshot_id
            ):
                raise ValueError(
                    "output policy provider returned an invalid snapshot ID"
                )
            payload["output_policy_snapshot_id"] = output_policy_snapshot_id
        snapshot = self._runtime_snapshot("config", payload)
        self.config_snapshot = snapshot
        self.availability = availability
        return snapshot

    def _runtime_snapshot(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        snapshot_id: str | None = None,
    ):
        operation = (
            self.snapshot_repository.save
            if self._persist_startup_snapshots
            else self.snapshot_repository.project
        )
        return operation(kind, payload, snapshot_id=snapshot_id)

    def _extension_snapshot(self):
        return (
            self.extension_service.snapshot()
            if self._persist_startup_snapshots
            else self.extension_service.project_snapshot()
        )

    def _extension_contributions(self, extension_snapshot):
        mcp_contributions = (
            self.mcp_supervisor.contribution_records(extension_snapshot.snapshot_id)
            if self.mcp_supervisor is not None
            else ()
        )
        if self._persist_startup_snapshots:
            return self.skill_runtime.contribution_snapshot(
                extension_snapshot.snapshot_id,
                mcp_contributions=mcp_contributions,
            )
        return self.skill_runtime.project_contribution_snapshot(
            extension_snapshot,
            mcp_contributions=mcp_contributions,
        )

    def resolve_model_selection(
        self,
        *,
        agent_model_id: str,
        image_model_id: str | None,
    ) -> tuple[str, str | None]:
        """Resolve each selector only inside its backend-owned modality.

        An image alias can therefore never become the Agent inference model.
        A missing image selector uses the signed catalog default when one is
        available, otherwise it remains explicitly unavailable for the Turn.
        """

        agent = self.model_catalog.resolve(
            agent_model_id, modality=ModelModality.CHAT
        ).canonical_model_id
        try:
            image = self.model_catalog.resolve(
                image_model_id, modality=ModelModality.IMAGE
            ).canonical_model_id
        except UnknownModelError:
            if image_model_id is not None and str(image_model_id).strip():
                raise
            image = None
        return agent, image

    def _prose_model_reference(
        self,
        intent: str,
        *,
        modality: ModelModality,
    ) -> str | None:
        """Resolve only a positive prose selector; mentions remain inert."""

        selected: list[str] = []
        for model in self.model_catalog.for_modality(modality):
            if any(
                _reference_is_selected(intent, reference)
                for reference in sorted(model.references, key=len, reverse=True)
            ):
                selected.append(model.model_id)
        canonical = tuple(dict.fromkeys(selected))
        if len(canonical) > 1:
            raise CapabilityIntentError(
                f"multiple {modality.value} models were explicitly selected"
            )
        return canonical[0] if canonical else None

    def _explicit_tool_references(
        self,
        intent: str,
        *,
        structured: tuple[str, ...] = (),
        contribution_snapshot=None,
    ) -> tuple[str, ...]:
        explicit: list[str] = []
        for reference in structured:
            if reference.startswith("skill:"):
                extension_id = reference.removeprefix("skill:")
                if contribution_snapshot is None or not any(
                    skill.extension_id == extension_id
                    for skill in contribution_snapshot.skills
                ):
                    raise CapabilityIntentError(
                        f"explicit Skill selection is unavailable: {extension_id!r}"
                    )
                explicit.append("skill_search")
                continue
            try:
                self.capability_registry.resolve(reference)
            except UnknownCapabilityError:
                raise CapabilityIntentError(
                    f"explicit tool selection is unavailable: {reference!r}"
                ) from None
            explicit.append(reference)
        for spec in self.capability_registry.all():
            for reference in sorted(spec.references, key=len, reverse=True):
                if _reference_is_selected(intent, reference):
                    explicit.append(reference)
                    break
        if contribution_snapshot is not None:
            for skill in contribution_snapshot.skills:
                references = {
                    normalize_reference(skill.name),
                    normalize_reference(skill.extension_id),
                }
                matched_references = {
                    reference
                    for reference in references
                    if reference and _reference_is_selected(intent, reference)
                }
                if not matched_references:
                    continue
                collisions = matched_references & self._reserved_tool_references
                if collisions:
                    # Unknown references are retained in the immutable plan as
                    # a diagnostic, while remaining fail-closed.  A Skill with
                    # a reserved display name can still be selected by its
                    # unique extension_id in a separate, unambiguous request.
                    explicit.extend(
                        f"reserved-skill-reference:{reference}"
                        for reference in sorted(collisions)
                    )
                    continue
                # An explicit Skill mention is ranking evidence consumed by
                # skill_search.  It never promotes the generic skill_read tool
                # or grants access to this (or any other) Skill revision.
        return tuple(dict.fromkeys(explicit))[:_MAX_EXPLICIT_TOOL_REFERENCES]

    def _tool_search(self, arguments, context) -> dict[str, object]:
        query = str(arguments["query"])
        limit = int(arguments.get("limit", 10))
        model_snapshot_id, model_payload = self._model_discovery_snapshot(context)
        results = self.capability_service.tool_search(
            context.capability_snapshot_id,
            query,
            limit=limit,
            exposure=Exposure.DEFERRED,
            model_catalog_payload=model_payload,
        )
        plan = self.capability_service.get_plan(context.capability_snapshot_id)
        return {
            "schema_version": 1,
            "capability_snapshot_id": plan.snapshot_id,
            "capability_catalog_digest": self.capability_registry.digest,
            "routing_policy_digest": plan.routing_policy_digest,
            "discovery_policy_id": plan.discovery_policy_id,
            "discovery_policy_version": plan.discovery_policy_version,
            "discovery_policy_digest": plan.discovery_policy_digest,
            "model_catalog_snapshot_id": model_snapshot_id,
            "query": query,
            "tools": [result.to_dict() for result in results],
        }

    def _model_discovery_snapshot(self, context) -> tuple[str, Mapping[str, Any]]:
        snapshot_id = self.model_snapshot.snapshot_id
        execution_scope = getattr(context, "execution_scope", None)
        job_id = getattr(execution_scope, "job_id", None)
        execution_batch_id = getattr(execution_scope, "execution_batch_id", None)
        if (
            isinstance(job_id, str)
            and job_id
            and isinstance(execution_batch_id, str)
            and execution_batch_id
        ):
            with self.snapshot_repository.database.reader() as connection:
                row = connection.execute(
                    "SELECT batch.model_catalog_snapshot_id "
                    "FROM turn_execution_batches AS batch "
                    "JOIN jobs AS job ON job.job_id = ? "
                    "WHERE batch.batch_id = ? "
                    "AND batch.turn_id = job.turn_id "
                    "AND batch.thread_id = job.thread_id",
                    (job_id, execution_batch_id),
                ).fetchone()
            if row is None:
                raise ValueError("tool discovery execution batch is invalid")
            snapshot_id = str(row["model_catalog_snapshot_id"])
        elif isinstance(job_id, str) and job_id:
            # Standalone/internal callers may not carry a model-facing batch.
            # A model tool call always has one and therefore takes the branch
            # above; this fallback does not participate in durable disclosure.
            with self.snapshot_repository.database.reader() as connection:
                row = connection.execute(
                    "SELECT model_catalog_snapshot_id FROM job_runtime_contexts "
                    "WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            if row is not None:
                snapshot_id = str(row["model_catalog_snapshot_id"])
        snapshot = self.snapshot_repository.get(snapshot_id)
        if snapshot.kind != "models":
            raise ValueError("tool discovery model snapshot is invalid")
        return snapshot.snapshot_id, snapshot.payload

    def _tool_describe(self, arguments, context) -> dict[str, object]:
        discovery_id = str(arguments["discovery_id"])
        scope = getattr(context, "execution_scope", None)
        search_record = self.tool_execution_repository.completed_search_for_discovery(
            execution_scope=scope,
            capability_snapshot_id=context.capability_snapshot_id,
            policy_snapshot_id=context.policy_snapshot_id,
            discovery_id=discovery_id,
        )
        if search_record is None:
            return {
                "schema_version": 1,
                "capability_snapshot_id": context.capability_snapshot_id,
                "found": False,
                "discovery_id": discovery_id,
                "reason": (
                    "invalid_discovery_id"
                    if not discovery_id.startswith("tool:")
                    or "@" not in discovery_id[5:]
                    else "search_result_required"
                ),
            }
        # A persisted row is not sufficient by shape alone.  Recompute the
        # bounded search under this exact frozen batch and require byte-for-byte
        # equivalent JSON data before issuing the describe grant linkage.
        expected_search = self._tool_search(search_record.arguments, context)
        if search_record.result != expected_search:
            return {
                "schema_version": 1,
                "capability_snapshot_id": context.capability_snapshot_id,
                "found": False,
                "discovery_id": discovery_id,
                "reason": "search_result_invalid",
            }
        try:
            description = self.capability_service.tool_describe(
                context.capability_snapshot_id,
                discovery_id,
            )
        except UnknownCapabilityError:
            return {
                "schema_version": 1,
                "capability_snapshot_id": context.capability_snapshot_id,
                "found": False,
                "discovery_id": discovery_id,
                "reason": "unknown_capability",
            }
        decision = description["decision"]
        if not decision["eligible"] or decision["exposure"] == "hidden":
            return {
                "schema_version": 1,
                "capability_snapshot_id": context.capability_snapshot_id,
                "found": True,
                "available": False,
                "tool": {
                    "decision": {
                        "tool_id": decision["tool_id"],
                        "tool_version": decision["tool_version"],
                        "eligible": False,
                        "exposure": "hidden",
                        "reason_codes": decision["reason_codes"],
                        "suppression_reasons": decision["suppression_reasons"],
                    }
                },
            }
        return {
            "schema_version": 1,
            "capability_snapshot_id": context.capability_snapshot_id,
            "found": True,
            "available": True,
            "discovery_id": discovery_id,
            "search_tool_call_id": search_record.tool_call_id,
            "search_result_sha256": search_record.result_sha256,
            "tool": description,
        }

    def _extension_snapshot_for_turn(self, turn_id: str) -> str:
        with self.snapshot_repository.database.reader() as connection:
            row = connection.execute(
                "SELECT context.extension_snapshot_id "
                "FROM jobs JOIN job_runtime_contexts AS context USING(job_id) "
                "WHERE jobs.turn_id = ? AND jobs.kind = 'agent_turn' "
                "ORDER BY jobs.created_at, jobs.job_id LIMIT 1",
                (turn_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Turn has no durable Extension snapshot")
        return str(row["extension_snapshot_id"])

    def workflow_instructions(
        self,
        extension_snapshot_id: str,
        workflow_skill_ids: tuple[str, ...],
    ) -> Mapping[str, Any] | None:
        """Resolve only product-linked guidance from the frozen Skill snapshot."""

        return self.skill_runtime.workflow_instructions(
            extension_snapshot_id,
            workflow_skill_ids,
        )

    def _extension_snapshot_for_scope(self, scope: ToolExecutionScope) -> str:
        if (
            not isinstance(scope, ToolExecutionScope)
            or not isinstance(scope.execution_batch_id, str)
            or not scope.execution_batch_id
        ):
            raise ValueError("Skill execution scope has no durable batch")
        with self.snapshot_repository.database.reader() as connection:
            row = connection.execute(
                "SELECT batch.extension_snapshot_id "
                "FROM turn_execution_batches AS batch "
                "JOIN jobs AS job ON job.job_id = ? "
                "JOIN turns AS turn ON turn.turn_id = batch.turn_id "
                "WHERE batch.batch_id = ? "
                "AND batch.turn_id = ? AND batch.thread_id = ? "
                "AND job.turn_id = batch.turn_id AND job.thread_id = batch.thread_id "
                "AND turn.thread_id = batch.thread_id AND job.kind = 'agent_turn'",
                (
                    scope.job_id,
                    scope.execution_batch_id,
                    scope.turn_id,
                    scope.thread_id,
                ),
            ).fetchone()
        if row is None:
            raise ValueError("Skill execution batch is invalid")
        frozen_snapshot_id = str(row["extension_snapshot_id"])
        frozen = self.extension_service.repository.snapshot_payload(frozen_snapshot_id)
        frozen_generation = frozen.get("extension_generation")
        current_generation = self.extension_service.repository.generation()
        if frozen_generation == current_generation:
            return frozen_snapshot_id
        # Skill state is intentionally live between model tool rounds. The
        # capability and permission snapshots remain batch-frozen; only the
        # content-addressed Extension catalog advances to the current durable
        # generation.
        return self.extension_service.snapshot().snapshot_id

    def _connector_snapshot_for_scope(self, scope: ToolExecutionScope):
        if (
            not isinstance(scope, ToolExecutionScope)
            or not isinstance(scope.execution_batch_id, str)
            or not scope.execution_batch_id
        ):
            raise ValueError("Connector execution scope has no durable batch")
        with self.snapshot_repository.database.reader() as connection:
            row = connection.execute(
                "SELECT config.snapshot_id, config.kind, config.payload_json, "
                "config.payload_sha256 "
                "FROM turn_execution_batches AS batch "
                "JOIN jobs AS job ON job.job_id = ? "
                "JOIN turns AS turn ON turn.turn_id = batch.turn_id "
                "JOIN runtime_snapshots AS config "
                "ON config.snapshot_id = batch.config_snapshot_id "
                "WHERE batch.batch_id = ? "
                "AND batch.turn_id = ? AND batch.thread_id = ? "
                "AND job.turn_id = batch.turn_id AND job.thread_id = batch.thread_id "
                "AND turn.thread_id = batch.thread_id AND job.kind = 'agent_turn'",
                (
                    scope.job_id,
                    scope.execution_batch_id,
                    scope.turn_id,
                    scope.thread_id,
                ),
            ).fetchone()
        if row is None or row["kind"] != "config":
            raise ValueError("Connector execution batch is invalid")
        payload_json = str(row["payload_json"])
        if hashlib.sha256(payload_json.encode("utf-8")).hexdigest() != str(
            row["payload_sha256"]
        ):
            raise ValueError("Connector config snapshot is invalid")
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            raise ValueError("Connector config snapshot is invalid") from None
        snapshot_id = (
            payload.get("connector_catalog_snapshot_id")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("Connector catalog snapshot is not bound to the batch")
        snapshot = self.snapshot_repository.get(snapshot_id)
        if snapshot.kind != "connectors":
            raise ValueError("Connector catalog snapshot kind is invalid")
        return snapshot

    def _turn_input_for_scope(self, scope: ToolExecutionScope) -> str:
        if (
            not isinstance(scope, ToolExecutionScope)
            or not isinstance(scope.execution_batch_id, str)
            or not scope.execution_batch_id
        ):
            raise ValueError("Skill execution scope has no durable batch")
        with self.snapshot_repository.database.reader() as connection:
            rows = connection.execute(
                "SELECT revision.input_text "
                "FROM turn_execution_batches AS batch "
                "JOIN jobs AS job ON job.job_id = ? "
                "JOIN turn_input_revisions AS revision "
                "ON revision.turn_id = batch.turn_id "
                "AND revision.ordinal BETWEEN batch.first_revision_ordinal "
                "AND batch.last_revision_ordinal "
                "WHERE batch.batch_id = ? "
                "AND batch.turn_id = ? AND batch.thread_id = ? "
                "AND job.turn_id = batch.turn_id AND job.thread_id = batch.thread_id "
                "AND job.kind = 'agent_turn' ORDER BY revision.ordinal",
                (
                    scope.job_id,
                    scope.execution_batch_id,
                    scope.turn_id,
                    scope.thread_id,
                ),
            ).fetchall()
        if not rows:
            raise ValueError("Skill execution batch has no frozen input revisions")
        return "\n".join(str(row["input_text"]) for row in rows)

    def _skill_search_fact(
        self,
        context,
        extension_snapshot_id: str,
        extension_contribution_snapshot_id: str,
        discovery_id: str,
    ):
        from ecorex.extensions.execution import SkillSearchFact

        scope = getattr(context, "execution_scope", None)
        record = self.tool_execution_repository.completed_skill_search_for_discovery(
            execution_scope=scope,
            capability_snapshot_id=context.capability_snapshot_id,
            policy_snapshot_id=context.policy_snapshot_id,
            extension_snapshot_id=extension_snapshot_id,
            extension_contribution_snapshot_id=(extension_contribution_snapshot_id),
            discovery_id=discovery_id,
        )
        if record is None or record.result_sha256 is None:
            return None
        return SkillSearchFact(
            tool_call_id=record.tool_call_id,
            arguments=record.arguments,
            result=record.result,
            result_sha256=record.result_sha256,
        )

    @staticmethod
    def _task_list(arguments):
        items = tuple(arguments.get("items", ()))
        identities = [item.get("id") for item in items]
        if len(set(identities)) != len(identities):
            raise ValueError("Task List item ids must be unique")
        if sum(item.get("status") == "in_progress" for item in items) > 1:
            raise ValueError("Task List permits at most one in-progress item")
        return {"schema_version": 1, "items": list(items)}

    def _skill_read_fact(
        self,
        context,
        extension_snapshot_id: str,
        extension_contribution_snapshot_id: str,
        discovery_id: str,
    ):
        from ecorex.extensions.execution import SkillReadFact

        record = self.tool_execution_repository.completed_skill_read_for_discovery(
            execution_scope=getattr(context, "execution_scope", None),
            capability_snapshot_id=context.capability_snapshot_id,
            policy_snapshot_id=context.policy_snapshot_id,
            extension_snapshot_id=extension_snapshot_id,
            extension_contribution_snapshot_id=extension_contribution_snapshot_id,
            discovery_id=discovery_id,
        )
        if record is None or record.result_sha256 is None:
            return None
        return SkillReadFact(
            tool_call_id=record.tool_call_id,
            arguments=record.arguments,
            result=record.result,
            result_sha256=record.result_sha256,
        )

    def _apply_extension_execution_availability(
        self,
        availability: RuntimeAvailability,
        contribution_snapshot,
    ) -> RuntimeAvailability:
        disabled = dict(availability.disabled_tools)
        if self.mcp_supervisor is not None:
            active = {
                tool_id
                for contribution in contribution_snapshot.mcp_contributions
                for tool_id in contribution.tool_ids
            }
            for spec in self.mcp_supervisor.tool_specs():
                if spec.tool_id in active:
                    disabled.pop(spec.tool_id, None)
                else:
                    disabled[spec.tool_id] = "extension_provider_inactive"
        return RuntimeAvailability(
            platform=availability.platform,
            installed_packs=availability.installed_packs,
            connected_connectors=availability.connected_connectors,
            disabled_tools=disabled,
            online=availability.online,
            selected_model_modalities=availability.selected_model_modalities,
            selected_model_capabilities=availability.selected_model_capabilities,
        )
