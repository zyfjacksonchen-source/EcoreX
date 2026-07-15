"""Signed capability-pack contracts and verified runtime binding.

Capability packs contain optional platform dependencies (Chromium, OCR,
Office format engines, managed media adapters).  They never get to redefine a tool
contract or dynamically import arbitrary Python into the local runtime.  The
signed descriptor is verified first, then a trusted product adapter is bound
to the exact backend-owned ``ToolSpec`` digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ecorex.update.manifest import SignatureEnvelope
from ecorex.update.verification import SignatureVerifier

from .models import ToolSpec, stable_digest
from .pack_services import PackServiceSpec, builtin_pack_service_specs
from .registry import CapabilityRegistry
from .service import ToolHandler


PACK_SCHEMA_VERSION = 2
MAX_PACK_MANIFEST_BYTES = 256 * 1024
MAX_PACK_ARTIFACT_BYTES = 500 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CapabilityPackError(RuntimeError):
    code = "capability_pack_error"


class CapabilityPackManifestError(CapabilityPackError):
    code = "capability_pack_manifest_invalid"


class CapabilityPackVerificationError(CapabilityPackError):
    code = "capability_pack_verification_failed"


class CapabilityPackBindingError(CapabilityPackError):
    code = "capability_pack_binding_failed"


def tool_spec_digest(spec: ToolSpec) -> str:
    return stable_digest(spec.to_dict(include_schemas=True))


@dataclass(frozen=True, slots=True)
class PackToolBinding:
    tool_id: str
    tool_version: str
    spec_sha256: str

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.tool_id):
            raise CapabilityPackManifestError("pack tool_id is unsafe")
        if not _SEMVER.fullmatch(self.tool_version):
            raise CapabilityPackManifestError("pack tool_version is not SemVer")
        if not _SHA256.fullmatch(self.spec_sha256):
            raise CapabilityPackManifestError("pack tool spec digest is invalid")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PackToolBinding":
        _exact_keys(value, {"tool_id", "tool_version", "spec_sha256"}, "pack tool")
        return cls(
            tool_id=_string(value, "tool_id"),
            tool_version=_string(value, "tool_version"),
            spec_sha256=_string(value, "spec_sha256").lower(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "tool_id": self.tool_id,
            "tool_version": self.tool_version,
            "spec_sha256": self.spec_sha256,
        }


@dataclass(frozen=True, slots=True)
class PackServiceBinding:
    service_id: str
    service_version: str
    contract_sha256: str

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.service_id):
            raise CapabilityPackManifestError("pack service_id is unsafe")
        if not _SEMVER.fullmatch(self.service_version):
            raise CapabilityPackManifestError("pack service_version is not SemVer")
        if not _SHA256.fullmatch(self.contract_sha256):
            raise CapabilityPackManifestError("pack service contract digest is invalid")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PackServiceBinding":
        _exact_keys(
            value,
            {"service_id", "service_version", "contract_sha256"},
            "pack service",
        )
        return cls(
            service_id=_string(value, "service_id"),
            service_version=_string(value, "service_version"),
            contract_sha256=_string(value, "contract_sha256").lower(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "service_id": self.service_id,
            "service_version": self.service_version,
            "contract_sha256": self.contract_sha256,
        }


@dataclass(frozen=True, slots=True)
class CapabilityPackManifest:
    schema_version: int
    pack_id: str
    version: str
    runtime_api_version: str
    platform: str
    architecture: str
    artifact_file_name: str
    artifact_size_bytes: int
    artifact_sha256: str
    tools: tuple[PackToolBinding, ...]
    services: tuple[PackServiceBinding, ...]
    signature: SignatureEnvelope

    def __post_init__(self) -> None:
        if self.schema_version != PACK_SCHEMA_VERSION or isinstance(
            self.schema_version, bool
        ):
            raise CapabilityPackManifestError("unsupported capability-pack schema")
        for label, value in (
            ("pack_id", self.pack_id),
            ("platform", self.platform),
            ("architecture", self.architecture),
        ):
            if not _SAFE_ID.fullmatch(value):
                raise CapabilityPackManifestError(f"{label} is unsafe")
        if not _SEMVER.fullmatch(self.version):
            raise CapabilityPackManifestError("pack version is not SemVer")
        if not _SEMVER.fullmatch(self.runtime_api_version):
            raise CapabilityPackManifestError("runtime API version is not SemVer")
        path = PurePosixPath(self.artifact_file_name.replace("\\", "/"))
        if (
            not self.artifact_file_name
            or path.is_absolute()
            or len(path.parts) != 1
            or path.name in {".", ".."}
            or ":" in self.artifact_file_name
            or len(self.artifact_file_name.encode("utf-8")) > 180
        ):
            raise CapabilityPackManifestError("pack artifact file name is unsafe")
        if (
            isinstance(self.artifact_size_bytes, bool)
            or not isinstance(self.artifact_size_bytes, int)
            or not 1 <= self.artifact_size_bytes <= MAX_PACK_ARTIFACT_BYTES
        ):
            raise CapabilityPackManifestError("pack artifact size is invalid")
        if not _SHA256.fullmatch(self.artifact_sha256):
            raise CapabilityPackManifestError("pack artifact digest is invalid")
        if not self.tools and not self.services:
            raise CapabilityPackManifestError(
                "pack must bind at least one tool or service"
            )
        if len(self.tools) > 128 or len(self.services) > 128:
            raise CapabilityPackManifestError("pack binding count exceeds policy")
        tool_ids = [binding.tool_id for binding in self.tools]
        if tool_ids != sorted(tool_ids) or len(tool_ids) != len(set(tool_ids)):
            raise CapabilityPackManifestError(
                "pack tool bindings must be unique and sorted by tool_id"
            )
        service_ids = [binding.service_id for binding in self.services]
        if service_ids != sorted(service_ids) or len(service_ids) != len(
            set(service_ids)
        ):
            raise CapabilityPackManifestError(
                "pack service bindings must be unique and sorted by service_id"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityPackManifest":
        _exact_keys(
            value,
            {
                "schema_version",
                "pack_id",
                "version",
                "runtime_api_version",
                "platform",
                "architecture",
                "artifact_file_name",
                "artifact_size_bytes",
                "artifact_sha256",
                "tools",
                "services",
                "signature",
            },
            "capability-pack manifest",
        )
        raw_tools = value.get("tools")
        raw_services = value.get("services")
        raw_signature = value.get("signature")
        if not isinstance(raw_tools, list) or not all(
            isinstance(item, Mapping) for item in raw_tools
        ):
            raise CapabilityPackManifestError("pack tools must be an array of objects")
        if not isinstance(raw_services, list) or not all(
            isinstance(item, Mapping) for item in raw_services
        ):
            raise CapabilityPackManifestError(
                "pack services must be an array of objects"
            )
        if not isinstance(raw_signature, Mapping):
            raise CapabilityPackManifestError("pack signature must be an object")
        schema_version = value.get("schema_version")
        size_bytes = value.get("artifact_size_bytes")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise CapabilityPackManifestError("pack schema_version must be an integer")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
            raise CapabilityPackManifestError("pack artifact size must be an integer")
        try:
            signature = SignatureEnvelope.from_dict(raw_signature)
        except (TypeError, ValueError) as exc:
            raise CapabilityPackManifestError("pack signature is invalid") from exc
        return cls(
            schema_version=schema_version,
            pack_id=_string(value, "pack_id"),
            version=_string(value, "version"),
            runtime_api_version=_string(value, "runtime_api_version"),
            platform=_string(value, "platform"),
            architecture=_string(value, "architecture"),
            artifact_file_name=_string(value, "artifact_file_name"),
            artifact_size_bytes=size_bytes,
            artifact_sha256=_string(value, "artifact_sha256").lower(),
            tools=tuple(PackToolBinding.from_dict(item) for item in raw_tools),
            services=tuple(
                PackServiceBinding.from_dict(item) for item in raw_services
            ),
            signature=signature,
        )

    @classmethod
    def from_bytes(cls, payload: bytes) -> "CapabilityPackManifest":
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_PACK_MANIFEST_BYTES:
            raise CapabilityPackManifestError("pack manifest size is invalid")
        try:
            decoded = payload.decode("utf-8")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapabilityPackManifestError("pack manifest must be canonical UTF-8 JSON") from exc
        if not isinstance(value, Mapping):
            raise CapabilityPackManifestError("pack manifest root must be an object")
        manifest = cls.from_dict(value)
        if manifest.to_bytes() != payload:
            raise CapabilityPackManifestError("pack manifest JSON is not canonical")
        return manifest

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "version": self.version,
            "runtime_api_version": self.runtime_api_version,
            "platform": self.platform,
            "architecture": self.architecture,
            "artifact_file_name": self.artifact_file_name,
            "artifact_size_bytes": self.artifact_size_bytes,
            "artifact_sha256": self.artifact_sha256,
            "tools": [binding.to_dict() for binding in self.tools],
            "services": [binding.to_dict() for binding in self.services],
        }

    def canonical_payload(self) -> bytes:
        encoded = json.dumps(
            self.unsigned_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return b"ecorex-capability-pack-v1\0" + encoded

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "signature": self.signature.to_dict()}

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


_VERIFIED_PACK_PROOF = object()


@dataclass(frozen=True, slots=True)
class VerifiedCapabilityPack:
    manifest: CapabilityPackManifest
    artifact_path: Path
    _proof: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _VERIFIED_PACK_PROOF:
            raise CapabilityPackVerificationError(
                "verified capability packs can only be created by the verifier"
            )

    @classmethod
    def _verified(
        cls,
        manifest: CapabilityPackManifest,
        artifact_path: Path,
    ) -> "VerifiedCapabilityPack":
        return cls(manifest, artifact_path, _VERIFIED_PACK_PROOF)


def verify_capability_pack(
    manifest: CapabilityPackManifest,
    artifact_path: Path | str,
    *,
    verifier: SignatureVerifier,
    platform: str,
    architecture: str,
    runtime_api_version: str,
) -> VerifiedCapabilityPack:
    """Verify identity, signature and content without following filesystem links."""

    if manifest.platform != platform or manifest.architecture != architecture:
        raise CapabilityPackVerificationError("pack targets a different platform")
    if manifest.runtime_api_version != runtime_api_version:
        raise CapabilityPackVerificationError("pack runtime API version is incompatible")
    try:
        verdict = verifier.verify(manifest.canonical_payload(), manifest.signature)
    except Exception as exc:
        raise CapabilityPackVerificationError(
            f"pack signature verification failed: {type(exc).__name__}"
        ) from None
    if verdict is not True:
        raise CapabilityPackVerificationError("pack verifier did not explicitly accept signature")

    path = Path(artifact_path)
    if path.name != manifest.artifact_file_name:
        raise CapabilityPackVerificationError("pack artifact name does not match manifest")
    try:
        before = path.lstat()
    except OSError as exc:
        raise CapabilityPackVerificationError("pack artifact cannot be inspected") from exc
    attributes = getattr(before, "st_file_attributes", 0)
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        not stat_module.S_ISREG(before.st_mode)
        or stat_module.S_ISLNK(before.st_mode)
        or bool(attributes & reparse_flag)
        or before.st_size != manifest.artifact_size_bytes
    ):
        raise CapabilityPackVerificationError("pack artifact is not a trusted regular file")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise CapabilityPackVerificationError("pack artifact changed while opening")
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except CapabilityPackVerificationError:
        raise
    except OSError as exc:
        raise CapabilityPackVerificationError("pack artifact cannot be read") from exc
    if (
        (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise CapabilityPackVerificationError("pack artifact changed during verification")
    if digest.hexdigest() != manifest.artifact_sha256:
        raise CapabilityPackVerificationError("pack artifact digest does not match manifest")
    return VerifiedCapabilityPack._verified(
        manifest=manifest,
        artifact_path=path.resolve(strict=True),
    )


class CapabilityPackRuntime:
    """Binds verified dependency packs to trusted in-process adapters."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry
        self._packs: dict[str, VerifiedCapabilityPack] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._service_specs: Mapping[str, PackServiceSpec] = (
            builtin_pack_service_specs()
        )
        self._services: dict[str, PackServiceBinding] = {}

    def bind(
        self,
        pack: VerifiedCapabilityPack,
        handlers: Mapping[str, ToolHandler],
    ) -> None:
        manifest = pack.manifest
        if manifest.pack_id in self._packs:
            raise CapabilityPackBindingError(f"pack is already bound: {manifest.pack_id}")
        expected_ids = {binding.tool_id for binding in manifest.tools}
        if set(handlers) != expected_ids:
            raise CapabilityPackBindingError("pack handlers do not exactly match signed tool bindings")
        for binding in manifest.tools:
            try:
                spec = self._registry.get(binding.tool_id)
            except Exception:
                raise CapabilityPackBindingError(
                    f"pack references unknown tool: {binding.tool_id}"
                ) from None
            if spec.version != binding.tool_version or tool_spec_digest(spec) != binding.spec_sha256:
                raise CapabilityPackBindingError(
                    f"pack tool contract does not match runtime: {binding.tool_id}"
                )
            if binding.tool_id in self._handlers:
                raise CapabilityPackBindingError(
                    f"tool handler is already supplied by another pack: {binding.tool_id}"
                )
            if not callable(handlers[binding.tool_id]):
                raise CapabilityPackBindingError(
                    f"pack handler is not callable: {binding.tool_id}"
                )
        for binding in manifest.services:
            expected_service = self._service_specs.get(binding.service_id)
            if expected_service is None:
                raise CapabilityPackBindingError(
                    f"pack references unknown service: {binding.service_id}"
                )
            if (
                expected_service.version != binding.service_version
                or expected_service.contract_sha256 != binding.contract_sha256
            ):
                raise CapabilityPackBindingError(
                    f"pack service contract does not match runtime: {binding.service_id}"
                )
            if binding.service_id in self._services:
                raise CapabilityPackBindingError(
                    f"service is already supplied by another pack: {binding.service_id}"
                )
        self._packs[manifest.pack_id] = pack
        self._handlers.update(handlers)
        self._services.update(
            {binding.service_id: binding for binding in manifest.services}
        )

    @property
    def installed_pack_ids(self) -> frozenset[str]:
        return frozenset(self._packs)

    @property
    def handlers(self) -> Mapping[str, ToolHandler]:
        return MappingProxyType(dict(self._handlers))

    @property
    def installed_service_ids(self) -> frozenset[str]:
        return frozenset(self._services)

    def disabled_tools(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                spec.tool_id: "verified_handler_not_installed"
                for spec in self._registry.all()
                if spec.tool_id not in self._handlers
            }
        )


def read_pack_manifest(path: Path | str) -> CapabilityPackManifest:
    manifest_path = Path(path)
    try:
        stat = manifest_path.lstat()
        if (
            not stat_module.S_ISREG(stat.st_mode)
            or stat_module.S_ISLNK(stat.st_mode)
            or stat.st_size > MAX_PACK_MANIFEST_BYTES
        ):
            raise CapabilityPackManifestError("pack manifest is not a regular bounded file")
        payload = manifest_path.read_bytes()
    except CapabilityPackManifestError:
        raise
    except OSError as exc:
        raise CapabilityPackManifestError("pack manifest cannot be read") from exc
    return CapabilityPackManifest.from_bytes(payload)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CapabilityPackManifestError(f"{label} contains missing or unknown fields")


def _string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise CapabilityPackManifestError(f"{key} must be a non-empty string")
    return result
