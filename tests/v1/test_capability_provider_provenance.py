from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ecorex.capabilities import (
    ApprovalRequirement,
    CapabilityEffect,
    CapabilityRegistry,
    CapabilityService,
    CapabilitySnapshotRepository,
    ExecutionPolicy,
    Exposure,
    RuntimeAvailability,
    ToolProviderKind,
    ToolProviderProvenance,
    ToolProviderTrust,
    ToolSpec,
)
from ecorex.extensions import (
    EXTENSION_CONTRACT_VERSION,
    ExtensionCompatibility,
    ExtensionExport,
    ExtensionExportKind,
    ExtensionExposure,
    ExtensionKind,
    ExtensionManifest,
    ExtensionService,
    ExtensionSignature,
    ExtensionSource,
    ExtensionTransport,
    ExtensionTrust,
    MCPClientSupervisor,
    MCPRuntimeBinding,
    MCPToolContract,
    RuntimeBoundary,
    SQLiteExtensionRepository,
    verify_core_extension,
)


def _provider(provider_id: str, *, evidence: str | None = None) -> ToolProviderProvenance:
    return ToolProviderProvenance(
        kind=ToolProviderKind.MCP,
        provider_id=provider_id,
        revision_id="extrev_" + hashlib.sha256(provider_id.encode()).hexdigest(),
        trust=ToolProviderTrust.VERIFIED_PUBLISHER,
        key_id=f"key-{provider_id}",
        evidence_sha256=evidence
        or hashlib.sha256(f"evidence:{provider_id}".encode()).hexdigest(),
    )


def _spec(
    tool_id: str,
    *,
    provider: ToolProviderProvenance | None = None,
) -> ToolSpec:
    values = {
        "tool_id": tool_id,
        "version": "1.0.0",
        "display_name": tool_id,
        "description": "Search one office record",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "default_exposure": Exposure.DEFERRED,
        "intent_tags": frozenset({"office"}),
    }
    if provider is not None:
        values["provider"] = provider
    return ToolSpec(**values)


def _service(specs: tuple[ToolSpec, ...], repository=None) -> tuple[CapabilityService, str]:
    service = CapabilityService(
        CapabilityRegistry(specs),
        snapshot_repository=repository,
    )
    plan = service.create_plan(
        intent="find office records",
        availability=RuntimeAvailability(platform="windows"),
        policy=ExecutionPolicy(snapshot_id="perm_provider_search"),
    )
    return service, plan.snapshot_id


def test_mcp_namespace_cannot_forge_default_core_or_reviewed_routing() -> None:
    with pytest.raises(ValueError, match="default Core provenance"):
        _spec("mcp.evil:lookup")

    provider = _provider("evil")
    with pytest.raises(ValueError, match="deferred metadata"):
        ToolSpec(
            tool_id="mcp.evil:lookup",
            version="1.0.0",
            display_name="lookup",
            description="Claims reviewed image routing",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            default_exposure=Exposure.DIRECT,
            routing_facets=frozenset({"media.image.create"}),
            provider=provider,
        )
    with pytest.raises(ValueError, match="MCP tool provenance"):
        ToolProviderProvenance(
            kind=ToolProviderKind.MCP,
            provider_id="evil",
            revision_id=provider.revision_id,
            trust=ToolProviderTrust.BUILTIN,
            key_id="self-asserted-core",
            evidence_sha256=provider.evidence_sha256,
            product_reviewed=True,
        )


def test_provider_fair_search_reserves_core_and_prevents_one_mcp_flood() -> None:
    flood = _provider("flood-a")
    peer = _provider("peer-b")
    specs = (
        _spec("core-office-a"),
        _spec("core-office-b"),
        *tuple(_spec(f"mcp.flood-a:tool-{index:03d}", provider=flood) for index in range(40)),
        _spec("mcp.peer-b:only", provider=peer),
    )
    service, snapshot_id = _service(specs)

    results = service.tool_search(snapshot_id, "office", limit=5)
    identities = [item.provider.identity for item in results]
    assert len(results) == 5
    assert identities.count(ToolProviderProvenance.core().identity) == 2
    assert flood.identity in identities
    assert peer.identity in identities
    assert identities.count(flood.identity) < len(results)
    assert all(item.provider.to_dict() == service.tool_describe(
        snapshot_id, item.discovery_id
    )["spec"]["provider"] for item in results)

    exact = service.tool_search(snapshot_id, "mcp.flood-a:tool-039", limit=1)
    assert [item.tool_id for item in exact] == ["mcp.flood-a:tool-039"]
    assert exact[0].match_class == "exact_reference"


def test_provider_search_and_plan_are_digest_stable_after_restart(tmp_path: Path) -> None:
    provider = _provider("stable-provider")
    specs = (_spec("core-office"), _spec("mcp.stable-provider:lookup", provider=provider))
    repository = CapabilitySnapshotRepository(tmp_path / "runtime.sqlite3")
    service, snapshot_id = _service(specs, repository)
    before = [item.to_dict() for item in service.tool_search(snapshot_id, "office", limit=2)]
    before_sha256 = hashlib.sha256(
        json.dumps(before, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    restarted = CapabilityService(
        CapabilityRegistry(specs),
        snapshot_repository=CapabilitySnapshotRepository(tmp_path / "runtime.sqlite3"),
    )
    replayed = restarted.get_plan(snapshot_id)
    after = [item.to_dict() for item in restarted.tool_search(snapshot_id, "office", limit=2)]
    after_sha256 = hashlib.sha256(
        json.dumps(after, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert after == before
    assert after_sha256 == before_sha256
    assert replayed.decision("mcp.stable-provider:lookup").provider == provider
    assert replayed.to_dict()["decisions"][1]["provider"]["revision_id"] == provider.revision_id

    changed = _provider("stable-provider", evidence="f" * 64)
    assert CapabilityRegistry(
        (_spec("mcp.stable-provider:lookup", provider=provider),)
    ).digest != CapabilityRegistry(
        (_spec("mcp.stable-provider:lookup", provider=changed),)
    ).digest


def _verified_mcp_manifest():
    artifact_sha256 = "a" * 64
    manifest = ExtensionManifest(
        schema_version=1,
        contract_version=EXTENSION_CONTRACT_VERSION,
        extension_id="ecorex.mcp.verified",
        version="1.0.0",
        kind=ExtensionKind.MCP_SERVER,
        display_name="Verified MCP",
        description="A verified office provider",
        artifact_sha256=artifact_sha256,
        source=ExtensionSource.CORE_BUNDLE,
        trust=ExtensionTrust.BUILTIN,
        runtime_boundary=RuntimeBoundary.PROCESS,
        transport=ExtensionTransport.STDIO,
        compatibility=ExtensionCompatibility(
            runtime_api="=1.0.0", platforms=(), architectures=()
        ),
        dependencies=(),
        conflicts=(),
        exports=(
            ExtensionExport(
                export_id="ecorex.mcp.verified",
                kind=ExtensionExportKind.MCP_SERVER,
                exposure=ExtensionExposure.DEFERRED,
                permission_effects=("network", "read"),
            ),
        ),
        supported_protocol_versions=("2025-11-25",),
        upstream_metadata=None,
        signature=ExtensionSignature(
            algorithm="core-slot-sha256",
            key_id="core-slot-provider-key",
            value=artifact_sha256,
        ),
    )
    return manifest, verify_core_extension(
        manifest,
        runtime_api_version="1.0.0",
        platform="windows",
        architecture="x64",
    )


def test_mcp_tool_provenance_comes_from_exact_verified_manifest_without_signature(
    tmp_path: Path,
) -> None:
    manifest, verified = _verified_mcp_manifest()
    service = ExtensionService(
        SQLiteExtensionRepository(tmp_path / "runtime.sqlite3"),
        runtime_api_version="1.0.0",
        platform="windows",
        architecture="x64",
    )
    service.register_runtime_bound(verified)
    contract = MCPToolContract(
        name="lookup",
        description="Look up one office record",
        input_schema={"type": "object"},
        effects=frozenset({CapabilityEffect.READ, CapabilityEffect.NETWORK}),
        approval_requirement=ApprovalRequirement.ON_REQUEST,
    )
    binding = MCPRuntimeBinding(
        extension_id=manifest.extension_id,
        revision_id=manifest.revision_id,
        artifact_sha256=manifest.artifact_sha256,
        transport=manifest.transport,
        tools=(contract,),
        verified_manifest=verified,
        session_factory=lambda _tenant: (_ for _ in ()).throw(
            AssertionError("discovery must not start an MCP process")
        ),
    )
    supervisor = MCPClientSupervisor(service, (binding,))
    spec = supervisor.tool_specs()[0]
    contribution = supervisor.contribution_records(service.snapshot().snapshot_id)[0]
    projected = json.dumps(spec.to_dict(), sort_keys=True)

    assert spec.provider.kind is ToolProviderKind.MCP
    assert spec.provider.provider_id == manifest.extension_id
    assert spec.provider.revision_id == manifest.revision_id
    assert spec.provider.trust is ToolProviderTrust.BUILTIN
    assert spec.provider.key_id == manifest.signature.key_id
    assert spec.provider.product_reviewed is False
    assert contribution.provider == spec.provider
    assert manifest.signature.value not in projected
    assert manifest.manifest_sha256 not in projected
    assert len(spec.provider.evidence_sha256) == 64

    with pytest.raises(ValueError, match="verified Extension manifest"):
        MCPRuntimeBinding(
            extension_id=manifest.extension_id,
            revision_id=manifest.revision_id,
            artifact_sha256=manifest.artifact_sha256,
            transport=manifest.transport,
            tools=(contract,),
            verified_manifest=None,  # type: ignore[arg-type]
            session_factory=lambda _tenant: None,
        )
