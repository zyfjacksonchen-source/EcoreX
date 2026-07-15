"""Strict, signed Extension Registry v1 contracts.

The manifest intentionally contains no host path, command, environment, token,
or endpoint. Executable artifacts are resolved only from a signed Release or
Capability-Pack CAS by product-owned adapters. Parsing is canonical and rejects
duplicate keys, non-finite values, unknown fields, and command-shaped metadata.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from ecorex.update import SignatureEnvelope, SignatureVerifier

from .errors import (
    ExtensionCompatibilityError,
    ExtensionManifestError,
    ExtensionVerificationError,
)


EXTENSION_MANIFEST_SCHEMA_VERSION = 1
EXTENSION_CONTRACT_VERSION = "1.0"
MAX_EXTENSION_MANIFEST_BYTES = 256 * 1024
SUPPORTED_MCP_PROTOCOL_VERSIONS = ("2025-11-25",)

_SAFE_ID = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_SIGNATURE_KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_EXPORT_ID = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_COMPATIBILITY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UPSTREAM_ID = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@._-]{0,191}$")
_UPSTREAM_NAMESPACE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@._/-]{0,191}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_RANGE_PART = re.compile(
    r"^(>=|<=|>|<|=)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_MCP_VERSION = re.compile(r"^20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class ExtensionKind(StrEnum):
    SKILL = "skill"
    MCP_SERVER = "mcp_server"
    TOOL_PROVIDER = "tool_provider"
    CONNECTOR_PROVIDER = "connector_provider"
    CAPABILITY_PACK = "capability_pack"


class ExtensionSource(StrEnum):
    CORE_BUNDLE = "core_bundle"
    SIGNED_RELEASE = "signed_release"
    CAPABILITY_PACK = "capability_pack"
    ADMINISTRATOR = "administrator"
    LOCAL_BUNDLE = "local_bundle"
    LEGACY_IMPORT = "legacy_import"


class ExtensionTrust(StrEnum):
    BUILTIN = "builtin"
    ADMINISTRATOR = "administrator"
    VERIFIED_PUBLISHER = "verified_publisher"
    LOCAL_UNTRUSTED = "local_untrusted"


class RuntimeBoundary(StrEnum):
    DECLARATIVE = "declarative"
    PROCESS = "process"
    MANAGED_ADAPTER = "managed_adapter"


class ExtensionTransport(StrEnum):
    NONE = "none"
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class ExtensionExportKind(StrEnum):
    TOOL = "tool"
    SKILL = "skill"
    MCP_SERVER = "mcp_server"
    CONNECTOR = "connector"
    CAPABILITY_PACK = "capability_pack"


class ExtensionExposure(StrEnum):
    DIRECT = "direct"
    DEFERRED = "deferred"
    HIDDEN = "hidden"


class ExtensionStatus(StrEnum):
    STAGED = "staged"
    ENABLED = "enabled"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"


class ExtensionHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    CIRCUIT_OPEN = "circuit_open"


@dataclass(frozen=True, slots=True)
class ExtensionSignature:
    algorithm: str
    key_id: str
    value: str

    def __post_init__(self) -> None:
        if self.algorithm not in {
            "ed25519",
            "core-slot-sha256",
            "local-content-sha256",
            "migration-record-sha256",
        }:
            raise ExtensionManifestError("unsupported extension signature algorithm")
        if not _SIGNATURE_KEY_ID.fullmatch(self.key_id):
            raise ExtensionManifestError("extension signature key_id is unsafe")
        if self.algorithm == "ed25519":
            try:
                decoded = base64.b64decode(self.value, validate=True)
            except (TypeError, ValueError) as error:
                raise ExtensionManifestError("extension signature must be canonical base64") from error
            if len(decoded) != 64:
                raise ExtensionManifestError("extension Ed25519 signature must contain 64 bytes")
        elif not _SHA256.fullmatch(self.value):
            raise ExtensionManifestError("extension provenance digest is invalid")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExtensionSignature":
        _exact_keys(value, {"algorithm", "key_id", "value"}, "signature")
        return cls(
            algorithm=_string(value, "algorithm"),
            key_id=_string(value, "key_id"),
            value=_string(value, "value"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"algorithm": self.algorithm, "key_id": self.key_id, "value": self.value}


@dataclass(frozen=True, slots=True)
class ExtensionCompatibility:
    runtime_api: str
    platforms: tuple[str, ...]
    architectures: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_version_range(self.runtime_api)
        _sorted_unique_safe(self.platforms, label="compatibility platforms")
        _sorted_unique_safe(self.architectures, label="compatibility architectures")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExtensionCompatibility":
        _exact_keys(value, {"runtime_api", "platforms", "architectures"}, "compatibility")
        return cls(
            runtime_api=_string(value, "runtime_api"),
            platforms=_string_tuple(value, "platforms", maximum=32),
            architectures=_string_tuple(value, "architectures", maximum=32),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_api": self.runtime_api,
            "platforms": list(self.platforms),
            "architectures": list(self.architectures),
        }


@dataclass(frozen=True, slots=True)
class ExtensionRequirement:
    extension_id: str
    version_range: str

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.extension_id):
            raise ExtensionManifestError("extension dependency identity is unsafe")
        validate_version_range(self.version_range)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, label: str) -> "ExtensionRequirement":
        _exact_keys(value, {"extension_id", "version_range"}, label)
        return cls(
            extension_id=_string(value, "extension_id"),
            version_range=_string(value, "version_range"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"extension_id": self.extension_id, "version_range": self.version_range}


_PERMISSION_EFFECTS = frozenset(
    {"read", "write", "network", "execute", "ui_automation", "generate_media", "subscribe"}
)


@dataclass(frozen=True, slots=True)
class ExtensionExport:
    export_id: str
    kind: ExtensionExportKind
    exposure: ExtensionExposure
    permission_effects: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _EXPORT_ID.fullmatch(self.export_id) or _CONTROL.search(self.export_id):
            raise ExtensionManifestError("extension export identity is unsafe")
        if tuple(sorted(set(self.permission_effects))) != self.permission_effects:
            raise ExtensionManifestError("extension permission effects must be unique and sorted")
        unknown = set(self.permission_effects) - _PERMISSION_EFFECTS
        if unknown:
            raise ExtensionManifestError("extension export contains an unknown permission effect")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExtensionExport":
        _exact_keys(value, {"export_id", "kind", "exposure", "permission_effects"}, "export")
        try:
            kind = ExtensionExportKind(_string(value, "kind"))
            exposure = ExtensionExposure(_string(value, "exposure"))
        except ValueError as error:
            raise ExtensionManifestError("extension export enum is invalid") from error
        return cls(
            export_id=_string(value, "export_id"),
            kind=kind,
            exposure=exposure,
            permission_effects=_string_tuple(value, "permission_effects", maximum=16),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "export_id": self.export_id,
            "kind": self.kind.value,
            "exposure": self.exposure.value,
            "permission_effects": list(self.permission_effects),
        }


@dataclass(frozen=True, slots=True)
class UpstreamMetadata:
    registry: str
    namespace: str
    name: str
    version: str

    def __post_init__(self) -> None:
        for value in (self.registry, self.name, self.version):
            if not _UPSTREAM_ID.fullmatch(value) or _CONTROL.search(value):
                raise ExtensionManifestError("upstream discovery metadata is unsafe")
        if (
            not _UPSTREAM_NAMESPACE.fullmatch(self.namespace)
            or _CONTROL.search(self.namespace)
            or "//" in self.namespace
            or "\\" in self.namespace
            or any(segment in {"", ".", ".."} for segment in self.namespace.split("/"))
        ):
            raise ExtensionManifestError("upstream registry namespace is unsafe")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "UpstreamMetadata":
        _exact_keys(value, {"registry", "namespace", "name", "version"}, "upstream_metadata")
        return cls(*(_string(value, key) for key in ("registry", "namespace", "name", "version")))

    def to_dict(self) -> dict[str, str]:
        return {
            "registry": self.registry,
            "namespace": self.namespace,
            "name": self.name,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    schema_version: int
    contract_version: str
    extension_id: str
    version: str
    kind: ExtensionKind
    display_name: str
    description: str
    artifact_sha256: str
    source: ExtensionSource
    trust: ExtensionTrust
    runtime_boundary: RuntimeBoundary
    transport: ExtensionTransport
    compatibility: ExtensionCompatibility
    dependencies: tuple[ExtensionRequirement, ...]
    conflicts: tuple[ExtensionRequirement, ...]
    exports: tuple[ExtensionExport, ...]
    supported_protocol_versions: tuple[str, ...]
    upstream_metadata: UpstreamMetadata | None
    signature: ExtensionSignature

    def __post_init__(self) -> None:
        if self.schema_version != EXTENSION_MANIFEST_SCHEMA_VERSION or isinstance(self.schema_version, bool):
            raise ExtensionManifestError("unsupported extension manifest schema")
        if self.contract_version != EXTENSION_CONTRACT_VERSION:
            raise ExtensionManifestError("unsupported extension contract version")
        if not _SAFE_ID.fullmatch(self.extension_id):
            raise ExtensionManifestError("extension_id is unsafe")
        parse_semver(self.version)
        for label, value, limit in (
            ("display_name", self.display_name, 128),
            ("description", self.description, 2048),
        ):
            if not value.strip() or len(value.encode("utf-8")) > limit or _CONTROL.search(value):
                raise ExtensionManifestError(f"extension {label} is invalid")
        if not _SHA256.fullmatch(self.artifact_sha256):
            raise ExtensionManifestError("extension artifact digest is invalid")
        _requirements_are_canonical(self.dependencies, label="dependencies")
        _requirements_are_canonical(self.conflicts, label="conflicts")
        export_keys = [(item.kind.value, item.export_id) for item in self.exports]
        if export_keys != sorted(export_keys) or len(export_keys) != len(set(export_keys)):
            raise ExtensionManifestError("extension exports must be unique and sorted")
        if not self.exports or len(self.exports) > 512:
            raise ExtensionManifestError("extension must declare between one and 512 exports")
        self._validate_boundary()
        self._validate_provenance()
        self._validate_protocols()

    def _validate_boundary(self) -> None:
        if self.kind is ExtensionKind.SKILL:
            if self.runtime_boundary is not RuntimeBoundary.DECLARATIVE:
                raise ExtensionManifestError("skills must use the declarative runtime boundary")
            if self.transport is not ExtensionTransport.NONE:
                raise ExtensionManifestError("declarative skills cannot declare a transport")
        else:
            if self.runtime_boundary not in {RuntimeBoundary.PROCESS, RuntimeBoundary.MANAGED_ADAPTER}:
                raise ExtensionManifestError("executable providers require process or managed_adapter isolation")
            if self.kind is ExtensionKind.MCP_SERVER:
                expected = (
                    ExtensionTransport.STDIO
                    if self.runtime_boundary is RuntimeBoundary.PROCESS
                    else ExtensionTransport.STREAMABLE_HTTP
                )
                if self.transport is not expected:
                    raise ExtensionManifestError("MCP transport does not match its isolation boundary")
            elif self.transport is not ExtensionTransport.NONE:
                raise ExtensionManifestError("only MCP servers may declare an extension transport")
        if self.trust is ExtensionTrust.LOCAL_UNTRUSTED and not (
            self.kind is ExtensionKind.SKILL and self.runtime_boundary is RuntimeBoundary.DECLARATIVE
        ):
            raise ExtensionManifestError("local_untrusted extensions may only be declarative skills")

    def _validate_provenance(self) -> None:
        mapping = {
            ExtensionSource.CORE_BUNDLE: (ExtensionTrust.BUILTIN, "core-slot-sha256"),
            ExtensionSource.LOCAL_BUNDLE: (
                ExtensionTrust.LOCAL_UNTRUSTED,
                "local-content-sha256",
            ),
            ExtensionSource.LEGACY_IMPORT: (ExtensionTrust.LOCAL_UNTRUSTED, "migration-record-sha256"),
            ExtensionSource.ADMINISTRATOR: (ExtensionTrust.ADMINISTRATOR, "ed25519"),
            ExtensionSource.SIGNED_RELEASE: (ExtensionTrust.VERIFIED_PUBLISHER, "ed25519"),
            ExtensionSource.CAPABILITY_PACK: (ExtensionTrust.VERIFIED_PUBLISHER, "ed25519"),
        }
        expected_trust, expected_algorithm = mapping[self.source]
        if self.trust is not expected_trust or self.signature.algorithm != expected_algorithm:
            raise ExtensionManifestError("extension source, trust, and signature evidence disagree")

    def _validate_protocols(self) -> None:
        if self.kind is ExtensionKind.MCP_SERVER:
            if not self.supported_protocol_versions:
                raise ExtensionManifestError("MCP extensions must declare supported protocol versions")
            if tuple(sorted(set(self.supported_protocol_versions))) != self.supported_protocol_versions:
                raise ExtensionManifestError("MCP protocol versions must be unique and sorted")
            for version in self.supported_protocol_versions:
                if not _MCP_VERSION.fullmatch(version) or version not in SUPPORTED_MCP_PROTOCOL_VERSIONS:
                    raise ExtensionManifestError("MCP extension requests an unsupported or draft protocol")
        elif self.supported_protocol_versions:
            raise ExtensionManifestError("only MCP extensions may declare protocol versions")

    @property
    def revision_id(self) -> str:
        return "extrev_" + hashlib.sha256(
            (
                f"{self.contract_version}\0{self.extension_id}\0{self.version}\0"
                f"{self.artifact_sha256}\0{self.unsigned_manifest_sha256}"
            ).encode("utf-8")
        ).hexdigest()

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @property
    def unsigned_manifest_sha256(self) -> str:
        """Stable product identity; detached signature rotation is only evidence."""

        return hashlib.sha256(self.canonical_payload()).hexdigest()

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "extension_id": self.extension_id,
            "version": self.version,
            "kind": self.kind.value,
            "display_name": self.display_name,
            "description": self.description,
            "artifact_sha256": self.artifact_sha256,
            "source": self.source.value,
            "trust": self.trust.value,
            "runtime_boundary": self.runtime_boundary.value,
            "transport": self.transport.value,
            "compatibility": self.compatibility.to_dict(),
            "dependencies": [item.to_dict() for item in self.dependencies],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "exports": [item.to_dict() for item in self.exports],
            "supported_protocol_versions": list(self.supported_protocol_versions),
            "upstream_metadata": self.upstream_metadata.to_dict() if self.upstream_metadata else None,
        }

    def canonical_payload(self) -> bytes:
        return b"ecorex-extension-manifest-v1\0" + _canonical_json(self.unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature.to_dict()}

    def to_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExtensionManifest":
        _exact_keys(
            value,
            {
                "schema_version", "contract_version", "extension_id", "version", "kind",
                "display_name", "description", "artifact_sha256", "source", "trust",
                "runtime_boundary", "transport", "compatibility", "dependencies", "conflicts",
                "exports", "supported_protocol_versions", "upstream_metadata", "signature",
            },
            "extension manifest",
        )
        schema_version = value.get("schema_version")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ExtensionManifestError("extension schema_version must be an integer")
        compatibility = value.get("compatibility")
        signature = value.get("signature")
        upstream = value.get("upstream_metadata")
        if not isinstance(compatibility, Mapping) or not isinstance(signature, Mapping):
            raise ExtensionManifestError("extension compatibility and signature must be objects")
        if upstream is not None and not isinstance(upstream, Mapping):
            raise ExtensionManifestError("extension upstream_metadata must be an object or null")
        try:
            kind = ExtensionKind(_string(value, "kind"))
            source = ExtensionSource(_string(value, "source"))
            trust = ExtensionTrust(_string(value, "trust"))
            boundary = RuntimeBoundary(_string(value, "runtime_boundary"))
            transport = ExtensionTransport(_string(value, "transport"))
        except ValueError as error:
            raise ExtensionManifestError("extension manifest enum is invalid") from error
        return cls(
            schema_version=schema_version,
            contract_version=_string(value, "contract_version"),
            extension_id=_string(value, "extension_id"),
            version=_string(value, "version"),
            kind=kind,
            display_name=_string(value, "display_name"),
            description=_string(value, "description"),
            artifact_sha256=_string(value, "artifact_sha256").lower(),
            source=source,
            trust=trust,
            runtime_boundary=boundary,
            transport=transport,
            compatibility=ExtensionCompatibility.from_dict(compatibility),
            dependencies=_requirements(value, "dependencies"),
            conflicts=_requirements(value, "conflicts"),
            exports=_exports(value),
            supported_protocol_versions=_string_tuple(value, "supported_protocol_versions", maximum=16),
            upstream_metadata=UpstreamMetadata.from_dict(upstream) if upstream is not None else None,
            signature=ExtensionSignature.from_dict(signature),
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "ExtensionManifest":
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_EXTENSION_MANIFEST_BYTES:
            raise ExtensionManifestError("extension manifest size is invalid")
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=_reject_non_finite,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ExtensionManifestError) as error:
            if isinstance(error, ExtensionManifestError):
                raise
            raise ExtensionManifestError("extension manifest must be canonical UTF-8 JSON") from error
        if not isinstance(value, Mapping):
            raise ExtensionManifestError("extension manifest root must be an object")
        manifest = cls.from_dict(value)
        if manifest.to_bytes() != payload:
            raise ExtensionManifestError("extension manifest JSON is not canonical")
        return manifest


_VERIFIED_PROOF = object()


@dataclass(frozen=True, slots=True)
class VerifiedExtensionManifest:
    manifest: ExtensionManifest
    _proof: object

    def __post_init__(self) -> None:
        if self._proof is not _VERIFIED_PROOF:
            raise ExtensionVerificationError("verified extension manifests require product verification")

    @classmethod
    def _verified(cls, manifest: ExtensionManifest) -> "VerifiedExtensionManifest":
        return cls(manifest=manifest, _proof=_VERIFIED_PROOF)


def verify_extension_manifest(
    manifest: ExtensionManifest,
    *,
    verifier: SignatureVerifier,
    runtime_api_version: str,
    platform: str,
    architecture: str,
) -> VerifiedExtensionManifest:
    """Verify a signed external manifest; discovery metadata never grants trust."""

    if manifest.source not in {
        ExtensionSource.SIGNED_RELEASE,
        ExtensionSource.CAPABILITY_PACK,
        ExtensionSource.ADMINISTRATOR,
    }:
        raise ExtensionVerificationError("external install source is not signature-verifiable")
    assert manifest.signature.algorithm == "ed25519"
    envelope = SignatureEnvelope(
        algorithm="ed25519",
        key_id=manifest.signature.key_id,
        value=manifest.signature.value,
    )
    try:
        verdict = verifier.verify(manifest.canonical_payload(), envelope)
    except Exception as error:
        raise ExtensionVerificationError(
            f"extension signature verification failed: {type(error).__name__}"
        ) from None
    if verdict is not True:
        raise ExtensionVerificationError("extension signature was not explicitly accepted")
    assert_compatible(
        manifest,
        runtime_api_version=runtime_api_version,
        platform=platform,
        architecture=architecture,
    )
    return VerifiedExtensionManifest._verified(manifest)


def verify_core_extension(
    manifest: ExtensionManifest,
    *,
    runtime_api_version: str,
    platform: str,
    architecture: str,
) -> VerifiedExtensionManifest:
    """Bind declarations already covered by the Bootstrap-verified Core slot."""

    if manifest.source is not ExtensionSource.CORE_BUNDLE or manifest.trust is not ExtensionTrust.BUILTIN:
        raise ExtensionVerificationError("Core extension provenance is invalid")
    if manifest.signature.value != manifest.artifact_sha256:
        raise ExtensionVerificationError("Core extension is not bound to its slot declaration digest")
    assert_compatible(
        manifest,
        runtime_api_version=runtime_api_version,
        platform=platform,
        architecture=architecture,
    )
    return VerifiedExtensionManifest._verified(manifest)


def verify_legacy_declarative_skill(manifest: ExtensionManifest) -> VerifiedExtensionManifest:
    if not (
        manifest.source is ExtensionSource.LEGACY_IMPORT
        and manifest.trust is ExtensionTrust.LOCAL_UNTRUSTED
        and manifest.kind is ExtensionKind.SKILL
        and manifest.runtime_boundary is RuntimeBoundary.DECLARATIVE
        and manifest.signature.value == manifest.artifact_sha256
    ):
        raise ExtensionVerificationError("legacy extension may only preserve declarative Skill metadata")
    return VerifiedExtensionManifest._verified(manifest)


def verify_local_bundle_skill(
    manifest: ExtensionManifest,
    *,
    artifact_sha256: str,
    runtime_api_version: str,
    platform: str,
    architecture: str,
) -> VerifiedExtensionManifest:
    """Verify integrity-only provenance for one product-normalized local Skill.

    ``local-content-sha256`` is deliberately not publisher trust.  The caller
    must first re-open and verify the content-addressed bundle; this function
    then binds that exact digest to the declarative Extension revision.
    """

    if not (
        manifest.source is ExtensionSource.LOCAL_BUNDLE
        and manifest.trust is ExtensionTrust.LOCAL_UNTRUSTED
        and manifest.kind is ExtensionKind.SKILL
        and manifest.runtime_boundary is RuntimeBoundary.DECLARATIVE
        and manifest.transport is ExtensionTransport.NONE
        and manifest.signature.algorithm == "local-content-sha256"
        and manifest.signature.value == artifact_sha256
        and manifest.artifact_sha256 == artifact_sha256
    ):
        raise ExtensionVerificationError(
            "local bundle provenance must bind one declarative Skill to its CAS digest"
        )
    assert_compatible(
        manifest,
        runtime_api_version=runtime_api_version,
        platform=platform,
        architecture=architecture,
    )
    return VerifiedExtensionManifest._verified(manifest)


def assert_compatible(
    manifest: ExtensionManifest,
    *,
    runtime_api_version: str,
    platform: str,
    architecture: str,
) -> None:
    if not version_satisfies(runtime_api_version, manifest.compatibility.runtime_api):
        raise ExtensionCompatibilityError("extension Runtime API range is not satisfied")
    if manifest.compatibility.platforms and platform not in manifest.compatibility.platforms:
        raise ExtensionCompatibilityError("extension platform is incompatible")
    if manifest.compatibility.architectures and architecture not in manifest.compatibility.architectures:
        raise ExtensionCompatibilityError("extension architecture is incompatible")


def parse_semver(value: str) -> tuple[int, int, int, tuple[tuple[int, object], ...]]:
    match = _SEMVER.fullmatch(str(value))
    if not match:
        raise ExtensionManifestError("extension version is not SemVer")
    pre = match.group(4)
    if pre is None:
        prerelease: tuple[tuple[int, object], ...] = ((2, ""),)
    else:
        parts: list[tuple[int, object]] = []
        for item in pre.split("."):
            if item.isdigit():
                if len(item) > 1 and item.startswith("0"):
                    raise ExtensionManifestError("SemVer prerelease numeric identifiers cannot have leading zeros")
                parts.append((0, int(item)))
            else:
                parts.append((1, item))
        prerelease = tuple(parts)
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease


def validate_version_range(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256 or _CONTROL.search(value):
        raise ExtensionManifestError("extension version range is invalid")
    if value == "*":
        return
    parts = value.split(",")
    if any(not part or part.strip() != part or not _RANGE_PART.fullmatch(part) for part in parts):
        raise ExtensionManifestError("extension version range uses unsupported syntax")


def version_satisfies(version: str, version_range: str) -> bool:
    current = parse_semver(version)
    if version_range == "*":
        return True
    for part in version_range.split(","):
        match = _RANGE_PART.fullmatch(part)
        if match is None:
            raise ExtensionManifestError("extension version range uses unsupported syntax")
        operator = match.group(1) or "="
        target_text = f"{match.group(2)}.{match.group(3)}.{match.group(4)}"
        if match.group(5):
            target_text += "-" + match.group(5)
        target = parse_semver(target_text)
        comparison = (current > target) - (current < target)
        if not {
            "=": comparison == 0,
            ">": comparison > 0,
            ">=": comparison >= 0,
            "<": comparison < 0,
            "<=": comparison <= 0,
        }[operator]:
            return False
    return True


def canonical_digest(value: Mapping[str, Any] | Sequence[Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ExtensionManifestError("extension manifest contains non-canonical JSON") from error


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExtensionManifestError("extension manifest contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ExtensionManifestError(f"extension manifest contains non-finite JSON: {value}")


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ExtensionManifestError(f"{label} contains missing or unknown fields")


def _string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ExtensionManifestError(f"{key} must be a non-empty string")
    return result


def _string_tuple(value: Mapping[str, Any], key: str, *, maximum: int) -> tuple[str, ...]:
    raw = value.get(key)
    if not isinstance(raw, list) or len(raw) > maximum or not all(isinstance(item, str) for item in raw):
        raise ExtensionManifestError(f"{key} must be a bounded string array")
    return tuple(raw)


def _requirements(value: Mapping[str, Any], key: str) -> tuple[ExtensionRequirement, ...]:
    raw = value.get(key)
    if not isinstance(raw, list) or len(raw) > 128 or not all(isinstance(item, Mapping) for item in raw):
        raise ExtensionManifestError(f"{key} must be a bounded object array")
    return tuple(ExtensionRequirement.from_dict(item, label=key[:-1]) for item in raw)


def _exports(value: Mapping[str, Any]) -> tuple[ExtensionExport, ...]:
    raw = value.get("exports")
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ExtensionManifestError("exports must be an object array")
    return tuple(ExtensionExport.from_dict(item) for item in raw)


def _sorted_unique_safe(values: tuple[str, ...], *, label: str) -> None:
    if tuple(sorted(set(values))) != values or any(
        not _COMPATIBILITY_ID.fullmatch(value) for value in values
    ):
        raise ExtensionManifestError(f"{label} must be unique, sorted, and safe")


def _requirements_are_canonical(values: tuple[ExtensionRequirement, ...], *, label: str) -> None:
    keys = [(item.extension_id, item.version_range) for item in values]
    if keys != sorted(keys) or len({item.extension_id for item in values}) != len(values):
        raise ExtensionManifestError(f"extension {label} must be unique and sorted")


__all__ = [
    "EXTENSION_CONTRACT_VERSION",
    "EXTENSION_MANIFEST_SCHEMA_VERSION",
    "MAX_EXTENSION_MANIFEST_BYTES",
    "SUPPORTED_MCP_PROTOCOL_VERSIONS",
    "ExtensionCompatibility",
    "ExtensionExport",
    "ExtensionExportKind",
    "ExtensionExposure",
    "ExtensionHealth",
    "ExtensionKind",
    "ExtensionManifest",
    "ExtensionRequirement",
    "ExtensionSignature",
    "ExtensionSource",
    "ExtensionStatus",
    "ExtensionTransport",
    "ExtensionTrust",
    "RuntimeBoundary",
    "UpstreamMetadata",
    "VerifiedExtensionManifest",
    "assert_compatible",
    "canonical_digest",
    "parse_semver",
    "utc_now_iso",
    "validate_version_range",
    "verify_core_extension",
    "verify_extension_manifest",
    "verify_legacy_declarative_skill",
    "verify_local_bundle_skill",
    "version_satisfies",
]
