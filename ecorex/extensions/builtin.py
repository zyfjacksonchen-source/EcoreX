"""Verified Core declarations for the unified Extension catalog."""

from __future__ import annotations

from collections.abc import Iterable
import re

from ecorex.capabilities import CapabilityRegistry
from ecorex.connectors import ConnectorRegistry

from .models import (
    EXTENSION_CONTRACT_VERSION,
    ExtensionCompatibility,
    ExtensionExport,
    ExtensionExportKind,
    ExtensionExposure,
    ExtensionKind,
    ExtensionManifest,
    ExtensionSignature,
    ExtensionSource,
    ExtensionTransport,
    ExtensionTrust,
    RuntimeBoundary,
    VerifiedExtensionManifest,
    verify_core_extension,
)
from .service import ExtensionService


_SAFE_EXTENSION_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")


def builtin_extension_manifests(
    *,
    product_version: str,
    core_build_digest: str,
    runtime_api_version: str,
    platform: str,
    architecture: str,
    capability_registry: CapabilityRegistry,
    connector_registry: ConnectorRegistry,
    installed_pack_ids: frozenset[str],
) -> tuple[VerifiedExtensionManifest, ...]:
    """Project already-verified Core composition into exact Extension revisions.

    These declarations cannot load code.  Their integrity evidence is the
    Bootstrap-verified Core build digest that already binds tool contracts,
    connector definitions and Capability Pack configuration.
    """

    declarations: list[ExtensionManifest] = []
    base_tools = tuple(
        spec for spec in capability_registry.all() if not spec.required_packs
    )
    if base_tools:
        declarations.append(
            _core_manifest(
                extension_id="ecorex.core.tools",
                product_version=product_version,
                build_digest=core_build_digest,
                kind=ExtensionKind.TOOL_PROVIDER,
                display_name="e-Mate 核心工具",
                description="随已验证 Core 发布的内置办公工具合同。",
                exports=tuple(_tool_export(spec) for spec in base_tools),
                runtime_api_version=runtime_api_version,
            )
        )
    for pack_id in sorted(installed_pack_ids):
        extension_id = f"ecorex.pack.{pack_id}"
        if not _SAFE_EXTENSION_ID.fullmatch(extension_id):
            raise ValueError("Capability Pack ID cannot form an exact v1 Extension identity")
        pack_tools = tuple(
            spec
            for spec in capability_registry.all()
            if pack_id in spec.required_packs
        )
        exports = (
            ExtensionExport(
                export_id=pack_id,
                kind=ExtensionExportKind.CAPABILITY_PACK,
                exposure=ExtensionExposure.DIRECT,
                permission_effects=(),
            ),
            *( _tool_export(spec) for spec in pack_tools ),
        )
        declarations.append(
            _core_manifest(
                extension_id=extension_id,
                product_version=product_version,
                build_digest=core_build_digest,
                kind=ExtensionKind.CAPABILITY_PACK,
                display_name=f"{pack_id} 能力包",
                description="随已验证 Core 槽位加载的签名能力包。",
                exports=tuple(sorted(exports, key=lambda item: (item.kind.value, item.export_id))),
                runtime_api_version=runtime_api_version,
            )
        )
    definitions = connector_registry.definitions()
    if definitions:
        declarations.append(
            _core_manifest(
                extension_id="ecorex.core.connectors",
                product_version=product_version,
                build_digest=core_build_digest,
                kind=ExtensionKind.CONNECTOR_PROVIDER,
                display_name="e-Mate 连接器适配层",
                description="随已验证 Core 发布的连接器定义与受管适配合同。",
                exports=tuple(
                    sorted(
                        (
                            ExtensionExport(
                                export_id=definition.connector_id,
                                kind=ExtensionExportKind.CONNECTOR,
                                exposure=ExtensionExposure.DIRECT,
                                permission_effects=tuple(
                                    sorted(
                                        {
                                            effect.value
                                            for action in definition.actions
                                            for effect in action.effects
                                        }
                                    )
                                ),
                            )
                            for definition in definitions
                        ),
                        key=lambda item: (item.kind.value, item.export_id),
                    )
                ),
                runtime_api_version=runtime_api_version,
            )
        )
    return tuple(
        verify_core_extension(
            manifest,
            runtime_api_version=runtime_api_version,
            platform=platform,
            architecture=architecture,
        )
        for manifest in sorted(declarations, key=lambda item: item.extension_id)
    )


def register_builtin_extensions(
    service: ExtensionService,
    declarations: Iterable[VerifiedExtensionManifest],
) -> tuple[str, ...]:
    registered: list[str] = []
    for declaration in declarations:
        projection = service.register_runtime_bound(declaration, initially_enabled=True)
        registered.append(projection.extension_id)
    return tuple(registered)


def _tool_export(spec) -> ExtensionExport:
    return ExtensionExport(
        export_id=spec.tool_id,
        kind=ExtensionExportKind.TOOL,
        exposure=ExtensionExposure(spec.default_exposure.value),
        permission_effects=tuple(sorted(effect.value for effect in spec.effects)),
    )


def _core_manifest(
    *,
    extension_id: str,
    product_version: str,
    build_digest: str,
    kind: ExtensionKind,
    display_name: str,
    description: str,
    exports: tuple[ExtensionExport, ...],
    runtime_api_version: str,
) -> ExtensionManifest:
    return ExtensionManifest(
        schema_version=1,
        contract_version=EXTENSION_CONTRACT_VERSION,
        extension_id=extension_id,
        version=product_version,
        kind=kind,
        display_name=display_name,
        description=description,
        artifact_sha256=build_digest,
        source=ExtensionSource.CORE_BUNDLE,
        trust=ExtensionTrust.BUILTIN,
        runtime_boundary=RuntimeBoundary.MANAGED_ADAPTER,
        transport=ExtensionTransport.NONE,
        compatibility=ExtensionCompatibility(
            runtime_api=f"={runtime_api_version}",
            platforms=(),
            architectures=(),
        ),
        dependencies=(),
        conflicts=(),
        exports=tuple(sorted(exports, key=lambda item: (item.kind.value, item.export_id))),
        supported_protocol_versions=(),
        upstream_metadata=None,
        signature=ExtensionSignature(
            algorithm="core-slot-sha256",
            key_id="core-slot-v1",
            value=build_digest,
        ),
    )


__all__ = ["builtin_extension_manifests", "register_builtin_extensions"]
