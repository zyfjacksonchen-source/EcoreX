"""Backend-owned capability contracts for EcoreX v1.

The WebUI never constructs these objects.  They are immutable snapshots built
from the runtime tool catalog, machine availability and the active policy
lease.  Keeping the contracts independent of FastAPI also makes routing and
replay deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
import re
from types import MappingProxyType
import unicodedata
from typing import Any, Mapping

from .schema import validate_schema_contract


_TOOL_ID_RE = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_ROUTING_FACET_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9_-]*){1,7}$")
_MODEL_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_PROVIDER_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_PROVIDER_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

MAX_TOOL_ALIASES = 32
MAX_INTENT_TAGS = 32
MAX_ROUTING_FACETS = 8
MAX_ROUTING_METADATA_TEXT = 128
MAX_WORKFLOW_SKILLS = 8
MAX_RECOVERY_HINTS = 8
_MODEL_MODALITIES = frozenset({"chat", "image", "vision", "audio", "embedding"})


def _freeze_model_capabilities(
    value: Mapping[str, frozenset[str]],
    *,
    label: str,
) -> Mapping[str, frozenset[str]]:
    """Validate and freeze modality-scoped model capabilities.

    Capabilities stay scoped to their selected model modality so a chat
    model's similarly named feature can never satisfy an image tool contract.
    ``normalize_reference`` is shared with the managed model catalog, making
    underscore/hyphen spelling differences canonical without fuzzy matching.
    """

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} are invalid")
    normalized: dict[str, frozenset[str]] = {}
    for raw_modality, raw_capabilities in value.items():
        if not isinstance(raw_modality, str):
            raise ValueError(f"{label} are invalid")
        modality = normalize_reference(raw_modality)
        if modality not in _MODEL_MODALITIES or not isinstance(
            raw_capabilities, frozenset
        ):
            raise ValueError(f"{label} are invalid")
        if modality in normalized or any(
            not isinstance(capability, str) for capability in raw_capabilities
        ):
            raise ValueError(f"{label} are invalid")
        capabilities = frozenset(
            normalize_reference(capability) for capability in raw_capabilities
        )
        try:
            capability_sizes = {
                capability: len(capability.encode("utf-8"))
                for capability in capabilities
            }
        except UnicodeEncodeError:
            raise ValueError(f"{label} are invalid") from None
        if len(capabilities) != len(raw_capabilities) or any(
            not capability
            or capability_sizes[capability] > 128
            or not _MODEL_CAPABILITY_RE.fullmatch(capability)
            for capability in capabilities
        ):
            raise ValueError(f"{label} are invalid")
        normalized[modality] = capabilities
    return MappingProxyType(dict(sorted(normalized.items())))


def _model_capabilities_to_dict(
    value: Mapping[str, frozenset[str]],
) -> dict[str, list[str]]:
    return {
        modality: sorted(capabilities)
        for modality, capabilities in sorted(value.items())
    }


def normalize_reference(value: str) -> str:
    """Normalize a user/model alias without broad fuzzy matching."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    normalized = re.sub(r"[\s_]+", "-", normalized)
    return normalized


def stable_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _plain_json_contract(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_contract(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_contract(item) for item in value]
    return value


def _freeze_json_contract(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json_contract(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json_contract(item) for item in value)
    return value


class CapabilityEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    NETWORK = "network"
    EXECUTE = "execute"
    UI_AUTOMATION = "ui_automation"
    GENERATE_MEDIA = "generate_media"


class IdempotencyClass(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class Exposure(StrEnum):
    DIRECT = "direct"
    DEFERRED = "deferred"
    HIDDEN = "hidden"


class SandboxLevel(StrEnum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class ApprovalRequirement(StrEnum):
    NEVER = "never"
    ON_REQUEST = "on-request"
    ALWAYS = "always"


class PermissionProfile(StrEnum):
    DEFAULT = "default"
    FULL_ACCESS = "full-access"


class ToolProviderKind(StrEnum):
    """Backend-owned origin of a model-visible tool contract."""

    CORE = "core"
    MCP = "mcp"


class ToolProviderTrust(StrEnum):
    """Sanitized trust verdict; never a provider-supplied label."""

    BUILTIN = "builtin"
    ADMINISTRATOR = "administrator"
    VERIFIED_PUBLISHER = "verified_publisher"
    USER_CONFIGURED = "user_configured"


_CORE_PROVIDER_EVIDENCE_SHA256 = hashlib.sha256(
    b"ecorex-core-tool-provider-contract-v1"
).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolProviderProvenance:
    """Non-secret evidence binding one tool to its verified provider.

    MCP descriptions and tags never populate this object.  Product
    composition derives it from the verified Extension revision and projects
    only a key identifier and evidence digest, never detached signature bytes.
    """

    kind: ToolProviderKind
    provider_id: str
    revision_id: str
    trust: ToolProviderTrust
    evidence_sha256: str
    key_id: str | None = None
    product_reviewed: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, ToolProviderKind)
            or not isinstance(self.trust, ToolProviderTrust)
            or not _PROVIDER_ID_RE.fullmatch(self.provider_id)
            or not _PROVIDER_REVISION_RE.fullmatch(self.revision_id)
            or not re.fullmatch(r"[0-9a-f]{64}", self.evidence_sha256)
            or not isinstance(self.product_reviewed, bool)
        ):
            raise ValueError("tool provider provenance is invalid")
        if self.key_id is not None and not _PROVIDER_KEY_ID_RE.fullmatch(self.key_id):
            raise ValueError("tool provider key identity is invalid")
        if self.kind is ToolProviderKind.CORE:
            if (
                self.provider_id != "ecorex.core"
                or self.revision_id != "core-contract-v1"
                or self.trust is not ToolProviderTrust.BUILTIN
                or self.key_id is not None
                or self.evidence_sha256 != _CORE_PROVIDER_EVIDENCE_SHA256
                or not self.product_reviewed
            ):
                raise ValueError("Core tool provenance is invalid")
        elif self.kind is ToolProviderKind.MCP:
            if (
                not self.revision_id.startswith("extrev_")
                or len(self.revision_id) != 71
                or self.key_id is None
                or self.trust
                not in {
                    ToolProviderTrust.BUILTIN,
                    ToolProviderTrust.ADMINISTRATOR,
                    ToolProviderTrust.VERIFIED_PUBLISHER,
                    ToolProviderTrust.USER_CONFIGURED,
                }
                # Even a Core-bundled transport remains an MCP protocol
                # provider. Product-reviewed routing metadata belongs to Core
                # ToolSpecs and is never inferred from an Extension trust
                # label or an MCP descriptor.
                or self.product_reviewed
            ):
                raise ValueError("MCP tool provenance is invalid")

    @classmethod
    def core(cls) -> "ToolProviderProvenance":
        return cls(
            kind=ToolProviderKind.CORE,
            provider_id="ecorex.core",
            revision_id="core-contract-v1",
            trust=ToolProviderTrust.BUILTIN,
            evidence_sha256=_CORE_PROVIDER_EVIDENCE_SHA256,
            product_reviewed=True,
        )

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.kind.value, self.provider_id, self.revision_id)

    @property
    def trust_rank(self) -> int:
        return {
            ToolProviderTrust.BUILTIN: 3,
            ToolProviderTrust.ADMINISTRATOR: 2,
            ToolProviderTrust.VERIFIED_PUBLISHER: 1,
            ToolProviderTrust.USER_CONFIGURED: 0,
        }[self.trust]

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind.value,
            "provider_id": self.provider_id,
            "revision_id": self.revision_id,
            "trust": self.trust.value,
            "evidence_sha256": self.evidence_sha256,
            "product_reviewed": self.product_reviewed,
        }
        if self.key_id is not None:
            result["key_id"] = self.key_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolProviderProvenance":
        required = {
            "kind",
            "provider_id",
            "revision_id",
            "trust",
            "evidence_sha256",
            "product_reviewed",
        }
        if not required <= set(value) or set(value) - (required | {"key_id"}):
            raise ValueError("tool provider provenance fields are invalid")
        key_id = value.get("key_id")
        product_reviewed = value.get("product_reviewed")
        if key_id is not None and not isinstance(key_id, str):
            raise ValueError("tool provider key identity is invalid")
        if not isinstance(product_reviewed, bool):
            raise ValueError("tool provider review verdict is invalid")
        return cls(
            kind=ToolProviderKind(str(value.get("kind"))),
            provider_id=str(value.get("provider_id")),
            revision_id=str(value.get("revision_id")),
            trust=ToolProviderTrust(str(value.get("trust"))),
            key_id=key_id,
            evidence_sha256=str(value.get("evidence_sha256")),
            product_reviewed=product_reviewed,
        )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    tool_id: str
    version: str
    display_name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    aliases: tuple[str, ...] = ()
    effects: frozenset[CapabilityEffect] = frozenset({CapabilityEffect.READ})
    idempotency: IdempotencyClass = IdempotencyClass.READ_ONLY
    concurrency_safe: bool = True
    required_sandbox: SandboxLevel = SandboxLevel.READ_ONLY
    approval_requirement: ApprovalRequirement = ApprovalRequirement.NEVER
    default_exposure: Exposure = Exposure.DEFERRED
    # Stable catalog preference used only as a ranking bias.  It never changes
    # availability, governance, exposure, or invocation authority.
    priority_bias: int = 0
    intent_tags: frozenset[str] = frozenset()
    # Product-reviewed semantic facets are deliberately separate from the
    # free-form search tags exposed by extension protocols.  Intent routing
    # may use these facets, while MCP-provided names, descriptions and tags
    # can only contribute bounded catalog-search evidence.
    routing_facets: frozenset[str] = frozenset()
    workflow_skill_ids: frozenset[str] = frozenset()
    recovery_hints: tuple[str, ...] = ()
    cache_ttl_seconds: int = 0
    required_packs: frozenset[str] = frozenset()
    required_connectors: frozenset[str] = frozenset()
    # Some capabilities need a model selected for a separate modality.  This
    # is a catalog fact rather than a tool-name special case, so a future
    # renderer can replace imagegen without changing the planner.
    required_model_modalities: frozenset[str] = frozenset()
    # Feature requirements are scoped to the model selected for each
    # modality.  Modality alone is intentionally insufficient: an arbitrary
    # image-labelled model must not become an image generation/edit backend.
    required_model_capabilities: Mapping[str, frozenset[str]] = field(
        default_factory=dict
    )
    supported_platforms: frozenset[str] = frozenset()
    # Provider identity is a Runtime fact, never an MCP descriptor field.  A
    # default construction is a reviewed Core contract for backwards-
    # compatible internal ToolSpec call sites; extension adapters must replace
    # it with their verified exact revision provenance.
    provider: ToolProviderProvenance = field(
        default_factory=ToolProviderProvenance.core
    )

    def __post_init__(self) -> None:
        if not _TOOL_ID_RE.fullmatch(self.tool_id):
            raise ValueError(f"invalid tool_id: {self.tool_id!r}")
        if not _SEMVER_RE.fullmatch(self.version):
            raise ValueError(f"invalid tool version: {self.version!r}")
        if not isinstance(self.provider, ToolProviderProvenance):
            raise ValueError("tool provider provenance is required")
        if self.provider.kind is ToolProviderKind.CORE and self.tool_id.startswith(
            "mcp."
        ):
            raise ValueError("MCP namespace cannot use default Core provenance")
        if self.provider.kind is ToolProviderKind.MCP:
            expected_prefix = f"mcp.{self.provider.provider_id}:"
            if (
                not self.tool_id.startswith(expected_prefix)
                or self.routing_facets
                or self.workflow_skill_ids
                or self.recovery_hints
                or self.cache_ttl_seconds != 0
                or self.priority_bias != 0
            ):
                raise ValueError(
                    "MCP tools require exact provider namespace and deferred metadata"
                )
        if not self.display_name.strip() or not self.description.strip():
            raise ValueError("tool display_name and description are required")
        if (
            isinstance(self.priority_bias, bool)
            or not isinstance(self.priority_bias, int)
            or not -100 <= self.priority_bias <= 100
        ):
            raise ValueError("tool priority bias is invalid")
        if not isinstance(self.input_schema, Mapping) or not isinstance(
            self.output_schema, Mapping
        ):
            raise TypeError("tool input_schema and output_schema must be mappings")
        if self.provider.kind is not ToolProviderKind.MCP:
            validate_schema_contract(
                self.input_schema, label=f"{self.tool_id}.input_schema"
            )
            validate_schema_contract(
                self.output_schema, label=f"{self.tool_id}.output_schema"
            )
        # ``frozen=True`` is shallow.  Copy and recursively freeze nested
        # schemas before a registry can cache its digest, otherwise a provider
        # could mutate validation/model-visible contracts without changing the
        # catalog identity recorded on queued Turns.
        object.__setattr__(
            self,
            "input_schema",
            _freeze_json_contract(_plain_json_contract(self.input_schema)),
        )
        object.__setattr__(
            self,
            "output_schema",
            _freeze_json_contract(_plain_json_contract(self.output_schema)),
        )
        normalized_aliases = [normalize_reference(alias) for alias in self.aliases]
        if len(self.aliases) > MAX_TOOL_ALIASES:
            raise ValueError("tool aliases exceed the product metadata limit")
        if any(not alias for alias in normalized_aliases):
            raise ValueError("tool aliases cannot be empty")
        if any(
            len(alias.encode("utf-8")) > MAX_ROUTING_METADATA_TEXT
            for alias in self.aliases
        ):
            raise ValueError("tool alias exceeds the product metadata length limit")
        if len(set(normalized_aliases)) != len(normalized_aliases):
            raise ValueError("tool aliases must be unique after normalization")
        if len(self.intent_tags) > MAX_INTENT_TAGS:
            raise ValueError("tool intent tags exceed the product metadata limit")
        normalized_tags = [normalize_reference(tag) for tag in self.intent_tags]
        if any(not tag for tag in normalized_tags):
            raise ValueError("tool intent tags cannot be empty")
        if any(
            len(tag.encode("utf-8")) > MAX_ROUTING_METADATA_TEXT
            for tag in self.intent_tags
        ):
            raise ValueError(
                "tool intent tag exceeds the product metadata length limit"
            )
        if len(set(normalized_tags)) != len(normalized_tags):
            raise ValueError("tool intent tags must be unique after normalization")
        if len(self.routing_facets) > MAX_ROUTING_FACETS:
            raise ValueError("tool routing facets exceed the product metadata limit")
        if any(
            not isinstance(facet, str)
            or not _ROUTING_FACET_RE.fullmatch(facet)
            or len(facet.encode("utf-8")) > MAX_ROUTING_METADATA_TEXT
            for facet in self.routing_facets
        ):
            raise ValueError("tool routing facet is invalid")
        if (
            not isinstance(self.workflow_skill_ids, frozenset)
            or len(self.workflow_skill_ids) > MAX_WORKFLOW_SKILLS
            or any(
                not isinstance(skill_id, str) or not _TOOL_ID_RE.fullmatch(skill_id)
                for skill_id in self.workflow_skill_ids
            )
        ):
            raise ValueError("tool workflow Skill identity is invalid")
        if (
            not isinstance(self.recovery_hints, tuple)
            or len(self.recovery_hints) > MAX_RECOVERY_HINTS
            or len(set(self.recovery_hints)) != len(self.recovery_hints)
            or any(
                not isinstance(hint, str)
                or not hint.strip()
                or len(hint.encode("utf-8")) > 256
                for hint in self.recovery_hints
            )
        ):
            raise ValueError("tool recovery hints are invalid")
        if (
            isinstance(self.cache_ttl_seconds, bool)
            or not isinstance(self.cache_ttl_seconds, int)
            or not 0 <= self.cache_ttl_seconds <= 86_400
            or self.cache_ttl_seconds > 0
            and self.idempotency is not IdempotencyClass.READ_ONLY
        ):
            raise ValueError("tool cache TTL is invalid")
        if (
            not isinstance(self.required_model_modalities, frozenset)
            or not self.required_model_modalities <= _MODEL_MODALITIES
        ):
            raise ValueError("tool required model modalities are invalid")
        required_model_capabilities = _freeze_model_capabilities(
            self.required_model_capabilities,
            label="tool required model capabilities",
        )
        if not set(required_model_capabilities) <= self.required_model_modalities:
            raise ValueError(
                "tool model capability requirements need the same required modality"
            )
        object.__setattr__(
            self,
            "required_model_capabilities",
            required_model_capabilities,
        )
        if self.idempotency is IdempotencyClass.READ_ONLY and any(
            effect in self.effects
            for effect in (CapabilityEffect.WRITE, CapabilityEffect.EXECUTE)
        ):
            raise ValueError("a write/execute tool cannot claim read_only idempotency")
        if (
            CapabilityEffect.EXECUTE in self.effects
            and self.idempotency is IdempotencyClass.IDEMPOTENT
        ):
            raise ValueError(
                "an opaque execute tool cannot claim idempotent replay semantics"
            )

    @property
    def references(self) -> frozenset[str]:
        return frozenset(
            {
                normalize_reference(self.tool_id),
                *(normalize_reference(a) for a in self.aliases),
            }
        )

    @property
    def requires_idempotency_key(self) -> bool:
        return self.idempotency is not IdempotencyClass.READ_ONLY and bool(
            self.effects & {CapabilityEffect.WRITE, CapabilityEffect.NETWORK}
        )

    def to_dict(self, *, include_schemas: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "tool_id": self.tool_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "aliases": list(self.aliases),
            "effects": sorted(effect.value for effect in self.effects),
            "idempotency": self.idempotency.value,
            "concurrency_safe": self.concurrency_safe,
            "required_sandbox": self.required_sandbox.value,
            "approval_requirement": self.approval_requirement.value,
            "default_exposure": self.default_exposure.value,
            "priority_bias": self.priority_bias,
            "intent_tags": sorted(self.intent_tags),
            "routing_facets": sorted(self.routing_facets),
            "workflow_skill_ids": sorted(self.workflow_skill_ids),
            "recovery_hints": list(self.recovery_hints),
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "required_packs": sorted(self.required_packs),
            "required_connectors": sorted(self.required_connectors),
            "required_model_modalities": sorted(self.required_model_modalities),
            "required_model_capabilities": _model_capabilities_to_dict(
                self.required_model_capabilities
            ),
            "supported_platforms": sorted(self.supported_platforms),
            "provider": self.provider.to_dict(),
        }
        if include_schemas:
            result["input_schema"] = _plain_json_contract(self.input_schema)
            result["output_schema"] = _plain_json_contract(self.output_schema)
        return result


@dataclass(frozen=True, slots=True)
class RuntimeAvailability:
    platform: str
    installed_packs: frozenset[str] = frozenset()
    connected_connectors: frozenset[str] = frozenset()
    disabled_tools: Mapping[str, str] = field(default_factory=dict)
    online: bool = True
    # None means the caller is planning outside a concrete Turn.  Runtime
    # composition always supplies a frozen set for admitted Turns, which lets
    # planning fail closed when (for example) a signed image model is absent.
    selected_model_modalities: frozenset[str] | None = None
    # Runtime composition derives this mapping from canonical managed model
    # selections.  It is never accepted as frontend authority.
    selected_model_capabilities: Mapping[str, frozenset[str]] | None = None

    def __post_init__(self) -> None:
        if self.selected_model_modalities is not None and (
            not isinstance(self.selected_model_modalities, frozenset)
            or not self.selected_model_modalities <= _MODEL_MODALITIES
        ):
            raise ValueError("selected model modalities are invalid")
        if self.selected_model_capabilities is not None:
            selected_model_capabilities = _freeze_model_capabilities(
                self.selected_model_capabilities,
                label="selected model capabilities",
            )
            if (
                self.selected_model_modalities is None
                or not set(selected_model_capabilities)
                <= self.selected_model_modalities
            ):
                raise ValueError(
                    "selected model capabilities need the same selected modality"
                )
            object.__setattr__(
                self,
                "selected_model_capabilities",
                selected_model_capabilities,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": normalize_reference(self.platform),
            "installed_packs": sorted(self.installed_packs),
            "connected_connectors": sorted(self.connected_connectors),
            "disabled_tools": dict(sorted(self.disabled_tools.items())),
            "online": self.online,
            "selected_model_modalities": (
                None
                if self.selected_model_modalities is None
                else sorted(self.selected_model_modalities)
            ),
            "selected_model_capabilities": (
                None
                if self.selected_model_capabilities is None
                else _model_capabilities_to_dict(self.selected_model_capabilities)
            ),
        }


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    snapshot_id: str
    profile: PermissionProfile = PermissionProfile.FULL_ACCESS
    admin_hard_denies: frozenset[str] = frozenset()
    # Keep remote deny facts auditable without turning the Control Plane into
    # a hidden local-execution gate.  The standalone/default capability API
    # retains the historical safe default; product Runtime opts out unless a
    # regulated deployment explicitly enables enforcement.
    enforce_admin_hard_denies: bool = True
    policy_denies: frozenset[str] = frozenset()
    allow_sandbox_escalation: bool = True

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip():
            raise ValueError("policy snapshot_id is required")

    @property
    def sandbox(self) -> SandboxLevel:
        return SandboxLevel.DANGER_FULL_ACCESS

    @property
    def approval_mode(self) -> ApprovalRequirement:
        return ApprovalRequirement.NEVER

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "profile": self.profile.value,
            "sandbox": self.sandbox.value,
            "approval_mode": self.approval_mode.value,
            "admin_hard_denies": sorted(
                normalize_reference(v) for v in self.admin_hard_denies
            ),
            "enforce_admin_hard_denies": self.enforce_admin_hard_denies,
            "policy_denies": sorted(normalize_reference(v) for v in self.policy_denies),
            "allow_sandbox_escalation": self.allow_sandbox_escalation,
        }


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    tool_id: str
    tool_version: str
    exposure: Exposure
    eligible: bool
    requires_approval: bool
    effective_sandbox: SandboxLevel
    score: int
    reason_codes: tuple[str, ...]
    matched_evidence: tuple[str, ...] = ()
    suppression_reasons: tuple[str, ...] = ()
    provider: ToolProviderProvenance = field(
        default_factory=ToolProviderProvenance.core
    )

    def __post_init__(self) -> None:
        if isinstance(self.score, bool) or not isinstance(self.score, int):
            raise ValueError("capability decision score must be an integer")
        if not isinstance(self.provider, ToolProviderProvenance):
            raise ValueError("capability decision provider provenance is invalid")
        for label, values in (
            ("reason codes", self.reason_codes),
            ("matched evidence", self.matched_evidence),
            ("suppression reasons", self.suppression_reasons),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > 128
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value.encode("utf-8")) > 512
                    for value in values
                )
            ):
                raise ValueError(f"capability decision {label} are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "tool_version": self.tool_version,
            "exposure": self.exposure.value,
            "eligible": self.eligible,
            "requires_approval": self.requires_approval,
            "effective_sandbox": self.effective_sandbox.value,
            "score": self.score,
            "reason_codes": list(self.reason_codes),
            "matched_evidence": list(self.matched_evidence),
            "suppression_reasons": list(self.suppression_reasons),
            "provider": self.provider.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CapabilityPlan:
    snapshot_id: str
    policy_snapshot_id: str
    intent: str
    decisions: tuple[CapabilityDecision, ...]
    catalog_digest: str = ""
    unresolved_explicit: tuple[str, ...] = ()
    # Runtime-owned context can promote a narrowly scoped capability without
    # claiming that the user explicitly selected it.  This keeps replay and
    # audit able to distinguish a user action from a capability that Core made
    # available solely because a Turn contains a matching bound resource.
    runtime_direct_tools: tuple[str, ...] = ()
    routing_policy_id: str = "routing.none"
    routing_policy_version: str = "0.0.0"
    routing_policy_digest: str = ""
    discovery_policy_id: str = "discovery.none"
    discovery_policy_version: str = "0.0.0"
    discovery_policy_digest: str = ""
    selected_model_capabilities: Mapping[str, frozenset[str]] | None = None

    def __post_init__(self) -> None:
        if self.catalog_digest and not re.fullmatch(
            r"[0-9a-f]{64}", self.catalog_digest
        ):
            raise ValueError("capability catalog digest is invalid")
        if not _ROUTING_FACET_RE.fullmatch(self.routing_policy_id):
            raise ValueError("capability routing policy ID is invalid")
        if not _SEMVER_RE.fullmatch(self.routing_policy_version):
            raise ValueError("capability routing policy version is invalid")
        if self.routing_policy_digest and not re.fullmatch(
            r"[0-9a-f]{64}", self.routing_policy_digest
        ):
            raise ValueError("capability routing policy digest is invalid")
        if not _ROUTING_FACET_RE.fullmatch(self.discovery_policy_id):
            raise ValueError("capability discovery policy ID is invalid")
        if not _SEMVER_RE.fullmatch(self.discovery_policy_version):
            raise ValueError("capability discovery policy version is invalid")
        if self.discovery_policy_digest and not re.fullmatch(
            r"[0-9a-f]{64}", self.discovery_policy_digest
        ):
            raise ValueError("capability discovery policy digest is invalid")
        if self.selected_model_capabilities is not None:
            object.__setattr__(
                self,
                "selected_model_capabilities",
                _freeze_model_capabilities(
                    self.selected_model_capabilities,
                    label="capability plan selected model capabilities",
                ),
            )

    @property
    def direct(self) -> tuple[CapabilityDecision, ...]:
        return tuple(
            decision
            for decision in self.decisions
            if decision.eligible and decision.exposure is Exposure.DIRECT
        )

    @property
    def deferred(self) -> tuple[CapabilityDecision, ...]:
        return tuple(
            decision
            for decision in self.decisions
            if decision.eligible and decision.exposure is Exposure.DEFERRED
        )

    @property
    def hidden(self) -> tuple[CapabilityDecision, ...]:
        return tuple(
            decision
            for decision in self.decisions
            if not decision.eligible or decision.exposure is Exposure.HIDDEN
        )

    def decision(self, tool_id: str) -> CapabilityDecision | None:
        return next((item for item in self.decisions if item.tool_id == tool_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "policy_snapshot_id": self.policy_snapshot_id,
            "intent": self.intent,
            "catalog_digest": self.catalog_digest,
            "unresolved_explicit": list(self.unresolved_explicit),
            "runtime_direct_tools": list(self.runtime_direct_tools),
            "routing_policy_id": self.routing_policy_id,
            "routing_policy_version": self.routing_policy_version,
            "routing_policy_digest": self.routing_policy_digest,
            "discovery_policy_id": self.discovery_policy_id,
            "discovery_policy_version": self.discovery_policy_version,
            "discovery_policy_digest": self.discovery_policy_digest,
            "selected_model_capabilities": (
                None
                if self.selected_model_capabilities is None
                else _model_capabilities_to_dict(self.selected_model_capabilities)
            ),
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


@dataclass(frozen=True, slots=True)
class ToolSearchResult:
    discovery_id: str
    tool_id: str
    tool_version: str
    display_name: str
    description: str
    exposure: Exposure
    score: int
    requires_approval: bool
    match_class: str
    matched_facets: tuple[str, ...] = ()
    matched_evidence: tuple[str, ...] = ()
    provider: ToolProviderProvenance = field(
        default_factory=ToolProviderProvenance.core
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.discovery_id, str)
            or not self.discovery_id.startswith("tool:")
            or len(self.discovery_id.encode("utf-8")) > 512
            or not isinstance(self.score, int)
            or isinstance(self.score, bool)
            or not isinstance(self.match_class, str)
            or not self.match_class
        ):
            raise ValueError("tool search result identity is invalid")
        if not isinstance(self.provider, ToolProviderProvenance):
            raise ValueError("tool search provider provenance is invalid")
        for label, values, maximum in (
            ("matched facets", self.matched_facets, 16),
            ("matched evidence", self.matched_evidence, 32),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) > maximum
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value.encode("utf-8")) > 256
                    for value in values
                )
            ):
                raise ValueError(f"tool search result {label} are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovery_id": self.discovery_id,
            "tool_id": self.tool_id,
            "tool_version": self.tool_version,
            "display_name": self.display_name,
            "description": self.description,
            "exposure": self.exposure.value,
            "score": self.score,
            "requires_approval": self.requires_approval,
            "match_class": self.match_class,
            "matched_facets": list(self.matched_facets),
            "matched_evidence": list(self.matched_evidence),
            "provider": self.provider.to_dict(),
        }
